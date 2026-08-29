"""What quota rules a migrated qtree gets on the destination.

A clone volume holds exactly one client's qtree, so its quota story is two
rules, not one:

**The volume-level tree rule** (`type=tree`, empty qtree target) covers
everything in the volume that is *not* inside a qtree. It is set to 0 —
nothing may be written outside the qtree the volume was created for. Without
it a client could fill the volume by writing at the root and never touch
their own quota.

**The qtree rule** carries the limits the qtree had on the source, so the
client sees the same ceiling they had before the migration.

Both go into the SVM's `default` quota policy, and both are created on PROD
**and** DR: quota rules belong to the quota policy, which is an SVM object.
Volume-level SnapMirror does not replicate them, so a rule that exists only
on PROD is simply absent the day DR is activated.

Lives here rather than in the engine because the pre-flight has to predict
exactly what the engine will do — it announces both rules, and the limits
they will carry, before anything is created.
"""

from typing import List, Optional, Sequence

from ..models import QuotaRule

# The quota policy the rules go into. ONTAP names the SVM's default policy
# 'default'; the REST API writes to whichever policy is active on the SVM
# without naming it, so this is what the pre-flight verifies is the one in
# force rather than something the transport can choose.
QUOTA_POLICY = "default"

# What the volume-level tree rule allows: nothing. Anything written outside
# the qtree is not this client's data and must not consume the volume.
VOLUME_TREE_LIMIT = 0


def source_rule_for(rules: Sequence[QuotaRule],
                    qtree: str) -> Optional[QuotaRule]:
    """The source's tree rule for one qtree, or None when it has no quota.

    Only tree rules are considered: a user or group rule limits a person
    across the volume, which is not a property of the qtree being migrated
    and would mean something different once the volume holds one qtree.
    """
    for rule in rules:
        if (rule.type or "").lower() != "tree":
            continue
        if (rule.qtree or "").lower() == (qtree or "").lower():
            return rule
    return None


def destination_rules(source: Optional[QuotaRule],
                      dest_qtree: str) -> List[QuotaRule]:
    """The two rules a migrated qtree gets, in the order they are created.

    The volume-level rule first: it is the one that must exist even when the
    source qtree had no quota at all, because it is what stops the volume
    being filled from outside the qtree.
    """
    volume_rule = QuotaRule(type="tree", qtree="",
                            space_hard_limit=VOLUME_TREE_LIMIT,
                            space_soft_limit=VOLUME_TREE_LIMIT)
    qtree_rule = QuotaRule(
        type="tree", qtree=dest_qtree,
        space_hard_limit=source.space_hard_limit if source else None,
        space_soft_limit=source.space_soft_limit if source else None,
        files_hard_limit=source.files_hard_limit if source else None,
        files_soft_limit=source.files_soft_limit if source else None)
    return [volume_rule, qtree_rule]


def describe_limit(value: Optional[int]) -> str:
    """A limit for a log line. None is 'unlimited', 0 is really zero."""
    if value is None:
        return "unlimited"
    if value == 0:
        return "0 B"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return str(value)


def describe_rule(rule: QuotaRule) -> str:
    """One line for a log table or a pre-flight detail."""
    target = f"qtree '{rule.qtree}'" if rule.qtree else "volume (qtree \"\")"
    return (f"{target}: disk={describe_limit(rule.space_hard_limit)} "
            f"soft={describe_limit(rule.space_soft_limit)}")
