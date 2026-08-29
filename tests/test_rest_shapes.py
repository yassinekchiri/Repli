"""The exact JSON the REST transport puts on the wire.

The engine and the pre-flight are covered against a fake cluster, which
proves the logic but says nothing about whether ONTAP accepts the body. Every
bug found on a real cluster in this project has been a shape bug, so the
shapes are pinned here rather than left to be discovered in production.
"""

import pytest

from netapp_migration.core.exports import DESTINATION_RULE
from netapp_migration.core.volumes import CLONE_VOLUME_SETTINGS
from netapp_migration.models import ExportRule
from netapp_migration.transport.rest import (RestClient,
                                             _export_rule_from_rest,
                                             _export_rule_to_rest,
                                             _relationship_type_of)


def body_for(**overrides):
    fields = dict(DESTINATION_RULE)
    fields.update(overrides)
    return _export_rule_to_rest(ExportRule(clients=["10.0.0.1"], **fields))


# =============================================================================
# Export rules: what goes out
# =============================================================================

def test_access_rules_are_bare_enum_strings():
    """ONTAP 9.16.1 answers this body with

        HTTP 400: The value "any" is invalid for field "rules[0].ro_rule[0]"

    when the access rules are sent as [{"name": "any"}] instead."""
    body = body_for()

    assert body["ro_rule"] == ["any"]
    assert body["rw_rule"] == ["any"]
    assert body["superuser"] == ["any"]


def test_protocols_are_bare_enum_strings_too():
    """The field that was already right, and the evidence for the others:
    it sits in the same object and ONTAP never rejected it."""
    assert body_for()["protocols"] == ["nfs4"]


def test_only_clients_is_a_list_of_objects():
    """'clients' is the one nested type here — it carries 'match'."""
    body = body_for()

    assert body["clients"] == [{"match": "10.0.0.1"}]
    for field in ("ro_rule", "rw_rule", "superuser", "protocols"):
        assert all(isinstance(v, str) for v in body[field]), field


def test_no_enum_list_is_ever_sent_as_objects():
    """A guard over the whole body rather than field by field, so a field
    added later cannot quietly reintroduce the bug."""
    body = body_for()

    for field, value in body.items():
        if field == "clients" or not isinstance(value, list):
            continue
        assert all(isinstance(v, str) for v in value), \
            f"{field} must be a list of bare strings"


def test_the_scalars_are_sent_as_scalars():
    body = body_for()

    assert body["anonymous_user"] == "none"
    assert body["allow_suid"] is True
    assert body["allow_device_creation"] is True
    assert body["ntfs_unix_security"] == "fail"
    assert body["chown_mode"] == "restricted"


def test_a_field_the_source_did_not_report_is_left_out():
    """Omitted means 'take ONTAP's default', which is what the source rule
    was doing. Sending an invented default would change behaviour."""
    body = _export_rule_to_rest(ExportRule(clients=["10.0.0.1"]))

    assert "allow_suid" not in body
    assert "allow_device_creation" not in body
    assert "ntfs_unix_security" not in body
    assert "chown_mode" not in body


def test_the_index_is_never_sent():
    assert "index" not in body_for()


# =============================================================================
# Export rules: what comes back
# =============================================================================

def test_a_rule_is_read_back_from_bare_strings():
    """The shape ONTAP actually returns. Read as objects it yielded [],
    which is indistinguishable from 'this rule grants no access'."""
    rule = _export_rule_from_rest({
        "index": 2,
        "clients": [{"match": "10.0.0.1"}, {"match": "10.0.0.2"}],
        "ro_rule": ["any"], "rw_rule": ["any"], "superuser": ["any"],
        "protocols": ["nfs4"], "anonymous_user": "none",
        "allow_suid": True, "allow_device_creation": True,
        "ntfs_unix_security": "fail", "chown_mode": "restricted",
    })

    assert rule.clients == ["10.0.0.1", "10.0.0.2"]
    assert rule.ro_rule == ["any"]
    assert rule.rw_rule == ["any"]
    assert rule.superuser == ["any"]
    assert rule.protocols == ["nfs4"]
    assert rule.index == 2


def test_the_object_shape_is_still_understood():
    """Tolerated on the way in so an ONTAP that changes shape degrades to a
    wrong-looking rule rather than to a silently empty one."""
    rule = _export_rule_from_rest({
        "clients": [{"match": "10.0.0.1"}],
        "ro_rule": [{"name": "sys"}], "rw_rule": [{"name": "sys"}],
        "superuser": [{"name": "none"}], "protocols": [{"name": "nfs3"}],
    })

    assert rule.ro_rule == ["sys"]
    assert rule.rw_rule == ["sys"]
    assert rule.superuser == ["none"]
    assert rule.protocols == ["nfs3"]


def test_an_absent_list_reads_as_empty_not_as_a_crash():
    rule = _export_rule_from_rest({"index": 1})

    assert rule.clients == []
    assert rule.ro_rule == []


def test_what_goes_out_can_be_read_back_in():
    """The round trip is what a destination inventory relies on."""
    original = ExportRule(clients=["10.0.0.1"], **DESTINATION_RULE)

    # ONTAP echoes the rule with an index it assigned.
    returned = dict(_export_rule_to_rest(original), index=1)
    rule = _export_rule_from_rest(returned)

    for field, expected in DESTINATION_RULE.items():
        assert getattr(rule, field) == expected, field
    assert rule.clients == ["10.0.0.1"]


# =============================================================================
# SnapMirror: the relationship kind REST does not name
# =============================================================================

@pytest.mark.parametrize("policy_type,expected", [
    ("async", "XDP"),
    ("Async", "XDP"),
    ("async_mirror", "XDP"),
    ("sync", "SYNC"),
    ("", ""),
])
def test_the_relationship_kind_is_derived_from_the_policy(policy_type,
                                                          expected):
    """REST has no 'type' field: an async policy is what makes an XDP
    relationship, so the kind is derived rather than guessed at."""
    assert _relationship_type_of({"policy": {"type": policy_type}}) == expected


def test_a_record_without_a_policy_reports_no_kind():
    assert _relationship_type_of({}) == ""


# =============================================================================
# Volume settings: the PATCH body
# =============================================================================

def settings_body(**overrides):
    fields = dict(CLONE_VOLUME_SETTINGS)
    fields.update(overrides)
    return RestClient._volume_settings_body(fields)


def test_each_setting_lands_where_ontap_expects_it():
    body = settings_body()

    assert body["encryption"] == {"enabled": True}
    assert body["space"] == {"snapshot": {"reserve_percent": 0}}
    assert body["guarantee"] == {"type": "none"}
    assert body["snapshot_policy"] == {"name": "none"}


def test_two_settings_under_the_same_key_are_merged():
    """security_style and export_policy both live under 'nas'. A plain
    update would send the second and silently drop the first."""
    body = settings_body()

    assert body["nas"] == {"security_style": "unix",
                           "export_policy": {"name": "default"}}


def test_a_single_setting_builds_the_same_fragment():
    """The retry that isolates a refused setting must send exactly what the
    combined body sent for it, or it would test something else."""
    for key, value in CLONE_VOLUME_SETTINGS.items():
        alone = RestClient._volume_settings_body({key: value})
        top = next(iter(alone))
        assert top in settings_body(), key


def test_an_unknown_setting_is_dropped_rather_than_guessed():
    assert RestClient._volume_settings_body({"not_a_setting": 1}) == {}
