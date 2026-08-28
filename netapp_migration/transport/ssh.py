"""ONTAP CLI over SSH transport (legacy fallback, --transport ssh).

Behaviour is identical to the historical single-file script: native SSH key
trust (no password), full command/stdout/stderr tracing at DEBUG level, and
strict error-pattern analysis of the CLI output.
"""

import datetime
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

from ..models import (OntapError, VolumeInfo, AggregateInfo, SnapMirrorInfo,
                      SvmInfo, PeerInfo, ExportRule, ExportPolicyInfo,
                      QtreeInfo, QuotaRule)
from .base import OntapClient

# Typical error patterns returned by the ONTAP CLI even when the SSH exit
# code is 0 (ONTAP often writes errors to stdout while exiting 0).
ONTAP_ERROR_PATTERNS = [
    re.compile(r"^\s*Error:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\berror\b.*\bfailed\b", re.IGNORECASE),
    re.compile(r"\bcommand failed\b", re.IGNORECASE),
    re.compile(r"\bis not a recognized command\b", re.IGNORECASE),
    re.compile(r"\bnot authorized\b", re.IGNORECASE),
    re.compile(r"\bduplicate\b.*\bentry\b", re.IGNORECASE),
    re.compile(r"\bdoes not exist\b", re.IGNORECASE),
    re.compile(r"\binsufficient\b", re.IGNORECASE),
]

# Legitimate outputs containing the word "error" (neutralised before analysis).
ONTAP_BENIGN_PATTERNS = [
    re.compile(r"Last Transfer Error\s*:\s*-?\s*$", re.IGNORECASE | re.MULTILINE),
]

_ALREADY_EXISTS_RE = re.compile(
    r"already exists|duplicate entry|entry already exists", re.IGNORECASE)


# =============================================================================
# CLI OUTPUT PARSERS
# =============================================================================

def parse_field_value(stdout: str, field_name: str) -> Optional[str]:
    pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*:\s*(.+?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    m = pattern.search(stdout)
    return m.group(1).strip() if m else None


def parse_instance(stdout: str) -> dict:
    """Parse an ONTAP -instance output into a lowercase {key: value} dict.

    A long value wraps onto the following line, which then carries no colon
    — an export rule listing several clients does this routinely. Such a
    line continues the field above it, and is joined without a separator
    because ONTAP breaks after the comma of a list or mid-token, never
    between two tokens that need a space put back.
    """
    fields: dict = {}
    last_key = ""
    for line in stdout.splitlines():
        m = re.match(r"^\s*(.+?)\s*:\s*(.*?)\s*$", line)
        if m:
            last_key = m.group(1).strip().lower()
            fields[last_key] = m.group(2).strip()
            continue
        continuation = line.strip()
        if continuation and last_key:
            fields[last_key] += continuation
    return fields


def get_instance_field(fields: dict, *names: str) -> Optional[str]:
    for name in names:
        value = fields.get(name.lower())
        if value not in (None, "", "-"):
            return value
    return None


def parse_size_to_bytes(size_str: Optional[str]) -> Optional[int]:
    """'100GB' / '1.5TB' / '2048' -> bytes."""
    if not size_str:
        return None
    size_str = size_str.strip().upper().replace("B", "").replace("I", "")
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)$", size_str)
    if not m:
        return None
    return int(float(m.group(1)) * units.get(m.group(2), 1))


def _as_int(value: Optional[str]) -> Optional[int]:
    """'12345' -> 12345; '-' / 'unlimited' / anything else -> None."""
    return int(value) if value and value.isdigit() else None


def _as_bool(value: Optional[str]) -> Optional[bool]:
    """'true'/'false' -> bool; anything else -> None (the source did not say).

    None matters: it is the difference between 'the source set this to false'
    and 'the source did not report it', and only the first should be written
    to the destination.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("true", "yes", "enabled"):
        return True
    if text in ("false", "no", "disabled"):
        return False
    return None


def parse_qtree_list(stdout: str) -> List[str]:
    qtrees: List[str] = []
    pattern = re.compile(r"^\s*Qtree Name\s*:\s*(.*?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    for m in pattern.finditer(stdout):
        name = m.group(1).strip().strip('"')
        if name and name not in ('""', "-"):
            qtrees.append(name)
    return qtrees


def _split_list_field(value: Optional[str]) -> List[str]:
    """'sys, krb5' / 'any' / '-' -> a list of plain strings."""
    if not value or value.strip() in ("-", '""'):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_export_rules(stdout: str) -> List[ExportRule]:
    """Parse `vserver export-policy rule show -instance` into ExportRules.

    One -instance block per rule. Blocks that carry no rule index are
    headers or blank noise and are skipped.
    """
    rules: List[ExportRule] = []
    for block in re.split(r"\n\s*\n", stdout):
        fields = parse_instance(block)
        index = get_instance_field(fields, "rule index")
        if index is None:
            continue
        # ONTAP words this field differently across releases; 9.16 uses the
        # first form. Read as a list so a rename does not silently produce a
        # rule with no client at all.
        clients = get_instance_field(
            fields,
            "list of client match hostnames, ip addresses, netgroups, "
            "or domains",
            "client match hostname, ip address, netgroup, or domain",
            "client match hostnames, ip addresses, netgroups, or domains",
            "client match spec", "client match")
        rules.append(ExportRule(
            clients=_split_list_field(clients),
            ro_rule=_split_list_field(
                get_instance_field(fields, "ro access rule")),
            rw_rule=_split_list_field(
                get_instance_field(fields, "rw access rule")),
            superuser=_split_list_field(
                get_instance_field(fields, "superuser security types")),
            protocols=_split_list_field(
                get_instance_field(fields, "access protocol")),
            anonymous_user=get_instance_field(
                fields, "user id to which anonymous users are mapped") or "",
            allow_suid=_as_bool(get_instance_field(
                fields, "honor setuid bits in setattr")),
            allow_device_creation=_as_bool(get_instance_field(
                fields, "allow creation of devices")),
            # The 'Vserver ...' variants of the next two are SVM settings
            # shown for context, not rule fields — never read as one.
            ntfs_unix_security=get_instance_field(
                fields, "ntfs unix security options") or "",
            chown_mode=get_instance_field(
                fields, "change ownership mode") or "",
            index=int(index) if index.isdigit() else None))
    return rules


def parse_cifs_shares_for_path(stdout: str, fragment: str) -> List[str]:
    shares: List[str] = []
    for block in re.split(r"\n\s*\n", stdout):
        share = (parse_field_value(block, "Share Name")
                 or parse_field_value(block, "share-name"))
        path = (parse_field_value(block, "Path")
                or parse_field_value(block, "path"))
        if share and path and fragment in path:
            shares.append(share)
    return shares


def _yes_no(flag: bool) -> str:
    """The ONTAP CLI spells booleans 'true'/'false'."""
    return "true" if flag else "false"


@dataclass
class _CommandResult:
    cluster: str
    command: str
    exit_code: int
    stdout: str
    stderr: str


# =============================================================================
# SSH CLIENT
# =============================================================================

class SshClient(OntapClient):
    """OntapClient implementation over the ONTAP CLI via SSH."""

    def __init__(self, logger: logging.Logger, ssh_backend: str = "subprocess",
                 ssh_user: Optional[str] = None, connect_timeout: int = 30):
        self.log = logger
        self.ssh_backend = ssh_backend
        self.ssh_user = ssh_user
        self.connect_timeout = connect_timeout
        self._paramiko = None
        if ssh_backend == "paramiko":
            try:
                import paramiko
                self._paramiko = paramiko
            except ImportError as exc:
                raise RuntimeError(
                    "SSH backend 'paramiko' requested but paramiko is not "
                    "installed (pip install paramiko).") from exc

    # ------------------------------------------------------------------ #
    # Command execution + tracing + error analysis
    # ------------------------------------------------------------------ #
    def _run(self, cluster: str, command: str,
             allow_failure: bool = False) -> _CommandResult:
        started_at = datetime.datetime.now()
        t0 = time.monotonic()

        if self.ssh_backend == "paramiko":
            exit_code, stdout, stderr = self._run_paramiko(cluster, command)
        else:
            exit_code, stdout, stderr = self._run_subprocess(cluster, command)

        duration = time.monotonic() - t0
        result = _CommandResult(cluster, command, exit_code, stdout, stderr)
        self.log.debug(
            "\n================ ONTAP CLI COMMAND ================\n"
            "Date/time    : %s\nTarget cluster: %s\nCommand      : %s\n"
            "Exit code    : %s\nDuration (s) : %.2f\n"
            "---- STDOUT ----\n%s\n---- STDERR ----\n%s\n"
            "====================================================",
            started_at.strftime("%Y-%m-%d %H:%M:%S"), cluster, command,
            exit_code, duration,
            stdout.rstrip() or "(empty)", stderr.rstrip() or "(empty)")

        reason = self._detect_error(result)
        if reason and not allow_failure:
            raise OntapError(cluster, command,
                             f"{reason} | stdout: {stdout.strip()[:500]} "
                             f"| stderr: {stderr.strip()[:500]}")
        if reason and allow_failure:
            self.log.debug("Tolerated error on %s: %s", cluster, reason)
        return result

    def _run_subprocess(self, cluster: str, command: str):
        target = f"{self.ssh_user}@{cluster}" if self.ssh_user else cluster
        ssh_cmd = ["ssh", "-o", "BatchMode=yes",
                   "-o", f"ConnectTimeout={self.connect_timeout}",
                   "-o", "StrictHostKeyChecking=accept-new",
                   target, command]
        try:
            proc = subprocess.run(ssh_cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, check=False)
            return proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as exc:
            raise RuntimeError("SSH client not found on the system.") from exc

    def _run_paramiko(self, cluster: str, command: str):
        client = self._paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=cluster, username=self.ssh_user,
                           timeout=self.connect_timeout,
                           look_for_keys=True, allow_agent=True)
            _stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            return (exit_code,
                    stdout.read().decode("utf-8", errors="replace"),
                    stderr.read().decode("utf-8", errors="replace"))
        finally:
            client.close()

    @staticmethod
    def _detect_error(r: _CommandResult) -> Optional[str]:
        if r.exit_code != 0:
            return f"non-zero exit code ({r.exit_code})"
        combined = f"{r.stdout}\n{r.stderr}"
        for benign in ONTAP_BENIGN_PATTERNS:
            combined = benign.sub("", combined)
        for pattern in ONTAP_ERROR_PATTERNS:
            if pattern.search(combined):
                return f"ONTAP error pattern matched ({pattern.pattern})"
        return None

    @staticmethod
    def _already_exists(r: _CommandResult) -> bool:
        return bool(_ALREADY_EXISTS_RE.search(f"{r.stdout}\n{r.stderr}"))

    # ------------------------------------------------------------------ #
    # Volumes
    # ------------------------------------------------------------------ #
    def get_volume(self, cluster, svm, volume) -> VolumeInfo:
        r = self._run(cluster,
                      f"volume show -vserver {svm} -volume {volume} -instance")
        fields = parse_instance(r.stdout)
        return VolumeInfo(
            name=volume, svm=svm,
            size_bytes=parse_size_to_bytes(
                get_instance_field(fields, "Volume Size", "size")),
            security_style=get_instance_field(fields, "Security Style",
                                              "security-style"),
            aggregate=get_instance_field(fields, "Aggregate Name", "Aggregate",
                                         "aggregate"),
        )

    def volume_exists(self, cluster, svm, volume) -> bool:
        r = self._run(cluster,
                      f"volume show -vserver {svm} -volume {volume} -instance",
                      allow_failure=True)
        return self._detect_error(r) is None and volume in r.stdout

    def create_dp_volume(self, cluster, svm, volume, aggregate, size_bytes,
                         security_style, idempotent=False):
        size_opt = f"-size {size_bytes} " if size_bytes else ""
        style_opt = f"-security-style {security_style} " if security_style else ""
        cmd = (f"volume create -vserver {svm} -volume {volume} "
               f"-aggregate {aggregate} {size_opt}{style_opt}"
               f"-space-guarantee none -type DP")
        if idempotent:
            r = self._run(cluster, cmd, allow_failure=True)
            if self._already_exists(r):
                self.log.warning("DP volume '%s' already exists on %s — skipping.",
                                 volume, cluster)
                return
            reason = self._detect_error(r)
            if reason:
                raise OntapError(cluster, cmd, reason)
        else:
            self._run(cluster, cmd)

    def create_clone(self, cluster, svm, clone_name, parent_volume,
                     parent_snapshot):
        self._run(cluster,
                  f"volume clone create -vserver {svm} "
                  f"-flexclone {clone_name} -parent-volume {parent_volume} "
                  f"-parent-snapshot {parent_snapshot} -junction-active true "
                  f"-junction-path /{clone_name}")

    def start_volume_move(self, cluster, svm, volume, dest_aggregate):
        self._run(cluster,
                  f"volume move start -vserver {svm} -volume {volume} "
                  f"-destination-aggregate {dest_aggregate}")

    # ------------------------------------------------------------------ #
    # Aggregates
    # ------------------------------------------------------------------ #
    def get_aggregate_available(self, cluster, aggregate):
        r = self._run(cluster,
                      f"storage aggregate show -aggregate {aggregate} -instance")
        fields = parse_instance(r.stdout)
        return parse_size_to_bytes(
            get_instance_field(fields, "Available Size", "availsize"))

    def list_aggregates(self, cluster) -> List[AggregateInfo]:
        r = self._run(cluster, "storage aggregate show -instance")
        out: List[AggregateInfo] = []
        for block in re.split(r"\n\s*\n", r.stdout):
            fields = parse_instance(block)
            name = get_instance_field(fields, "Aggregate", "aggregate")
            if not name:
                continue
            avail = parse_size_to_bytes(
                get_instance_field(fields, "Available Size", "availsize")) or 0
            out.append(AggregateInfo(name=name, available_bytes=avail))
        return out

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    def create_snapshot(self, cluster, svm, volume, snapshot):
        self._run(cluster,
                  f"volume snapshot create -vserver {svm} "
                  f"-volume {volume} -snapshot {snapshot}")

    def snapshot_exists(self, cluster, svm, volume, snapshot) -> bool:
        r = self._run(cluster,
                      f"volume snapshot show -vserver {svm} -volume {volume} "
                      f"-snapshot {snapshot} -instance", allow_failure=True)
        return snapshot in r.stdout and self._detect_error(r) is None

    # ------------------------------------------------------------------ #
    # Qtrees
    # ------------------------------------------------------------------ #
    def list_qtrees(self, cluster, svm, volume) -> List[str]:
        r = self._run(cluster,
                      f"volume qtree show -vserver {svm} "
                      f"-volume {volume} -instance")
        return parse_qtree_list(r.stdout)

    def set_qtree_export_policy(self, cluster, svm, volume, qtree, policy):
        self._run(cluster,
                  f"volume qtree modify -vserver {svm} -volume {volume} "
                  f"-qtree {qtree} -export-policy {policy}")

    def get_qtree_export_policy(self, cluster, svm, volume, qtree) -> str:
        r = self._run(cluster,
                      f"volume qtree show -vserver {svm} -volume {volume} "
                      f"-qtree {qtree} -instance")
        fields = parse_instance(r.stdout)
        return get_instance_field(fields, "export policy",
                                  "export policy name") or ""

    def export_policy_exists(self, cluster, svm, policy) -> bool:
        r = self._run(cluster,
                      f"vserver export-policy show -vserver {svm} "
                      f"-policyname {policy}", allow_failure=True)
        if r.exit_code != 0:
            return False
        text = f"{r.stdout}{r.stderr}".lower()
        return "no entries matching" not in text and "does not exist" not in text

    def get_export_policy_rules(self, cluster, svm, policy) -> List[ExportRule]:
        """A policy with no rule prints 'no entries', which is not an error."""
        r = self._run(cluster,
                      f"vserver export-policy rule show -vserver {svm} "
                      f"-policyname {policy} -instance", allow_failure=True)
        if "no entries matching" in f"{r.stdout}{r.stderr}".lower():
            return []
        if r.exit_code != 0:
            raise OntapError(cluster, f"export policy '{policy}' on {svm}",
                             r.stderr.strip() or r.stdout.strip())
        return parse_export_rules(r.stdout)

    def create_export_policy(self, cluster, svm, policy, rules=None):
        """Create the policy, then add each rule in order.

        Unlike REST, the CLI has no way to create a policy and its rules in
        one command, so the policy exists ruleless — denying everyone — for
        as long as the rules take to add. That is the safe direction to fail
        in, and this only ever runs against a destination no client uses yet.
        """
        self._run(cluster,
                  f"vserver export-policy create -vserver {svm} "
                  f"-policyname {policy}")
        for position, rule in enumerate(rules or [], start=1):
            command = (f"vserver export-policy rule create -vserver {svm} "
                       f"-policyname {policy} -ruleindex {position} "
                       f"-clientmatch {','.join(rule.clients)} "
                       f"-rorule {','.join(rule.ro_rule) or 'any'} "
                       f"-rwrule {','.join(rule.rw_rule) or 'any'} "
                       f"-protocol {','.join(rule.protocols) or 'any'} "
                       f"-superuser {','.join(rule.superuser) or 'none'}")
            if rule.anonymous_user:
                command += f" -anon {rule.anonymous_user}"
            # Only what the source actually reported: an option left off
            # takes ONTAP's default, which is what the source rule had too.
            if rule.allow_suid is not None:
                command += f" -allow-suid {_yes_no(rule.allow_suid)}"
            if rule.allow_device_creation is not None:
                command += f" -allow-dev {_yes_no(rule.allow_device_creation)}"
            if rule.ntfs_unix_security:
                command += f" -ntfs-unix-security-ops {rule.ntfs_unix_security}"
            if rule.chown_mode:
                command += f" -chown-mode {rule.chown_mode}"
            self._run(cluster, command)

    def rename_qtree(self, cluster, svm, volume, qtree, new_name):
        self._run(cluster,
                  f"volume qtree rename -vserver {svm} -volume {volume} "
                  f"-qtree {qtree} -new-qtree-name {new_name}")

    def delete_qtree(self, cluster, svm, volume, qtree):
        """Delete a qtree and everything in it. Irreversible.

        -force is required: ONTAP refuses to delete a qtree that holds data.
        """
        self._run(cluster,
                  f"volume qtree delete -vserver {svm} -volume {volume} "
                  f"-qtree {qtree} -force true")

    # ------------------------------------------------------------------ #
    # CIFS shares
    # ------------------------------------------------------------------ #
    def find_cifs_shares(self, cluster, svm, path_fragment) -> List[str]:
        r = self._run(cluster,
                      f"vserver cifs share show -vserver {svm} -instance",
                      allow_failure=True)
        return parse_cifs_shares_for_path(r.stdout, path_fragment)

    def delete_cifs_share(self, cluster, svm, share):
        self._run(cluster,
                  f"vserver cifs share delete -vserver {svm} "
                  f"-share-name {share}")

    # ------------------------------------------------------------------ #
    # SnapMirror
    # ------------------------------------------------------------------ #
    def snapmirror_create(self, cluster, source_path, dest_path,
                          policy="MirrorAllSnapshots", schedule="hourly",
                          idempotent=False):
        cmd = (f"snapmirror create -source-path {source_path} "
               f"-destination-path {dest_path} -type XDP "
               f"-policy {policy} -schedule {schedule} -throttle unlimited")
        if idempotent:
            r = self._run(cluster, cmd, allow_failure=True)
            if self._already_exists(r):
                self.log.warning("SnapMirror %s -> %s already exists — skipping.",
                                 source_path, dest_path)
                return
            reason = self._detect_error(r)
            if reason:
                raise OntapError(cluster, cmd, reason)
        else:
            self._run(cluster, cmd)

    def snapmirror_initialize(self, cluster, dest_path):
        self._run(cluster,
                  f"snapmirror initialize -destination-path {dest_path}")

    def snapmirror_update(self, cluster, dest_path):
        self._run(cluster,
                  f"snapmirror update -destination-path {dest_path}")

    def snapmirror_resync(self, cluster, dest_path):
        self._run(cluster,
                  f"snapmirror resync -destination-path {dest_path}")

    def get_snapmirror(self, cluster, dest_path) -> SnapMirrorInfo:
        r = self._run(cluster,
                      f"snapmirror show -destination-path {dest_path} -instance",
                      allow_failure=True)
        fields = parse_instance(r.stdout)
        if not fields or self._detect_error(r):
            return SnapMirrorInfo(dest_path=dest_path, exists=False,
                                  state="absent", transfer_state="idle")
        state = (get_instance_field(fields, "Mirror State", "state")
                 or "unknown").lower()
        status = (get_instance_field(fields, "Relationship Status", "status")
                  or "unknown").lower()
        return SnapMirrorInfo(
            dest_path=dest_path,
            state=state,
            transfer_state="idle" if status == "idle" else "transferring",
            last_transfer_size=get_instance_field(
                fields, "Last Transfer Size", "Total Transfer Bytes") or "-",
        )

    # ------------------------------------------------------------------ #
    # File security (DACL forcing)
    # ------------------------------------------------------------------ #
    def apply_file_security(self, cluster, svm, path, groups, rights):
        """Full 'vserver security file-directory' sequence (5 CLI steps)."""
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        sd_name = f"sd_migr_{stamp}"
        policy_name = f"pol_migr_{stamp}"

        self._run(cluster,
                  f"vserver security file-directory ntfs create "
                  f"-vserver {svm} -ntfs-sd {sd_name}")
        for group in groups:
            self._run(cluster,
                      f"vserver security file-directory ntfs dacl add "
                      f"-vserver {svm} -ntfs-sd {sd_name} "
                      f"-access-type allow -account \"{group}\" "
                      f"-rights {rights} "
                      f"-apply-to this-folder,sub-folders,files")
        self._run(cluster,
                  f"vserver security file-directory policy create "
                  f"-vserver {svm} -policy-name {policy_name}")
        self._run(cluster,
                  f"vserver security file-directory policy task add "
                  f"-vserver {svm} -policy-name {policy_name} "
                  f"-path \"{path}\" -ntfs-sd {sd_name} "
                  f"-security-type ntfs -propagation-mode propagate")
        self._run(cluster,
                  f"vserver security file-directory policy apply "
                  f"-vserver {svm} -policy-name {policy_name}")

    # ================================================================== #
    # READ-ONLY INTROSPECTION (pre-flight checks)
    # ================================================================== #
    def get_svm(self, cluster, svm) -> SvmInfo:
        r = self._run(cluster, f"vserver show -vserver {svm} -instance",
                      allow_failure=True)
        fields = parse_instance(r.stdout)
        if not fields or self._detect_error(r):
            return SvmInfo(name=svm, exists=False, state="absent")
        state = (get_instance_field(fields, "Vserver Operational State",
                                    "Operational State", "state")
                 or "unknown").lower()
        allowed = (get_instance_field(fields, "Allowed Protocols") or "").lower()
        return SvmInfo(name=svm, exists=True, state=state,
                       cifs_enabled=("cifs" in allowed) if allowed else None)

    def aggregate_exists(self, cluster, aggregate) -> bool:
        r = self._run(cluster,
                      f"storage aggregate show -aggregate {aggregate} -instance",
                      allow_failure=True)
        return self._detect_error(r) is None and aggregate in r.stdout

    def list_cluster_peers(self, cluster) -> List[PeerInfo]:
        r = self._run(cluster, "cluster peer show -instance",
                      allow_failure=True)
        peers: List[PeerInfo] = []
        for block in re.split(r"\n\s*\n", r.stdout):
            fields = parse_instance(block)
            name = get_instance_field(fields, "Peer Cluster Name", "cluster")
            if not name:
                continue
            state = (get_instance_field(fields, "Availability of the Remote Cluster",
                                        "Availability") or "unknown").lower()
            peers.append(PeerInfo(name=name, state=state))
        return peers

    def list_svm_peers(self, cluster) -> List[PeerInfo]:
        r = self._run(cluster, "vserver peer show -instance", allow_failure=True)
        peers: List[PeerInfo] = []
        for block in re.split(r"\n\s*\n", r.stdout):
            fields = parse_instance(block)
            local = get_instance_field(fields, "Vserver", "vserver")
            remote = get_instance_field(fields, "Peer Vserver", "peer-vserver")
            if not (local and remote):
                continue
            peers.append(PeerInfo(
                name=remote,
                state=(get_instance_field(fields, "Peer State", "state")
                       or "unknown").lower(),
                local_svm=local, peer_svm=remote,
                peer_cluster=get_instance_field(fields, "Peer Cluster",
                                                "peer-cluster") or "",
            ))
        return peers

    def snapmirror_policy_exists(self, cluster, policy) -> bool:
        r = self._run(cluster, f"snapmirror policy show -policy {policy}",
                      allow_failure=True)
        return self._detect_error(r) is None and policy in r.stdout

    def schedule_exists(self, cluster, schedule) -> bool:
        r = self._run(cluster, f"job schedule show -name {schedule}",
                      allow_failure=True)
        return self._detect_error(r) is None and schedule in r.stdout

    def junction_path_exists(self, cluster, svm, path) -> bool:
        r = self._run(cluster,
                      f"volume show -vserver {svm} -fields volume,junction-path",
                      allow_failure=True)
        if self._detect_error(r):
            return False
        wanted = "/" + path.strip("/")
        for line in r.stdout.splitlines():
            for token in line.split():
                junction = token.rstrip("/")
                if junction.startswith("/") and (
                        wanted == junction or wanted.startswith(junction + "/")):
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Read-only inventory (reporting)
    # ------------------------------------------------------------------ #
    def list_qtree_details(self, cluster, svm, volume) -> List[QtreeInfo]:
        r = self._run(cluster,
                      f"volume qtree show -vserver {svm} "
                      f"-volume {volume} -instance")
        details = []
        for block in re.split(r"\n\s*\n", r.stdout):
            fields = parse_instance(block)
            name = get_instance_field(fields, "qtree name")
            if not name or name in ('""', "-"):
                continue
            qid = get_instance_field(fields, "qtree id")
            details.append(QtreeInfo(
                name=name.strip('"'),
                id=int(qid) if qid and qid.isdigit() else None,
                volume=volume,
                path=get_instance_field(fields, "qtree path") or "",
                export_policy=get_instance_field(fields, "export policy",
                                                 "export policy name") or "",
                security_style=get_instance_field(fields,
                                                  "security style") or ""))
        return details

    def get_export_policy(self, cluster, svm, policy) -> Optional[ExportPolicyInfo]:
        r = self._run(cluster,
                      f"vserver export-policy show -vserver {svm} "
                      f"-policyname {policy} -instance", allow_failure=True)
        text = f"{r.stdout}{r.stderr}".lower()
        if r.exit_code != 0 or "no entries matching" in text:
            return None
        fields = parse_instance(r.stdout)
        pid = get_instance_field(fields, "policy id")
        return ExportPolicyInfo(
            name=policy, svm=svm,
            id=int(pid) if pid and pid.isdigit() else None,
            rules=self.get_export_policy_rules(cluster, svm, policy))

    def list_quota_rules(self, cluster, svm, volume) -> List[QuotaRule]:
        """The CLI has no UUID for a quota rule: it is keyed by its target."""
        r = self._run(cluster,
                      f"volume quota policy rule show -vserver {svm} "
                      f"-volume {volume} -instance", allow_failure=True)
        if "no entries matching" in f"{r.stdout}{r.stderr}".lower():
            return []
        if self._detect_error(r):
            raise OntapError(cluster, f"quota rules of {svm}:{volume}",
                             r.stderr.strip() or r.stdout.strip())
        rules = []
        for block in re.split(r"\n\s*\n", r.stdout):
            fields = parse_instance(block)
            qtype = get_instance_field(fields, "type")
            if not qtype:
                continue
            rules.append(QuotaRule(
                type=qtype,
                qtree=get_instance_field(fields, "qtree name") or "",
                target=get_instance_field(fields, "target") or "",
                space_hard_limit=parse_size_to_bytes(
                    get_instance_field(fields, "disk limit")),
                space_soft_limit=parse_size_to_bytes(
                    get_instance_field(fields, "soft disk limit")),
                files_hard_limit=_as_int(
                    get_instance_field(fields, "files limit")),
                files_soft_limit=_as_int(
                    get_instance_field(fields, "soft files limit"))))
        return rules

    def get_quota_policy(self, cluster, svm) -> str:
        r = self._run(cluster,
                      f"vserver show -vserver {svm} -fields quota-policy",
                      allow_failure=True)
        if self._detect_error(r):
            return ""
        for line in r.stdout.splitlines():
            tokens = line.split()
            if len(tokens) == 2 and tokens[0] == svm:
                return tokens[1]
        return ""
