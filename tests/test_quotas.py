"""The quota rules a clone volume gets.

A clone holds exactly one client's qtree, so its quota story is two rules:
the volume-level tree at 0 — nothing may be written outside the qtree the
volume was created for — and the qtree rule carrying the ceiling the qtree
had on the source.

Quota rules belong to the quota policy, an SVM object. SnapMirror does not
replicate them, so they are created on PROD *and* DR: a rule that exists
only on PROD is simply absent the day DR is activated.
"""

import pytest

from netapp_migration.core.quotas import (QUOTA_POLICY, VOLUME_TREE_LIMIT,
                                          describe_limit, destination_rules,
                                          source_rule_for)
from netapp_migration.models import QuotaRule

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def rules_on(client, cluster, svm, volume):
    return client.quota_rules.get((cluster, svm, volume), [])


def source_rules(client, params):
    return client.quota_rules[(params.source_cluster, params.source_vserver,
                               params.volume)]


# =============================================================================
# The two rules
# =============================================================================

def test_two_rules_are_created_per_volume(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    rules = rules_on(client, params.dest_cluster, params.dest_vserver,
                     "vol_q_fin")
    assert len(rules) == 2


def test_the_volume_rule_targets_the_empty_qtree_at_zero(engine, ready,
                                                         client, params):
    """Nothing may be written outside the qtree the volume came for."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    volume_rule = rules_on(client, params.dest_cluster, params.dest_vserver,
                           "vol_q_fin")[0]
    assert volume_rule.type == "tree"
    assert volume_rule.qtree == ""
    assert volume_rule.space_hard_limit == 0
    assert volume_rule.space_soft_limit == 0


def test_the_qtree_rule_copies_the_source_limits(engine, ready, client,
                                                 params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    qtree_rule = rules_on(client, params.dest_cluster, params.dest_vserver,
                          "vol_q_fin")[1]
    assert qtree_rule.qtree == "q_fin"
    assert qtree_rule.space_hard_limit == 100 * 1024 ** 3
    assert qtree_rule.space_soft_limit == 80 * 1024 ** 3


def test_each_qtree_gets_its_own_source_limits(engine, ready, client, params):
    """Not one client's ceiling applied to another's volume."""
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    fin = rules_on(client, params.dest_cluster, params.dest_vserver,
                   "vol_q_fin")[1]
    hr = rules_on(client, params.dest_cluster, params.dest_vserver,
                  "vol_q_hr")[1]
    assert fin.space_hard_limit == 100 * 1024 ** 3
    assert hr.space_hard_limit == 50 * 1024 ** 3


def test_the_qtree_rule_follows_the_renamed_qtree(engine, ready, client,
                                                  params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    qtree_rule = rules_on(client, params.dest_cluster, params.dest_vserver,
                          "vol_q_fin")[1]
    assert qtree_rule.qtree == "finance"


# =============================================================================
# Both sides
# =============================================================================

def test_the_rules_are_created_on_dr_as_well(engine, ready, client, params):
    """SnapMirror does not replicate quota rules: a rule only on PROD is
    absent the day DR is activated."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert len(rules_on(client, params.dr_cluster, params.dr_vserver,
                        "vol_q_fin")) == 2


def test_dr_gets_the_same_limits(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    prod = rules_on(client, params.dest_cluster, params.dest_vserver,
                    "vol_q_fin")
    dr = rules_on(client, params.dr_cluster, params.dr_vserver, "vol_q_fin")
    assert [(r.qtree, r.space_hard_limit) for r in prod] == \
           [(r.qtree, r.space_hard_limit) for r in dr]


def test_the_test_environment_gets_them_too(engine, ready, client, params):
    """A test that ignored quotas would let the client validate a volume
    with limits production will not have."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert len(rules_on(client, params.dest_cluster, params.dest_vserver,
                        "vol_q_fin")) == 2


# =============================================================================
# Unlimited is not zero
# =============================================================================

def test_a_qtree_with_no_source_quota_gets_no_limit(engine, ready, client,
                                                    params):
    """None means unlimited. Sending 0 would mean the exact opposite, and
    the client could write nothing at all."""
    engine.clone("q_ops", job=ready, volume_map=vmap("q_ops"))

    qtree_rule = rules_on(client, params.dest_cluster, params.dest_vserver,
                          "vol_q_ops")[1]
    assert qtree_rule.space_hard_limit is None
    assert qtree_rule.space_soft_limit is None


def test_the_volume_rule_is_still_created_without_a_source_quota(engine, ready,
                                                                 client,
                                                                 params):
    """It is the rule that stops the volume being filled from outside the
    qtree, so it does not depend on the source having one."""
    engine.clone("q_ops", job=ready, volume_map=vmap("q_ops"))

    volume_rule = rules_on(client, params.dest_cluster, params.dest_vserver,
                           "vol_q_ops")[0]
    assert volume_rule.space_hard_limit == 0


def test_unlimited_and_zero_read_differently():
    assert describe_limit(None) == "unlimited"
    assert describe_limit(0) == "0 B"


# =============================================================================
# Which source rule is used
# =============================================================================

def test_only_tree_rules_are_read_from_the_source():
    """A user or group rule limits a person across the volume, which is not
    a property of the qtree being migrated."""
    rules = [QuotaRule(type="user", qtree="q_fin", target="DOMAIN\\jdoe",
                       space_hard_limit=1),
             QuotaRule(type="tree", qtree="q_fin", space_hard_limit=999)]

    assert source_rule_for(rules, "q_fin").space_hard_limit == 999


def test_a_qtree_with_no_tree_rule_reports_none():
    assert source_rule_for([QuotaRule(type="tree", qtree="other")],
                           "q_fin") is None


def test_the_rules_are_built_in_a_fixed_order():
    """The volume rule first: it is the one that must exist even when the
    source qtree had no quota at all."""
    volume_rule, qtree_rule = destination_rules(None, "finance")

    assert volume_rule.qtree == ""
    assert volume_rule.space_hard_limit == VOLUME_TREE_LIMIT
    assert qtree_rule.qtree == "finance"


# =============================================================================
# What the run reports
# =============================================================================

def test_the_result_reports_what_was_created(engine, ready):
    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    quota = result["quotas"]["q_fin"]
    assert quota["policy"] == QUOTA_POLICY
    assert quota["had_source_quota"] is True
    assert quota["source_limit"] == 100 * 1024 ** 3
    assert sorted(quota["created_on"]) == ["DR", "PROD"]
    assert [r["qtree"] for r in quota["rules"]] == ["", "q_fin"]


def test_the_result_survives_json_encoding(engine, ready):
    import json

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert json.loads(json.dumps(result["quotas"])) == result["quotas"]


# =============================================================================
# The pre-flight says it in advance
# =============================================================================

def test_the_preflight_announces_both_rules(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    plan = next(c for c in report.to_dict()["checks"]
                if c["code"] == "QUOTA_PLAN")
    assert 'volume (qtree "")' in plan["detail"]
    assert "0 B" in plan["detail"]
    assert "100.0 GiB" in plan["detail"]


def test_the_preflight_warns_when_the_source_has_no_quota(engine, ready):
    report = engine.checker.for_clone(ready, ["q_ops"],
                                      volume_map=vmap("q_ops"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "QUOTA_NO_SOURCE_LIMIT")
    assert not check["passed"] and check["severity"] == "warning"
    assert report.ok, "no quota on the source must not refuse the clone"


def test_the_preflight_passes_when_the_source_has_one(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "QUOTA_NO_SOURCE_LIMIT")
    assert check["passed"]


def test_the_preflight_uses_the_renamed_qtree(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"),
                                      qtree_map={"q_fin": "finance"})

    plan = next(c for c in report.to_dict()["checks"]
                if c["code"] == "QUOTA_PLAN")
    assert "finance" in plan["detail"]


def test_unreadable_source_quotas_are_a_warning_not_a_refusal(engine, ready,
                                                              client):
    from netapp_migration.models import OntapError

    def refuse(cluster, svm, volume):
        raise OntapError(cluster, "quota rule show", "not authorized")

    client.list_quota_rules = refuse

    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    codes = {c["code"] for c in report.to_dict()["checks"]}
    assert "QUOTA_RULES_UNREADABLE" in codes
    assert report.ok


def test_the_preflight_predicts_what_the_engine_creates(engine, ready, client,
                                                        params):
    """The report is worthless if it announces limits other than the ones
    that get written."""
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))
    predicted = next(c for c in report.to_dict()["checks"]
                     if c["code"] == "QUOTA_PLAN")["detail"]

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    for rule in rules_on(client, params.dest_cluster, params.dest_vserver,
                         "vol_q_fin"):
        assert describe_limit(rule.space_hard_limit) in predicted
