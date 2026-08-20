"""A job must say how it actually went, not only how far the cascade got.

Reported from production: `status` read `completed` whether the last action
had succeeded or blown up. It could only ever say how far the CREATE got —
nothing else ever wrote to it — so a failed clone left the job looking
perfectly healthy.
"""

import pytest

from netapp_migration.models import (ConfirmationRequired, OntapError,
                                     PreflightFailed)
from netapp_migration.core.jobs import JobStore

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def outcome_of(store, job):
    return JobStore.outcome(store.load(job["job_id"]))


# =============================================================================
# The reported bug
# =============================================================================

def test_a_failed_clone_no_longer_looks_completed(engine, ready, client, store):
    def explode(*_args, **_kwargs):
        raise OntapError("PRD", "DELETE /storage/qtrees/x/2",
                         'HTTP 400: Unexpected Argument "force"')
    client.delete_qtree = explode

    with pytest.raises(OntapError):
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    outcome = outcome_of(store, ready)
    assert outcome["cascade_status"] == "completed", \
        "the cascade really is replicated: that part was never wrong"
    assert outcome["last_action"] == "clone"
    assert outcome["last_action_state"] == "failed"
    assert "Unexpected Argument" in outcome["last_action_error"]


def test_a_successful_clone_is_recorded_as_such(engine, ready, store):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    outcome = outcome_of(store, ready)
    assert outcome["last_action"] == "clone"
    assert outcome["last_action_state"] == "success"
    assert outcome["last_action_error"] == ""


# =============================================================================
# Telling apart the ways an action can end
# =============================================================================

def test_a_preflight_refusal_is_not_a_failure(engine, ready, store):
    """Nothing was touched: that is a different thing from a crash."""
    with pytest.raises(PreflightFailed):
        engine.clone("q_absent", job=ready, volume_map={"q_absent": "vol_x"})

    outcome = outcome_of(store, ready)
    assert outcome["last_action_state"] == "refused"
    assert "check(s) failed" in outcome["last_action_error"]


def test_a_confirmation_request_is_not_a_failure(engine, client, params, store):
    """resume asks before firing the fan-out; being asked is not a failure."""
    job = store.create(params, "full")
    store.set_status(job, "pivot_initialized")
    # The state resume expects: pivot baselined, PROD and DR declared but
    # not yet initialised.
    for cluster, svm, aggr in ((params.pivot_cluster, params.pivot_vserver,
                                params.pivot_aggr),
                               (params.dest_cluster, params.dest_vserver,
                                params.dest_aggr),
                               (params.dr_cluster, params.dr_vserver,
                                params.dr_aggr)):
        client.add_volume(cluster, svm, params.volume, aggregate=aggr)
    client.add_relationship(params.path(params.pivot_vserver, params.volume),
                            state="snapmirrored", transfer_state="idle")
    for svm in (params.dest_vserver, params.dr_vserver):
        client.add_relationship(params.path(svm, params.volume),
                                state="uninitialized", transfer_state="idle")

    with pytest.raises(ConfirmationRequired):
        engine.resume(job, confirm=False)

    assert outcome_of(store, job)["last_action_state"] == "needs_confirmation"


def test_an_action_in_flight_reads_as_running(engine, ready, client, store):
    """A crash mid-run leaves 'running', never a stale 'success'."""
    seen = {}

    def capture(*_args, **_kwargs):
        seen["state"] = outcome_of(store, ready)["last_action_state"]
        raise OntapError("PRD", "boom", "stopped here")
    client.create_clone = capture

    with pytest.raises(OntapError):
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert seen["state"] == "running"


# =============================================================================
# The trail
# =============================================================================

def test_the_history_keeps_what_happened_before(engine, ready, client, store):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    def explode(*_args, **_kwargs):
        raise OntapError("PRD", "clone", "second attempt failed")
    client.create_clone = explode
    with pytest.raises(OntapError):
        engine.clone("q_hr", job=ready, volume_map=vmap("q_hr"))

    history = store.load(ready["job_id"])["history"]
    assert [h["state"] for h in history] == ["success", "failed"]
    assert all(h["action"] == "clone" for h in history)
    assert all(h["ended_at"] for h in history)


def test_the_history_does_not_grow_without_bound(engine, ready, store):
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))   # makes the path
    for _ in range(25):
        engine.acl("CORP\\grp", acl_path="/vol_q_fin", job=ready)

    assert len(store.load(ready["job_id"])["history"]) == 20


def test_checking_the_status_does_not_overwrite_the_record(engine, ready,
                                                           client, store):
    """Looking at a job must not erase what you are looking for."""
    def explode(*_args, **_kwargs):
        raise OntapError("PRD", "clone", "it broke")
    client.create_clone = explode
    with pytest.raises(OntapError):
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))
    client.create_clone = None

    engine.check_status(ready)

    outcome = outcome_of(store, ready)
    assert outcome["last_action"] == "clone", \
        "check-status must not file itself over the failure"
    assert outcome["last_action_state"] == "failed"


def test_every_action_records_itself(engine, ready, client, store):
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))
    assert outcome_of(store, ready)["last_action"] == "test"

    engine.acl("CORP\\grp", acl_path="/vol_q_fin/data", job=ready)
    assert outcome_of(store, ready)["last_action"] == "acl"


# =============================================================================
# Compatibility and isolation
# =============================================================================

def test_a_job_file_from_before_this_change_still_reads(store, params):
    """No last_action: report 'unknown', never imply success."""
    job = store.create(params, "full")
    job.pop("last_action", None)
    store.save(job)

    outcome = JobStore.outcome(store.load(job["job_id"]))
    assert outcome["last_action_state"] == "unknown"
    assert outcome["last_action"] == ""


def test_a_dry_run_never_writes_an_outcome(client, params, tmp_path):
    """A simulation must not rewrite the state of a real job."""
    import logging
    from netapp_migration.core.engine import MigrationEngine

    real = JobStore(str(tmp_path / "jobs"))
    job = real.create(params, "full")
    real.set_status(job, "completed")
    cascade_ready(client, params)

    simulated = JobStore(str(tmp_path / "jobs"), read_only=True)
    logger = logging.getLogger("tests.dryrun")
    MigrationEngine(client, params, simulated, logger).clone(
        "q_fin", job=job, volume_map=vmap("q_fin"))

    assert "last_action" not in real.load(job["job_id"]), \
        "the file on disk must be untouched by a simulated run"


# =============================================================================
# What the API shows
# =============================================================================

def test_the_api_reports_the_outcome_next_to_the_status(api):   # noqa: F811
    from netapp_migration.models import MigrationParams
    from test_api import CREATE_BODY

    http, store, fake, _tokens = api
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)

    def explode(*_args, **_kwargs):
        raise OntapError("PRD", "clone", "it broke")
    fake.create_clone = explode

    http.post(f"/api/v1/migrations/{job['job_id']}/clone",
              json={"qtrees": "q_fin", "volume_map": {"q_fin": "vol_fin"}})

    import time
    for _ in range(80):
        body = http.get(f"/api/v1/migrations/{job['job_id']}").json()
        if body["outcome"]["last_action_state"] == "failed":
            break
        time.sleep(0.05)

    assert body["job"]["status"] == "completed"
    assert body["outcome"]["last_action"] == "clone"
    assert body["outcome"]["last_action_state"] == "failed"
    assert "it broke" in body["outcome"]["last_action_error"]

    listing = http.get("/api/v1/migrations").json()["jobs"][0]
    assert listing["last_action_state"] == "failed", \
        "the listing must not show a healthy job either"
