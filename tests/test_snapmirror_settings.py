"""What each SnapMirror relationship is created with.

Two kinds of relationship, configured differently. The cascade stages the
data across once; the clone mirror is the one the client lives with
afterwards, so its schedule decides how far behind DR is allowed to fall.

Nothing here may fall back on a transport default: the pre-flight checks a
policy and a schedule are visible *before* the engine asks ONTAP to use
them, and a check that verifies a different name from the one the engine
sends is worse than no check at all.
"""

import pytest

from netapp_migration.core.replication import (CASCADE_POLICY,
                                               CASCADE_SCHEDULE, CASCADE_TYPE,
                                               CLONE_POLICY, CLONE_SCHEDULE,
                                               CLONE_TYPE)
from netapp_migration.models import PreflightFailed

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def creations(client):
    return [c for c in client.calls if c.startswith("snapmirror_create")]


# =============================================================================
# The settings themselves
# =============================================================================

def test_the_clone_mirror_settings_are_the_ones_asked_for():
    """Pinned: these decide how the client's DR copy behaves in production."""
    assert CLONE_TYPE == "XDP"
    assert CLONE_POLICY == "MFA_MirrorAllSnapshots"
    assert CLONE_SCHEDULE == "pg-15-minutely"


def test_the_cascade_keeps_its_own_settings():
    """Staging the data is a different job from running the DR copy."""
    assert CASCADE_TYPE == "XDP"
    assert CASCADE_POLICY == "MirrorAllSnapshots"
    assert CASCADE_SCHEDULE == "hourly"


# =============================================================================
# What the clone actually creates
# =============================================================================

def test_the_clone_mirror_is_created_with_them(engine, ready, client):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    created = creations(client)[-1]
    assert f"type={CLONE_TYPE}" in created
    assert f"policy={CLONE_POLICY}" in created
    assert f"schedule={CLONE_SCHEDULE}" in created


def test_the_test_environment_mirror_matches_production(engine, ready, client):
    """The point of the test run is that it behaves like the real thing."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    created = creations(client)[-1]
    assert f"policy={CLONE_POLICY}" in created
    assert f"schedule={CLONE_SCHEDULE}" in created


def test_it_is_created_on_the_dr_cluster(engine, ready, client, params):
    """SnapMirror is declared on the cluster hosting the DESTINATION."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert f"{params.dr_vserver}:vol_q_fin" in creations(client)[-1]


def test_every_clone_gets_its_own_relationship(engine, ready, client):
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    clone_mirrors = [c for c in creations(client)
                     if f"policy={CLONE_POLICY}" in c]
    assert len(clone_mirrors) == 2


# =============================================================================
# The cascade is not affected
# =============================================================================

def test_the_cascade_legs_use_the_cascade_settings(engine, store, params,
                                                   client):
    engine.create(store.create(params, "full"))

    assert len(creations(client)) == 3, "source->pivot, pivot->PROD, pivot->DR"
    for created in creations(client):
        assert f"policy={CASCADE_POLICY}" in created
        assert f"schedule={CASCADE_SCHEDULE}" in created
        assert f"type={CASCADE_TYPE}" in created


def test_the_two_kinds_do_not_share_a_policy(engine, ready, client, params,
                                             store):
    """A clone mirror must never be created with the cascade's schedule."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    clone_mirror = creations(client)[-1]
    assert f"schedule={CASCADE_SCHEDULE}" not in clone_mirror


# =============================================================================
# The pre-flight checks the names the engine will really send
# =============================================================================

def test_a_missing_clone_policy_refuses_the_clone(engine, ready, client):
    client.policies["DRC"].remove(CLONE_POLICY)

    with pytest.raises(PreflightFailed) as raised:
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "SNAPMIRROR_POLICY_MISSING" in codes


def test_a_missing_clone_schedule_refuses_the_clone(engine, ready, client):
    client.schedules["DRC"].remove(CLONE_SCHEDULE)

    with pytest.raises(PreflightFailed) as raised:
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "SCHEDULE_MISSING" in codes


def test_a_refused_clone_creates_no_relationship(engine, ready, client):
    """ONTAP resolves a policy with the caller's permissions, so 'not found'
    can also mean 'invisible to this role'. Either way, nothing is built."""
    client.policies["DRC"].remove(CLONE_POLICY)
    client.calls.clear()

    with pytest.raises(PreflightFailed):
        engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert creations(client) == []


def test_the_preflight_checks_the_clone_names_not_the_cascade_ones(engine,
                                                                   ready,
                                                                   client):
    """Removing the cascade policy from DRC must not refuse a clone: the
    clone mirror does not use it."""
    client.policies["DRC"].remove(CASCADE_POLICY)

    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    assert report.ok


# =============================================================================
# What the inventory reports afterwards
# =============================================================================

def test_the_inventory_reports_the_type_policy_and_schedule(engine, ready,
                                                            store):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    inventory = engine.check_status(store.load(ready["job_id"]))["destination"]

    mirror = inventory["volumes"][0]["snapmirror"]
    assert mirror["type"] == CLONE_TYPE
    assert mirror["policy"] == CLONE_POLICY
    assert mirror["schedule"] == CLONE_SCHEDULE
