"""What a destination clone volume is configured with.

A clone starts as a copy of the source volume's settings — tuned for a shared
volume holding every client. The destination holds one client's qtree, so
the settings are reapplied rather than inherited.

Applied right after creation and BEFORE the clone mirror exists: that is the
only window in which the DR clone is still writable.
"""

import pytest

from netapp_migration.core.volumes import (CLONE_VOLUME_SETTINGS,
                                           describe_settings)

from conftest import cascade_ready, vmap
from test_api import api                                      # noqa: F401


@pytest.fixture()
def ready(store, params, client):
    job = store.create(params, "full")
    store.set_status(job, "completed")
    cascade_ready(client, params)
    return job


def volume_on(client, cluster, svm, volume):
    return client.volumes[(cluster, svm, volume)]


def configure_calls(client):
    return [c for c in client.calls if c.startswith("configure_volume")]


# =============================================================================
# The settings themselves
# =============================================================================

def test_the_settings_are_the_ones_asked_for():
    """Pinned: these decide how every migrated volume behaves."""
    assert CLONE_VOLUME_SETTINGS == {
        "encryption": True,
        "snapshot_reserve_percent": 0,
        "space_guarantee": "none",
        "snapshot_policy": "none",
        "security_style": "unix",
        "export_policy": "default",
    }


def test_they_are_applied_to_the_prod_clone(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    volume = volume_on(client, params.dest_cluster, params.dest_vserver,
                       "vol_q_fin")
    assert volume.encrypted is True
    assert volume.snapshot_reserve_percent == 0
    assert volume.space_guarantee == "none"
    assert volume.snapshot_policy == "none"
    assert volume.security_style == "unix"
    assert volume.export_policy == "default"


def test_they_are_applied_to_the_dr_clone_too(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    volume = volume_on(client, params.dr_cluster, params.dr_vserver,
                       "vol_q_fin")
    assert volume.encrypted is True
    assert volume.security_style == "unix"


def test_every_clone_is_configured(engine, ready, client, params):
    engine.clone("q_fin,q_hr", job=ready, volume_map=vmap("q_fin", "q_hr"))

    assert len(configure_calls(client)) == 4, "two volumes, two sides"


def test_the_test_environment_gets_them_too(engine, ready, client, params):
    """What the client validates must be the volume they will actually get."""
    engine.test("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert volume_on(client, params.dest_cluster, params.dest_vserver,
                     "vol_q_fin").security_style == "unix"


def test_the_source_volume_is_never_touched(engine, ready, client, params):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert not any(params.source_cluster in c
                   for c in configure_calls(client))
    assert volume_on(client, params.source_cluster, params.source_vserver,
                     params.volume).security_style == "ntfs", "as it was"


# =============================================================================
# Ordering: the DR clone stops being writable
# =============================================================================

def test_they_are_applied_before_the_clone_mirror(engine, ready, client):
    """Once the DR clone is a SnapMirror destination it is read-only and
    cannot be modified at all."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    configured = max(i for i, c in enumerate(client.calls)
                     if c.startswith("configure_volume"))
    mirrored = min(i for i, c in enumerate(client.calls)
                   if c.startswith("snapmirror_create"))
    assert configured < mirrored


def test_they_are_applied_before_the_qtrees_are_renamed(engine, ready, client):
    """The volume is reconfigured first, then its contents are arranged."""
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"),
                 qtree_map={"q_fin": "finance"})

    configured = min(i for i, c in enumerate(client.calls)
                     if c.startswith("configure_volume"))
    renamed = min(i for i, c in enumerate(client.calls)
                  if c.startswith("rename_qtree"))
    assert configured < renamed


# =============================================================================
# One refused setting must not cost the others
# =============================================================================

def test_a_refused_setting_does_not_abort_the_clone(engine, ready, client,
                                                    params):
    """A FlexClone shares blocks with its parent, so a cluster may refuse to
    encrypt one that is still attached. The other five are still worth
    having on a volume nobody is using yet."""
    client.refuse_volume_settings["encryption"] = "clone is still attached"

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert result["clone_volumes"] == ["vol_q_fin"], "the clone still ran"
    assert volume_on(client, params.dest_cluster, params.dest_vserver,
                     "vol_q_fin").security_style == "unix"


def test_a_refused_setting_is_reported_by_name(engine, ready, client):
    client.refuse_volume_settings["encryption"] = "clone is still attached"

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    refused = result["volume_settings"]["q_fin"]["PROD"]["refused"]
    assert refused == {"encryption": "clone is still attached"}


def test_what_was_applied_is_reported_too(engine, ready, client):
    client.refuse_volume_settings["encryption"] = "clone is still attached"

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    applied = result["volume_settings"]["q_fin"]["PROD"]["applied"]
    assert "security_style" in applied
    assert "encryption" not in applied


def test_a_clean_run_refuses_nothing(engine, ready):
    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    for side in result["volume_settings"]["q_fin"].values():
        assert side["refused"] == {}
        assert len(side["applied"]) == len(CLONE_VOLUME_SETTINGS)


def test_the_result_survives_json_encoding(engine, ready):
    import json

    result = engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    assert json.loads(json.dumps(result["volume_settings"])) == \
        result["volume_settings"]


# =============================================================================
# The pre-flight says it in advance
# =============================================================================

def test_the_preflight_states_the_settings(engine, ready):
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "VOLUME_SETTINGS")
    assert "encryption=true" in check["detail"]
    assert "security style=unix" in check["detail"]
    assert "snapshot reserve=0" in check["detail"]


def test_the_preflight_warns_that_unix_clones_cannot_take_acls(engine, ready):
    """Two requirements in tension: a 'unix' clone cannot take AD-group
    DACLs, so action 'acl' will refuse every migrated volume."""
    report = engine.checker.for_clone(ready, ["q_fin"],
                                      volume_map=vmap("q_fin"))

    check = next(c for c in report.to_dict()["checks"]
                 if c["code"] == "VOLUME_SECURITY_STYLE_VS_ACL")
    assert not check["passed"] and check["severity"] == "warning"
    assert report.ok, "it is a consequence of the settings, not a refusal"


def test_the_description_reads_like_the_cluster_does():
    described = describe_settings()

    assert "space guarantee=none" in described
    assert "snapshot policy=none" in described
    assert "export policy=default" in described


# =============================================================================
# And the inventory shows what the volumes ended up with
# =============================================================================

def test_the_inventory_reports_every_setting(engine, ready, store):
    engine.clone("q_fin", job=ready, volume_map=vmap("q_fin"))

    inventory = engine.check_status(store.load(ready["job_id"]))["destination"]

    volume = inventory["volumes"][0]["sides"]["PROD"]["volume"]
    assert volume["encrypted"] is True
    assert volume["snapshot_policy"] == "none"
    assert volume["snapshot_reserve_percent"] == 0
    assert volume["space_guarantee"] == "none"
    assert volume["security_style"] == "unix"
    assert volume["export_policy"] == "default"
