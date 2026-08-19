"""Migration engine: the business logic of the Y-topology cascade.

    Source -> Pivot -> PROD + DR   (simultaneous fan-out from the pivot)

The engine is interface-agnostic: it never touches argparse, FastAPI,
sys.argv or input(). It receives typed MigrationParams, talks to clusters
only through the OntapClient contract, persists state through the JobStore,
and reports progress through the standard logger. Interactive decisions are
surfaced as ConfirmationRequired exceptions that each interface translates
(CLI prompt / HTTP 409).

Actions and their job-file checkpoints are identical to the historical
single-file script:

    started -> space_checked -> volumes_created -> relationships_created
            -> pivot_initialized -> dest_initialized -> completed
"""

import datetime
import logging
import time
from typing import Dict, List, Optional

from ..models import (MigrationParams, OntapError, ConfirmationRequired,
                      PreflightFailed, PreflightReport, SnapMirrorInfo)
from ..transport.base import OntapClient
from .jobs import JobStore, CREATE_STATUS_ORDER
from .preflight import PreflightChecker


class MigrationEngine:
    """Drives one migration (one source volume) through all its actions."""

    def __init__(self, client: OntapClient, params: MigrationParams,
                 jobstore: JobStore, logger: logging.Logger):
        self.c = client
        self.p = params
        self.jobs = jobstore
        self.log = logger
        # Pre-flight checks share the transport: they are read-only probes.
        # Under dry-run their verdict is informational (simulated=True).
        self.checker = PreflightChecker(client, params, logger,
                                        simulated=params.dry_run)
        # Set as soon as a job file exists so callers can print the job id
        # on any failure.
        self.job_id: Optional[str] = None
        # Report of the last pre-flight run, exposed to the interfaces.
        self.last_preflight: Optional[PreflightReport] = None

    # ------------------------------------------------------------------ #
    # Presentation helpers (plain log lines; interfaces decide rendering)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _table_lines(headers: List[str], rows: List[List[str]]) -> List[str]:
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(cell)))
        sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

        def fmt(cells):
            return "|" + "|".join(
                f" {str(cells[i]).ljust(widths[i])} " if i < len(cells)
                else " " * (widths[i] + 2)
                for i in range(len(widths))) + "|"

        lines = [sep, fmt(headers), sep]
        lines += [fmt(r) for r in rows]
        lines.append(sep)
        return lines

    def _log_table(self, headers, rows):
        for line in self._table_lines(headers, rows):
            self.log.info(line)

    def _log_arch(self, pivot_note="", prod_note="", dr_note=""):
        """Y-topology diagram with per-leg status notes."""
        p = self.p
        prod_w = max(len(p.dest_cluster), len(prod_note), 14)
        self.log.info("")
        self.log.info("  [ SOURCE ]  %s", p.source_cluster)
        self.log.info("       |")
        if pivot_note:
            self.log.info("       |  %s", pivot_note)
        self.log.info("       v")
        self.log.info("  [ PIVOT  ]  %s", p.pivot_cluster)
        self.log.info("      /  \\")
        self.log.info("     /    \\")
        self.log.info("    v      v")
        self.log.info("  [ PROD  ]    [ DR    ]")
        self.log.info("  %-*s  %s", prod_w, p.dest_cluster, p.dr_cluster)
        if prod_note or dr_note:
            self.log.info("  %-*s  %s", prod_w, prod_note, dr_note)
        self.log.info("")

    # ------------------------------------------------------------------ #
    # Pre-flight
    # ------------------------------------------------------------------ #
    def _preflight(self, report: PreflightReport) -> PreflightReport:
        """Log a pre-flight report and stop the action if it blocks.

        Nothing has been mutated when PreflightFailed is raised: the caller
        (CLI or API) renders every individual check to the operator.
        """
        self.last_preflight = report
        self.log.info("--- Pre-flight checks: action '%s' ---", report.action)

        rows = []
        for check in report.checks:
            if check.passed:
                verdict = "OK"
            elif check.severity == "warning":
                verdict = "WARN"
            else:
                verdict = "FAIL"
            # Long observations (ONTAP error bodies) are truncated to keep
            # the table readable; the full text stays in the DEBUG log.
            detail = check.detail or "-"
            if len(detail) > 68:
                detail = detail[:65] + "..."
            rows.append([verdict, check.title, detail, check.target or "-"])
        if rows:
            self._log_table(["", "Check", "Observed", "Target"], rows)

        for check in report.checks:
            if not check.passed:
                self.log.debug("Check %s: %s | %s", check.code, check.title,
                               check.detail)
        for check in report.checks:
            if not check.passed and check.hint:
                level = self.log.warning if check.blocking else self.log.info
                level("  %s [%s]: %s", "FIX" if check.blocking else "note",
                      check.code, check.hint)

        if report.simulated and report.failures:
            self.log.warning("[DRY-RUN] %d check(s) would block a real run "
                             "(simulated state, not blocking here).",
                             len(report.failures))
        if not report.ok:
            self.log.error("%s", report.summary())
            raise PreflightFailed(report)
        self.log.info("%s", report.summary())
        return report

    # ------------------------------------------------------------------ #
    # Shared building blocks
    # ------------------------------------------------------------------ #
    def _paths(self):
        p = self.p
        return (p.path(p.pivot_vserver, p.volume),
                p.path(p.dest_vserver, p.volume),
                p.path(p.dr_vserver, p.volume))

    def _wait_snapmirror(self, cluster: str, dest_path: str,
                         want: str) -> SnapMirrorInfo:
        """Poll until 'ready' (baseline done) or 'idle' (transfer done)."""
        self.log.info("Waiting for '%s' on %s ...", want, dest_path)
        deadline = time.monotonic() + self.p.timeout
        while True:
            sm = self.c.get_snapmirror(cluster, dest_path)
            self.log.debug("SnapMirror %s: state=%s transfer=%s",
                           dest_path, sm.state, sm.transfer_state)
            if want == "ready" and sm.is_ready:
                return sm
            if want == "idle" and sm.is_idle:
                return sm
            if time.monotonic() > deadline:
                raise OntapError(cluster, f"wait {want} on {dest_path}",
                                 f"timeout after {self.p.timeout}s "
                                 f"(state={sm.state}, "
                                 f"transfer={sm.transfer_state})")
            time.sleep(self.p.poll_interval)

    def _resolve_qtrees(self, qtrees_arg: str) -> List[str]:
        """'all' -> discovery on the source volume; else split the CSV."""
        if qtrees_arg.strip().lower() == "all":
            qtrees = self.c.list_qtrees(self.p.source_cluster,
                                        self.p.source_vserver, self.p.volume)
        else:
            qtrees = [q.strip() for q in qtrees_arg.split(",") if q.strip()]
        if not qtrees:
            raise OntapError(self.p.source_cluster, "qtree resolution",
                             "no Qtree to process")
        return qtrees

    def _check_space(self, cluster: str, aggregate: str,
                     required: Optional[int]):
        available = self.c.get_aggregate_available(cluster, aggregate)
        if required is None or available is None:
            self.log.warning("Cannot compare space on %s/%s "
                             "(required=%s, available=%s) — manual check "
                             "recommended.", cluster, aggregate,
                             required, available)
            return
        self.log.info("Aggregate %s/%s: available=%d, required=%d.",
                      cluster, aggregate, available, required)
        if available < required:
            raise OntapError(cluster, f"space check on {aggregate}",
                             f"insufficient space "
                             f"(available={available} < required={required})")

    def _find_best_aggregate(self, cluster: str,
                             exclude: Optional[str] = None) -> str:
        best = None
        for aggr in self.c.list_aggregates(cluster):
            if exclude and aggr.name.lower() == exclude.lower():
                continue
            if best is None or aggr.available_bytes > best.available_bytes:
                best = aggr
        if best is None:
            raise OntapError(cluster, "aggregate selection",
                             f"no suitable aggregate (excluded: {exclude})")
        self.log.debug("Best aggregate on %s: %s (%d bytes free).",
                       cluster, best.name, best.available_bytes)
        return best.name

    # =====================================================================
    # ACTION 'create'
    # =====================================================================
    def create(self, create_mode: str = "full",
               job: Optional[dict] = None) -> dict:
        """Initialise the cascade. A pre-created job record may be passed
        (the REST API creates it first so it can answer with the job id
        before the long-running work starts)."""
        p = self.p
        self.log.info("=" * 60)
        self.log.info("  ACTION: create  (mode=%s)", create_mode)
        self.log.info("=" * 60)

        # Nothing is created until every prerequisite is verified: SVMs,
        # source volume, aggregates and their capacity, absence of leftover
        # DP volumes and relationships, peering, policy and schedule.
        self._preflight(self.checker.for_create())

        if job is None:
            job = self.jobs.create(p, create_mode)
        self.job_id = job["job_id"]
        self.log.info("Job ID: %s", self.job_id)
        pivot_path, prod_path, dr_path = self._paths()

        # -- Source volume characteristics (one call) ----------------------
        src = self.c.get_volume(p.source_cluster, p.source_vserver, p.volume)
        self.log.info("Source volume '%s': size=%s bytes, security-style=%s",
                      p.volume, src.size_bytes, src.security_style or "unknown")

        # -- Space checks on the three target aggregates -------------------
        self._check_space(p.pivot_cluster, p.pivot_aggr, src.size_bytes)
        self._check_space(p.dest_cluster,  p.dest_aggr,  src.size_bytes)
        self._check_space(p.dr_cluster,    p.dr_aggr,    src.size_bytes)
        self.jobs.set_status(job, "space_checked")

        # -- DP volumes on pivot, PROD, DR ----------------------------------
        for cluster, svm, aggr in ((p.pivot_cluster, p.pivot_vserver, p.pivot_aggr),
                                   (p.dest_cluster,  p.dest_vserver,  p.dest_aggr),
                                   (p.dr_cluster,    p.dr_vserver,    p.dr_aggr)):
            self.c.create_dp_volume(cluster, svm, p.volume, aggr,
                                    src.size_bytes, src.security_style)
            self.log.info("DP volume '%s' created on %s (aggr %s).",
                          p.volume, cluster, aggr)
        self.jobs.set_status(job, "volumes_created")

        # -- The three SnapMirror relationships (no transfer yet) ----------
        self.c.snapmirror_create(p.pivot_cluster,
                                 p.path(p.source_vserver, p.volume), pivot_path)
        self.c.snapmirror_create(p.dest_cluster, pivot_path, prod_path)
        self.c.snapmirror_create(p.dr_cluster,   pivot_path, dr_path)
        self.log.info("SnapMirror relationships declared (src->pivot, "
                      "pivot->PROD, pivot->DR).")
        self.jobs.set_status(job, "relationships_created")

        # -- Pivot initialize (destination transfers must wait for it) -----
        self.c.snapmirror_initialize(p.pivot_cluster, pivot_path)
        self.jobs.set_status(job, "pivot_initialized")

        if create_mode == "pivot-only":
            self.log.info("=" * 60)
            self.log.info("  Pivot SnapMirror initialize launched  [pivot-only mode]")
            self._log_arch(pivot_note=">>> initializing ...",
                           prod_note="(waiting)", dr_note="(waiting)")
            self.log.info("  Job ID : %s", self.job_id)
            self.log.info("  Resume with action 'resume' when the pivot is idle.")
            self.log.info("=" * 60)
            return job

        # -- Full mode: wait pivot, then fan out to PROD + DR ---------------
        self._wait_snapmirror(p.pivot_cluster, pivot_path, want="ready")
        self.c.snapmirror_initialize(p.dest_cluster, prod_path)
        self.c.snapmirror_initialize(p.dr_cluster,   dr_path)
        self.jobs.set_status(job, "dest_initialized")

        self._wait_snapmirror(p.dest_cluster, prod_path, want="ready")
        self._wait_snapmirror(p.dr_cluster,   dr_path,   want="ready")
        self.jobs.set_status(job, "completed")

        self.log.info("=" * 60)
        self.log.info("  ACTION 'create' complete — cascade initialized and synchronized")
        self._log_arch(pivot_note="idle", prod_note="idle", dr_note="idle")
        self.log.info("=" * 60)
        return job

    # =====================================================================
    # ACTION 'resume'
    # =====================================================================
    def resume(self, job: dict, confirm: bool = False) -> dict:
        p = self.p
        self.job_id = job["job_id"]
        status = job.get("status", "unknown")
        pivot_path = job["pivot_dest_path"]
        prod_path = job["dest_dest_path"]
        dr_path = job["dr_dest_path"]

        self.log.info("=" * 60)
        self.log.info("  ACTION: resume  |  Job %s  [%s]", self.job_id, status)
        self.log.info("  Created: %s", job.get("created_at", "unknown"))
        self.log.info("=" * 60)

        if status == "completed":
            self._log_arch(pivot_note="idle", prod_note="idle", dr_note="idle")
            self.log.info("  Job %s already completed. Nothing to do.", self.job_id)
            return job

        if status == "dest_initialized":
            self.log.info("PROD and DR already initialized. Live state:")
            return self.check_status(job) or job

        if status in ("started", "space_checked", "volumes_created",
                      "relationships_created"):
            self.log.info("  The 'create' action stopped before the pivot "
                          "initialize (last checkpoint: %s).", status)
            self.log.info("  Nothing to resume yet — run action 'retry' "
                          "first to complete the remaining phases.")
            return job

        # Verify the pivot really finished and that neither destination has
        # already been initialized (a second initialize would restart PROD).
        self._preflight(self.checker.for_resume(job))

        # -- Single pivot status check (no polling loop) -------------------
        sm = self.c.get_snapmirror(p.pivot_cluster, pivot_path)
        self._log_table(
            ["Role", "Cluster", "Path", "State", "Transfer", "Transferred"],
            [["PIVOT", p.pivot_cluster, pivot_path, sm.state,
              sm.transfer_state, sm.last_transfer_size]])

        if not sm.is_ready:
            self.log.info("  Pivot not yet ready (state='%s'). "
                          "Run 'resume' again later.", sm.state)
            return job

        # -- Pivot ready: explicit confirmation required --------------------
        if not confirm:
            raise ConfirmationRequired(
                "pivot replication is complete; destination fan-out (PROD + "
                "DR initialize) requires explicit confirmation")

        self.c.snapmirror_initialize(p.dest_cluster, prod_path)
        self.c.snapmirror_initialize(p.dr_cluster,   dr_path)
        self.jobs.set_status(job, "dest_initialized")

        self.log.info("=" * 60)
        self.log.info("  PROD and DR SnapMirror initializes launched.")
        self._log_arch(pivot_note="idle",
                       prod_note=">>> initializing", dr_note=">>> initializing")
        self.log.info("  Job ID : %s", self.job_id)
        self.log.info("  Monitor with action 'check-status'.")
        self.log.info("=" * 60)
        return job

    # =====================================================================
    # ACTION 'check-status'
    # =====================================================================
    def check_status(self, job: dict, persist: bool = True) -> dict:
        """Live replication state; also returned as a structured dict.

        persist=False makes this a strictly read-only probe (used by
        GET /status): the observed state is reported but the job file is
        never rewritten.
        """
        p = self.p
        self.job_id = job["job_id"]
        status = job.get("status", "unknown")
        pivot_path = job["pivot_dest_path"]
        prod_path = job["dest_dest_path"]
        dr_path = job.get("dr_dest_path", "")

        self.log.info("=" * 60)
        self.log.info("  ACTION: check-status  |  Job %s", self.job_id)
        self.log.info("  Created: %s  |  Status: %s",
                      job.get("created_at", "unknown"), status)
        self.log.info("=" * 60)

        result = {"job_id": self.job_id, "status": status,
                  "completed": status == "completed", "legs": []}

        if status == "completed":
            self._log_arch(pivot_note="idle", prod_note="idle", dr_note="idle")
            self.log.info("  Job %s already completed.", self.job_id)
            return result

        def leg(role, cluster, path) -> dict:
            sm = self.c.get_snapmirror(cluster, path)
            return {"role": role, "cluster": cluster, "path": path,
                    "state": sm.state, "transfer_state": sm.transfer_state,
                    "transferred": sm.last_transfer_size,
                    "ready": sm.is_ready,
                    "healthy": not sm.is_broken,
                    "reason": sm.unhealthy_reason or "ready"}

        # -- 'create' interrupted before the pivot initialize ---------------
        # (started / space_checked / volumes_created / relationships_created)
        # There may be nothing to query on the clusters yet: show the
        # checkpoint progression and point the operator at action 'retry'.
        if status in ("started", "space_checked", "volumes_created",
                      "relationships_created"):
            idx = CREATE_STATUS_ORDER.index(status)
            phase_labels = [
                ("space_checked",         "Aggregate space check"),
                ("volumes_created",       "DP volume creation"),
                ("relationships_created", "SnapMirror relationships"),
                ("pivot_initialized",     "Pivot initialize"),
                ("dest_initialized",      "PROD + DR initialize"),
                ("completed",             "Final synchronization"),
            ]
            rows = [[label,
                     "done" if idx >= CREATE_STATUS_ORDER.index(cp)
                     else "pending"]
                    for cp, label in phase_labels]
            self._log_table(["Create phase", "State"], rows)
            result["pending_phases"] = [
                label for cp, label in phase_labels
                if idx < CREATE_STATUS_ORDER.index(cp)]
            self.log.info("  The 'create' action stopped before the pivot "
                          "initialize (last checkpoint: %s).", status)
            self.log.info("  Resume it with action 'retry' — completed "
                          "phases will be skipped.")
            return result

        if status == "pivot_initialized":
            pivot = leg("PIVOT", p.pivot_cluster, pivot_path)
            result["legs"] = [pivot]
            self._log_table(
                ["Role", "Cluster", "Path", "State", "Transfer", "Status"],
                [[pivot["role"], pivot["cluster"], pivot["path"],
                  pivot["state"], pivot["transfer_state"],
                  pivot["reason"]]])
            if pivot["ready"]:
                self.log.info("  Pivot ready. Launch PROD + DR with action 'resume'.")
            else:
                self.log.info("  Pivot still in progress. Check again later.")
            return result

        if status == "dest_initialized":
            legs = [leg("PROD", p.dest_cluster, prod_path)]
            if dr_path:
                legs.append(leg("DR", p.dr_cluster, dr_path))
            result["legs"] = legs
            self._log_table(
                ["Role", "Cluster", "Path", "State", "Transfer", "Status"],
                [[l["role"], l["cluster"], l["path"], l["state"],
                  l["transfer_state"], l["reason"]] for l in legs])

            if all(l["ready"] for l in legs):
                # Every leg is mirrored AND nothing is in flight: safe to
                # declare the replication finished.
                if persist:
                    self.jobs.set_status(job, "completed")
                    self.log.info("  Both PROD and DR complete. Job %s marked "
                                  "as completed.", self.job_id)
                else:
                    self.log.info("  Both PROD and DR complete (read-only "
                                  "check: job file unchanged).")
                result["status"] = "completed"
                result["completed"] = True
                self._log_arch(pivot_note="idle", prod_note="idle", dr_note="idle")
            else:
                pending = [f"{l['role']}: {l['reason']}"
                           for l in legs if not l["ready"]]
                self.log.info("  Not complete yet — %s", "; ".join(pending))
                result["pending"] = pending
            return result

        self.log.warning("Unrecognised job status '%s'.", status)
        return result

    # =====================================================================
    # ACTION 'retry'
    # =====================================================================
    def retry(self, job: dict) -> dict:
        """Re-enter 'create' at the last successful checkpoint."""
        p = self.p
        self.job_id = job["job_id"]
        status = job.get("status", "started")
        create_mode = job.get("create_mode", "full")
        pivot_path = job["pivot_dest_path"]
        prod_path = job["dest_dest_path"]
        dr_path = job.get("dr_dest_path", "")

        self.log.info("=" * 60)
        self.log.info("  ACTION: retry  |  Job %s  [%s]", self.job_id, status)
        self.log.info("=" * 60)

        if status == "completed":
            self.log.info("  Job already completed. Nothing to do.")
            return job
        if status == "dest_initialized":
            self.log.info("  PROD and DR already launched. Delegating to "
                          "check-status.")
            self.check_status(job)
            return job
        if status == "pivot_initialized":
            if create_mode == "pivot-only":
                self.log.info("  Pivot already initialized (pivot-only mode). "
                              "Use action 'resume' when the pivot is idle.")
            else:
                self.log.info("  Pivot already initialized. Delegating to resume.")
                try:
                    self.resume(job, confirm=False)
                except ConfirmationRequired:
                    self.log.info("  Pivot is ready — run action 'resume' with "
                                  "confirmation to launch PROD + DR.")
            return job

        self._preflight(self.checker.for_retry(job))

        idx = CREATE_STATUS_ORDER.index(status)
        need_space = idx < CREATE_STATUS_ORDER.index("space_checked")
        need_volumes = idx < CREATE_STATUS_ORDER.index("volumes_created")
        need_rels = idx < CREATE_STATUS_ORDER.index("relationships_created")

        src = None
        if need_space or need_volumes:
            src = self.c.get_volume(p.source_cluster, p.source_vserver, p.volume)
            self.log.info("Source volume '%s': size=%s, style=%s",
                          p.volume, src.size_bytes, src.security_style)

        if need_space:
            self.log.info("--- Phase: aggregate space check ---")
            self._check_space(p.pivot_cluster, p.pivot_aggr, src.size_bytes)
            self._check_space(p.dest_cluster,  p.dest_aggr,  src.size_bytes)
            self._check_space(p.dr_cluster,    p.dr_aggr,    src.size_bytes)
            self.jobs.set_status(job, "space_checked")
        else:
            self.log.info("Skipping space check (already completed).")

        if need_volumes:
            self.log.info("--- Phase: DP volume creation (idempotent) ---")
            for cluster, svm, aggr in (
                    (p.pivot_cluster, p.pivot_vserver, p.pivot_aggr),
                    (p.dest_cluster,  p.dest_vserver,  p.dest_aggr),
                    (p.dr_cluster,    p.dr_vserver,    p.dr_aggr)):
                self.c.create_dp_volume(cluster, svm, p.volume, aggr,
                                        src.size_bytes, src.security_style,
                                        idempotent=True)
            self.jobs.set_status(job, "volumes_created")
        else:
            self.log.info("Skipping DP volume creation (already completed).")

        if need_rels:
            self.log.info("--- Phase: SnapMirror creation (idempotent) ---")
            self.c.snapmirror_create(p.pivot_cluster,
                                     p.path(p.source_vserver, p.volume),
                                     pivot_path, idempotent=True)
            self.c.snapmirror_create(p.dest_cluster, pivot_path, prod_path,
                                     idempotent=True)
            if dr_path:
                self.c.snapmirror_create(p.dr_cluster, pivot_path, dr_path,
                                         idempotent=True)
            self.jobs.set_status(job, "relationships_created")
        else:
            self.log.info("Skipping relationship creation (already completed).")

        self.log.info("--- Phase: pivot SnapMirror initialize ---")
        self.c.snapmirror_initialize(p.pivot_cluster, pivot_path)
        self.jobs.set_status(job, "pivot_initialized")

        self.log.info("=" * 60)
        self.log.info("  Pivot SnapMirror initialize launched  [retry]")
        self._log_arch(pivot_note=">>> initializing ...",
                       prod_note="(waiting)", dr_note="(waiting)")
        self.log.info("  Job ID : %s", self.job_id)
        self.log.info("  Monitor with 'check-status', continue with 'resume'.")
        self.log.info("=" * 60)
        return job

    # =====================================================================
    # Clone prerequisites (pre-flight, explicit-parameter mode)
    # =====================================================================
    def check_clone_prerequisites(self):
        """Verify the whole cascade is healthy and idle before cloning."""
        p = self.p
        self.log.info("--- Pre-flight: checking cascade health before clone ---")
        checks = [(p.pivot_cluster, p.pivot_vserver),
                  (p.dest_cluster,  p.dest_vserver),
                  (p.dr_cluster,    p.dr_vserver)]
        for cluster, svm in checks:
            if not self.c.volume_exists(cluster, svm, p.volume):
                raise OntapError(cluster, "clone pre-flight",
                                 f"DP volume '{p.volume}' not found on "
                                 f"{cluster}/{svm}")
            self.log.info("DP volume '%s' confirmed on %s.", p.volume, cluster)
            dest_path = p.path(svm, p.volume)
            sm = self.c.get_snapmirror(cluster, dest_path)
            if not sm.exists:
                raise OntapError(cluster, "clone pre-flight",
                                 f"no SnapMirror relationship for {dest_path}")
            if not (sm.is_ready and sm.is_idle):
                raise OntapError(cluster, "clone pre-flight",
                                 f"SnapMirror {dest_path} not ready "
                                 f"(state={sm.state}, "
                                 f"transfer={sm.transfer_state})")
            self.log.info("SnapMirror %s: state=%s transfer=%s — OK.",
                          dest_path, sm.state, sm.transfer_state)
        self.log.info("Pre-flight checks passed. Cascade healthy and idle.")

    # =====================================================================
    # Shared snapshot propagation (clone + test)
    # =====================================================================
    def _propagate_snapshot(self, snap_name: str, step_offset: int,
                            total_steps: int):
        """Create the snapshot on the source and push it down the cascade."""
        p = self.p
        pivot_path, prod_path, dr_path = self._paths()

        self.log.info(">> [%d/%d]  Creating snapshot '%s' on source",
                      step_offset, total_steps, snap_name)
        self.c.create_snapshot(p.source_cluster, p.source_vserver,
                               p.volume, snap_name)
        self.log.info("         Snapshot created.")

        self.log.info(">> [%d/%d]  Propagating snapshot — Pivot",
                      step_offset + 1, total_steps)
        self.c.snapmirror_update(p.pivot_cluster, pivot_path)
        self._wait_snapmirror(p.pivot_cluster, pivot_path, want="idle")
        self.log.info("         Pivot: transfer complete.")

        self.log.info(">> [%d/%d]  Propagating snapshot — PROD + DR",
                      step_offset + 2, total_steps)
        self.c.snapmirror_update(p.dest_cluster, prod_path)
        self.c.snapmirror_update(p.dr_cluster,   dr_path)
        self._wait_snapmirror(p.dest_cluster, prod_path, want="idle")
        self._wait_snapmirror(p.dr_cluster,   dr_path,   want="idle")

        for cluster, svm in ((p.dest_cluster, p.dest_vserver),
                             (p.dr_cluster,   p.dr_vserver)):
            if not self.c.snapshot_exists(cluster, svm, p.volume, snap_name):
                raise OntapError(cluster, "snapshot verification",
                                 f"snapshot '{snap_name}' not found on "
                                 f"{cluster}/{svm}")
        self.log.info("         Snapshot confirmed on both destinations.")

    def _create_clones_on_both(self, qtrees: List[str], snap_name: str,
                               volume_map: Dict[str, str]):
        p = self.p
        for qtree in qtrees:
            clone_vol = volume_map[qtree]
            self.c.create_clone(p.dest_cluster, p.dest_vserver, clone_vol,
                                p.volume, snap_name)
            self.c.create_clone(p.dr_cluster, p.dr_vserver, clone_vol,
                                p.volume, snap_name)
            self.log.info("         '%s'  created on PROD and DR.", clone_vol)

    def _save_clone_metadata(self, job: Optional[dict],
                             volume_map: Dict[str, str], qtrees: List[str],
                             renames: Optional[Dict[str, str]] = None):
        if job is not None:
            job["volume_map"] = {q: volume_map[q] for q in qtrees}
            if renames:
                job["qtree_map"] = {q: renames[q] for q in qtrees
                                    if q in renames}
            job["clone_volumes"] = [volume_map[q] for q in qtrees]
            job["test_env"] = False    # definitive clones, not a test env
            self.jobs.save(job)

    def _resolve_volume_map(self, qtrees: List[str],
                            volume_map: Optional[Dict[str, str]],
                            job: Optional[dict]) -> Dict[str, str]:
        """Target volume name per qtree, chosen by the client.

        Falls back to the mapping recorded on the job (set by a previous
        test run) so a promotion reuses the very same names.
        """
        mapping = dict((job or {}).get("volume_map") or {})
        mapping.update({k: v for k, v in (volume_map or {}).items()})
        resolved = {}
        for qtree in qtrees:
            name = mapping.get(qtree) or next(
                (v for k, v in mapping.items() if k.lower() == qtree.lower()),
                None)
            if not name:
                raise OntapError(
                    self.p.dest_cluster, "volume mapping",
                    f"no target volume name given for qtree '{qtree}' — "
                    f"supply one per qtree (--volume-map CSV, or the "
                    f"volume_map field of the API request)")
            resolved[qtree] = name
        return resolved

    def _resolve_qtree_map(self, qtrees: List[str],
                           qtree_map: Optional[Dict[str, str]],
                           job: Optional[dict]) -> Dict[str, str]:
        """New qtree name per qtree, chosen by the client. Optional.

        A qtree absent from the map keeps the name it has on the source.
        Falls back to what a previous test run recorded, so a promotion
        reuses the very same names.
        """
        mapping = dict((job or {}).get("qtree_map") or {})
        mapping.update({k: v for k, v in (qtree_map or {}).items() if v})
        resolved = {}
        for qtree in qtrees:
            name = mapping.get(qtree) or next(
                (v for k, v in mapping.items() if k.lower() == qtree.lower()),
                None)
            if name and name != qtree:
                resolved[qtree] = name
        return resolved

    def _rename_qtrees_in_clones(self, qtrees: List[str],
                                 volumes: Dict[str, str],
                                 renames: Dict[str, str]):
        """Rename inside the PROD clones only.

        The DR clone is a SnapMirror destination, hence read-only: renaming
        there is impossible and unnecessary. Done BEFORE the clone mirror is
        established so the very first resync carries the new name to DR,
        instead of leaving the two sides differing until the next update.
        """
        if not renames:
            return
        p = self.p
        for qtree in qtrees:
            new_name = renames.get(qtree)
            if not new_name:
                continue
            self.c.rename_qtree(p.dest_cluster, p.dest_vserver,
                                volumes[qtree], qtree, new_name)
            self.log.info("         '%s'  qtree %s -> %s.",
                          volumes[qtree], qtree, new_name)

    def _promote_test_env(self, qtrees_arg: str, job: dict) -> dict:
        """Promote the existing TEST environment to the definitive one.

        The test clones already carry the client data and the PROD->DR
        mirror: nothing is rebuilt. After a final idle check on the clone
        relationships, only the volume moves are launched to detach the
        clones from their parent DP volumes.
        """
        p = self.p
        existing = set(job.get("clone_volumes", []))
        qtrees = self._resolve_qtrees(qtrees_arg)
        # Names come from the mapping recorded by the test run: a promotion
        # never renames anything.
        wanted = self._resolve_volume_map(qtrees, None, job)
        missing = [v for v in wanted.values() if v not in existing]
        if missing:
            raise OntapError(
                p.dest_cluster, "clone promotion",
                f"the test environment has no clone(s) {missing}. Promote "
                f"with the same qtrees as the test run ({sorted(existing)}), "
                f"or delete the test environment and run a full clone")

        self.log.info("=" * 60)
        self.log.info("  ACTION: clone  — PROMOTION of the test environment")
        self.log.info("  Test created: %s  |  valid until: %s",
                      job.get("test_created_at", "unknown"),
                      job.get("test_expires_at", "unknown"))
        self.log.info("=" * 60)

        expires_at = job.get("test_expires_at")
        if expires_at:
            try:
                expired = (datetime.datetime.fromisoformat(expires_at)
                           < datetime.datetime.now())
            except ValueError:
                expired = False
            if expired:
                self.log.warning("Test environment expired on %s — the clones "
                                 "should have been deleted. Promoting anyway.",
                                 expires_at)

        # [1/3] Final health check: every clone mirror must be idle.
        self.log.info(">> [1/3]  Verifying clone mirrors (PROD -> DR)")
        for qtree in qtrees:
            dr_clone = p.path(p.dr_vserver, wanted[qtree])
            self._wait_snapmirror(p.dr_cluster, dr_clone, want="idle")
            self.log.info("         '%s'  mirror idle.", wanted[qtree])

        # [2/3] Best aggregates (parents excluded).
        self.log.info(">> [2/3]  Selecting target aggregates")
        prod_parent = self.c.get_volume(p.dest_cluster, p.dest_vserver,
                                        p.volume).aggregate
        prod_aggr = self._find_best_aggregate(p.dest_cluster,
                                              exclude=prod_parent)
        self.log.info("         PROD  best aggregate : %s  (parent excluded: %s)",
                      prod_aggr, prod_parent or "none")
        dr_parent = self.c.get_volume(p.dr_cluster, p.dr_vserver,
                                      p.volume).aggregate
        dr_aggr = self._find_best_aggregate(p.dr_cluster, exclude=dr_parent)
        self.log.info("         DR    best aggregate : %s  (parent excluded: %s)",
                      dr_aggr, dr_parent or "none")

        # [3/3] Volume moves (fire-and-forget) — the actual promotion.
        self.log.info(">> [3/3]  Launching volume moves (detach from parents)")
        for qtree in qtrees:
            clone_vol = wanted[qtree]
            self.c.start_volume_move(p.dest_cluster, p.dest_vserver,
                                     clone_vol, prod_aggr)
            self.c.start_volume_move(p.dr_cluster, p.dr_vserver,
                                     clone_vol, dr_aggr)
            self.log.info("         '%s'  move launched  PROD -> %s  /  "
                          "DR -> %s.", clone_vol, prod_aggr, dr_aggr)

        job["test_env"] = False
        job.pop("test_expires_at", None)
        job["clone_promoted_at"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        self.jobs.save(job)

        self.log.info("=" * 60)
        self.log.info("  Test environment PROMOTED — volume moves launched "
                      "for %d clone(s).", len(qtrees))
        self.log.info("")
        self._log_table(["Clone volume", "PROD aggregate", "DR aggregate"],
                        [[wanted[q], prod_aggr, dr_aggr] for q in qtrees])
        self.log.info("")
        self.log.info("  Monitor moves: volume move show -vserver %s / %s",
                      p.dest_vserver, p.dr_vserver)
        self.log.info("=" * 60)
        return {"promoted": True,
                "volume_map": dict(wanted),
                "clone_volumes": sorted(wanted.values()),
                "prod_aggregate": prod_aggr, "dr_aggregate": dr_aggr}

    # =====================================================================
    # ACTION 'clone'
    # =====================================================================
    def clone(self, qtrees_arg: str, job: Optional[dict] = None,
              fresh: bool = False,
              volume_map: Optional[Dict[str, str]] = None,
              qtree_map: Optional[Dict[str, str]] = None) -> dict:
        """Definitive clones.

        Three modes:
          - a TEST environment exists in the job file -> PROMOTION: the test
            clones already carry the data and the PROD->DR mirror; only the
            volume moves (detach from parents) are launched;
          - fresh=True -> the existing test environment (if any) is IGNORED
            and the full flow runs on a clean base; the old test clones are
            left in place and must be deleted manually (commands printed);
          - no test environment -> full flow (snapshot, propagation,
            FlexClones, clone mirror + resync, volume moves).
        """
        p = self.p
        old_test_env: Optional[dict] = None
        if job is not None:
            self.job_id = job["job_id"]
            # Checked before any branch so a promotion with the wrong qtree
            # set, or a clone on an unhealthy cascade, is refused up front.
            self._preflight(self.checker.for_clone(
                job, self._resolve_qtrees(qtrees_arg), fresh=fresh,
                volume_map=volume_map, qtree_map=qtree_map))
            if job.get("test_env") and job.get("clone_volumes"):
                if not fresh:
                    return self._promote_test_env(qtrees_arg, job)
                # Fresh start requested: keep track of the abandoned test
                # environment so its clones can be cleaned up afterwards.
                old_test_env = {"volumes": list(job.get("clone_volumes", []))}
                self.log.warning("FRESH mode: ignoring the existing test "
                                 "environment — its clones (%s) stay in place "
                                 "and must be deleted manually (see end of "
                                 "run).", ", ".join(old_test_env["volumes"]))

        self.log.info("=" * 60)
        self.log.info("  ACTION: clone%s", "  [fresh]" if fresh else "")
        self.log.info("  %s  >>  %s  +  %s",
                      p.source_cluster, p.dest_cluster, p.dr_cluster)
        self.log.info("=" * 60)

        qtrees = self._resolve_qtrees(qtrees_arg)
        self.log.info("Qtrees (%d): %s", len(qtrees), ", ".join(qtrees))

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_name = f"clone_migr_{stamp}"
        mapping = self._resolve_volume_map(
            qtrees, volume_map, None if fresh else job)
        renames = self._resolve_qtree_map(
            qtrees, qtree_map, None if fresh else job)
        self._log_table(["Qtree", "Target volume", "Qtree in the clone"],
                        [[q, mapping[q], renames.get(q, q)] for q in qtrees])

        # Steps 1-3: snapshot + cascade propagation + verification.
        self._propagate_snapshot(snap_name, step_offset=1, total_steps=7)

        # Step 4: FlexClones on PROD and DR.
        self.log.info(">> [4/7]  Creating FlexClone volumes on PROD and DR")
        self._create_clones_on_both(qtrees, snap_name, mapping)

        # Step 4b: rename inside the PROD clones, before the mirror exists,
        # so the first resync carries the new names to DR.
        if renames:
            self.log.info(">> [4/7]  Renaming qtrees inside the PROD clones")
            self._rename_qtrees_in_clones(qtrees, mapping, renames)

        # Step 5: SnapMirror between the clones + resync.
        self.log.info(">> [5/7]  SnapMirror (clone PROD -> clone DR) + resync")
        for qtree in qtrees:
            clone_vol = mapping[qtree]
            prod_clone = p.path(p.dest_vserver, clone_vol)
            dr_clone = p.path(p.dr_vserver, clone_vol)
            self.c.snapmirror_create(p.dr_cluster, prod_clone, dr_clone)
            self.c.snapmirror_resync(p.dr_cluster, dr_clone)
            self.log.info("         '%s'  relationship created, resync "
                          "launched.", clone_vol)

        # Step 6: wait all resyncs idle.
        self.log.info(">> [6/7]  Waiting for clone resyncs")
        for qtree in qtrees:
            dr_clone = p.path(p.dr_vserver, mapping[qtree])
            self._wait_snapmirror(p.dr_cluster, dr_clone, want="idle")
            self.log.info("         '%s'  resync complete.", mapping[qtree])

        # Step 7: best aggregates + volume moves (fire-and-forget).
        self.log.info(">> [7/7]  Selecting aggregates, launching volume moves")
        prod_parent = self.c.get_volume(p.dest_cluster, p.dest_vserver,
                                        p.volume).aggregate
        prod_aggr = self._find_best_aggregate(p.dest_cluster, exclude=prod_parent)
        self.log.info("         PROD  best aggregate : %s  (parent excluded: %s)",
                      prod_aggr, prod_parent or "none")
        dr_parent = self.c.get_volume(p.dr_cluster, p.dr_vserver,
                                      p.volume).aggregate
        dr_aggr = self._find_best_aggregate(p.dr_cluster, exclude=dr_parent)
        self.log.info("         DR    best aggregate : %s  (parent excluded: %s)",
                      dr_aggr, dr_parent or "none")

        for qtree in qtrees:
            clone_vol = mapping[qtree]
            self.c.start_volume_move(p.dest_cluster, p.dest_vserver,
                                     clone_vol, prod_aggr)
            self.c.start_volume_move(p.dr_cluster, p.dr_vserver,
                                     clone_vol, dr_aggr)
            self.log.info("         '%s'  move launched  PROD -> %s  /  "
                          "DR -> %s.", clone_vol, prod_aggr, dr_aggr)

        self._save_clone_metadata(job, mapping, qtrees, renames)

        self.log.info("=" * 60)
        self.log.info("  Volume moves launched for %d clone(s). Exiting.",
                      len(qtrees))
        self.log.info("")
        self._log_table(["Qtree", "Clone volume", "Qtree in the clone",
                         "PROD aggregate", "DR aggregate"],
                        [[q, mapping[q], renames.get(q, q), prod_aggr, dr_aggr]
                         for q in qtrees])
        self.log.info("")
        self.log.info("  Monitor moves: volume move show -vserver %s / %s",
                      p.dest_vserver, p.dr_vserver)
        if old_test_env:
            self.log.info("")
            self.log.warning("  Abandoned TEST environment — delete its "
                             "clones once convenient:")
            for vol in old_test_env["volumes"]:
                self.log.info("      snapmirror delete -destination-path %s",
                              p.path(p.dr_vserver, vol))
            for cluster, svm in ((p.dest_cluster, p.dest_vserver),
                                 (p.dr_cluster,   p.dr_vserver)):
                self.log.info("      On %s:", cluster)
                for vol in old_test_env["volumes"]:
                    self.log.info("        volume offline -vserver %s -volume "
                                  "%s ; volume delete -vserver %s -volume %s",
                                  svm, vol, svm, vol)
        self.log.info("=" * 60)
        return {"volume_map": {q: mapping[q] for q in qtrees},
                "qtree_map": dict(renames),
                "clone_volumes": [mapping[q] for q in qtrees],
                "prod_aggregate": prod_aggr, "dr_aggregate": dr_aggr,
                "abandoned_test_env": old_test_env}

    # =====================================================================
    # ACTION 'test'
    # =====================================================================
    def test(self, qtrees_arg: str, job: Optional[dict] = None,
             validity_days: int = 7,
             volume_map: Optional[Dict[str, str]] = None,
             qtree_map: Optional[Dict[str, str]] = None) -> dict:
        """Full TEST environment: everything except the split / volume move.

        Builds the exact future production layout so the client can validate
        access, permissions and replication:

          - FlexClones on future PROD and future DR, each named by the
            client through the qtree -> volume mapping,
          - SnapMirror relationships between the PROD and DR clones,
            resynced and waited to idle.

        The clones stay attached to their parent DP volumes (thin, no disk
        space consumed). The environment is TIME-LIMITED (validity_days,
        expiry stored in the job file):

          - before expiry, action 'clone' PROMOTES this environment to the
            definitive one (volume moves only — nothing is rebuilt);
          - past expiry, the clones must be deleted (commands printed below).
        """
        p = self.p
        if job is not None:
            self.job_id = job["job_id"]

        self.log.info("=" * 60)
        self.log.info("  ACTION: test  (full environment — no split, no move)")
        self.log.info("  %s  >>  %s  +  %s",
                      p.source_cluster, p.dest_cluster, p.dr_cluster)
        self.log.info("=" * 60)

        # Cascade complete and healthy, no test environment already in place,
        # qtrees existing and unique, peering/policy for the clone mirror.
        qtrees = self._resolve_qtrees(qtrees_arg)
        self._preflight(self.checker.for_test(job or {}, qtrees,
                                              volume_map=volume_map,
                                              qtree_map=qtree_map))
        self.log.info("Qtrees (%d): %s", len(qtrees), ", ".join(qtrees))

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_name = f"test_migr_{stamp}"
        mapping = self._resolve_volume_map(qtrees, volume_map, job)
        renames = self._resolve_qtree_map(qtrees, qtree_map, job)
        self._log_table(["Qtree", "Target volume", "Qtree in the clone"],
                        [[q, mapping[q], renames.get(q, q)] for q in qtrees])

        # Steps 1-3: snapshot + cascade propagation + verification.
        self._propagate_snapshot(snap_name, step_offset=1, total_steps=6)

        # Step 4: FlexClones on future PROD and future DR.
        self.log.info(">> [4/6]  Creating thin FlexClone volumes on PROD and DR")
        self._create_clones_on_both(qtrees, snap_name, mapping)

        # Step 4b: rename inside the PROD clones, before the mirror exists,
        # so the first resync carries the new names to DR. The test
        # environment must show the client the names production will have.
        if renames:
            self.log.info(">> [4/6]  Renaming qtrees inside the PROD clones")
            self._rename_qtrees_in_clones(qtrees, mapping, renames)

        # Step 5: SnapMirror between the clones + resync (like production).
        self.log.info(">> [5/6]  SnapMirror (clone PROD -> clone DR) + resync")
        for qtree in qtrees:
            clone_vol = mapping[qtree]
            prod_clone = p.path(p.dest_vserver, clone_vol)
            dr_clone = p.path(p.dr_vserver, clone_vol)
            self.c.snapmirror_create(p.dr_cluster, prod_clone, dr_clone)
            self.c.snapmirror_resync(p.dr_cluster, dr_clone)
            self.log.info("         '%s'  relationship created, resync "
                          "launched.", clone_vol)

        # Step 6: wait all clone resyncs idle.
        self.log.info(">> [6/6]  Waiting for clone resyncs")
        for qtree in qtrees:
            dr_clone = p.path(p.dr_vserver, mapping[qtree])
            self._wait_snapmirror(p.dr_cluster, dr_clone, want="idle")
            self.log.info("         '%s'  resync complete.", mapping[qtree])

        # Persist the test environment metadata (with its expiry date).
        created = datetime.datetime.now()
        expires = created + datetime.timedelta(days=validity_days)
        if job is not None:
            job["volume_map"] = {q: mapping[q] for q in qtrees}
            job["qtree_map"] = {q: renames[q] for q in qtrees if q in renames}
            job["clone_volumes"] = [mapping[q] for q in qtrees]
            job["test_env"] = True
            job["test_qtrees"] = qtrees
            job["test_created_at"] = created.isoformat(timespec="seconds")
            job["test_expires_at"] = expires.isoformat(timespec="seconds")
            self.jobs.save(job)

        self.log.info("=" * 60)
        self.log.info("  TEST environment ready — %d clone(s), mirrored "
                      "PROD -> DR, no disk space consumed.", len(qtrees))
        self.log.info("  Valid until: %s  (%d day(s))",
                      expires.strftime("%Y-%m-%d %H:%M"), validity_days)
        self.log.info("")
        self._log_table(["Qtree", "Test clone",
                         f"PROD ({p.dest_cluster})", f"DR ({p.dr_cluster})",
                         "Mirror"],
                        [[q, mapping[q], "created", "created", "idle"]
                         for q in qtrees])
        self.log.info("")
        self.log.info("  The client can now validate access and permissions.")
        self.log.info("  BEFORE %s:", expires.strftime("%Y-%m-%d"))
        self.log.info("    - promote to the definitive environment with "
                      "action 'clone' (volume moves only), OR")
        self.log.info("    - delete the test environment:")
        for qtree in qtrees:
            self.log.info("      snapmirror delete -destination-path %s",
                          p.path(p.dr_vserver, mapping[qtree]))
        for cluster, svm in ((p.dest_cluster, p.dest_vserver),
                             (p.dr_cluster,   p.dr_vserver)):
            self.log.info("      On %s:", cluster)
            for qtree in qtrees:
                self.log.info("        volume offline -vserver %s -volume %s "
                              "; volume delete -vserver %s -volume %s",
                              svm, mapping[qtree], svm, mapping[qtree])
        self.log.info("=" * 60)
        return {"volume_map": {q: mapping[q] for q in qtrees},
                "clone_volumes": [mapping[q] for q in qtrees],
                "expires_at": expires.isoformat(timespec="seconds")}

    # =====================================================================
    # ACTION 'acl'
    # =====================================================================
    def acl(self, ad_groups: str, acl_path: str,
            acl_rights: str = "full-control",
            job: Optional[dict] = None) -> dict:
        """Force AD-group DACLs server-side on ONE destination path.

        Fully decoupled from the 'test' and 'clone' actions: the client
        provides the exact path (any path on his destination volumes, e.g.
        '/v_q_fin_8072b8' or '/v_q_fin_8072b8/projects') and the groups to
        force. Runs on the PROD destination cluster; the DR side receives
        the same ACLs through the clone SnapMirror replication.
        """
        p = self.p
        if job is not None:
            self.job_id = job["job_id"]

        groups = [g.strip() for g in ad_groups.split(",") if g.strip()]

        # Path canonical, non-root, inside this job's clones, resolvable on
        # the SVM, NTFS security style, group syntax.
        self._preflight(self.checker.for_acl(job, acl_path, groups,
                                             acl_rights))
        acl_path = "/" + "/".join(s for s in acl_path.split("/") if s)

        self.log.info("=" * 60)
        self.log.info("  ACTION: acl  (force AD groups via NTFS DACL)")
        self.log.info("  Target: %s / vserver %s / path '%s'",
                      p.dest_cluster, p.dest_vserver, acl_path)
        self.log.info("=" * 60)
        self._log_table(["AD group", "Rights", "Propagation"],
                        [[g, acl_rights, "this-folder, sub-folders, files"]
                         for g in groups])

        self.log.info(">> Forcing DACL on '%s'", acl_path)
        self.c.apply_file_security(p.dest_cluster, p.dest_vserver,
                                   acl_path, groups, acl_rights)

        self.log.info("=" * 60)
        self.log.info("  DACL forcing launched on '%s'.", acl_path)
        self.log.info("  Note: DR receives the same ACLs through the clone")
        self.log.info("  SnapMirror replication (next update/resync).")
        self.log.info("=" * 60)
        return {"path": acl_path, "groups": groups, "rights": acl_rights}

    # =====================================================================
    # ACTION 'cleanup'
    # =====================================================================
    def prune(self, qtrees_arg: str, job: Optional[dict] = None,
              confirm: bool = False) -> dict:
        """Delete, in each clone volume, every qtree it does not own.

        A FlexClone is a copy of the WHOLE parent volume, so the volume
        created for q_fin also holds q_hr, q_ops and the rest. After the
        volume move has detached it from its parent, that surplus is real
        occupied space — and other clients' data sitting in this client's
        volume. This removes it.

        Irreversible, so it is fenced in:
          - the volume must be split from its parent and its move finished,
            otherwise the space would not be freed anyway;
          - the qtree the volume was created for is never touched;
          - PROD only: the DR clone is a mirror destination and receives the
            deletions by replication;
          - nothing happens without confirm=True.

        The SOURCE volume is never touched by this action.
        """
        p = self.p
        if job is not None:
            self.job_id = job["job_id"]
        qtrees = self._resolve_qtrees(qtrees_arg)
        self._preflight(self.checker.for_prune(job or {}, qtrees))

        mapping = self._resolve_volume_map(qtrees, None, job)
        renames = self._resolve_qtree_map(qtrees, None, job)

        self.log.info("=" * 60)
        self.log.info("  ACTION: prune  |  removing inherited qtrees")
        self.log.info("=" * 60)

        # What would go, per volume, before anything goes.
        plan: Dict[str, List[str]] = {}
        for qtree in qtrees:
            volume = mapping[qtree]
            kept = renames.get(qtree, qtree)
            present = self.c.list_qtrees(p.dest_cluster, p.dest_vserver, volume)
            plan[qtree] = [q for q in present
                           if q.lower() != kept.lower() and q not in ("", "-")]

        rows = [[q, mapping[q], renames.get(q, q),
                 ", ".join(plan[q]) or "(nothing to remove)"] for q in qtrees]
        self._log_table(["Qtree", "Clone volume", "Kept", "To be DELETED"], rows)

        total = sum(len(v) for v in plan.values())
        if not total:
            self.log.info("  Nothing to prune: every clone holds only its own "
                          "qtree.")
            return {"pruned": {}, "deleted_count": 0}

        if not confirm:
            raise ConfirmationRequired(
                f"prune would permanently delete {total} qtree(s) and all "
                f"their data from {len(qtrees)} clone volume(s) — re-run with "
                f"confirmation once the table above has been checked")

        deleted: Dict[str, List[str]] = {}
        for qtree in qtrees:
            volume = mapping[qtree]
            for surplus in plan[qtree]:
                self.c.delete_qtree(p.dest_cluster, p.dest_vserver,
                                    volume, surplus)
                deleted.setdefault(qtree, []).append(surplus)
                self.log.info("         '%s'  qtree '%s' deleted.",
                              volume, surplus)

        self.log.info("=" * 60)
        self.log.info("  %d qtree(s) deleted across %d clone volume(s).",
                      sum(len(v) for v in deleted.values()), len(deleted))
        self.log.info("  The DR copies follow on the next SnapMirror update.")
        self.log.info("=" * 60)
        return {"pruned": deleted,
                "deleted_count": sum(len(v) for v in deleted.values())}

    def cleanup(self, qtree: str, job: Optional[dict] = None) -> dict:
        """Cut source access for one qtree (export-policy, CIFS, rename)."""
        p = self.p
        self.log.info("=" * 60)
        self.log.info("  ACTION: cleanup  |  source qtree '%s'", qtree)
        self.log.info("=" * 60)

        # Qtree non-empty and existing, migration confirmed complete, and an
        # explicit preview of the CIFS shares that will be deleted.
        self._preflight(self.checker.for_cleanup(job, qtree))
        qtree = qtree.strip()

        self.c.set_qtree_export_policy(p.source_cluster, p.source_vserver,
                                       p.volume, qtree, p.noaccess_policy)
        self.log.info("Export-policy '%s' applied to qtree '%s'.",
                      p.noaccess_policy, qtree)

        shares = self.c.find_cifs_shares(p.source_cluster, p.source_vserver,
                                         f"/{qtree}")
        if not shares:
            self.log.info("No CIFS share associated with qtree '%s'.", qtree)
        for share in shares:
            self.c.delete_cifs_share(p.source_cluster, p.source_vserver, share)
            self.log.info("CIFS share '%s' deleted.", share)

        today = datetime.datetime.now().strftime("%d_%m_%Y")
        new_name = f"{qtree}_tobedeleted_migratedtosfs_{today}"
        self.c.rename_qtree(p.source_cluster, p.source_vserver, p.volume,
                            qtree, new_name)
        self.log.info("Source qtree renamed: '%s' -> '%s'.", qtree, new_name)

        self.log.info("ACTION 'cleanup' complete for qtree '%s'.", qtree)
        return {"qtree": qtree, "renamed_to": new_name,
                "deleted_shares": shares}
