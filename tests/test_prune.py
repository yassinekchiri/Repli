"""Each clone keeps only the qtree it was created for.

A FlexClone copies the whole parent volume, so the volume created for q_fin
also holds q_hr and q_ops — other clients' data inside this client's volume.
clone and test remove that surplus as part of their own run.
"""

import time

import pytest

from netapp_migration.models import OntapError

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def qtrees_of(client, params, volume):
    return client.qtrees.get((params.dest_cluster, params.dest_vserver,
                              volume), [])


def wait_for(fake, prefix, attempts=80):
    for _ in range(attempts):
        if [c for c in fake.calls if c.startswith(prefix)]:
            return
        time.sleep(0.05)
    raise AssertionError(f"no {prefix} call after waiting: {fake.calls}")


# =============================================================================
# The default behaviour
# =============================================================================

def test_a_clone_keeps_only_its_own_qtree(engine, ready, client, params):
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    assert qtrees_of(client, params, "vol_q_fin") == ["q_fin"]
    assert qtrees_of(client, params, "vol_q_hr") == ["q_hr"]


def test_test_environments_are_pruned_too(engine, ready, client, params):
    """The client must validate a volume holding their data and nothing else."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert qtrees_of(client, params, "vol_q_fin") == ["q_fin"]


def test_the_deletions_precede_the_clone_mirror(engine, ready, client):
    """So DR receives them on the first resync and never holds the surplus."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    kinds = [c.split()[0] for c in client.calls
             if c.startswith(("delete_qtree", "snapmirror_create"))]
    assert kinds.index("delete_qtree") < kinds.index("snapmirror_create")


def test_the_deletions_precede_the_volume_move(engine, ready, client):
    """So the move relocates only what is left."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    kinds = [c.split()[0] for c in client.calls
             if c.startswith(("delete_qtree", "volume_move"))]
    assert kinds.index("delete_qtree") < kinds.index("volume_move")


def test_deletions_happen_on_prod_only(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    deletes = [c for c in client.calls if c.startswith("delete_qtree")]
    assert deletes
    assert all(params.dest_cluster in c for c in deletes)
    assert not any(params.dr_cluster in c for c in deletes)


def test_the_source_volume_is_never_touched(engine, ready, client, params):
    before = list(client.qtrees[(params.source_cluster, params.source_vserver,
                                 params.volume)])

    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    assert client.qtrees[(params.source_cluster, params.source_vserver,
                          params.volume)] == before


def test_a_renamed_qtree_is_the_one_kept(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    assert qtrees_of(client, params, "vol_q_fin") == ["finance"]


def test_the_result_reports_what_was_removed(engine, ready):
    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert sorted(result["pruned"]["q_fin"]) == ["q_hr", "q_ops"]


# =============================================================================
# Opting out
# =============================================================================

def test_prune_false_keeps_everything(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"), prune=False)

    assert sorted(qtrees_of(client, params, "vol_q_fin")) == \
        ["q_fin", "q_hr", "q_ops"]
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_the_preflight_warns_when_pruning_is_off(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"), prune=False)

    codes = [c["code"] for c in report.to_dict()["checks"] if not c["passed"]]
    assert "PRUNE_DISABLED" in codes
    assert report.ok, "a warning must not block the action"


# =============================================================================
# Announced before it happens
# =============================================================================

def test_the_preflight_lists_the_deletions_in_advance(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    plan = [c for c in report.to_dict()["checks"] if c["code"] == "PRUNE_PLAN"]
    assert plan, "the operator must see the deletions before running"
    assert "q_hr" in plan[0]["detail"] and "q_ops" in plan[0]["detail"]
    assert "keeps 'q_fin'" in plan[0]["detail"]


def test_pruning_never_empties_a_volume(engine, ready, client, params):
    """Defence in depth: if what the volume came for is not in the clone,
    abort rather than delete everything else and leave it empty.

    Unreachable through clone today — the qtrees are checked and the rename
    applied first — which is exactly why it is tested at the helper.
    """
    client.qtrees[(params.dest_cluster, params.dest_vserver,
                   "vol_q_fin")] = ["q_hr", "q_ops"]

    with pytest.raises(OntapError) as raised:
        engine._prune_inherited_qtrees(["q_fin"], {"q_fin": "vol_q_fin"}, {})

    assert "refusing" in str(raised.value)
    assert sorted(qtrees_of(client, params, "vol_q_fin")) == ["q_hr", "q_ops"]


# =============================================================================
# Through the API
# =============================================================================

def _ready_via_api(api):                                      # noqa: F811
    from netapp_migration.models import MigrationParams
    from test_api import CREATE_BODY

    http, store, fake, tokens = api
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)
    return http, fake, job, params


def test_api_clone_prunes_by_default(api):                    # noqa: F811
    http, fake, job, params = _ready_via_api(api)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/clone",
                         json={"qtrees": "q_fin",
                               "volume_map": {"q_fin": "vol_fin"}})

    assert response.status_code == 202, response.text
    wait_for(fake, "delete_qtree")
    assert fake.qtrees[(params.dest_cluster, params.dest_vserver,
                        "vol_fin")] == ["q_fin"]


def test_api_prune_false_is_honoured(api):                    # noqa: F811
    http, fake, job, _params = _ready_via_api(api)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/clone",
                         json={"qtrees": "q_fin", "prune": False,
                               "volume_map": {"q_fin": "vol_fin"}})

    assert response.status_code == 202, response.text
    wait_for(fake, "volume_move")
    assert not [c for c in fake.calls if c.startswith("delete_qtree")]


def test_api_preflight_announces_the_deletions(api):          # noqa: F811
    http, fake, job, _params = _ready_via_api(api)

    report = http.post(f"/api/v1/migrations/{job['job_id']}/preflight/clone",
                       json={"qtrees": "q_fin",
                             "volume_map": {"q_fin": "vol_fin"}}).json()

    plan = [c for c in report["checks"] if c["code"] == "PRUNE_PLAN"]
    assert plan and "q_hr" in plan[0]["detail"]
    assert not [c for c in fake.calls if c.startswith("delete_qtree")]
