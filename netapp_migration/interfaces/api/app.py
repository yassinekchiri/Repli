"""REST API exposing the migration engine.

Launch:
    uvicorn netapp_migration.interfaces.api.app:app --host 0.0.0.0 --port 8000

Server-side configuration comes from the environment:
    NETAPP_MIGRATION_CONFIG   JSON credentials file (see README)
    NETAPP_MIGRATION_JOB_DIR  job files directory (default: CWD)

Endpoint map (prefix /api/v1):

    POST /migrations                    create the cascade      -> 202 (background)
    GET  /migrations                    list all jobs
    GET  /migrations/{job_id}           job record + last run info
    GET  /migrations/{job_id}/status    live ONTAP replication state
    POST /migrations/{job_id}/resume    fan-out PROD + DR       (confirm: true)
    POST /migrations/{job_id}/retry     re-enter create         -> 202 (background)
    POST /migrations/{job_id}/test      thin FlexClones         -> 202 (background)
    POST /migrations/{job_id}/clone     real clones + vol move  -> 202 (background)
    POST /migrations/{job_id}/acl       force AD-group DACLs
    POST /migrations/{job_id}/cleanup   cut source access

Long actions run in a background thread; poll GET /migrations/{job_id} to
follow their console output and final state. One action at a time per job
(409 otherwise).
"""

import logging
import os
import threading
import uuid
from typing import Callable, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ...config import CredentialsResolver, job_dir
from ...core.engine import MigrationEngine
from ...core.jobs import JobStore, JobNotFound
from ...models import MigrationParams, OntapError, ConfirmationRequired
from ...transport import build_client
from .schemas import (CreateMigrationRequest, ResumeRequest, CloneRequest,
                      TestRequest, AclRequest, CleanupRequest,
                      ActionAccepted, ActionResult)

_MAX_CAPTURED_LOG_LINES = 4000

# Swagger UI assets are served locally (static/) because the target
# servers have no Internet access: the default FastAPI /docs page pulls
# its JS/CSS from a public CDN and renders blank offline.
#
# Vendored Swagger UI is 4.15.5 on purpose: v5 requires a recent browser
# (modern JS syntax -> silent blank page on older corporate browsers).
# 4.15.5 only renders OpenAPI 3.0.x, hence openapi_version="3.0.2".
app = FastAPI(
    title="NetApp Cascade Migration API",
    description="Y fan-out migration orchestration "
                "(Source -> Pivot -> PROD + DR) over the ONTAP REST API.",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
)
# Attribute (not a constructor parameter): pin the version string so
# Swagger UI 4.x accepts the definition.
app.openapi_version = "3.0.2"

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_SWAGGER_UI_VERSION = "4.15.5"   # cache-buster: bump when assets change
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.middleware("http")
async def _no_cache_docs(request, call_next):
    """Forbid browser caching of the docs page and the OpenAPI schema.

    Without Cache-Control headers, browsers (Edge in particular) cache
    /openapi.json heuristically and keep rendering a stale schema in
    Swagger UI even after the server is updated.
    """
    response = await call_next(request)
    if request.url.path in ("/docs", "/openapi.json"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} — Swagger UI",
        swagger_js_url=f"/static/swagger-ui-bundle.js?v={_SWAGGER_UI_VERSION}",
        swagger_css_url=f"/static/swagger-ui.css?v={_SWAGGER_UI_VERSION}",
        swagger_favicon_url="/static/favicon-32x32.png",
    )

_store = JobStore(job_dir())
_registry_lock = threading.Lock()
# job_id -> info about the last (or current) action run on that job.
_runs: Dict[str, dict] = {}


# =============================================================================
# RUN MANAGEMENT (log capture + one action at a time per job)
# =============================================================================

class _ListHandler(logging.Handler):
    """Captures log lines into a bounded list (returned by the API)."""

    def __init__(self, sink: List[str]):
        super().__init__(level=logging.INFO)
        self._sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record):
        if len(self._sink) < _MAX_CAPTURED_LOG_LINES:
            self._sink.append(self.format(record))


def _make_run_logger(logs: List[str]) -> logging.Logger:
    """Dedicated logger per run: captured lines + server console echo."""
    logger = logging.getLogger(f"netapp_migration.api.run.{uuid.uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_ListHandler(logs))
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(message)s",
                                           datefmt="%H:%M:%S"))
    logger.addHandler(console)
    return logger


def _engine_for(params: MigrationParams, logger: logging.Logger) -> MigrationEngine:
    resolver = CredentialsResolver()          # env-driven on the server
    client = build_client(params, logger, resolver)
    return MigrationEngine(client, params, _store, logger)


def _load_job_or_404(job_id: str) -> dict:
    try:
        return _store.load(job_id)
    except (JobNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _begin_run(job_id: str, action: str) -> dict:
    """Register a run; refuse if another action is already running (409)."""
    with _registry_lock:
        current = _runs.get(job_id)
        if current and current["state"] == "running":
            raise HTTPException(
                status_code=409,
                detail=f"action '{current['action']}' is already running for "
                       f"job {job_id}; wait for it to finish.")
        run = {"action": action, "state": "running", "error": None, "logs": []}
        _runs[job_id] = run
        return run


def _run_in_background(job_id: str, action: str,
                       target: Callable[[logging.Logger], None]) -> dict:
    run = _begin_run(job_id, action)
    logger = _make_run_logger(run["logs"])

    def wrapper():
        try:
            target(logger)
            run["state"] = "success"
        except ConfirmationRequired as exc:
            run["state"] = "confirmation_required"
            run["error"] = str(exc)
        except OntapError as exc:
            run["state"] = "error"
            run["error"] = str(exc)
            logger.error("ONTAP FAILURE: %s", exc)
        except Exception as exc:  # noqa: BLE001 — thread boundary
            run["state"] = "error"
            run["error"] = str(exc)
            logger.exception("UNEXPECTED FAILURE: %s", exc)

    threading.Thread(target=wrapper, daemon=True,
                     name=f"migration-{action}-{job_id}").start()
    return run


def _run_sync(job_id: Optional[str], action: str,
              target: Callable[[logging.Logger], dict]) -> ActionResult:
    """Run a short action synchronously, capturing its log lines."""
    run = _begin_run(job_id, action) if job_id else \
        {"action": action, "state": "running", "error": None, "logs": []}
    logger = _make_run_logger(run["logs"])
    try:
        result = target(logger) or {}
        run["state"] = "success"
        return ActionResult(job_id=job_id, action=action,
                            result=result, logs=run["logs"])
    except ConfirmationRequired as exc:
        run["state"] = "confirmation_required"
        run["error"] = str(exc)
        raise HTTPException(status_code=409,
                            detail=f"confirmation required: {exc}. "
                                   f"Re-POST with {{\"confirm\": true}}.")
    except OntapError as exc:
        run["state"] = "error"
        run["error"] = str(exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# =============================================================================
# ENDPOINTS
# =============================================================================

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "job_dir": _store.directory}


@app.post("/api/v1/migrations", status_code=202, response_model=ActionAccepted)
def create_migration(req: CreateMigrationRequest):
    """Start the cascade initialisation; answers immediately with the job id."""
    params = MigrationParams.from_dict(req.model_dump())
    job = _store.create(params, req.create_mode)
    job_id = job["job_id"]

    def target(logger):
        engine = _engine_for(params, logger)
        engine.create(create_mode=req.create_mode, job=job)

    _run_in_background(job_id, "create", target)
    return ActionAccepted(job_id=job_id, action="create",
                          detail="cascade initialisation started; poll "
                                 f"GET /api/v1/migrations/{job_id}")


@app.get("/api/v1/migrations")
def list_migrations():
    jobs = _store.list_jobs()
    return {"count": len(jobs),
            "jobs": [{"job_id": j.get("job_id"),
                      "status": j.get("status"),
                      "created_at": j.get("created_at"),
                      "volume": j.get("params", {}).get("volume")}
                     for j in jobs]}


@app.get("/api/v1/migrations/{job_id}")
def get_migration(job_id: str, logs: int = 50):
    """Job record + last action run (state and captured log tail)."""
    job = _load_job_or_404(job_id)
    run = _runs.get(job_id)
    last_run = None
    if run:
        last_run = {"action": run["action"], "state": run["state"],
                    "error": run["error"],
                    "logs": run["logs"][-max(0, logs):]}
    return {"job": job, "last_run": last_run}


@app.get("/api/v1/migrations/{job_id}/status")
def migration_status(job_id: str):
    """Live ONTAP replication state (queries the clusters)."""
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger) -> dict:
        engine = _engine_for(params, logger)
        return engine.check_status(job)

    return _run_sync(None, "check-status", target)


@app.post("/api/v1/migrations/{job_id}/resume", response_model=ActionResult)
def resume_migration(job_id: str, req: ResumeRequest):
    """Fan out to PROD + DR once the pivot is ready.

    Without {"confirm": true} the call answers 409 when the pivot is ready,
    asking for explicit confirmation (destructive-free but long transfers).
    """
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger) -> dict:
        engine = _engine_for(params, logger)
        engine.resume(job, confirm=req.confirm)
        return {"status": job.get("status")}

    return _run_sync(job_id, "resume", target)


@app.post("/api/v1/migrations/{job_id}/retry", status_code=202,
          response_model=ActionAccepted)
def retry_migration(job_id: str):
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger):
        engine = _engine_for(params, logger)
        engine.retry(job)

    _run_in_background(job_id, "retry", target)
    return ActionAccepted(job_id=job_id, action="retry",
                          detail=f"poll GET /api/v1/migrations/{job_id}")


@app.post("/api/v1/migrations/{job_id}/test", status_code=202,
          response_model=ActionAccepted)
def test_migration(job_id: str, req: TestRequest):
    """Full TEST environment: clones + PROD->DR mirror, no split/move.

    Time-limited (validity_days); promote it with POST .../clone before
    expiry, or delete the clones after it.
    """
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger):
        engine = _engine_for(params, logger)
        engine.test(req.qtrees_csv, job=job, validity_days=req.validity_days)

    _run_in_background(job_id, "test", target)
    return ActionAccepted(job_id=job_id, action="test",
                          detail=f"poll GET /api/v1/migrations/{job_id}")


@app.post("/api/v1/migrations/{job_id}/clone", status_code=202,
          response_model=ActionAccepted)
def clone_migration(job_id: str, req: CloneRequest):
    """Definitive clones.

    If a test environment exists for this job, it is PROMOTED (volume moves
    only) — unless "fresh": true, which ignores the test environment and
    runs the full flow on a clean base. Without a test environment the full
    flow runs (propagation, FlexClones, clone mirror, volume moves).
    """
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger):
        engine = _engine_for(params, logger)
        engine.clone(req.qtrees_csv, job=job, fresh=req.fresh)

    _run_in_background(job_id, "clone", target)
    return ActionAccepted(job_id=job_id, action="clone",
                          detail=f"poll GET /api/v1/migrations/{job_id}")


@app.post("/api/v1/migrations/{job_id}/acl", response_model=ActionResult)
def acl_migration(job_id: str, req: AclRequest):
    """Force AD-group DACLs on ONE destination path (server-side).

    Decoupled from test/clone: the caller provides the exact path.
    """
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger) -> dict:
        engine = _engine_for(params, logger)
        return engine.acl(req.ad_groups_csv, acl_path=req.acl_path,
                          acl_rights=req.acl_rights, job=job)

    return _run_sync(job_id, "acl", target)


@app.post("/api/v1/migrations/{job_id}/cleanup", response_model=ActionResult)
def cleanup_migration(job_id: str, req: CleanupRequest):
    """Cut source access for one qtree (export-policy, CIFS, rename)."""
    job = _load_job_or_404(job_id)
    params = _store.params_of(job)

    def target(logger) -> dict:
        engine = _engine_for(params, logger)
        return engine.cleanup(req.qtree)

    return _run_sync(job_id, "cleanup", target)


@app.exception_handler(OntapError)
def ontap_error_handler(_request, exc: OntapError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})
