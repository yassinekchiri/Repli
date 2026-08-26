"""Carrying each qtree's NFS clients over to PROD and DR.

An export policy is an SVM object: it does not travel with the data. A
FlexClone inherits the policy NAME from its parent volume, and that name
means nothing on the destination SVM — so without this step the migrated
qtree points at a policy that is absent, or worse, belongs to somebody else.
"""

import pytest

from netapp_migration.core.naming import destination_export_policy
from netapp_migration.models import ExportRule, OntapError
from netapp_migration.transport.ssh import parse_export_rules

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def policies_on(client, cluster, svm):
    return client.export_policies.get((cluster, svm), {})


def source_policy(client, params, policy="ep_source"):
    return client.export_policies[(params.source_cluster,
                                   params.source_vserver)][policy]


# =============================================================================
# What gets created, and where
# =============================================================================

def test_the_policy_is_created_on_prod_and_on_dr(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    for cluster, svm in ((params.dest_cluster, params.dest_vserver),
                         (params.dr_cluster, params.dr_vserver)):
        assert "ep_q_fin" in policies_on(client, cluster, svm)


def test_dr_gets_the_policy_even_though_nothing_is_applied_there(
        engine, ready, client, params):
    """The DR clone is a read-only mirror destination: the assignment comes
    over the wire, but the policy object must already be there to receive it."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert "ep_q_fin" in policies_on(client, params.dr_cluster,
                                     params.dr_vserver)
    applied = [c for c in client.calls if c.startswith("export_policy ")]
    assert applied and all(params.dest_cluster in c for c in applied), \
        "nothing may be modified on the DR clone"
    assert not any(params.dr_cluster in c for c in applied)


def test_the_clients_are_the_ones_the_source_qtree_had(engine, ready, client,
                                                       params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["10.0.0.0/8", "@admins"]]
    assert carried[0].ro_rule == ["sys"]
    assert carried[0].protocols == ["nfs"]


def test_every_rule_is_carried_not_just_the_first(engine, ready, client,
                                                  params):
    source_policy(client, params).append(
        ExportRule(clients=["192.168.0.0/16"], ro_rule=["krb5"],
                   rw_rule=["never"], protocols=["nfs4"], index=2))

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert len(carried) == 2
    assert carried[1].clients == ["192.168.0.0/16"]
    assert carried[1].rw_rule == ["never"]


def test_the_policy_is_applied_to_the_qtree_in_the_prod_clone(engine, ready,
                                                              client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert client.get_qtree_export_policy(
        params.dest_cluster, params.dest_vserver, "vol_q_fin",
        "q_fin") == "ep_q_fin"


def test_each_qtree_gets_its_own_policy(engine, ready, client, params):
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    prod = policies_on(client, params.dest_cluster, params.dest_vserver)
    assert "ep_q_fin" in prod and "ep_q_hr" in prod


# =============================================================================
# Naming
# =============================================================================

def test_the_policy_follows_the_destination_name_not_the_source_one(
        engine, ready, client, params):
    """The client renamed q_fin to 'finance': that is the name on the cluster."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    prod = policies_on(client, params.dest_cluster, params.dest_vserver)
    assert "ep_finance" in prod
    assert "ep_q_fin" not in prod


def test_the_naming_rule_is_shared_with_the_preflight():
    assert destination_export_policy("finance") == "ep_finance"


# =============================================================================
# Ordering: it has to happen before the mirror
# =============================================================================

def test_the_policy_is_applied_before_the_clone_mirror(engine, ready, client):
    """Otherwise DR keeps the old assignment until the next update."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    applied = next(i for i, c in enumerate(client.calls)
                   if c.startswith("export_policy ") and "vol_q_fin/" in c)
    mirrored = next(i for i, c in enumerate(client.calls)
                    if c.startswith("snapmirror_create"))
    assert applied < mirrored


def test_the_policy_is_applied_after_the_rename(engine, ready, client):
    """It is applied to the qtree by its new name, so the rename comes first."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    renamed = next(i for i, c in enumerate(client.calls)
                   if c.startswith("rename_qtree"))
    applied = next(i for i, c in enumerate(client.calls)
                   if "=ep_finance" in c)
    assert renamed < applied


# =============================================================================
# Cases that are not failures
# =============================================================================

def test_a_source_policy_with_no_rule_gives_a_policy_with_no_rule(
        engine, ready, client, params):
    """A CIFS-only qtree. Denying every NFS client is the correct answer —
    it must just never happen quietly (see the pre-flight warning)."""
    client.qtree_policies[(params.source_cluster, params.source_vserver,
                           params.volume, "q_fin")] = "default"

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert policies_on(client, params.dest_cluster,
                       params.dest_vserver)["ep_q_fin"] == []


def test_an_existing_policy_is_left_alone(engine, ready, client, params):
    """It may belong to something this job knows nothing about."""
    theirs = [ExportRule(clients=["172.16.0.0/12"], index=1)]
    client.export_policies.setdefault(
        (params.dest_cluster, params.dest_vserver), {})["ep_q_fin"] = theirs

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert policies_on(client, params.dest_cluster,
                       params.dest_vserver)["ep_q_fin"] == theirs
    created = [c for c in client.calls
               if c.startswith("create_export_policy")
               and params.dest_cluster in c]
    assert created == []


def test_the_qtree_is_still_pointed_at_the_reused_policy(engine, ready, client,
                                                         params):
    """Reusing the policy object must not mean skipping the assignment."""
    client.export_policies.setdefault(
        (params.dest_cluster, params.dest_vserver), {})["ep_q_fin"] = []

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert client.get_qtree_export_policy(
        params.dest_cluster, params.dest_vserver, "vol_q_fin",
        "q_fin") == "ep_q_fin"


# =============================================================================
# The test environment gets the same treatment
# =============================================================================

def test_a_test_environment_carries_the_policies_too(engine, ready, client,
                                                     params):
    """The point of the test run is that the client validates real access."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert "ep_q_fin" in policies_on(client, params.dest_cluster,
                                     params.dest_vserver)
    assert "ep_q_fin" in policies_on(client, params.dr_cluster,
                                     params.dr_vserver)


# =============================================================================
# What ends up on the job
# =============================================================================

def test_the_job_records_which_policy_each_qtree_got(engine, ready, client,
                                                     store):
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    saved = store.load(ready["job_id"])
    assert saved["export_policy_map"] == {"q_fin": "ep_q_fin",
                                          "q_hr": "ep_q_hr"}


def test_the_result_reports_the_clients_that_were_carried(engine, ready):
    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = result["export_policies"]["q_fin"]
    assert carried["source_policy"] == "ep_source"
    assert carried["policy"] == "ep_q_fin"
    assert carried["clients"] == ["10.0.0.0/8", "@admins"]
    assert sorted(carried["created_on"]) == ["DR", "PROD"]


# =============================================================================
# The pre-flight says all of this in advance
# =============================================================================

def _codes(report):
    return {c["code"] for c in report.to_dict()["checks"]}


def test_the_preflight_announces_the_policy_it_will_create(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    plan = [c for c in report.to_dict()["checks"]
            if c["code"] == "EXPORT_POLICY_PLAN"]
    assert plan and "ep_q_fin" in plan[0]["detail"]


def test_the_preflight_lists_the_clients(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_POLICY_NO_CLIENT")
    assert "10.0.0.0/8" in check["detail"]
    assert check["passed"]


def test_the_preflight_warns_when_no_client_would_be_carried(ready, engine,
                                                             client, params):
    client.qtree_policies[(params.source_cluster, params.source_vserver,
                           params.volume, "q_fin")] = "default"

    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_POLICY_NO_CLIENT")
    assert not check["passed"]
    assert check["severity"] == "warning", "a CIFS-only qtree is legitimate"
    assert report.ok, "a warning must not refuse the clone"


def test_the_preflight_warns_when_the_name_is_already_taken(ready, engine,
                                                            client, params):
    client.export_policies.setdefault(
        (params.dest_cluster, params.dest_vserver), {})["ep_q_fin"] = []

    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_POLICY_NAME_TAKEN")
    assert not check["passed"] and check["severity"] == "warning"
    assert report.ok


def test_the_preflight_uses_the_renamed_qtree(ready, engine):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"),
                               qtree_map={"q_fin": "finance"})

    plan = next(c for c in report.to_dict()["checks"]
                if c["code"] == "EXPORT_POLICY_PLAN")
    assert "ep_finance" in plan["detail"]


def test_an_unreadable_policy_is_a_warning_not_a_crash(ready, engine, client,
                                                       params):
    """Typically an RBAC gap on /api/protocols/nfs/export-policies."""
    def refuse(cluster, svm, policy):
        raise OntapError(cluster, "export-policy show", "not authorized")

    client.get_export_policy_rules = refuse

    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    assert "EXPORT_RULES_UNREADABLE" in _codes(report)
    assert report.ok, "an RBAC gap must not silently refuse the clone"


def test_the_preflight_covers_the_test_action_too(ready, engine):
    report = engine.checker.for_test(ready, ["q_fin"], volume_map=vmap("q_fin"))

    assert "EXPORT_POLICY_PLAN" in _codes(report)


# =============================================================================
# The SSH fallback reads the same thing out of CLI text
# =============================================================================

RULE_SHOW = """
                                    Vserver: svm_source
                                Policy Name: ep_source
                                 Rule Index: 1
                            Access Protocol: nfs3, nfs4
Client Match Hostname, IP Address, Netgroup, or Domain: 10.0.0.0/8, @admins
                             RO Access Rule: sys
                             RW Access Rule: sys
User ID To Which Anonymous Users Are Mapped: 65534
                   Superuser Security Types: none

                                    Vserver: svm_source
                                Policy Name: ep_source
                                 Rule Index: 2
                            Access Protocol: any
Client Match Hostname, IP Address, Netgroup, or Domain: backup.example.com
                             RO Access Rule: sys
                             RW Access Rule: never
User ID To Which Anonymous Users Are Mapped: 65534
                   Superuser Security Types: sys
"""


def test_the_ssh_parser_reads_every_rule():
    rules = parse_export_rules(RULE_SHOW)

    assert [r.index for r in rules] == [1, 2]
    assert rules[0].clients == ["10.0.0.0/8", "@admins"]
    assert rules[0].protocols == ["nfs3", "nfs4"]
    assert rules[1].clients == ["backup.example.com"]
    assert rules[1].rw_rule == ["never"]
    assert rules[1].superuser == ["sys"]


def test_the_ssh_parser_ignores_blocks_that_are_not_rules():
    assert parse_export_rules("There are no entries matching your query.") == []


def test_the_ssh_parser_treats_a_dash_as_no_value():
    text = RULE_SHOW.replace("Superuser Security Types: none",
                             "Superuser Security Types: -")
    assert parse_export_rules(text)[0].superuser == []


# =============================================================================
# Promotion: nothing is rebuilt, so the policies must already be there
# =============================================================================

def test_promoting_a_test_environment_keeps_its_policies(engine, ready, store):
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    report = engine.checker.for_clone(store.load(ready["job_id"]), ["q_fin"])

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "PROMOTION_NO_EXPORT_POLICIES")
    assert check["passed"] and "ep_q_fin" in check["detail"]


def test_promoting_an_older_test_environment_warns(engine, ready, store):
    """Built before this step existed: the clones have no policy of their own."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))
    job = store.load(ready["job_id"])
    del job["export_policy_map"]

    report = engine.checker.for_clone(job, ["q_fin"])

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "PROMOTION_NO_EXPORT_POLICIES")
    assert not check["passed"] and check["severity"] == "warning"
    assert report.ok, "a promotion must still be possible"
