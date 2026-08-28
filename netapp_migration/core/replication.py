"""SnapMirror settings, in one place.

The tool creates two different kinds of relationship, and they are not
configured the same way:

**The cascade** — source to pivot, pivot to PROD and to DR. This carries the
whole source volume across, once, to stage the data.

**The clone mirror** — the PROD clone to the DR clone. This is the
relationship the client lives with afterwards, so it is the one whose
schedule decides how far behind DR is allowed to fall.

Both live here rather than as transport defaults because the engine, the
pre-flight and the transports all have to agree: the pre-flight checks that
a policy and a schedule are visible to the API user *before* the engine asks
ONTAP to use them, and a check that verifies a different name from the one
the engine sends is worse than no check at all.

ONTAP resolves a referenced policy or schedule with the CALLER's
permissions, so one the role cannot read is reported as "not found" even
when it exists — which is why the names are checked rather than assumed.
"""

# Staging the data across the cascade.
CASCADE_TYPE = "XDP"
CASCADE_POLICY = "MirrorAllSnapshots"
CASCADE_SCHEDULE = "hourly"

# The relationship the client lives with: PROD clone -> DR clone.
CLONE_TYPE = "XDP"
CLONE_POLICY = "MFA_MirrorAllSnapshots"
CLONE_SCHEDULE = "pg-15-minutely"


def describe_clone_mirror() -> str:
    """One line naming what the clone mirror is created with."""
    return (f"type={CLONE_TYPE} policy={CLONE_POLICY} "
            f"schedule={CLONE_SCHEDULE}")
