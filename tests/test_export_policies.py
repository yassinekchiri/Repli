"""Carrying each qtree's NFS clients over to PROD and DR.

An export policy is an SVM object: it does not travel with the data. A
FlexClone inherits the policy NAME from its parent volume, and that name
means nothing on the destination SVM — so without this step the migrated
qtree points at a policy that is absent, or worse, belongs to somebody else.
"""

import pytest

from netapp_migration.core.exports import (CLIENT_HOST, CLIENT_NETWORK,
                                          CLIENT_OTHER,
                                          DESTINATION_RULE,
                                          classify_client,
                                          destination_rules)
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
    """One rule per client: the source rule named two hosts and a network."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["10.0.0.1"], ["10.0.0.2"]]


def test_every_other_field_of_the_rule_is_copied_unchanged(engine, ready,
                                                           client, params):
    """The destination rule must behave exactly like the source one."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    rule = policies_on(client, params.dest_cluster,
                       params.dest_vserver)["ep_q_fin"][0]
    assert rule.ro_rule == ["any"]
    assert rule.rw_rule == ["any"]
    assert rule.superuser == ["any"]
    assert rule.protocols == ["nfs4"]
    assert rule.anonymous_user == "none"
    assert rule.allow_suid is True
    assert rule.allow_device_creation is True
    assert rule.ntfs_unix_security == "fail"
    assert rule.chown_mode == "restricted"


def test_the_destination_index_is_left_to_ontap(engine, ready, client, params):
    """The split renumbers everything anyway, so the source index is dropped."""
    carried_before = engine.clone("q_fin", job=ready,
                                  volume_map=vmap("q_fin"))

    rules = policies_on(client, params.dest_cluster,
                        params.dest_vserver)["ep_q_fin"]
    assert all(r.index is None for r in rules)
    assert carried_before["export_policies"]["q_fin"]["rules"] == 2


def test_a_network_is_not_carried_over(engine, ready, client, params):
    """A subnet names whatever lives in that range on the destination side,
    which is not necessarily the same machines."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    every_client = [c for r in carried for c in r.clients]
    assert "10.20.0.0/16" not in every_client


def test_a_network_does_not_fail_the_clone(engine, ready, client, params):
    """The rest of the policy is still worth having."""
    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert result["export_policies"]["q_fin"]["skipped_networks"] == \
        ["10.20.0.0/16"]
    assert result["export_policies"]["q_fin"]["clients"] == ["10.0.0.1",
                                                             "10.0.0.2"]


def test_a_host_written_as_a_full_prefix_is_still_a_host(engine, ready, client,
                                                         params):
    """10.0.0.9/32 covers exactly one address: a host spelled the long way."""
    source_policy(client, params)[0].clients = ["10.0.0.9/32"]

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["10.0.0.9/32"]]


def test_hostnames_and_netgroups_are_carried_one_per_rule(engine, ready,
                                                          client, params):
    source_policy(client, params)[0].clients = ["host.example.com", "@admins",
                                                ".example.com"]

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["host.example.com"], ["@admins"],
                                            [".example.com"]]


def test_a_policy_of_networks_only_carries_nothing(engine, ready, client,
                                                   params):
    source_policy(client, params)[0].clients = ["10.0.0.0/8", "192.168.0.0/16"]

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert policies_on(client, params.dest_cluster,
                       params.dest_vserver)["ep_q_fin"] == []
    assert len(result["export_policies"]["q_fin"]["skipped_networks"]) == 2


def test_every_rule_is_carried_not_just_the_first(engine, ready, client,
                                                  params):
    source_policy(client, params).append(
        ExportRule(clients=["192.168.0.5"], ro_rule=["krb5"],
                   rw_rule=["never"], protocols=["nfs3"], index=3))

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["10.0.0.1"], ["10.0.0.2"],
                                            ["192.168.0.5"]]
    assert carried[2].rw_rule == DESTINATION_RULE["rw_rule"], \
        "the source rule's own settings are not inherited"


def test_the_same_client_twice_makes_one_rule(engine, ready, client, params):
    """ONTAP rejects a duplicate client match within a policy."""
    source_policy(client, params).append(
        ExportRule(clients=["10.0.0.1"], index=3))

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    carried = policies_on(client, params.dest_cluster,
                          params.dest_vserver)["ep_q_fin"]
    assert [r.clients for r in carried] == [["10.0.0.1"], ["10.0.0.2"]]


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
    assert carried["clients"] == ["10.0.0.1", "10.0.0.2"]
    assert carried["source_rules"] == 1 and carried["rules"] == 2
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
    assert "10.0.0.1" in check["detail"] and "10.0.0.2" in check["detail"]
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

# Copied from a 9.16.1 cluster, wrapped value and all. The field is worded
# differently from the block above — reading only one spelling produced a
# rule with NO client instead of failing, which is the worst outcome
# available: the migration would silently grant nobody access.
RULE_SHOW_9161 = """
                                Policy Name: exp_f5EEbTP57
                                 Rule Index: 2
                            Access Protocol: nfs4
List of Client Match Hostnames, IP Addresses, Netgroups, or Domains: 10.0.0.1,10.0.0.2,10.20.0.0/16,192.168.1.
222
                             RO Access Rule: any
                             RW Access Rule: any
User ID To Which Anonymous Users Are Mapped: none
                   Superuser Security Types: any
               Honor SetUID Bits in SETATTR: true
                  Allow Creation of Devices: true
                 NTFS Unix Security Options: fail
         Vserver NTFS Unix Security Options: use_export_policy
                      Change Ownership Mode: restricted
              Vserver Change Ownership Mode: use_export_policy
                                  Policy ID: 373662154756
"""


def test_the_ssh_parser_reads_every_rule():
    rules = parse_export_rules(RULE_SHOW)

    assert [r.index for r in rules] == [1, 2]
    assert rules[0].clients == ["10.0.0.0/8", "@admins"]
    assert rules[0].protocols == ["nfs3", "nfs4"]
    assert rules[1].clients == ["backup.example.com"]
    assert rules[1].rw_rule == ["never"]
    assert rules[1].superuser == ["sys"]


def test_the_ssh_parser_reads_the_9161_wording():
    rule = parse_export_rules(RULE_SHOW_9161)[0]

    assert rule.clients, "an unread client list would grant nobody access"
    assert rule.protocols == ["nfs4"]
    assert rule.allow_suid is True
    assert rule.allow_device_creation is True


def test_the_ssh_parser_reassembles_a_wrapped_client_list():
    """ONTAP wraps a long value onto the next line. Dropping the remainder
    would silently lose whichever clients happened to fall past the margin."""
    rule = parse_export_rules(RULE_SHOW_9161)[0]

    assert rule.clients == ["10.0.0.1", "10.0.0.2", "10.20.0.0/16",
                            "192.168.1.222"]


def test_the_svm_wide_settings_are_never_read_as_rule_settings():
    """'Vserver NTFS Unix Security Options' is what the rule falls back to,
    not what the rule is. Writing it per rule would reconfigure the SVM."""
    rule = parse_export_rules(RULE_SHOW_9161)[0]

    assert rule.ntfs_unix_security == "fail"
    assert rule.chown_mode == "restricted"


def test_a_real_rule_becomes_one_destination_rule_per_host():
    rule = parse_export_rules(RULE_SHOW_9161)[0]

    carried, skipped = destination_rules([rule])

    assert [r.clients[0] for r in carried] == ["10.0.0.1", "10.0.0.2",
                                               "192.168.1.222"]
    assert [m for _, m in skipped] == ["10.20.0.0/16"]


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


# =============================================================================
# Only the client comes from the source
# =============================================================================

def test_the_source_parameters_are_not_inherited(engine, ready, client, params):
    """The destination is a new uniform environment, not a copy of whatever
    each source policy accumulated over the years."""
    source_policy(client, params)[0].ro_rule = ["krb5"]
    source_policy(client, params)[0].protocols = ["nfs3"]
    source_policy(client, params)[0].chown_mode = "unrestricted"
    source_policy(client, params)[0].allow_suid = False

    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    rule = policies_on(client, params.dest_cluster,
                       params.dest_vserver)["ep_q_fin"][0]
    assert rule.ro_rule == ["any"]
    assert rule.protocols == ["nfs4"]
    assert rule.chown_mode == "restricted"
    assert rule.allow_suid is True


def test_every_destination_rule_gets_the_same_parameters(engine, ready, client,
                                                         params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    rules = policies_on(client, params.dest_cluster,
                        params.dest_vserver)["ep_q_fin"]
    assert len(rules) > 1
    for rule in rules:
        for field, expected in DESTINATION_RULE.items():
            assert getattr(rule, field) == expected


def test_the_forced_parameters_are_the_ones_asked_for():
    """Pinned: changing one changes every migrated qtree's access."""
    assert DESTINATION_RULE == {
        "ro_rule": ["any"],
        "rw_rule": ["any"],
        "superuser": ["any"],
        "protocols": ["nfs4"],
        "anonymous_user": "none",
        "allow_suid": True,
        "allow_device_creation": True,
        "ntfs_unix_security": "fail",
        "chown_mode": "restricted",
    }


# =============================================================================
# Classifying a client match
# =============================================================================

@pytest.mark.parametrize("match", [
    "10.0.0.7", "10.0.0.7/32", "2001:db8::1", "2001:db8::1/128",
])
def test_a_single_address_is_a_host(match):
    assert classify_client(match) == CLIENT_HOST


@pytest.mark.parametrize("match", [
    "10.0.0.0/8", "10.0.0.0/255.0.0.0", "2001:db8::/64", "0.0.0.0/0",
])
def test_a_range_is_a_network(match):
    assert classify_client(match) == CLIENT_NETWORK


@pytest.mark.parametrize("match", [
    "host.example.com", ".example.com", "@admins", "", "  ",
])
def test_what_is_neither_is_left_alone(match):
    assert classify_client(match) == CLIENT_OTHER


# =============================================================================
# The pre-flight says all of this in advance
# =============================================================================

def test_the_preflight_names_the_networks_it_will_skip(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_NETWORK_SKIPPED")
    assert not check["passed"] and check["severity"] == "warning"
    assert "10.20.0.0/16" in check["detail"]
    assert report.ok, "a skipped network must not refuse the clone"


def test_the_preflight_passes_that_check_without_networks(engine, ready,
                                                          client, params):
    source_policy(client, params)[0].clients = ["10.0.0.1"]

    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_NETWORK_SKIPPED")
    assert check["passed"]


def test_the_preflight_announces_the_split(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    plan = next(c for c in report.to_dict()["checks"]
                if c["code"] == "EXPORT_POLICY_PLAN")
    assert "1 source rule(s) -> 2 rule(s)" in plan["detail"]


def test_the_preflight_states_the_forced_parameters(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_RULE_PARAMETERS")
    assert "proto=nfs4" in check["detail"]
    assert "chown=restricted" in check["detail"]


def test_the_preflight_warns_when_only_networks_would_be_carried(
        engine, ready, client, params):
    source_policy(client, params)[0].clients = ["10.0.0.0/8"]

    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "EXPORT_POLICY_NO_CLIENT")
    assert not check["passed"]
    assert report.ok


def test_the_preflight_predicts_what_the_engine_does(engine, ready, client,
                                                     params):
    """The two must agree: the report is worthless if it predicts something
    other than what runs."""
    report = engine.checker.for_clone(ready, ["q_fin"], volume_map=vmap("q_fin"))
    predicted = next(c for c in report.to_dict()["checks"]
                     if c["code"] == "EXPORT_POLICY_NO_CLIENT")["detail"]

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    for client_match in result["export_policies"]["q_fin"]["clients"]:
        assert client_match in predicted
