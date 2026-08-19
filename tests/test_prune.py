"""Pruning the qtrees a clone inherited but does not own.

A FlexClone copies the whole parent volume, so the volume created for q_fin
also holds q_hr and q_ops. This deletes that surplus — irreversibly — which
is why most of what follows tests the refusals rather than the happy path.
"""

import pytest

from netapp_migration.models import ConfirmationRequired, PreflightFailed

from conftest import cascade_ready, vmap
from test_api import api          # noqa: F401


@pytest.fixture()
def cloned(engine, client, params, store):
    """A job whose clones exist, are split, and hold every source qtree."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"))
    client.calls.clear()
    return job


def qtrees_of(client, params, volume):
    return client.qtrees.get((params.dest_cluster, params.dest_vserver,
                              volume), [])


# =============================================================================
# The problem it solves
# =============================================================================

def test_a_clone_starts_out_holding_every_source_qtree(cloned, client, params):
    """The premise: without pruning, each client's volume holds all data."""
    assert sorted(qtrees_of(client, params, "vol_q_fin")) == \
        ["q_fin", "q_hr", "q_ops"]


def test_pruning_leaves_only_the_qtree_the_volume_owns(engine, cloned, client,
                                                       params):
    engine.prune("q_fin,q_hr", job=cloned, confirm=True)

    assert qtrees_of(client, params, "vol_q_fin") == ["q_fin"]
    assert qtrees_of(client, params, "vol_q_hr") == ["q_hr"]


def test_deletions_happen_on_prod_only(engine, cloned, client, params):
    """DR is a mirror destination: the deletions reach it by replication."""
    engine.prune("q_fin", job=cloned, confirm=True)

    deletes = [c for c in client.calls if c.startswith("delete_qtree")]
    assert deletes, "expected deletions"
    assert all(params.dest_cluster in c for c in deletes)
    assert not any(params.dr_cluster in c for c in deletes)


def test_the_source_volume_is_never_touched(engine, cloned, client, params):
    before = list(client.qtrees[(params.source_cluster, params.source_vserver,
                                 params.volume)])

    engine.prune("q_fin,q_hr", job=cloned, confirm=True)

    assert client.qtrees[(params.source_cluster, params.source_vserver,
                          params.volume)] == before


def test_a_renamed_qtree_is_the_one_kept(engine, client, params, store):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    engine.prune("q_fin", job=job, confirm=True)

    assert qtrees_of(client, params, "vol_q_fin") == ["finance"]


def test_the_result_lists_what_was_deleted(engine, cloned):
    result = engine.prune("q_fin", job=cloned, confirm=True)

    assert result["deleted_count"] == 2
    assert sorted(result["pruned"]["q_fin"]) == ["q_hr", "q_ops"]


# =============================================================================
# Refusals — the reason this action is fenced in
# =============================================================================

def test_nothing_is_deleted_without_confirmation(engine, cloned, client):
    with pytest.raises(ConfirmationRequired) as raised:
        engine.prune("q_fin,q_hr", job=cloned)

    assert "4 qtree(s)" in str(raised.value), "the count must be stated"
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_an_unsplit_clone_is_refused(engine, client, params, store):
    """Before the move the clone shares its blocks: deleting frees nothing."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"))
    # Put it back the way it is between the clone and the end of the move.
    client.volumes[(params.dest_cluster, params.dest_vserver,
                    "vol_q_fin")].is_flexclone = True
    client.calls.clear()

    with pytest.raises(PreflightFailed) as raised:
        engine.prune("q_fin", job=job, confirm=True)

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "PRUNE_NOT_SPLIT" in codes
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_a_move_still_running_is_refused(engine, client, params, store):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"))
    client.volumes[(params.dest_cluster, params.dest_vserver,
                    "vol_q_fin")].move_state = "replicating"
    client.calls.clear()

    with pytest.raises(PreflightFailed) as raised:
        engine.prune("q_fin", job=job, confirm=True)

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "PRUNE_MOVE_RUNNING" in codes
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_a_test_environment_is_refused(engine, client, params, store):
    """Test clones are still attached and time-limited: never prune them."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.test("q_fin", job=job, volume_map=vmap("q_fin"))
    client.calls.clear()

    with pytest.raises(PreflightFailed) as raised:
        engine.prune("q_fin", job=job, confirm=True)

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "PRUNE_ON_TEST_ENV" in codes
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_pruning_before_any_clone_exists_is_refused(engine, client, params,
                                                    store):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)

    with pytest.raises(PreflightFailed) as raised:
        engine.prune("q_fin", job=job, confirm=True)

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "PRUNE_NO_CLONES" in codes


def test_pruning_twice_is_harmless(engine, cloned, client):
    engine.prune("q_fin", job=cloned, confirm=True)
    client.calls.clear()

    result = engine.prune("q_fin", job=cloned, confirm=True)

    assert result["deleted_count"] == 0
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


# =============================================================================
# Through the API
# =============================================================================

def _cloned_job_via_api(api):
    """Same starting point as the `cloned` fixture, but on the API's estate."""
    from netapp_migration.models import MigrationParams
    from netapp_migration.core.engine import MigrationEngine
    import logging

    http, store, fake, _ = api
    from test_api import CREATE_BODY
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)
    logger = logging.getLogger("tests.prune.api")
    MigrationEngine(fake, params, store, logger).clone(
        "q_fin", job=job, volume_map=vmap("q_fin"))
    fake.calls.clear()
    return http, store, fake, job


def test_api_refuses_without_confirm_and_says_how_many(api):
    http, _store, fake, job = _cloned_job_via_api(api)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/prune",
                         json={"qtrees": "q_fin"})

    assert response.status_code == 409, response.text
    assert "2 qtree(s)" in str(response.json()["detail"])
    assert not [c for c in fake.calls if c.startswith("delete_qtree")]


def test_api_prunes_once_confirmed(api):
    http, _store, fake, job = _cloned_job_via_api(api)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/prune",
                         json={"qtrees": "q_fin", "confirm": True})

    assert response.status_code == 200, response.text
    assert response.json()["result"]["deleted_count"] == 2
    assert len([c for c in fake.calls if c.startswith("delete_qtree")]) == 2


def test_api_preflight_reports_the_plan_without_deleting(api):
    http, _store, fake, job = _cloned_job_via_api(api)

    report = http.post(
        f"/api/v1/migrations/{job['job_id']}/preflight/prune",
        json={"qtrees": "q_fin"}).json()

    plan = [c for c in report["checks"] if c["code"] == "PRUNE_PLAN"]
    assert plan and "q_hr" in plan[0]["detail"], plan
    assert not [c for c in fake.calls if c.startswith("delete_qtree")]


def test_a_scoped_token_cannot_prune_another_qtree(api):
    http, _store, _fake, job = _cloned_job_via_api(api)
    _, _, _, tokens = api
    scoped = tokens.upsert("q_fin", ["prune"], "NEW_TOKEN")["token"]

    response = http.post(f"/api/v1/migrations/{job['job_id']}/prune",
                         json={"qtrees": "q_hr", "confirm": True},
                         headers={"Authorization": f"Bearer {scoped}"})

    assert response.status_code == 403
