"""Pre-flight checks: every action must refuse an infeasible request BEFORE
mutating anything, and say precisely why.
"""

import pytest

from netapp_migration.core.preflight import PreflightChecker
from netapp_migration.models import PreflightFailed

from conftest import cascade_ready, vmap


def checker(client, params, logger, simulated=False):
    return PreflightChecker(client, params, logger, simulated=simulated)


def codes(report):
    return {c.code for c in report.checks if not c.passed}


def failing_codes(report):
    return {c.code for c in report.failures}


# =============================================================================
# create
# =============================================================================

def test_create_passes_on_a_healthy_estate(client, params, logger):
    report = checker(client, params, logger).for_create()
    assert report.ok, report.summary()
    assert failing_codes(report) == set()


def test_create_detects_missing_svm(client, params, logger):
    del client.svms[("DRC", "svm_dr")]
    report = checker(client, params, logger).for_create()
    assert not report.ok
    assert "SVM_MISSING" in failing_codes(report)


def test_create_detects_missing_source_volume(client, params, logger):
    client.volumes.clear()
    report = checker(client, params, logger).for_create()
    assert "SOURCE_VOLUME_MISSING" in failing_codes(report)


def test_create_detects_existing_dp_volume(client, params, logger):
    """A leftover DP volume must be reported, not silently collided with."""
    client.add_volume("PRD", "svm_dest", params.volume)
    report = checker(client, params, logger).for_create()
    assert "VOLUME_ALREADY_EXISTS" in failing_codes(report)
    hint = next(c.hint for c in report.failures
                if c.code == "VOLUME_ALREADY_EXISTS")
    assert "retry" in hint


def test_create_detects_missing_aggregate(client, params, logger):
    client.aggregates["PRD"].pop("aggr_prd")
    report = checker(client, params, logger).for_create()
    assert "AGGREGATE_MISSING" in failing_codes(report)


def test_create_detects_insufficient_space(client, params, logger):
    client.aggregates["DRC"]["aggr_dr"] = 1024        # 1 KiB for a 10 GiB volume
    report = checker(client, params, logger).for_create()
    assert "AGGREGATE_SPACE" in failing_codes(report)


def test_create_detects_missing_cluster_peering(client, params, logger):
    client.cluster_peers["DRC"] = []
    report = checker(client, params, logger).for_create()
    assert "CLUSTER_PEER_MISSING" in failing_codes(report)


def test_create_detects_missing_svm_peering(client, params, logger):
    client.svm_peers["PRD"] = []
    report = checker(client, params, logger).for_create()
    assert "SVM_PEER_MISSING" in failing_codes(report)
    hint = next(c.hint for c in report.failures if c.code == "SVM_PEER_MISSING")
    assert "vserver peer create" in hint


def test_create_detects_invisible_schedule(client, params, logger):
    """The exact failure seen on the first real run: schedule not visible."""
    client.schedules["DRC"] = []
    report = checker(client, params, logger).for_create()
    assert "SCHEDULE_MISSING" in failing_codes(report)
    check = next(c for c in report.failures if c.code == "SCHEDULE_MISSING")
    assert "job schedule cron create" in check.hint


def test_create_detects_invisible_policy(client, params, logger):
    client.policies["PIV"] = []
    report = checker(client, params, logger).for_create()
    assert "SNAPMIRROR_POLICY_MISSING" in failing_codes(report)


def test_create_detects_existing_relationship(client, params, logger):
    client.add_relationship(params.path(params.pivot_vserver, params.volume))
    report = checker(client, params, logger).for_create()
    assert "SNAPMIRROR_ALREADY_EXISTS" in failing_codes(report)


def test_create_rejects_duplicate_clusters(client, params, logger):
    params.dr_cluster = params.dest_cluster
    report = checker(client, params, logger).for_create()
    assert "TOPOLOGY_DUPLICATE_CLUSTER" in failing_codes(report)


def test_create_rejects_empty_parameters(client, params, logger):
    params.dr_cluster = "   "
    report = checker(client, params, logger).for_create()
    assert "PARAM_MISSING" in failing_codes(report)


def test_simulated_report_never_blocks(client, params, logger):
    client.volumes.clear()
    report = checker(client, params, logger, simulated=True).for_create()
    assert report.failures, "the failure is still reported"
    assert report.ok, "a simulated report must not block"


# =============================================================================
# resume
# =============================================================================

def test_resume_rejects_wrong_job_status(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_resume(job)   # status=started
    assert "JOB_STATUS_INVALID" in failing_codes(report)


def test_resume_requires_pivot_finished(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "pivot_initialized")
    client.add_relationship(job["pivot_dest_path"], state="snapmirrored",
                            transfer_state="transferring")
    report = checker(client, params, logger).for_resume(job)
    assert "SNAPMIRROR_NOT_READY" in failing_codes(report)


def test_resume_refuses_second_initialize(client, params, logger, store):
    """P1-2: PROD already initialized must not be initialized again."""
    job = store.create(params)
    store.set_status(job, "pivot_initialized")
    cascade_ready(client, params)
    report = checker(client, params, logger).for_resume(job)
    assert "SNAPMIRROR_ALREADY_INITIALIZED" in failing_codes(report)


# =============================================================================
# test / clone
# =============================================================================

def test_test_requires_a_complete_cascade(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_test(
        job, ["q_fin"], volume_map=vmap("q_fin"))
    assert "CASCADE_NOT_READY" in failing_codes(report)


def test_test_rejects_unknown_qtree(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    report = checker(client, params, logger).for_test(
        job, ["q_missing"], volume_map=vmap("q_missing"))
    assert "QTREES_MISSING" in failing_codes(report)


def test_test_rejects_duplicate_qtrees(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    report = checker(client, params, logger).for_test(
        job, ["q_fin", "q_fin"], volume_map=vmap("q_fin"))
    assert "QTREES_DUPLICATED" in failing_codes(report)


def test_test_refuses_when_environment_exists(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    job.update({"test_env": True, "volume_map": {"q_fin": "vol_fin"},
                "clone_volumes": ["vol_fin"]})
    report = checker(client, params, logger).for_test(
        job, ["q_fin"], volume_map=vmap("q_fin"))
    assert "TEST_ENV_ALREADY_EXISTS" in failing_codes(report)


def test_test_detects_broken_cascade(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    client.add_relationship(job["dr_dest_path"], state="broken_off",
                            transfer_state="failed")
    report = checker(client, params, logger).for_test(
        job, ["q_fin"], volume_map=vmap("q_fin"))
    assert "SNAPMIRROR_NOT_READY" in failing_codes(report)


def test_clone_promotion_rejects_partial_qtree_set(client, params, logger,
                                                   store):
    """P1-3: promoting a subset would orphan the other clones."""
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    mapping = {"q_fin": "vol_fin", "q_hr": "vol_rh"}
    job.update({"test_env": True, "volume_map": mapping,
                "test_qtrees": ["q_fin", "q_hr"],
                "clone_volumes": list(mapping.values())})
    for qtree, volume in mapping.items():
        for cluster, svm in (("PRD", "svm_dest"), ("DRC", "svm_dr")):
            client.add_volume(cluster, svm, volume)
        client.add_relationship(f"svm_dr:{volume}")
    report = checker(client, params, logger).for_clone(job, ["q_fin"])
    assert "PROMOTION_QTREE_MISMATCH" in failing_codes(report)


def test_clone_promotion_accepts_exact_qtree_set(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    mapping = {"q_fin": "vol_fin", "q_hr": "vol_rh"}
    job.update({"test_env": True, "volume_map": mapping,
                "test_qtrees": ["q_fin", "q_hr"],
                "clone_volumes": list(mapping.values())})
    for qtree, volume in mapping.items():
        for cluster, svm in (("PRD", "svm_dest"), ("DRC", "svm_dr")):
            client.add_volume(cluster, svm, volume)
        client.add_relationship(f"svm_dr:{volume}")
    report = checker(client, params, logger).for_clone(job, ["q_hr", "q_fin"])
    assert report.ok, report.summary()


# =============================================================================
# acl
# =============================================================================

def test_acl_refuses_root_path(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_acl(job, "/", ["DOM\\g"],
                                                    "modify")
    assert "ACL_PATH_IS_ROOT" in failing_codes(report)


def test_acl_refuses_traversal(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_acl(
        job, "/v_q_fin_abc/../../etc", ["DOM\\g"], "modify")
    assert "ACL_PATH_TRAVERSAL" in failing_codes(report)


def test_acl_refuses_path_outside_the_job(client, params, logger, store):
    job = store.create(params)
    job["clone_volumes"] = ["v_q_fin_abc123"]
    report = checker(client, params, logger).for_acl(
        job, "/some_other_volume/data", ["DOM\\g"], "modify")
    assert "ACL_PATH_OUTSIDE_JOB" in failing_codes(report)


def test_acl_accepts_a_clone_subdirectory(client, params, logger, store):
    job = store.create(params)
    uid = "abc123"
    job["clone_volumes"] = [f"v_q_fin_{uid}"]
    client.add_volume("PRD", "svm_dest", f"v_q_fin_{uid}")
    report = checker(client, params, logger).for_acl(
        job, f"/v_q_fin_{uid}/projects", ["DOM\\grp_rw"], "modify")
    assert report.ok, report.summary()


def test_acl_rejects_non_ntfs_volume(client, params, logger, store):
    job = store.create(params)
    uid = "abc123"
    job["clone_volumes"] = [f"v_q_fin_{uid}"]
    client.add_volume("PRD", "svm_dest", f"v_q_fin_{uid}",
                      security_style="unix")
    report = checker(client, params, logger).for_acl(
        job, f"/v_q_fin_{uid}", ["DOM\\grp"], "modify")
    assert "ACL_SECURITY_STYLE" in failing_codes(report)


def test_acl_rejects_missing_path(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_acl(job, "", ["DOM\\g"],
                                                    "modify")
    assert "ACL_PATH_MISSING" in failing_codes(report)


def test_acl_requires_groups(client, params, logger, store):
    job = store.create(params)
    uid = "abc123"
    job["clone_volumes"] = [f"v_q_fin_{uid}"]
    client.add_volume("PRD", "svm_dest", f"v_q_fin_{uid}")
    report = checker(client, params, logger).for_acl(job, f"/v_q_fin_{uid}",
                                                     [], "modify")
    assert "ACL_GROUPS_EMPTY" in failing_codes(report)


# =============================================================================
# cleanup
# =============================================================================

def test_cleanup_refuses_empty_qtree(client, params, logger, store):
    """P0-6: an empty qtree would match every CIFS share of the SVM."""
    job = store.create(params)
    report = checker(client, params, logger).for_cleanup(job, "")
    assert "CLEANUP_QTREE_MISSING" in failing_codes(report)


def test_cleanup_refuses_unknown_qtree(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_cleanup(job, "q_nope")
    assert "CLEANUP_QTREE_NOT_FOUND" in failing_codes(report)


def test_cleanup_refuses_incomplete_migration(client, params, logger, store):
    job = store.create(params)
    report = checker(client, params, logger).for_cleanup(job, "q_fin")
    assert "CLEANUP_MIGRATION_INCOMPLETE" in failing_codes(report)


def test_cleanup_previews_the_exact_shares(client, params, logger, store):
    job = store.create(params)
    store.set_status(job, "completed")
    job["clone_promoted_at"] = "2026-08-10T10:00:00"
    report = checker(client, params, logger).for_cleanup(job, "q_fin")
    preview = next(c for c in report.checks
                   if c.code == "CLEANUP_SHARES_PREVIEW")
    assert "fin_share" in preview.detail
    assert "hr_share" not in preview.detail, "must not match a sibling qtree"


def test_qtrees_all_is_expanded(client, params, logger, store):
    """The 'all' keyword must resolve to the source volume's qtrees."""
    job = store.create(params)
    store.set_status(job, "completed")
    cascade_ready(client, params)
    report = checker(client, params, logger).for_test(
        job, "all", volume_map=vmap("q_fin", "q_hr", "q_ops"))
    assert report.ok, report.summary()
    check = next(c for c in report.checks if c.code == "QTREES_EMPTY")
    assert "3 qtree(s)" in check.detail
