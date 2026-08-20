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
                      SvmInfo, PeerInfo)
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
    """Parse an ONTAP -instance output into a lowercase {key: value} dict."""
    fields: dict = {}
    for line in stdout.splitlines():
        m = re.match(r"^\s*(.+?)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        fields[m.group(1).strip().lower()] = m.group(2).strip()
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


def parse_qtree_list(stdout: str) -> List[str]:
    qtrees: List[str] = []
    pattern = re.compile(r"^\s*Qtree Name\s*:\s*(.*?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    for m in pattern.finditer(stdout):
        name = m.group(1).strip().strip('"')
        if name and name not in ('""', "-"):
            qtrees.append(name)
    return qtrees


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

    def export_policy_exists(self, cluster, svm, policy) -> bool:
        r = self._run(cluster,
                      f"vserver export-policy show -vserver {svm} "
                      f"-policyname {policy}", allow_failure=True)
        if r.exit_code != 0:
            return False
        text = f"{r.stdout}{r.stderr}".lower()
        return "no entries matching" not in text and "does not exist" not in text

    def create_export_policy(self, cluster, svm, policy):
        """Created with no rule: an empty policy denies every client."""
        self._run(cluster,
                  f"vserver export-policy create -vserver {svm} "
                  f"-policyname {policy}")

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
