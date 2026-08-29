"""What a destination clone volume is configured with.

A clone starts life inheriting its parent's settings, which are the source
volume's — sized and tuned for a shared volume holding every client. The
destination is a new, uniform environment holding one client's qtree, so the
clone is reconfigured rather than left as it came.

One place decides it for every migrated qtree. Changing a value here changes
it for every clone the tool creates from then on.

Encryption is the odd one: a FlexClone shares blocks with its parent, so
ONTAP will not let an attached clone differ from it. The setting is applied
anyway and, if the cluster refuses it while the clone is still attached, the
refusal is reported by name rather than swallowed — the operator then knows
to encrypt on the volume move instead.
"""

from typing import Any, Dict

# Applied to every clone on PROD and DR, right after it is created and
# before the clone mirror exists — while the DR side is still writable.
CLONE_VOLUME_SETTINGS: Dict[str, Any] = {
    "encryption": True,
    "snapshot_reserve_percent": 0,
    "space_guarantee": "none",
    "snapshot_policy": "none",
    "security_style": "unix",
    "export_policy": "default",
}

# Human labels, so a log line or a check reads like the cluster's own
# vocabulary rather than like a Python identifier.
SETTING_LABELS = {
    "encryption": "encryption",
    "snapshot_reserve_percent": "snapshot reserve",
    "space_guarantee": "space guarantee",
    "snapshot_policy": "snapshot policy",
    "security_style": "security style",
    "export_policy": "export policy",
}


def describe_settings(settings: Dict[str, Any] = None) -> str:
    """One line naming what every clone volume is created with."""
    settings = CLONE_VOLUME_SETTINGS if settings is None else settings
    return " ".join(f"{SETTING_LABELS.get(key, key)}="
                    f"{str(value).lower() if isinstance(value, bool) else value}"
                    for key, value in settings.items())
