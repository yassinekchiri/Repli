"""REST API: readable statuses, structured pre-flight errors, no side effects."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient, cascade_ready


@pytest.fixture
def api(tmp_path, monkeypatch, client, params):
    """API wired onto the in-memory fake estate and a temporary job dir."""
    import netapp_migration.interfaces.api.app as app_module
    from netapp_migration.core.jobs import JobStore
    from netapp_migration.core.engine import MigrationEngine

    store = JobStore(str(tmp_path))
    monkeypatch.setattr(app_module, "_store", store)
    monkeypatch.setattr(app_module, "_runs", {})

    def fake_engine(engine_params, logger):
        target = (JobStore(str(tmp_path), read_only=True)
                  if engine_params.dry_run else store)
        return MigrationEngine(client, engine_params, target, logger)

    monkeypatch.setattr(app_module, "_engine_for", fake_engine)
    return TestClient(app_module.app), store, client


CREATE_BODY = {
    "source_cluster": "SRC", "pivot_cluster": "PIV",
    "dest_cluster": "PRD", "dr_cluster": "DRC",
    "volume": "vol_prod_01",
    "source_vserver": "svm_source", "pivot_vserver": "svm_pivot",
    "dest_vserver": "svm_dest", "dr_vserver": "svm_dr",
    "pivot_aggr": "aggr_piv", "dest_aggr": "aggr_prd", "dr_aggr": "aggr_dr",
    "timeout": 1, "poll_interval": 0,
}


def test_health(api):
    http, _, _ = api
    assert http.get("/api/v1/health").json()["status"] == "ok"


def test_preflight_create_reports_every_check(api):
    http, _, fake = api
    fake.schedules["DRC"] = []
    body = http.post("/api/v1/preflight/create", json=CREATE_BODY).json()
    assert body["ok"] is False
    assert body["failed_count"] >= 1
    codes = {c["code"] for c in body["checks"] if not c["passed"]}
    assert "SCHEDULE_MISSING" in codes
    # every check carries a human title and an actionable hint
    failing = next(c for c in body["checks"]
                   if c["code"] == "SCHEDULE_MISSING" and not c["passed"])
    assert failing["title"] and failing["hint"] and failing["target"]


def test_preflight_create_passes_and_mutates_nothing(api):
    http, store, fake = api
    body = http.post("/api/v1/preflight/create", json=CREATE_BODY).json()
    assert body["ok"] is True
    assert fake.calls == []
    assert store.list_jobs() == []


def test_create_returns_422_with_failed_checks(api):
    """An infeasible create is refused up front, not attempted."""
    http, _, fake = api
    fake.aggregates["PRD"].pop("aggr_prd")
    r = http.post("/api/v1/migrations", json=CREATE_BODY)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "preflight_failed"
    assert detail["action"] == "create"
    assert any(c["code"] == "AGGREGATE_MISSING"
               for c in detail["failed_checks"])
    assert "no cluster was modified" in detail["hint"]
    assert fake.calls == []


def test_create_accepted_on_healthy_estate(api):
    http, store, fake = api
    r = http.post("/api/v1/migrations", json=CREATE_BODY)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert store.load(job_id)["job_id"] == job_id


def test_status_is_read_only(api):
    http, store, fake = api
    from netapp_migration.models import MigrationParams
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "dest_initialized")
    cascade_ready(fake, params)

    body = http.get(f"/api/v1/migrations/{job['job_id']}/status").json()
    assert body["result"]["completed"] is True
    assert store.load(job["job_id"])["status"] == "dest_initialized"

    # ... while the explicit refresh does persist it
    http.post(f"/api/v1/migrations/{job['job_id']}/refresh")
    assert store.load(job["job_id"])["status"] == "completed"


def test_preflight_action_endpoint(api):
    http, store, fake = api
    from netapp_migration.models import MigrationParams
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)

    ok = http.post(f"/api/v1/migrations/{job['job_id']}/preflight/test",
                   params={"qtrees": "q_fin,q_hr"}).json()
    assert ok["ok"] is True

    bad = http.post(f"/api/v1/migrations/{job['job_id']}/preflight/test",
                    params={"qtrees": "q_missing"}).json()
    assert bad["ok"] is False
    assert any(c["code"] == "QTREES_MISSING" for c in bad["checks"])
    assert fake.calls == []


def test_preflight_unknown_action(api):
    http, store, _ = api
    from netapp_migration.models import MigrationParams
    job = store.create(MigrationParams.from_dict(CREATE_BODY))
    r = http.post(f"/api/v1/migrations/{job['job_id']}/preflight/banana")
    assert r.status_code == 400


def test_acl_rejects_root_with_readable_checks(api):
    http, store, fake = api
    from netapp_migration.models import MigrationParams
    job = store.create(MigrationParams.from_dict(CREATE_BODY))
    r = http.post(f"/api/v1/migrations/{job['job_id']}/acl",
                  json={"ad_groups": ["DOM\\grp"], "acl_path": "/"})
    assert r.status_code == 422
    codes = {c["code"] for c in r.json()["detail"]["failed_checks"]}
    assert "ACL_PATH_IS_ROOT" in codes
    assert fake.calls == []


def test_cleanup_rejects_empty_qtree(api):
    http, store, fake = api
    r = http.post("/api/v1/migrations/x/cleanup", json={"qtree": ""})
    assert r.status_code in (404, 422)          # unknown job or refused qtree
    assert fake.calls == []


def test_logs_zero_returns_no_logs(api):
    """P2: logs=0 used to return every captured line."""
    http, store, fake = api
    r = http.post("/api/v1/migrations", json=CREATE_BODY)
    job_id = r.json()["job_id"]
    body = http.get(f"/api/v1/migrations/{job_id}", params={"logs": 0}).json()
    assert body["last_run"]["logs"] == []


def test_missing_job_is_404(api):
    http, _, _ = api
    assert http.get("/api/v1/migrations/NOPE").status_code == 404


def test_openapi_is_renderable(api):
    http, _, _ = api
    spec = http.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.0")
    assert "/api/v1/preflight/create" in spec["paths"]
