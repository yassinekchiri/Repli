"""Cutting client access to the source: the most sensitive action there is.

It makes shares disappear for real users, so most of what follows is about
what it refuses to do. Nothing here deletes data — the source qtrees stay in
place, renamed and unreachable, and removing them is a separate decision.
"""

import pytest

from netapp_migration.core.naming import MIGRATED_MARK, migrated_qtree_name
from netapp_migration.models import PreflightFailed

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def migrated(engine, client, params, store):
    """A job whose q_fin and q_hr really have been cloned and promoted."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"))
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    store.save(job)
    client.calls.clear()
    return job


def source_qtrees(client, params):
    return client.qtrees[(params.source_cluster, params.source_vserver,
                          params.volume)]


# =============================================================================
# One, several, or all
# =============================================================================

def test_several_qtrees_in_one_call(engine, migrated, client, params):
    result = engine.cleanup("q_fin,q_hr", job=migrated)

    assert result["qtrees"] == ["q_fin", "q_hr"]
    remaining = source_qtrees(client, params)
    assert "q_fin" not in remaining and "q_hr" not in remaining
    assert "q_ops" in remaining, "an untouched qtree stays as it was"


def test_a_single_qtree_still_works(engine, migrated, client, params):
    engine.cleanup("q_fin", job=migrated)

    assert "q_fin" not in source_qtrees(client, params)
    assert "q_hr" in source_qtrees(client, params)


def test_all_is_refused_for_a_qtree_that_was_never_migrated(engine, migrated):
    """'all' includes q_ops, which this job never cloned."""
    with pytest.raises(PreflightFailed) as raised:
        engine.cleanup("all", job=migrated)

    codes = [c["code"] for c in raised.value.report.to_dict()["checks"]
             if not c["passed"]]
    assert "CLEANUP_QTREE_NOT_MIGRATED" in codes


def test_all_works_once_every_qtree_has_been_migrated(engine, client, params,
                                                      store):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("all", job=job, volume_map=vmap("q_fin", "q_hr", "q_ops"))
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    store.save(job)

    result = engine.cleanup("all", job=job)

    assert sorted(result["qtrees"]) == ["q_fin", "q_hr", "q_ops"]


# =============================================================================
# What it does to each qtree
# =============================================================================

def test_the_export_policy_is_created_when_missing(engine, migrated, client,
                                                   params):
    """With no rules: an empty policy denies every client."""
    assert not client.export_policy_exists(params.source_cluster,
                                           params.source_vserver,
                                           params.noaccess_policy)

    result = engine.cleanup("q_fin", job=migrated)

    assert result["export_policy_created"] is True
    assert client.export_policy_exists(params.source_cluster,
                                       params.source_vserver,
                                       params.noaccess_policy)
    assert any(c.startswith("create_export_policy") for c in client.calls)


def test_an_existing_policy_is_reused_not_recreated(engine, migrated, client,
                                                    params):
    client.export_policies[(params.source_cluster,
                            params.source_vserver)] = {params.noaccess_policy}

    result = engine.cleanup("q_fin", job=migrated)

    assert result["export_policy_created"] is False
    assert not [c for c in client.calls
                if c.startswith("create_export_policy")]


def test_the_policy_is_applied_to_every_qtree(engine, migrated, client):
    engine.cleanup("q_fin,q_hr", job=migrated)

    applied = [c for c in client.calls if c.startswith("export_policy")]
    assert len(applied) == 2
    assert all("ep_noaccess" in c for c in applied)


def test_cifs_shares_are_deleted(engine, migrated, client):
    result = engine.cleanup("q_fin", job=migrated)

    assert result["results"][0]["deleted_shares"] == ["fin_share"]
    assert "delete_share fin_share" in client.calls


def test_a_qtree_without_a_share_is_not_a_problem(engine, migrated, client,
                                                  params):
    client.shares[(params.source_cluster, params.source_vserver)].pop(
        "hr_share")

    result = engine.cleanup("q_hr", job=migrated)

    assert result["results"][0]["deleted_shares"] == []
    assert result["results"][0]["renamed_to"], "the rest still happened"


def test_the_rename_says_where_the_data_went(engine, migrated, client, params):
    result = engine.cleanup("q_fin", job=migrated)

    new_name = result["results"][0]["renamed_to"]
    assert MIGRATED_MARK in new_name
    assert migrated["job_id"].split("_")[-1][:8] in new_name, "which job"
    assert "vol_q_fin" in new_name, "which volume it went to"
    assert new_name in source_qtrees(client, params)


def test_a_renamed_qtree_appears_in_the_new_name(engine, client, params, store):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin", job=job, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    store.save(job)

    result = engine.cleanup("q_fin", job=job)

    assert result["results"][0]["renamed_to"].endswith("__finance")


def test_no_data_is_deleted(engine, migrated, client, params):
    """The qtree stays: renamed and unreachable, not removed."""
    before = len(source_qtrees(client, params))

    engine.cleanup("q_fin,q_hr", job=migrated)

    assert len(source_qtrees(client, params)) == before
    assert not [c for c in client.calls if c.startswith("delete_qtree")]


def test_the_job_records_what_was_cut(engine, migrated, store):
    engine.cleanup("q_fin", job=migrated)

    job = store.load(migrated["job_id"])
    assert "q_fin" in job["cleaned_up"]
    assert job["cleaned_up_at"]


# =============================================================================
# What it refuses — nothing is touched in any of these
# =============================================================================

def _refused(engine, client, **kwargs):
    with pytest.raises(PreflightFailed) as raised:
        engine.cleanup(**kwargs)
    assert not [c for c in client.calls
                if c.startswith(("export_policy", "delete_share",
                                 "rename_qtree", "create_export_policy"))], \
        "a refusal must change nothing at all"
    return [c["code"] for c in raised.value.report.to_dict()["checks"]
            if not c["passed"]]


def test_an_empty_qtree_list_is_refused(engine, migrated, client):
    assert "CLEANUP_QTREE_MISSING" in _refused(engine, client, qtrees_arg="",
                                               job=migrated)


def test_a_qtree_that_does_not_exist_is_refused(engine, migrated, client):
    assert "CLEANUP_QTREE_NOT_FOUND" in _refused(
        engine, client, qtrees_arg="q_absent", job=migrated)


def test_an_unmigrated_qtree_is_refused(engine, migrated, client):
    """Cutting a source with no copy would leave the client with nothing."""
    assert "CLEANUP_QTREE_NOT_MIGRATED" in _refused(
        engine, client, qtrees_arg="q_ops", job=migrated)


def test_an_incomplete_migration_is_refused(engine, client, params, store):
    job = store.create(params, "full")          # status: started
    cascade_ready(client, params)

    assert "CLEANUP_MIGRATION_INCOMPLETE" in _refused(
        engine, client, qtrees_arg="q_fin", job=job)


def test_a_missing_target_volume_is_refused(engine, migrated, client, params):
    """The copy has to be there when the source is cut."""
    del client.volumes[(params.dest_cluster, params.dest_vserver, "vol_q_fin")]

    assert "CLEANUP_TARGET_MISSING" in _refused(
        engine, client, qtrees_arg="q_fin", job=migrated)


def test_one_bad_qtree_refuses_the_whole_run(engine, migrated, client):
    """q_fin is fine, q_ops is not: neither is touched."""
    codes = _refused(engine, client, qtrees_arg="q_fin,q_ops", job=migrated)
    assert "CLEANUP_QTREE_NOT_MIGRATED" in codes


def test_cleaning_up_twice_is_refused(engine, migrated, client, params):
    engine.cleanup("q_fin", job=migrated)
    client.calls.clear()
    cleaned = [q for q in source_qtrees(client, params) if MIGRATED_MARK in q]

    assert "CLEANUP_ALREADY_DONE" in _refused(
        engine, client, qtrees_arg=cleaned[0], job=migrated)


# =============================================================================
# Announced in advance
# =============================================================================

def test_the_preflight_lists_the_shares_before_deleting_them(engine, migrated):
    report = engine.checker.for_cleanup(migrated, ["q_fin"])

    preview = [c for c in report.to_dict()["checks"]
               if c["code"] == "CLEANUP_SHARES_PREVIEW"]
    assert preview and "fin_share" in preview[0]["detail"]
    assert preview[0]["severity"] == "warning", "informational, not blocking"


def test_the_preflight_announces_the_new_name(engine, migrated):
    report = engine.checker.for_cleanup(migrated, ["q_fin"])

    check = [c for c in report.to_dict()["checks"]
             if c["code"] == "CLEANUP_NEW_NAME_TAKEN"]
    assert check and MIGRATED_MARK in check[0]["detail"]


def test_the_preflight_warns_the_policy_will_be_created(engine, migrated):
    report = engine.checker.for_cleanup(migrated, ["q_fin"])

    check = [c for c in report.to_dict()["checks"]
             if c["code"] == "EXPORT_POLICY_ABSENT"]
    assert check and "no rule" in check[0]["detail"]


# =============================================================================
# Naming
# =============================================================================

def test_the_name_stays_within_ontap_s_limit():
    name = migrated_qtree_name("q_" + "x" * 60, "20260820_152136_2da725",
                               "a_very_long_destination_volume_name",
                               "renamed_to_something_long")
    assert len(name) <= 64
    assert MIGRATED_MARK in name, "the marker survives the trimming"
    assert "2da725" in name, "so does the job reference"


# =============================================================================
# Through the API
# =============================================================================

def test_the_api_accepts_a_list(api):                         # noqa: F811
    from netapp_migration.models import MigrationParams
    from netapp_migration.core.engine import MigrationEngine
    from test_api import CREATE_BODY
    import logging

    http, store, fake, _tokens = api
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)
    MigrationEngine(fake, params, store, logging.getLogger("t")).clone(
        "q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"))
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    store.save(job)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/cleanup",
                         json={"qtrees": ["q_fin", "q_hr"]})

    assert response.status_code == 200, response.text
    assert response.json()["result"]["qtrees"] == ["q_fin", "q_hr"]


def test_the_api_still_accepts_the_singular_field(api):        # noqa: F811
    from netapp_migration.models import MigrationParams
    from netapp_migration.core.engine import MigrationEngine
    from test_api import CREATE_BODY
    import logging

    http, store, fake, _tokens = api
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)
    MigrationEngine(fake, params, store, logging.getLogger("t")).clone(
        "q_fin", job=job, volume_map=vmap("q_fin"))
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    store.save(job)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/cleanup",
                         json={"qtree": "q_fin"})

    assert response.status_code == 200, response.text
    assert response.json()["result"]["qtrees"] == ["q_fin"]


def test_the_api_refuses_an_unmigrated_qtree(api):             # noqa: F811
    from netapp_migration.models import MigrationParams
    from test_api import CREATE_BODY

    http, store, fake, _tokens = api
    params = MigrationParams.from_dict(CREATE_BODY)
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(fake, params)

    response = http.post(f"/api/v1/migrations/{job['job_id']}/cleanup",
                         json={"qtrees": "q_fin"})

    assert response.status_code == 422, response.text
    codes = [c["code"] for c in response.json()["detail"]["failed_checks"]]
    assert "CLEANUP_QTREE_NOT_MIGRATED" in codes
    assert not [c for c in fake.calls if c.startswith("rename_qtree")]
