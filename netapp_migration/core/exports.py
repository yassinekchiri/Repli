"""How a source export rule becomes destination export rules.

Two rules, and only two:

**One rule, one client.** ONTAP lets a single rule name several clients; the
destination is built the other way round, one rule per client match. A source
rule granting three machines becomes three destination rules, so one machine
can later be revoked without touching the other two.

**Only the client comes from the source.** Everything else — access rules,
protocol, superuser, anonymous mapping, SetUID, device creation, NTFS
security, chown mode — is forced to the values in DESTINATION_RULE below.
The destination is a new, uniform environment: it is not the place to
inherit whatever each source policy happened to accumulate over the years.

Networks are not carried. A subnet on the source names hosts that may not
exist — or may mean something else entirely — on the destination side of the
migration, so copying one would silently grant access to whatever happens to
live in that range over there. They are skipped and reported, never guessed
at and never fatal: the rest of the policy is still worth having.

Lives here rather than in the engine because the pre-flight has to predict
exactly what the engine will do — it announces the split, the forced
parameters and the skipped networks before anything is created.
"""

import ipaddress
from typing import List, Sequence, Tuple

from ..models import ExportRule

# What a client match turns out to be.
CLIENT_HOST = "host"        # a single machine: 10.0.0.7, 2001:db8::1, /32
CLIENT_NETWORK = "network"  # a range: 10.0.0.0/8, 10.0.0.0/255.0.0.0
CLIENT_OTHER = "other"      # hostname, domain (.example.com), netgroup (@grp)

# The parameters every destination rule is given, whatever the source rule
# said. Changing a value here changes it for every migrated qtree, which is
# the point: one place decides how the destination exports behave.
DESTINATION_RULE = {
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


def classify_client(match: str) -> str:
    """What kind of client match this is: host, network, or something else.

    A full-length prefix (/32 on IPv4, /128 on IPv6) covers exactly one
    address, so it is a host written the long way — not a network.
    """
    text = (match or "").strip()
    if not text:
        return CLIENT_OTHER
    # Netgroups and domains are never addresses, and '@grp' would otherwise
    # fall through to a hostname guess.
    if text.startswith("@") or text.startswith("."):
        return CLIENT_OTHER
    try:
        ipaddress.ip_address(text)
        return CLIENT_HOST
    except ValueError:
        pass
    if "/" not in text:
        return CLIENT_OTHER              # a hostname
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return CLIENT_OTHER
    return CLIENT_HOST if network.num_addresses == 1 else CLIENT_NETWORK


def destination_rules(rules: Sequence[ExportRule]
                      ) -> Tuple[List[ExportRule], List[Tuple[int, str]]]:
    """Source rules -> the rules to create on PROD and DR.

    One rule per client match, every other parameter forced to
    DESTINATION_RULE. Returns those rules and the client matches that were
    dropped, as (source rule index, match), so the caller can report exactly
    what was left behind and why.

    The destination index is left unset: rules are created in order and
    ONTAP numbers them itself, so neither a gap in the source numbering nor
    the renumbering this split forces has to be reproduced.
    """
    carried: List[ExportRule] = []
    skipped: List[Tuple[int, str]] = []
    seen = set()

    for rule in rules:
        for match in rule.clients:
            match = (match or "").strip()
            if not match:
                continue
            if classify_client(match) == CLIENT_NETWORK:
                skipped.append((rule.index, match))
                continue
            # Two source rules naming the same client would otherwise give
            # the destination two identical rules, which ONTAP rejects as a
            # duplicate.
            if match in seen:
                continue
            seen.add(match)
            carried.append(ExportRule(clients=[match], **DESTINATION_RULE))
    return carried, skipped


def describe_skipped(skipped: Sequence[Tuple[int, str]]) -> str:
    """One line naming the networks that will not be carried over."""
    return ", ".join(
        f"{match} (source rule {index})" if index is not None else match
        for index, match in skipped)


def describe_forced() -> str:
    """One line stating the parameters every destination rule is given."""
    return (f"proto={'|'.join(DESTINATION_RULE['protocols'])} "
            f"ro={'|'.join(DESTINATION_RULE['ro_rule'])} "
            f"rw={'|'.join(DESTINATION_RULE['rw_rule'])} "
            f"superuser={'|'.join(DESTINATION_RULE['superuser'])} "
            f"anon={DESTINATION_RULE['anonymous_user']} "
            f"suid={str(DESTINATION_RULE['allow_suid']).lower()} "
            f"dev={str(DESTINATION_RULE['allow_device_creation']).lower()} "
            f"ntfs-unix={DESTINATION_RULE['ntfs_unix_security']} "
            f"chown={DESTINATION_RULE['chown_mode']}")
