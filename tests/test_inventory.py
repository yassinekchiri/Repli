"""What check-status reports once the clones exist.

The question an operator has after a clone is not 'did it work' — the run
already said so — but 'what exists now, and how do I find it again?'. So
every object is reported with the handle ONTAP knows it by, next to the
values that decide behaviour.

Nothing here mutates anything: the inventory is a report, and a report that
cannot read one object must still deliver the rest.
"""

import pytest

from netapp_migration.models import ExportRule, OntapError, QuotaRule

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def cloned(engine, client, params, store):
    """A finished clone of q_fin (renamed 'finance') and q_hr."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    engine.clone("q_fin,q_hr", job=job, volume_map=vmap("q_fin", "q_hr"),
                 qtree_map={"q_fin": "finance"})
    return store.load(job["job_id"])


def entry_for(inventory, volume):
    return next(e for e in inventory["volumes"] if e["volume"] == volume)


# =============================================================================
# It appears where it is useful, and only there
# =============================================================================

def test_status_reports_the_destination_once_the_clones_exist(engine, cloned):
    result = engine.check_status(cloned)

    assert "destination" in result
    assert sorted(e["volume"] for e in result["destination"]["volumes"]) == \
        ["vol_q_fin", "vol_q_hr"]


def test_a_cascade_with_no_clone_yet_reports_no_destination(engine, client,
                                                            params, store):
    """Nothing has been created on the destinations: there is nothing to list."""
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)

    result = engine.check_status(job)

    assert "destination" not in result


def test_the_inventory_never_writes_anything(engine, cloned, client):
    client.calls.clear()

    engine.check_status(cloned, persist=False)

    assert client.calls == [], "a report must not mutate the estate"


# =============================================================================
# Volumes
# =============================================================================

def test_every_volume_is_reported_with_its_uuid(engine, cloned, params):
    inventory = engine.check_status(cloned)["destination"]

    prod = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["volume"]
    assert prod["uuid"] == "uuid-prd-vol_q_fin"
    assert prod["state"] == "online"
    assert prod["junction_path"] == "/vol_q_fin"
    assert prod["size_bytes"] == 10 * 1024 ** 3


def test_both_sides_are_reported(engine, cloned, params):
    inventory = engine.check_status(cloned)["destination"]
    sides = entry_for(inventory, "vol_q_fin")["sides"]

    assert sides["PROD"]["cluster"] == params.dest_cluster
    assert sides["DR"]["cluster"] == params.dr_cluster
    assert sides["PROD"]["volume"]["uuid"] != sides["DR"]["volume"]["uuid"]


def test_the_volume_move_that_split_the_clone_is_reported(engine, cloned):
    """Whether a clone is still attached to its parent decides whether it
    occupies real space, so it is the first thing an operator checks."""
    inventory = engine.check_status(cloned)["destination"]

    volume = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["volume"]
    assert volume["is_flexclone"] is False, "the move split it"
    assert volume["move_state"] == "success"
    assert volume["clone_parent"] == "vol_prod_01", "where it came from"


# =============================================================================
# Qtrees
# =============================================================================

def test_qtrees_are_reported_with_their_id_and_path(engine, cloned):
    inventory = engine.check_status(cloned)["destination"]

    qtrees = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["qtrees"]
    assert len(qtrees) == 1, "pruning left only the qtree the volume came for"
    assert qtrees[0]["name"] == "finance"
    assert qtrees[0]["id"] == 1
    assert qtrees[0]["path"] == "/vol_q_fin/finance"


def test_the_qtree_carries_the_policy_the_clone_applied(engine, cloned):
    inventory = engine.check_status(cloned)["destination"]

    qtrees = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["qtrees"]
    assert qtrees[0]["export_policy"] == "ep_finance"


def test_dr_shows_what_replication_actually_delivered(engine, cloned):
    """Not what PROD was told to do — what the DR side ended up with."""
    inventory = engine.check_status(cloned)["destination"]

    dr = entry_for(inventory, "vol_q_fin")["sides"]["DR"]["qtrees"]
    assert [q["name"] for q in dr] == ["finance"]
    assert dr[0]["export_policy"] == "ep_finance"


# =============================================================================
# Export policies
# =============================================================================

def test_the_export_policy_is_reported_with_its_rules(engine, cloned):
    inventory = engine.check_status(cloned)["destination"]

    policies = entry_for(inventory,
                         "vol_q_fin")["sides"]["PROD"]["export_policies"]
    policy = next(p for p in policies if p["name"] == "ep_finance")
    assert policy["present"] is True
    assert policy["id"] is not None
    assert policy["rules"][0]["clients"] == ["10.0.0.1"], "one rule, one client"
    assert policy["rules"][0]["ro_rule"] == ["any"]


def test_a_policy_a_qtree_points_at_is_reported_even_if_unexpected(
        engine, cloned, client, params):
    """Something outside this job repointed the qtree: say so, do not hide it."""
    client.export_policies[(params.dest_cluster, params.dest_vserver)][
        "ep_someone_else"] = [ExportRule(clients=["172.16.0.0/12"], index=1)]
    client.qtree_policies[(params.dest_cluster, params.dest_vserver,
                           "vol_q_fin", "finance")] = "ep_someone_else"

    inventory = engine.check_status(cloned)["destination"]

    names = [p["name"] for p in
             entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["export_policies"]]
    assert "ep_someone_else" in names, "the policy actually in force"
    assert "ep_finance" in names, "and the one this job set"


def test_a_policy_that_no_longer_exists_is_reported_as_absent(engine, cloned,
                                                              client, params):
    del client.export_policies[(params.dest_cluster,
                                params.dest_vserver)]["ep_finance"]

    inventory = engine.check_status(cloned)["destination"]

    policy = next(p for p in entry_for(
        inventory, "vol_q_fin")["sides"]["PROD"]["export_policies"]
        if p["name"] == "ep_finance")
    assert policy["present"] is False
    assert policy["rules"] == []


# =============================================================================
# Quotas
# =============================================================================

def test_quota_rules_are_reported_with_both_limits(engine, cloned, client,
                                                   params):
    client.quota_rules[(params.dest_cluster, params.dest_vserver,
                        "vol_q_fin")] = [
        QuotaRule(uuid="rule-uuid-1", type="tree", qtree="finance",
                  space_hard_limit=100 * 1024 ** 3,
                  space_soft_limit=80 * 1024 ** 3,
                  files_hard_limit=1_000_000, files_soft_limit=800_000)]

    inventory = engine.check_status(cloned)["destination"]

    rule = entry_for(inventory,
                     "vol_q_fin")["sides"]["PROD"]["quota_rules"][0]
    assert rule["uuid"] == "rule-uuid-1"
    assert rule["type"] == "tree"
    assert rule["space_hard_limit"] == 100 * 1024 ** 3
    assert rule["space_soft_limit"] == 80 * 1024 ** 3
    assert rule["files_hard_limit"] == 1_000_000
    assert rule["files_soft_limit"] == 800_000


def test_an_unset_limit_stays_none(engine, cloned, client, params):
    """None means 'no limit'. Reporting 0 would read as 'nothing allowed'."""
    client.quota_rules[(params.dest_cluster, params.dest_vserver,
                        "vol_q_fin")] = [
        QuotaRule(uuid="u", type="user", target="DOMAIN\\jdoe",
                  space_hard_limit=5 * 1024 ** 3)]

    inventory = engine.check_status(cloned)["destination"]

    rule = entry_for(inventory,
                     "vol_q_fin")["sides"]["PROD"]["quota_rules"][0]
    assert rule["space_soft_limit"] is None
    assert rule["files_hard_limit"] is None


def test_the_rules_the_clone_created_are_reported(engine, cloned):
    """The clone gives every volume two rules; the inventory shows them."""
    inventory = engine.check_status(cloned)["destination"]

    rules = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["quota_rules"]
    assert [r["qtree"] for r in rules] == ["", "finance"]


def test_no_quota_rule_is_a_real_answer(engine, cloned, client, params):
    """A volume nobody put a rule on reports none, not an error."""
    client.quota_rules[(params.dest_cluster, params.dest_vserver,
                        "vol_q_fin")] = []

    inventory = engine.check_status(cloned)["destination"]

    assert entry_for(inventory,
                     "vol_q_fin")["sides"]["PROD"]["quota_rules"] == []


def test_the_quota_policy_of_each_side_is_reported(engine, cloned):
    inventory = engine.check_status(cloned)["destination"]

    assert inventory["quota_policies"]["PROD"] == "default"
    assert inventory["quota_policies"]["DR"] == "default"


def test_a_transport_that_cannot_name_the_quota_policy_says_so(engine, cloned,
                                                               client):
    """REST applies rules to the SVM's active policy without naming it."""
    client.quota_policies.clear()

    inventory = engine.check_status(cloned)["destination"]

    assert inventory["quota_policies"]["PROD"] == "not exposed by the REST API"


# =============================================================================
# SnapMirror
# =============================================================================

def test_the_clone_mirror_is_reported_with_its_uuid(engine, cloned, params):
    inventory = engine.check_status(cloned)["destination"]

    mirror = entry_for(inventory, "vol_q_fin")["snapmirror"]
    assert mirror["uuid"]
    assert mirror["dest_path"] == params.path(params.dr_vserver, "vol_q_fin")
    assert mirror["source_path"] == params.path(params.dest_vserver,
                                                "vol_q_fin")
    assert mirror["state"] == "snapmirrored"
    assert mirror["policy"] == "MFA_MirrorAllSnapshots"
    assert mirror["schedule"] == "pg-15-minutely"
    assert mirror["type"] == "XDP"


# =============================================================================
# One unreadable object must not cost the whole report
# =============================================================================

def test_an_unreadable_object_degrades_one_line_only(engine, cloned, client):
    def refuse(cluster, svm, volume):
        raise OntapError(cluster, "quota rule show", "not authorized")

    client.list_quota_rules = refuse

    inventory = engine.check_status(cloned)["destination"]

    assert inventory["unreadable"], "the failure is reported, not swallowed"
    assert "not authorized" in inventory["unreadable"][0]["reason"]
    # Everything else still came through.
    assert entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["volume"]["uuid"]
    assert entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["qtrees"]


def test_a_volume_that_vanished_is_reported_not_raised(engine, cloned, client,
                                                       params):
    del client.volumes[(params.dest_cluster, params.dest_vserver, "vol_q_fin")]

    inventory = engine.check_status(cloned)["destination"]

    volume = entry_for(inventory, "vol_q_fin")["sides"]["PROD"]["volume"]
    assert volume["state"] == "unreadable"
    assert any("vol_q_fin" in item["object"]
               for item in inventory["unreadable"])


def test_a_clean_report_lists_nothing_as_unreadable(engine, cloned):
    inventory = engine.check_status(cloned)["destination"]

    assert inventory["unreadable"] == []


# =============================================================================
# Through the API
# =============================================================================

def test_the_status_endpoint_carries_the_inventory(api, engine, cloned):
    http, store, _fake, _tokens = api
    store.save(cloned)

    body = http.get(f"/api/v1/migrations/{cloned['job_id']}/status").json()

    inventory = body["result"]["destination"]
    assert inventory["volumes"]
    assert inventory["volumes"][0]["sides"]["PROD"]["volume"]["uuid"]


def test_the_status_endpoint_also_returns_the_printed_tables(api, engine,
                                                             cloned):
    """The operator reading the API sees the same report as the CLI."""
    http, store, _fake, _tokens = api
    store.save(cloned)

    body = http.get(f"/api/v1/migrations/{cloned['job_id']}/status").json()

    logs = "\n".join(body["logs"])
    assert "DESTINATION ENVIRONMENT" in logs
    assert "uuid-prd-vol_q_fin" in logs


def test_the_inventory_survives_json_encoding(api, engine, cloned, client,
                                              params):
    """Everything in it must be a plain type — no dataclass leaks through."""
    import json

    client.quota_rules[(params.dest_cluster, params.dest_vserver,
                        "vol_q_fin")] = [QuotaRule(uuid="u", type="tree")]

    inventory = engine.check_status(cloned)["destination"]

    assert json.loads(json.dumps(inventory)) == inventory
