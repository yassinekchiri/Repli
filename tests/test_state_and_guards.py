"""SnapMirror state machine + guarantee that a failed pre-flight mutates nothing.

These reproduce the audit findings P0-4, P0-5 and P1-4.
"""

import pytest

from netapp_migration.core.engine import MigrationEngine
from netapp_migration.core.jobs import JobStore
from netapp_migration.models import (MigrationParams, OntapError,
                                     PreflightFailed, SnapMirrorInfo)

from conftest import FakeClient, cascade_ready


# =============================================================================
# SnapMirror predicates (P0-4 / P0-5)
# =============================================================================

@pytest.mark.parametrize("state,transfer,is_idle,is_ready", [
    ("snapmirrored", "idle",         True,  True),
    ("in_sync",      "idle",         True,  True),
    ("snapmirrored", "transferring", False, False),
    ("snapmirrored", "queued",       False, False),
    ("snapmirrored", "preparing",    False, False),
    ("snapmirrored", "finalizing",   False, False),
    ("snapmirrored", "failed",       False, False),
    ("snapmirrored", "aborted",      False, False),
    ("broken_off",   "idle",         False, False),
    ("out_of_sync",  "idle",         False, False),
    ("uninitialized", "idle",        True,  False),
    ("snapmirrored", "unknown",      False, False),
    ("weird_state",  "idle",         True,  False),
])
def test_state_matrix(state, transfer, is_idle, is_ready):
    sm = SnapMirrorInfo("svm:vol", state=state, transfer_state=transfer)
    assert sm.is_idle is is_idle
    assert sm.is_ready is is_ready


def test_absent_relationship_is_never_idle_nor_ready():
    """P0-5: an absent relationship used to satisfy a wait for 'idle'."""
    sm = SnapMirrorInfo("svm:vol", state="absent", transfer_state="unknown",
                        exists=False)
    assert not sm.is_idle
    assert not sm.is_ready
    assert sm.unhealthy_reason == "relationship does not exist"


def test_transferring_relationship_is_not_ready():
    """P0-4: a transferring relationship used to be reported as ready."""
    sm = SnapMirrorInfo("svm:vol", state="snapmirrored",
                        transfer_state="transferring")
    assert not sm.is_ready
    assert "transfer in progress" in sm.unhealthy_reason


def test_wait_idle_times_out_on_a_failed_relationship(client, params, store,
                                                      logger):
    """A broken mirror must make the wait fail, not return immediately."""
    engine = MigrationEngine(client, params, store, logger)
    client.add_relationship("svm_dr:v_x", state="broken_off",
                            transfer_state="failed")
    with pytest.raises(OntapError) as err:
        engine._wait_snapmirror("DRC", "svm_dr:v_x", want="idle")
    assert "timeout" in str(err.value)


# =============================================================================
# check_status no longer lies (P0-4)
# =============================================================================

def test_check_status_does_not_complete_while_transferring(client, params,
                                                           store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    store.set_status(job, "dest_initialized")
    for svm, cluster in ((params.dest_vserver, "PRD"),
                         (params.dr_vserver, "DRC")):
        client.add_relationship(params.path(svm, params.volume),
                                state="snapmirrored",
                                transfer_state="transferring")
    result = engine.check_status(job)
    assert result["completed"] is False
    assert store.load(job["job_id"])["status"] == "dest_initialized"


def test_check_status_completes_when_really_idle(client, params, store, logger):
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    store.set_status(job, "dest_initialized")
    cascade_ready(client, params)
    result = engine.check_status(job)
    assert result["completed"] is True
    assert store.load(job["job_id"])["status"] == "completed"


def test_check_status_read_only_never_persists(client, params, store, logger):
    """GET /status must not rewrite the job file."""
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    store.set_status(job, "dest_initialized")
    cascade_ready(client, params)
    result = engine.check_status(job, persist=False)
    assert result["completed"] is True
    assert store.load(job["job_id"])["status"] == "dest_initialized"


# =============================================================================
# dry-run isolation (P1-4)
# =============================================================================

def test_dry_run_store_never_writes(tmp_path, params, client, logger):
    real = JobStore(str(tmp_path))
    job = real.create(params)
    real.set_status(job, "dest_initialized")

    simulated = JobStore(str(tmp_path), read_only=True)
    loaded = simulated.load(job["job_id"])
    simulated.set_status(loaded, "completed")          # would corrupt the job

    assert real.load(job["job_id"])["status"] == "dest_initialized"


def test_job_store_creates_its_directory(tmp_path):
    target = tmp_path / "nested" / "jobs"
    store = JobStore(str(target))
    assert target.is_dir()


# =============================================================================
# A failed pre-flight mutates nothing
# =============================================================================

def test_create_mutates_nothing_when_preflight_fails(client, params, store,
                                                     logger):
    engine = MigrationEngine(client, params, store, logger)
    client.schedules["DRC"] = []                       # invisible schedule
    with pytest.raises(PreflightFailed) as err:
        engine.create()
    assert "SCHEDULE_MISSING" in {c.code for c in err.value.report.failures}
    assert client.calls == [], f"unexpected mutations: {client.calls}"


def test_test_action_mutates_nothing_when_qtree_unknown(client, params, store,
                                                        logger):
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    with pytest.raises(PreflightFailed):
        engine.test("q_missing", job=job)
    assert not any(c.startswith("create_clone") for c in client.calls)
    assert not any(c.startswith("create_snapshot") for c in client.calls)


def test_cleanup_mutates_nothing_on_empty_qtree(client, params, store, logger):
    """P0-6: the empty qtree used to select every CIFS share."""
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    store.set_status(job, "completed")
    with pytest.raises(PreflightFailed):
        engine.cleanup("", job=job)
    assert client.calls == []
    assert "fin_share" in client.shares[("SRC", "svm_source")]


def test_acl_mutates_nothing_on_root_path(client, params, store, logger):
    """P0-7: '/' would rewrite the ACLs of every volume of the SVM."""
    engine = MigrationEngine(client, params, store, logger)
    job = store.create(params)
    with pytest.raises(PreflightFailed):
        engine.acl("DOM\\grp", acl_path="/", job=job)
    assert client.calls == []


def test_create_succeeds_end_to_end_on_a_healthy_estate(client, params, store,
                                                         logger):
    engine = MigrationEngine(client, params, store, logger)
    job = engine.create(create_mode="full")
    assert store.load(job["job_id"])["status"] == "completed"
    # The three DP volumes and the three relationships were created.
    assert sum(1 for c in client.calls if c.startswith("create_dp_volume")) == 3
    assert sum(1 for c in client.calls
               if c.startswith("snapmirror_create")) == 3
