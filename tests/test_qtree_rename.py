"""Renaming a qtree inside its clone volume.

The client supplies, per qtree, the volume to create AND the name the qtree
takes inside it. The rename is optional: no name, no rename.
"""

import pytest

from netapp_migration.models import PreflightFailed
from netapp_migration.security import csvio

from conftest import cascade_ready, vmap


def ready_job(store, params, client):
    """A job whose cascade is complete — the state clone/test require."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


# =============================================================================
# The CSV the client hands over
# =============================================================================

def test_third_column_is_optional_per_row():
    parsed = csvio.parse_clone_map_csv(
        "qtree,volume,new_qtree\n"
        "q_fin,vol_finance_prod,finance\n"
        "q_hr,vol_rh_prod,\n")

    volumes, renames = csvio.split_clone_map(parsed)
    assert volumes == {"q_fin": "vol_finance_prod", "q_hr": "vol_rh_prod"}
    assert renames == {"q_fin": "finance"}, "an empty cell means: keep the name"


def test_a_two_column_csv_still_works():
    """Files written for the previous version must keep working."""
    volumes, renames = csvio.split_clone_map(
        csvio.parse_clone_map_csv("qtree,volume\nq_fin,vol_finance_prod\n"))

    assert volumes == {"q_fin": "vol_finance_prod"}
    assert renames == {}


@pytest.mark.parametrize("name, expected", [
    ("with/slash", "'/'"),
    ('with"quote', "'\"'"),
    ("x" * 65, "65 characters"),
])
def test_illegal_names_are_refused_with_the_reason(name, expected):
    with pytest.raises(ValueError) as raised:
        csvio.parse_clone_map_csv(f"qtree,volume,new_qtree\nq,v,{name}\n")
    assert expected in str(raised.value)


# =============================================================================
# What the engine does with it
# =============================================================================

def test_clone_renames_on_prod_only(engine, client, params, store):
    """DR is a mirror destination: read-only, so it must not be touched."""
    job = ready_job(store, params, client)

    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    renames = [c for c in client.calls if c.startswith("rename_qtree")]
    assert len(renames) == 1, f"expected exactly one rename, got {renames}"
    assert params.dest_cluster in renames[0]
    assert params.dr_cluster not in renames[0]
    assert "q_fin -> finance" in renames[0]


def test_the_rename_precedes_the_clone_mirror(engine, client, params, store):
    """Renaming first means the first resync carries the new name to DR."""
    job = ready_job(store, params, client)

    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    order = [i for i, c in enumerate(client.calls)
             if c.startswith(("rename_qtree", "snapmirror_create"))]
    kinds = [client.calls[i].split()[0] for i in order]
    assert kinds.index("rename_qtree") < kinds.index("snapmirror_create")


def test_no_rename_requested_means_no_rename_call(engine, client, params, store):
    job = ready_job(store, params, client)

    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"))

    assert not [c for c in client.calls if c.startswith("rename_qtree")]


def test_renaming_to_the_same_name_is_not_a_rename(engine, client, params,
                                                   store):
    job = ready_job(store, params, client)

    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "q_fin"})

    assert not [c for c in client.calls if c.startswith("rename_qtree")]


def test_the_job_records_the_new_names(engine, client, params, store):
    job = ready_job(store, params, client)

    result = engine.clone("q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"),
                          qtree_map={"q_fin": "finance"})

    assert result["qtree_map"] == {"q_fin": "finance"}
    assert store.load(job["job_id"])["qtree_map"] == {"q_fin": "finance"}


def test_test_builds_the_environment_with_the_final_names(engine, client,
                                                          params, store):
    """The client validates the layout production will have, names included."""
    job = ready_job(store, params, client)

    engine.test("q_fin", job=job, volume_map=vmap("q_fin"),
                qtree_map={"q_fin": "finance"})

    assert any("q_fin -> finance" in c for c in client.calls)
    assert store.load(job["job_id"])["qtree_map"] == {"q_fin": "finance"}


def test_a_promotion_reuses_the_names_recorded_by_test(engine, client, params,
                                                       store):
    job = ready_job(store, params, client)
    engine.test("q_fin", job=job, volume_map=vmap("q_fin"),
                qtree_map={"q_fin": "finance"})

    before = len([c for c in client.calls if c.startswith("rename_qtree")])
    engine.clone("q_fin", job=job)          # promotion: nothing is rebuilt

    after = len([c for c in client.calls if c.startswith("rename_qtree")])
    assert after == before, "a promotion renames nothing: test already did"


# =============================================================================
# Refused before anything is created
# =============================================================================

def test_a_name_already_used_in_the_source_is_refused(engine, client, params,
                                                      store):
    """The clone inherits every qtree of the source volume."""
    job = ready_job(store, params, client)
    source = (params.source_cluster, params.source_vserver, params.volume)
    client.qtrees[source] = ["q_fin", "finance"]     # the name is already taken

    with pytest.raises(PreflightFailed) as raised:
        engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                     qtree_map={"q_fin": "finance"})

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "QTREE_NAME_TAKEN" in codes
    assert not [c for c in client.calls if c.startswith("create_clone")], \
        "nothing may be created when the pre-flight refuses"


def test_two_qtrees_cannot_take_the_same_new_name(engine, client, params,
                                                  store):
    job = ready_job(store, params, client)

    with pytest.raises(PreflightFailed) as raised:
        engine.clone("q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"),
                     qtree_map={"q_fin": "data", "q_hr": "data"})

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "QTREE_NAME_DUPLICATE" in codes


def test_an_illegal_name_is_refused_by_the_preflight(engine, client, params,
                                                     store):
    job = ready_job(store, params, client)

    with pytest.raises(PreflightFailed) as raised:
        engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                     qtree_map={"q_fin": "bad/name"})

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "QTREE_NAME_ILLEGAL" in codes
