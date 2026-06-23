#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netapp_cascade_migration.py
===========================

Orchestration tool for NetApp ONTAP storage migration using a cascading
topology ("pass-through" / cascading) across 3 tiers:

    Upper Tier (Sources)          ->  High-end production clusters
                                      (e.g. CMOPARPA4MUT100, CMOPARTIGMUT100)
                |
                |  SnapMirror (relationship 1)
                v
    Middle Tier (Pivot)           ->  Transit / staging cluster
                                      (CMOPARTIGBKP110)
                |
                |  SnapMirror (relationship 2)
                v
    Lower Tier (Destinations)     ->  Low-end target clusters
                                      (e.g. CMOPARPA4SFS100, CMOPARDC5SFS100)

Goal: migrate data while SPLITTING the storage structure. Source volumes
contain multiple Qtrees; at the final destination each Qtree must be
isolated in its own dedicated volume (rule: 1 Volume = 1 Qtree).

------------------------------------------------------------------------------
STRICT CONSTRAINTS IMPLEMENTED
------------------------------------------------------------------------------
* ONTAP CLI EXECUTION   : every action goes through standard ONTAP CLI
  commands executed over SSH.
* AUTHENTICATION        : no login/password management. Relies exclusively on
  native SSH key trust (key exchange already in place). The default SSH
  transport is the system native SSH client (via subprocess), which honours
  ~/.ssh/config and the SSH agent. A paramiko backend is also available
  (--ssh-backend paramiko) if paramiko is installed; it also uses system keys
  (no password).
* FULL TRACEABILITY     : every command sent to a cluster is logged exhaustively
  (date/time, target cluster, raw command, exit code, full stdout and stderr)
  in a dedicated log file.
* ERROR HANDLING        : stdout/stderr of every command is analysed. Any
  command that fails (exit code != 0) or whose output contains an ONTAP error
  pattern stops execution immediately, archives the error in the log, and
  raises a clean exception (OntapCliError).

------------------------------------------------------------------------------
CLI INTERFACE
------------------------------------------------------------------------------
Required arguments:
    --source-cluster   Name/IP of the source cluster (upper tier)
    --pivot-cluster    Name/IP of the pivot cluster (CMOPARTIGBKP110)
    --dest-cluster     Name/IP of the destination cluster (lower tier)
    --volume           Name of the source volume
    --action           'create' | 'clone' | 'cleanup'

Action-specific arguments:
    --qtrees           (clone)   comma-separated list 'q1,q2' OR keyword 'all'
    --qtree            (cleanup) single target Qtree

Additional technical arguments (required for real ONTAP commands,
provided with sensible defaults; adapt to your environment):
    --source-vserver / --pivot-vserver / --dest-vserver   SVM per tier
    --pivot-aggr / --dest-aggr                            Target aggregates
    --noaccess-policy                                     Restrictive export-policy
    --ssh-backend {subprocess,paramiko}                   SSH transport
    --ssh-user                                            optional user@host
    --log-file                                            log file path
    --dry-run                                             simulation (nothing executed)
    --timeout / --poll-interval                           SnapMirror polling

Examples:
    python3 netapp_cascade_migration.py \\
        --source-cluster CMOPARTIGMUT100 \\
        --pivot-cluster  CMOPARTIGBKP110 \\
        --dest-cluster   CMOPARPA4SFS100 \\
        --volume vol_prod_01 \\
        --action create \\
        --pivot-aggr aggr1_pivot --dest-aggr aggr1_dest

    python3 netapp_cascade_migration.py ... --action clone  --qtrees all
    python3 netapp_cascade_migration.py ... --action cleanup --qtree qtree_finance
"""

import argparse
import datetime
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


# =============================================================================
# 1. JOB FILE UTILITIES
# =============================================================================

_JOB_FILE_PREFIX = "netapp_migration_"


def _job_file_path(job_id: str) -> str:
    return os.path.join(os.getcwd(), f"{_JOB_FILE_PREFIX}{job_id}.json")


def _save_job(job_id: str, data: dict) -> str:
    """Write job data to a JSON file in the current directory. Returns the path."""
    path = _job_file_path(job_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def _load_job(job_id: str) -> dict:
    """Load and return job data from its JSON file. Raises FileNotFoundError if missing."""
    path = _job_file_path(job_id)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Job file not found: {path}\n"
            f"Make sure you are running the script from the same directory as the original run."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _generate_job_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


# =============================================================================
# 2. EXCEPTIONS AND DATA STRUCTURES
# =============================================================================

class OntapCliError(Exception):
    """Raised whenever an ONTAP CLI command fails.

    Carries full context (cluster, command, exit code, outputs) so that the
    error surfaced to the caller is immediately actionable.
    """

    def __init__(self, cluster: str, command: str, exit_code: int,
                 stdout: str, stderr: str, reason: str = ""):
        self.cluster = cluster
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.reason = reason
        message = (
            f"ONTAP command failed on '{cluster}' "
            f"(exit={exit_code}{', ' + reason if reason else ''}) : {command}"
        )
        super().__init__(message)


@dataclass
class CommandResult:
    """Structured result of an ONTAP CLI command executed over SSH."""
    cluster: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    started_at: datetime.datetime
    duration_s: float


# =============================================================================
# 2. ONTAP CLI COMMAND EXECUTOR (SSH + LOGGING + ERROR DETECTION)
# =============================================================================

# Typical error patterns returned by the ONTAP CLI even when the SSH exit code
# is 0 (ONTAP CLI often writes errors to stdout/stderr while keeping SSH exit
# code at 0). Kept intentionally strict.
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

# Allowed patterns (false positives): some legitimate outputs contain the word
# "error" without it being an actual error (e.g. empty "Last Transfer Error"
# column in snapmirror show). Neutralised before strict analysis.
ONTAP_BENIGN_PATTERNS = [
    re.compile(r"Last Transfer Error\s*:\s*-?\s*$", re.IGNORECASE | re.MULTILINE),
]


class OntapCliExecutor:
    """Wraps execution of an ONTAP CLI command on a cluster over SSH.

    - Relies on native SSH key trust (no password management).
    - Logs every command exhaustively via the provided logger.
    - Analyses stdout/stderr and exit code to detect errors.
    """

    def __init__(self, logger: logging.Logger, ssh_backend: str = "subprocess",
                 ssh_user: Optional[str] = None, dry_run: bool = False,
                 connect_timeout: int = 30):
        self.log = logger
        self.ssh_backend = ssh_backend
        self.ssh_user = ssh_user
        self.dry_run = dry_run
        self.connect_timeout = connect_timeout
        self._paramiko = None  # lazy load

        if ssh_backend == "paramiko":
            self._init_paramiko()

    # ---- Paramiko initialisation (optional) ------------------------------ #
    def _init_paramiko(self):
        try:
            import paramiko  # local import so paramiko is not a hard dependency
            self._paramiko = paramiko
        except ImportError as exc:
            raise RuntimeError(
                "SSH backend 'paramiko' requested but the paramiko module is "
                "not installed (pip install paramiko)."
            ) from exc

    # ---- SSH target construction ----------------------------------------- #
    def _ssh_target(self, cluster: str) -> str:
        """Returns 'user@cluster' if a user is provided, otherwise 'cluster'."""
        return f"{self.ssh_user}@{cluster}" if self.ssh_user else cluster

    # ---- Main execution -------------------------------------------------- #
    def run(self, cluster: str, command: str,
            allow_failure: bool = False) -> CommandResult:
        """Execute `command` on `cluster` over SSH and return a CommandResult.

        :param allow_failure: if True, do not raise on error (useful for
            tolerant discovery commands). The error is still logged.
        :raises OntapCliError: if the command fails and allow_failure=False.
        """
        started_at = datetime.datetime.now()
        t0 = time.monotonic()

        if self.dry_run:
            # In dry-run mode no cluster is contacted: only the intent is logged.
            duration = time.monotonic() - t0
            result = CommandResult(cluster, command, 0,
                                   "[DRY-RUN] command not executed", "",
                                   started_at, duration)
            self._trace(result, dry_run=True)
            return result

        if self.ssh_backend == "paramiko":
            exit_code, stdout, stderr = self._run_paramiko(cluster, command)
        else:
            exit_code, stdout, stderr = self._run_subprocess(cluster, command)

        duration = time.monotonic() - t0
        result = CommandResult(cluster, command, exit_code, stdout, stderr,
                               started_at, duration)
        self._trace(result)

        # Error detection: non-zero exit code OR error pattern in output.
        error_reason = self._detect_error(result)
        if error_reason and not allow_failure:
            # Explicitly archive the error then raise a clean exception.
            self.log.error("STOPPING: error detected (%s) on cluster %s.",
                           error_reason, cluster)
            raise OntapCliError(cluster, command, exit_code, stdout, stderr,
                                reason=error_reason)
        if error_reason and allow_failure:
            self.log.warning("Tolerated error (allow_failure) on %s: %s",
                             cluster, error_reason)

        return result

    # ---- subprocess backend (native SSH client) -------------------------- #
    def _run_subprocess(self, cluster: str, command: str):
        """Execute via the system SSH client (native key trust)."""
        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",               # never prompt interactively
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            self._ssh_target(cluster),
            command,                              # full ONTAP CLI command
        ]
        try:
            proc = subprocess.run(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as exc:
            raise RuntimeError("SSH client not found on the system.") from exc

    # ---- paramiko backend ----------------------------------------------- #
    def _run_paramiko(self, cluster: str, command: str):
        """Execute via paramiko using system SSH keys."""
        client = self._paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        try:
            # look_for_keys + agent: keys only, no password.
            client.connect(
                hostname=cluster,
                username=self.ssh_user,            # may be None -> current user
                timeout=self.connect_timeout,
                look_for_keys=True,
                allow_agent=True,
            )
            _stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return exit_code, out, err
        finally:
            client.close()

    # ---- Exhaustive traceability ----------------------------------------- #
    def _trace(self, r: CommandResult, dry_run: bool = False):
        """Log the command and its result in full."""
        banner = "DRY-RUN " if dry_run else ""
        self.log.info(
            "\n%s================ ONTAP CLI COMMAND ================\n"
            "%sDate/time    : %s\n"
            "%sTarget cluster: %s\n"
            "%sCommand      : %s\n"
            "%sExit code    : %s\n"
            "%sDuration (s) : %.2f\n"
            "%s---- STDOUT ----\n%s\n"
            "%s---- STDERR ----\n%s\n"
            "%s====================================================",
            banner,
            banner, r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            banner, r.cluster,
            banner, r.command,
            banner, r.exit_code,
            banner, r.duration_s,
            banner, r.stdout.rstrip() or "(empty)",
            banner, r.stderr.rstrip() or "(empty)",
            banner,
        )

    # ---- Error analysis -------------------------------------------------- #
    def _detect_error(self, r: CommandResult) -> Optional[str]:
        """Return an error reason string if detected, otherwise None."""
        if r.exit_code != 0:
            return f"non-zero exit code ({r.exit_code})"

        combined = f"{r.stdout}\n{r.stderr}"

        # Strip known false positives before strict pattern matching.
        for benign in ONTAP_BENIGN_PATTERNS:
            combined = benign.sub("", combined)

        for pattern in ONTAP_ERROR_PATTERNS:
            if pattern.search(combined):
                return f"ONTAP error pattern matched ({pattern.pattern})"
        return None


# =============================================================================
# 3. ONTAP OUTPUT PARSING UTILITIES
# =============================================================================

def parse_qtree_list(stdout: str) -> List[str]:
    """Extract Qtree names from 'volume qtree show -instance' output.

    In -instance mode each Qtree is described as a key/value block, e.g.:

                            Vserver Name: svm1
                             Volume Name: vol_prod_01
                              Qtree Name: qtree_finance
                                   ...

    The ONTAP CLI always exposes a default Qtree (volume root, empty name / '-')
    which is ignored.
    """
    qtrees: List[str] = []
    # One "Qtree Name : <value>" occurrence per described Qtree.
    pattern = re.compile(r"^\s*Qtree Name\s*:\s*(.*?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    for m in pattern.finditer(stdout):
        qtree_name = m.group(1).strip().strip('"')
        # Ignore the root Qtree (empty) represented as "" or '-'.
        if qtree_name and qtree_name not in ('""', "-"):
            qtrees.append(qtree_name)
    return qtrees


def parse_field_value(stdout: str, field_name: str) -> Optional[str]:
    """Extract a single value from an ONTAP -instance output (key: value format)."""
    pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*:\s*(.+?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    m = pattern.search(stdout)
    return m.group(1).strip() if m else None


def parse_instance(stdout: str) -> dict:
    """Parse an ONTAP -instance output (single object) into a {key: value} dict.

    All 'Field : value' pairs are collected in a single pass, avoiding the need
    to re-run a command (and open a connection) for each field. Keys are
    normalised to lowercase for case-insensitive lookups.
    """
    fields: dict = {}
    for line in stdout.splitlines():
        m = re.match(r"^\s*(.+?)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        value = m.group(2).strip()
        fields[key] = value
    return fields


def get_instance_field(fields: dict, *names: str) -> Optional[str]:
    """Read a field from a parse_instance dict, with alternative names.

    Returns the first non-empty (not '' or '-') value found among `names`
    (case-insensitive comparison), or None if none match.
    """
    for name in names:
        value = fields.get(name.lower())
        if value not in (None, "", "-"):
            return value
    return None


def parse_size_to_bytes(size_str: str) -> Optional[int]:
    """Convert an ONTAP size string ('100GB', '1.5TB', '512MB', '2048') to bytes."""
    if not size_str:
        return None
    size_str = size_str.strip().upper().replace("B", "").replace("I", "")
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)$", size_str)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    return int(value * units.get(unit, 1))


def parse_cifs_shares_for_path(stdout: str, qtree_path_fragment: str) -> List[str]:
    """Return CIFS share names whose path contains `qtree_path_fragment`.

    Expected input: output of 'vserver cifs share show -instance', i.e. one
    key/value block per share, e.g.:

                              Vserver: svm1
                           Share Name: finance_share
                                 Path: /vol_prod_01/qtree_finance
                                   ...

    Shares are split by block (separated by blank lines) so that each
    'Share Name' is correctly associated with its 'Path'.
    """
    shares: List[str] = []
    # Split into blocks (one share per block) on blank lines.
    for block in re.split(r"\n\s*\n", stdout):
        share_name = parse_field_value(block, "Share Name") or \
            parse_field_value(block, "share-name")
        path = parse_field_value(block, "Path") or \
            parse_field_value(block, "path")
        if share_name and path and qtree_path_fragment in path:
            shares.append(share_name)
    return shares


# =============================================================================
# 4. MIGRATION ORCHESTRATOR (BUSINESS LOGIC FOR THE 3 ACTIONS)
# =============================================================================

class MigrationOrchestrator:
    """Drives the Source -> Pivot -> Destination cascade via the ONTAP CLI."""

    def __init__(self, executor: OntapCliExecutor, args: argparse.Namespace):
        self.x = executor
        self.log = executor.log
        self.a = args  # shortcut to CLI arguments

        # Readable shortcuts to clusters / SVMs.
        self.src_cluster = args.source_cluster
        self.pivot_cluster = args.pivot_cluster
        self.dest_cluster = args.dest_cluster
        self.dr_cluster = args.dr_cluster
        self.volume = args.volume

        self.src_svm = args.source_vserver
        self.pivot_svm = args.pivot_vserver
        self.dest_svm = args.dest_vserver
        self.dr_svm = args.dr_vserver

    # ----- SnapMirror path helpers ---------------------------------------- #
    def _path(self, svm: str, volume: str) -> str:
        """Build a SnapMirror path 'svm:volume'."""
        return f"{svm}:{volume}"

    # =====================================================================
    # ACTION 1 : 'create' -> Cascade initialisation
    # =====================================================================
    def action_create(self):
        create_mode = getattr(self.a, "create_mode", "full")
        self.log.info("######## ACTION 'create' (mode=%s): cascade initialisation ########",
                      create_mode)

        # --- Generate job ID and persist all parameters -------------------
        job_id = _generate_job_id()
        pivot_dest_path = self._path(self.pivot_svm, self.volume)
        dest_dest_path  = self._path(self.dest_svm,  self.volume)
        dr_dest_path    = self._path(self.dr_svm,    self.volume)
        job_data = {
            "job_id":           job_id,
            "created_at":       datetime.datetime.now().isoformat(timespec="seconds"),
            "status":           "started",
            "create_mode":      create_mode,
            "pivot_dest_path":  pivot_dest_path,
            "dest_dest_path":   dest_dest_path,
            "dr_dest_path":     dr_dest_path,
            "params": {
                "source_cluster":   self.a.source_cluster,
                "pivot_cluster":    self.a.pivot_cluster,
                "dest_cluster":     self.a.dest_cluster,
                "dr_cluster":       self.a.dr_cluster,
                "volume":           self.a.volume,
                "source_vserver":   self.a.source_vserver,
                "pivot_vserver":    self.a.pivot_vserver,
                "dest_vserver":     self.a.dest_vserver,
                "dr_vserver":       self.a.dr_vserver,
                "pivot_aggr":       self.a.pivot_aggr,
                "dest_aggr":        self.a.dest_aggr,
                "dr_aggr":          self.a.dr_aggr,
                "noaccess_policy":  self.a.noaccess_policy,
                "ssh_backend":      self.a.ssh_backend,
                "ssh_user":         self.a.ssh_user,
                "log_file":         self.a.log_file,
                "timeout":          self.a.timeout,
                "poll_interval":    self.a.poll_interval,
                "dry_run":          self.x.dry_run,
            },
        }
        job_path = _save_job(job_id, job_data)
        self.log.info("Job file created: %s", job_path)
        self.log.info("Job ID: %s", job_id)

        # --- Step 0: retrieve source volume characteristics ---------------
        # Single ONTAP call: the full object is cached in a dict; each needed
        # field is then read from the dict (no redundant connections).
        src_info = self._get_volume_info(self.src_cluster, self.src_svm, self.volume)

        src_size = get_instance_field(src_info, "Volume Size", "size")
        self.log.info("Source volume '%s' size: %s", self.volume, src_size)
        src_bytes = parse_size_to_bytes(src_size) if src_size else None

        src_style = get_instance_field(src_info, "Security Style", "security-style")
        self.log.info("Source volume '%s' security style: %s",
                      self.volume, src_style or "unknown")

        # --- Step 1: check available space on Pivot, PROD and DR ----------
        self._check_aggregate_space(self.pivot_cluster, self.a.pivot_aggr, src_bytes)
        self._check_aggregate_space(self.dest_cluster,  self.a.dest_aggr,  src_bytes)
        self._check_aggregate_space(self.dr_cluster,    self.a.dr_aggr,    src_bytes)

        # --- Step 2: create DP volumes (Pivot, PROD, DR) ------------------
        # SnapMirror destination volumes are of type 'DP', created without
        # space reservation and with the same security style as the source.
        self._create_dp_volume(self.pivot_cluster, self.pivot_svm, self.volume,
                               self.a.pivot_aggr, src_size, src_style)
        self._create_dp_volume(self.dest_cluster, self.dest_svm, self.volume,
                               self.a.dest_aggr, src_size, src_style)
        self._create_dp_volume(self.dr_cluster, self.dr_svm, self.volume,
                               self.a.dr_aggr, src_size, src_style)

        # --- Step 3a: declare all three relationships (no transfer yet) ---
        # Source->Pivot, Pivot->PROD and Pivot->DR are all declared upfront
        # so they exist when their respective initialize is triggered.
        # No data transfer starts at this step.
        self._snapmirror_create(
            run_on=self.pivot_cluster,
            source_path=self._path(self.src_svm, self.volume),
            dest_path=pivot_dest_path,
        )
        self._snapmirror_create(
            run_on=self.dest_cluster,
            source_path=pivot_dest_path,
            dest_path=dest_dest_path,
        )
        self._snapmirror_create(
            run_on=self.dr_cluster,
            source_path=pivot_dest_path,
            dest_path=dr_dest_path,
        )

        # --- Step 3b: initialize Pivot ------------------------------------
        # Strict rule: the destination transfer must not start until the pivot
        # is fully synchronized (idle). Running both initializes concurrently
        # could overload the pivot or start from an incomplete source.
        self._snapmirror_initialize(run_on=self.pivot_cluster,
                                    dest_path=pivot_dest_path)

        if create_mode == "pivot-only":
            # Save status and exit — the user will resume via --action resume.
            job_data["status"] = "pivot_initialized"
            _save_job(job_id, job_data)
            self.log.info("Pivot SnapMirror initialize launched. Script exiting (pivot-only mode).")
            self.log.info("=" * 64)
            self.log.info("KEEP YOUR JOB ID TO RESUME LATER: %s", job_id)
            self.log.info("Resume command:")
            self.log.info("  python3 %s --action resume --job-id %s",
                          os.path.basename(sys.argv[0]), job_id)
            self.log.info("=" * 64)
            return

        # --- Step 3c (full mode): wait for Pivot then initialize PROD + DR --
        # Both PROD and DR initialize are launched back-to-back (Y fan-out)
        # so the pivot streams to both destinations simultaneously.
        self._wait_snapmirror_ready(self.pivot_cluster, pivot_dest_path)

        self._snapmirror_initialize(run_on=self.dest_cluster,
                                    dest_path=dest_dest_path)
        self._snapmirror_initialize(run_on=self.dr_cluster,
                                    dest_path=dr_dest_path)
        self._wait_snapmirror_ready(self.dest_cluster, dest_dest_path)
        self._wait_snapmirror_ready(self.dr_cluster,   dr_dest_path)

        job_data["status"] = "completed"
        _save_job(job_id, job_data)
        self.log.info("ACTION 'create' complete: cascade initialised and synchronised "
                      "(PROD + DR).")

    # =====================================================================
    # ACTION 'resume' -> Check pivot status and optionally replicate to dest
    # =====================================================================
    def action_resume(self, job_data: dict):
        job_id          = job_data["job_id"]
        pivot_dest_path = job_data["pivot_dest_path"]
        dest_dest_path  = job_data["dest_dest_path"]
        dr_dest_path    = job_data["dr_dest_path"]
        created_at      = job_data.get("created_at", "unknown")
        status          = job_data.get("status", "unknown")

        self.log.info("######## ACTION 'resume': job %s ########", job_id)
        self.log.info("Original job created at: %s | status: %s", created_at, status)
        self.log.info("Pivot: %s | PROD dest: %s | DR dest: %s",
                      pivot_dest_path, dest_dest_path, dr_dest_path)

        if status == "completed":
            self.log.info("Job %s is already marked as completed. Nothing to do.", job_id)
            return

        if status == "dest_initialized":
            self.log.info("Destination replication was already initialized for this job.")
            self.log.info("Use check-status to monitor progress:")
            self.log.info("  python3 %s --action check-status --job-id %s",
                          os.path.basename(sys.argv[0]), job_id)
            # Delegate to check-status so the user also gets the live ONTAP state.
            self.action_check_status(job_data)
            return

        # --- Single status check on the pivot (no polling loop) -----------
        r = self.x.run(self.pivot_cluster,
                       f"snapmirror show -destination-path {pivot_dest_path} -instance")
        sm_info = parse_instance(r.stdout)
        state  = (get_instance_field(sm_info, "Mirror State",
                                     "Relationship Status", "state") or "").lower()
        transferred = get_instance_field(sm_info, "Last Transfer Size",
                                         "Total Transfer Bytes") or "unknown"
        self.log.info("Pivot replication status: '%s' | last transfer size: %s",
                      state or "unknown", transferred)

        if state not in ("snapmirrored", "idle"):
            self.log.info("Pivot replication is not yet complete (state='%s').",
                          state or "unknown")
            self.log.info("Re-run this command later to check again:")
            self.log.info("  python3 %s --action resume --job-id %s",
                          os.path.basename(sys.argv[0]), job_id)
            return

        # --- Pivot is idle: ask the user whether to proceed ---------------
        self.log.info("Pivot replication is complete.")
        try:
            answer = input("Proceed with destination replication? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"

        if answer != "y":
            self.log.info("User chose not to proceed. Exiting. Job ID: %s", job_id)
            return

        # --- Initialize PROD and DR simultaneously then EXIT immediately ---
        # Both initialize are launched back-to-back (Y fan-out from pivot).
        # We never block on long transfers: use check-status to monitor.
        self._snapmirror_initialize(run_on=self.dest_cluster,
                                    dest_path=dest_dest_path)
        self._snapmirror_initialize(run_on=self.dr_cluster,
                                    dest_path=dr_dest_path)
        job_data["status"] = "dest_initialized"
        _save_job(job_id, job_data)
        self.log.info("PROD and DR SnapMirror initializes launched. Script exiting.")
        self.log.info("=" * 64)
        self.log.info("Check replication progress at any time:")
        self.log.info("  python3 %s --action check-status --job-id %s",
                      os.path.basename(sys.argv[0]), job_id)
        self.log.info("=" * 64)

    # =====================================================================
    # ACTION 'check-status' -> Report current replication progress
    # =====================================================================
    def action_check_status(self, job_data: dict):
        job_id          = job_data["job_id"]
        pivot_dest_path = job_data["pivot_dest_path"]
        dest_dest_path  = job_data["dest_dest_path"]
        created_at      = job_data.get("created_at", "unknown")
        status          = job_data.get("status", "unknown")
        script          = os.path.basename(sys.argv[0])

        self.log.info("######## ACTION 'check-status': job %s ########", job_id)
        self.log.info("Created at: %s | Current status: %s", created_at, status)

        if status == "completed":
            self.log.info("Job %s is already completed. Nothing to do.", job_id)
            return

        if status in ("started", "pivot_initialized"):
            # Check pivot replication.
            self.log.info("Checking pivot replication (%s) ...", pivot_dest_path)
            r = self.x.run(self.pivot_cluster,
                           f"snapmirror show -destination-path {pivot_dest_path} -instance")
            sm_info     = parse_instance(r.stdout)
            state       = (get_instance_field(sm_info, "Mirror State",
                                              "Relationship Status", "state") or "").lower()
            transferred = get_instance_field(sm_info, "Last Transfer Size",
                                             "Total Transfer Bytes") or "unknown"
            progress    = get_instance_field(sm_info, "Last Transfer Duration",
                                             "Transfer Progress") or "unknown"
            self.log.info("Pivot state: '%s' | transferred: %s | progress: %s",
                          state or "unknown", transferred, progress)

            if state in ("snapmirrored", "idle"):
                self.log.info("Pivot replication is complete.")
                self.log.info("Run the following command to start destination replication:")
                self.log.info("  python3 %s --action resume --job-id %s", script, job_id)
            else:
                self.log.info("Pivot replication still in progress. Check again later:")
                self.log.info("  python3 %s --action check-status --job-id %s", script, job_id)
            return

        if status == "dest_initialized":
            dr_dest_path = job_data.get("dr_dest_path", "")

            # --- Check PROD destination ---
            self.log.info("Checking PROD destination replication (%s) ...", dest_dest_path)
            r_prod = self.x.run(self.dest_cluster,
                                f"snapmirror show -destination-path {dest_dest_path} -instance")
            prod_info = parse_instance(r_prod.stdout)
            prod_state = (get_instance_field(prod_info, "Mirror State",
                                             "Relationship Status", "state") or "").lower()
            prod_xfer  = get_instance_field(prod_info, "Last Transfer Size",
                                            "Total Transfer Bytes") or "unknown"
            prod_prog  = get_instance_field(prod_info, "Last Transfer Duration",
                                            "Transfer Progress") or "unknown"
            self.log.info("PROD state: '%s' | transferred: %s | progress: %s",
                          prod_state or "unknown", prod_xfer, prod_prog)

            # --- Check DR destination ---
            dr_state = "unknown"
            if dr_dest_path:
                self.log.info("Checking DR destination replication (%s) ...", dr_dest_path)
                r_dr = self.x.run(self.dr_cluster,
                                  f"snapmirror show -destination-path {dr_dest_path} -instance")
                dr_info  = parse_instance(r_dr.stdout)
                dr_state = (get_instance_field(dr_info, "Mirror State",
                                               "Relationship Status", "state") or "").lower()
                dr_xfer  = get_instance_field(dr_info, "Last Transfer Size",
                                              "Total Transfer Bytes") or "unknown"
                dr_prog  = get_instance_field(dr_info, "Last Transfer Duration",
                                              "Transfer Progress") or "unknown"
                self.log.info("DR state: '%s' | transferred: %s | progress: %s",
                              dr_state or "unknown", dr_xfer, dr_prog)

            prod_done = prod_state in ("snapmirrored", "idle")
            dr_done   = (not dr_dest_path) or dr_state in ("snapmirrored", "idle")

            if prod_done and dr_done:
                job_data["status"] = "completed"
                _save_job(job_id, job_data)
                self.log.info("Both PROD and DR replication complete. Job %s marked as completed.", job_id)
            else:
                pending = []
                if not prod_done:
                    pending.append(f"PROD ({prod_state or 'unknown'})")
                if not dr_done:
                    pending.append(f"DR ({dr_state or 'unknown'})")
                self.log.info("Replication still in progress: %s. Check again later:",
                              ", ".join(pending))
                self.log.info("  python3 %s --action check-status --job-id %s", script, job_id)
            return

        self.log.warning("Unrecognised job status '%s'. Manual inspection required.", status)

    # =====================================================================
    # ACTION 2 : 'clone' -> Qtree split & target volume creation
    # =====================================================================
    def action_clone(self):
        self.log.info("######## ACTION 'clone': Qtree split ########")

        # --- Step 1: determine the list of Qtrees to process --------------
        if self.a.qtrees.strip().lower() == "all":
            self.log.info("Mode 'all': discovering Qtrees from source volume.")
            qtrees = self._list_source_qtrees()
        else:
            qtrees = [q.strip() for q in self.a.qtrees.split(",") if q.strip()]

        if not qtrees:
            raise OntapCliError(self.src_cluster, "volume qtree show", 0, "", "",
                                reason="no Qtree to process")
        self.log.info("Qtrees to migrate (%d): %s", len(qtrees), ", ".join(qtrees))

        # The full Qtree list is needed for the split operation (deleting all
        # other Qtrees from each cloned volume).
        all_qtrees = self._list_source_qtrees()

        # --- Step 2: process each Qtree individually ----------------------
        for qtree in qtrees:
            self._clone_single_qtree(qtree, all_qtrees)

        self.log.info("ACTION 'clone' complete: %d target volume(s) created.",
                      len(qtrees))

    def _clone_single_qtree(self, qtree: str, all_qtrees: List[str]):
        """Process a single Qtree: snapshot, cascade update, verify, clone, split."""
        self.log.info("---- Processing Qtree '%s' ----", qtree)

        # Timestamped snapshot name for traceability and uniqueness.
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_name = f"migr_{qtree}_{stamp}"
        clone_volume = f"v_{qtree}"  # naming rule: v_[qtree_name]

        # a. Create a snapshot on the SOURCE cluster.
        self.x.run(self.src_cluster,
                   f"volume snapshot create -vserver {self.src_svm} "
                   f"-volume {self.volume} -snapshot {snap_name}")

        # b. Propagate the snapshot through the cascade (Source->Pivot, then Pivot->Dest).
        self.x.run(self.pivot_cluster,
                   f"snapmirror update "
                   f"-destination-path {self._path(self.pivot_svm, self.volume)}")
        self._wait_snapmirror_idle(self.pivot_cluster,
                                   self._path(self.pivot_svm, self.volume))

        self.x.run(self.dest_cluster,
                   f"snapmirror update "
                   f"-destination-path {self._path(self.dest_svm, self.volume)}")
        self._wait_snapmirror_idle(self.dest_cluster,
                                   self._path(self.dest_svm, self.volume))

        # c. Verify the snapshot has reached the DESTINATION.
        self._verify_snapshot_present(self.dest_cluster, self.dest_svm,
                                      self.volume, snap_name)

        # d. Create the FlexClone on the DESTINATION targeting this exact snapshot.
        self.x.run(self.dest_cluster,
                   f"volume clone create -vserver {self.dest_svm} "
                   f"-flexclone {clone_volume} -parent-volume {self.volume} "
                   f"-parent-snapshot {snap_name} -junction-active true")
        self.log.info("FlexClone '%s' created from snapshot '%s'.",
                      clone_volume, snap_name)

        # e. SPLIT operation: delete all OTHER Qtrees from the clone so that
        #    only the target Qtree remains (1 Volume = 1 Qtree rule).
        others = [q for q in all_qtrees if q != qtree]
        for other in others:
            self.x.run(self.dest_cluster,
                       f"volume qtree delete -vserver {self.dest_svm} "
                       f"-volume {clone_volume} -qtree {other} -force true")
            self.log.info("Qtree '%s' deleted from clone '%s'.", other, clone_volume)

        self.log.info("Qtree '%s' isolated in volume '%s'.", qtree, clone_volume)

    # =====================================================================
    # ACTION 3 : 'cleanup' -> Source access cut-off
    # =====================================================================
    def action_cleanup(self):
        self.log.info("######## ACTION 'cleanup': source access cut-off ########")
        qtree = self.a.qtree

        # 1. Isolation: apply the restrictive export-policy to the source Qtree.
        self.x.run(self.src_cluster,
                   f"volume qtree modify -vserver {self.src_svm} "
                   f"-volume {self.volume} -qtree {qtree} "
                   f"-export-policy {self.a.noaccess_policy}")
        self.log.info("Export-policy '%s' applied to Qtree '%s'.",
                      self.a.noaccess_policy, qtree)

        # 2. CIFS cleanup: identify then delete shares pointing to this Qtree.
        shares = self._find_cifs_shares_for_qtree(qtree)
        if not shares:
            self.log.info("No CIFS share associated with Qtree '%s'.", qtree)
        for share in shares:
            self.x.run(self.src_cluster,
                       f"vserver cifs share delete -vserver {self.src_svm} "
                       f"-share-name {share}")
            self.log.info("CIFS share '%s' deleted.", share)

        # 3. Rename: append a dated suffix to mark the Qtree for deletion.
        today = datetime.datetime.now().strftime("%d_%m_%Y")
        new_name = f"{qtree}_tobedeleted_migratedtosfs_{today}"
        self.x.run(self.src_cluster,
                   f"volume qtree rename -vserver {self.src_svm} "
                   f"-volume {self.volume} -qtree {qtree} "
                   f"-new-qtree-name {new_name}")
        self.log.info("Source Qtree renamed: '%s' -> '%s'.", qtree, new_name)

        self.log.info("ACTION 'cleanup' complete for Qtree '%s'.", qtree)

    # =====================================================================
    # REUSABLE ONTAP PRIMITIVES
    # =====================================================================
    def _get_volume_info(self, cluster: str, svm: str, volume: str) -> dict:
        """Retrieve ALL volume attributes in a single ONTAP call.

        A single 'volume show -instance' is executed and the entire object is
        cached in a dict. Callers then read the desired field (size, security
        style, ...) via get_instance_field without opening extra connections.
        """
        r = self.x.run(cluster,
                       f"volume show -vserver {svm} -volume {volume} "
                       f"-instance")
        return parse_instance(r.stdout)

    def _check_aggregate_space(self, cluster: str, aggr: str,
                               required_bytes: Optional[int]):
        """Verify that the aggregate has enough free space for the source volume."""
        self.log.info("Checking space on %s / aggregate %s.", cluster, aggr)
        r = self.x.run(cluster,
                       f"storage aggregate show -aggregate {aggr} "
                       f"-instance")
        aggr_info = parse_instance(r.stdout)
        avail_str = get_instance_field(aggr_info, "Available Size", "availsize")
        avail_bytes = parse_size_to_bytes(avail_str) if avail_str else None

        if required_bytes is None or avail_bytes is None:
            self.log.warning(
                "Cannot accurately compare space (required=%s, available=%s). "
                "Manual verification recommended.", required_bytes, avail_bytes)
            return

        self.log.info("Aggregate %s: available=%d bytes, required=%d bytes.",
                      aggr, avail_bytes, required_bytes)
        if avail_bytes < required_bytes:
            raise OntapCliError(
                cluster, f"storage aggregate show -aggregate {aggr}", 0,
                r.stdout, r.stderr,
                reason=(f"insufficient space on aggregate {aggr} "
                        f"(available={avail_bytes} < required={required_bytes})"))
        self.log.info("Sufficient space on aggregate %s.", aggr)

    def _create_dp_volume(self, cluster: str, svm: str, volume: str,
                          aggr: str, size: Optional[str],
                          security_style: Optional[str] = None):
        """Create a DP-type volume (SnapMirror destination) mirroring the source.

        The volume is created without space reservation (-space-guarantee none)
        and, if known, with the same security style as the source volume.
        """
        size_opt = f"-size {size} " if size else ""
        style_opt = f"-security-style {security_style} " if security_style else ""
        self.x.run(cluster,
                   f"volume create -vserver {svm} -volume {volume} "
                   f"-aggregate {aggr} {size_opt}{style_opt}"
                   f"-space-guarantee none -type DP")
        self.log.info("DP volume '%s' created on %s (aggregate %s, size %s, "
                      "security-style %s, space-guarantee none).",
                      volume, cluster, aggr, size or "auto",
                      security_style or "default")

    def _snapmirror_create(self, run_on: str, source_path: str, dest_path: str):
        """Declare an XDP SnapMirror relationship (no transfer triggered)."""
        self.log.info("SnapMirror create %s -> %s (on %s).",
                      source_path, dest_path, run_on)
        self.x.run(run_on,
                   f"snapmirror create -source-path {source_path} "
                   f"-destination-path {dest_path} -type XDP "
                   f"-policy XDP_SG -schedule hourly -throttle unlimited")

    def _snapmirror_initialize(self, run_on: str, dest_path: str):
        """Trigger the baseline transfer (initialize) for a SnapMirror relationship."""
        self.log.info("SnapMirror initialize %s (on %s).", dest_path, run_on)
        self.x.run(run_on,
                   f"snapmirror initialize -destination-path {dest_path}")

    def _wait_snapmirror_ready(self, cluster: str, dest_path: str):
        """Wait until the relationship reaches 'Snapmirrored' state (init done)."""
        self.log.info("Waiting for 'Snapmirrored' state on %s ...", dest_path)
        if self.x.dry_run:
            self.log.info("[DRY-RUN] Relationship %s assumed 'Snapmirrored'.", dest_path)
            return
        deadline = time.monotonic() + self.a.timeout
        while True:
            r = self.x.run(cluster,
                           f"snapmirror show -destination-path {dest_path} "
                           f"-instance")
            sm_info = parse_instance(r.stdout)
            state = (get_instance_field(sm_info, "Mirror State",
                                        "Relationship Status", "state")
                     or "").lower()
            self.log.info("SnapMirror state %s: '%s'.", dest_path, state or "unknown")
            if state in ("snapmirrored", "idle"):
                self.log.info("Relationship %s ready (state='%s').", dest_path, state)
                return
            if time.monotonic() > deadline:
                raise OntapCliError(
                    cluster, f"snapmirror show -destination-path {dest_path}", 0,
                    r.stdout, r.stderr,
                    reason=(f"timeout ({self.a.timeout}s) waiting for "
                            f"'Snapmirrored' (current state='{state}')"))
            time.sleep(self.a.poll_interval)

    def _wait_snapmirror_idle(self, cluster: str, dest_path: str):
        """Wait for a transfer to complete (status 'Idle') after a snapmirror update."""
        self.log.info("Waiting for transfer to complete (Idle) on %s ...", dest_path)
        if self.x.dry_run:
            self.log.info("[DRY-RUN] Transfer %s assumed 'Idle'.", dest_path)
            return
        deadline = time.monotonic() + self.a.timeout
        while True:
            r = self.x.run(cluster,
                           f"snapmirror show -destination-path {dest_path} "
                           f"-instance")
            sm_info = parse_instance(r.stdout)
            status = (get_instance_field(sm_info, "Relationship Status", "status")
                      or "").lower()
            self.log.info("Transfer status %s: '%s'.", dest_path, status or "unknown")
            if status == "idle":
                return
            if time.monotonic() > deadline:
                raise OntapCliError(
                    cluster, f"snapmirror show -destination-path {dest_path}", 0,
                    r.stdout, r.stderr,
                    reason=(f"timeout ({self.a.timeout}s) waiting for 'Idle' "
                            f"(current status='{status}')"))
            time.sleep(self.a.poll_interval)

    def _list_source_qtrees(self) -> List[str]:
        """List Qtrees on the source volume via 'volume qtree show'."""
        r = self.x.run(self.src_cluster,
                       f"volume qtree show -vserver {self.src_svm} "
                       f"-volume {self.volume} -instance")
        return parse_qtree_list(r.stdout)

    def _verify_snapshot_present(self, cluster: str, svm: str, volume: str,
                                 snapshot: str):
        """Verify that the expected snapshot is present on the destination."""
        r = self.x.run(cluster,
                       f"volume snapshot show -vserver {svm} -volume {volume} "
                       f"-snapshot {snapshot} -instance")
        if self.x.dry_run:
            self.log.info("[DRY-RUN] Snapshot '%s' assumed present on %s.",
                          snapshot, cluster)
            return
        if snapshot not in r.stdout:
            raise OntapCliError(
                cluster,
                f"volume snapshot show -snapshot {snapshot}", 0,
                r.stdout, r.stderr,
                reason=f"snapshot '{snapshot}' not found on destination")
        self.log.info("Snapshot '%s' confirmed present on %s.", snapshot, cluster)

    def _find_cifs_shares_for_qtree(self, qtree: str) -> List[str]:
        """Identify CIFS shares pointing to the Qtree (matched by path)."""
        r = self.x.run(self.src_cluster,
                       f"vserver cifs share show -vserver {self.src_svm} "
                       f"-instance", allow_failure=True)
        # Match the path fragment for this Qtree, e.g. '/vol_prod_01/qtree_finance'.
        fragment = f"/{qtree}"
        return parse_cifs_shares_for_path(r.stdout, fragment)


# =============================================================================
# 5. LOGGING SETUP (EXHAUSTIVE TRACEABILITY)
# =============================================================================

def setup_logging(log_file: str) -> logging.Logger:
    """Configure a logger writing to both a dedicated file and the console."""
    logger = logging.getLogger("netapp_cascade_migration")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # avoid duplicate handlers on re-call

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler: exhaustive and persistent trace.
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler: real-time operator feedback.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


# =============================================================================
# 6. CLI ARGUMENT PARSING
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NetApp ONTAP cascading migration orchestration "
                    "(Source -> Pivot -> Destination) with Qtree splitting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Required arguments ---
    parser.add_argument("--source-cluster",
                        help="Name/IP of the source cluster (upper tier). "
                             "Not required for --action resume.")
    parser.add_argument("--pivot-cluster",
                        help="Name/IP of the pivot cluster (CMOPARTIGBKP110). "
                             "Not required for --action resume.")
    parser.add_argument("--dest-cluster",
                        help="Name/IP of the destination cluster (lower tier). "
                             "Not required for --action resume.")
    parser.add_argument("--volume",
                        help="Name of the source volume. "
                             "Not required for --action resume.")
    parser.add_argument("--action", required=True,
                        choices=["create", "clone", "cleanup", "resume", "check-status"],
                        help="Action to execute.")

    # --- Action-specific arguments ---
    parser.add_argument("--qtrees",
                        help="(clone) comma-separated list 'q1,q2' OR keyword 'all'.")
    parser.add_argument("--qtree",
                        help="(cleanup) single target Qtree.")
    parser.add_argument("--create-mode", choices=["full", "pivot-only"], default="full",
                        help="(create) 'full': initialize pivot, wait, then initialize "
                             "destination. 'pivot-only': initialize pivot and exit "
                             "immediately; use --action resume --job-id <ID> to continue "
                             "(default: full).")
    parser.add_argument("--job-id",
                        help="(resume) Job ID returned by a previous --create-mode "
                             "pivot-only run.")

    # --- Additional technical arguments (required for real ONTAP commands) ---
    parser.add_argument("--source-vserver", default="svm_source",
                        help="SVM/vserver on the source side (default: svm_source).")
    parser.add_argument("--pivot-vserver", default="svm_pivot",
                        help="SVM/vserver on the pivot side (default: svm_pivot).")
    parser.add_argument("--dest-vserver", default="svm_dest",
                        help="SVM/vserver on the destination side (default: svm_dest).")
    parser.add_argument("--dr-cluster",
                        help="Name/IP of the DR cluster (Y fan-out, second destination). "
                             "Required for --action create.")
    parser.add_argument("--dr-vserver", default="svm_dr",
                        help="SVM/vserver on the DR side (default: svm_dr).")
    parser.add_argument("--pivot-aggr", default="aggr1_pivot",
                        help="Target aggregate on the pivot (default: aggr1_pivot).")
    parser.add_argument("--dest-aggr", default="aggr1_dest",
                        help="Target aggregate on the destination (default: aggr1_dest).")
    parser.add_argument("--dr-aggr", default="aggr1_dr",
                        help="Target aggregate on the DR cluster (default: aggr1_dr).")
    parser.add_argument("--noaccess-policy", default="ep_noaccess",
                        help="Restrictive export-policy name (default: ep_noaccess).")

    # --- SSH transport / traceability / execution safety ---
    parser.add_argument("--ssh-backend", choices=["subprocess", "paramiko"],
                        default="subprocess",
                        help="SSH transport (default: subprocess = native ssh client).")
    parser.add_argument("--ssh-user", default=None,
                        help="Optional SSH user (user@cluster). "
                             "Defaults to current user / SSH config.")
    parser.add_argument("--log-file", default=None,
                        help="Log file path "
                             "(default: migration_<action>_<timestamp>.log).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulation mode: log commands without executing them.")

    # --- SnapMirror polling parameters ---
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Timeout (s) waiting for SnapMirror states (default: 3600).")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Interval (s) between 'snapmirror show' polls (default: 30).")

    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser):
    """Validate argument consistency for the requested action."""
    if args.action in ("resume", "check-status"):
        if not args.job_id:
            parser.error(f"--job-id is required for --action {args.action}.")
        return  # all other params come from the job file

    # All non-resume actions require cluster/volume identifiers.
    for flag in ("source_cluster", "pivot_cluster", "dest_cluster", "volume"):
        if not getattr(args, flag, None):
            parser.error(f"--{flag.replace('_', '-')} is required for --action {args.action}.")

    if args.action == "create" and not getattr(args, "dr_cluster", None):
        parser.error("--dr-cluster is required for --action create (Y fan-out topology).")

    if args.action == "clone" and not args.qtrees:
        parser.error("--qtrees is required for action 'clone' "
                     "(comma-separated list 'q1,q2' or 'all').")
    if args.action == "cleanup" and not args.qtree:
        parser.error("--qtree is required for action 'cleanup'.")


# =============================================================================
# 7. ENTRY POINT
# =============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    # ---- Resume / check-status: reconstruct everything from the job file --
    if args.action in ("resume", "check-status"):
        try:
            job_data = _load_job(args.job_id)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        # Reconstruct args namespace from the saved parameters.
        saved = job_data["params"]
        resume_args = argparse.Namespace(
            source_cluster  = saved["source_cluster"],
            pivot_cluster   = saved["pivot_cluster"],
            dest_cluster    = saved["dest_cluster"],
            dr_cluster      = saved.get("dr_cluster"),
            volume          = saved["volume"],
            source_vserver  = saved["source_vserver"],
            pivot_vserver   = saved["pivot_vserver"],
            dest_vserver    = saved["dest_vserver"],
            dr_vserver      = saved.get("dr_vserver", "svm_dr"),
            pivot_aggr      = saved["pivot_aggr"],
            dest_aggr       = saved["dest_aggr"],
            dr_aggr         = saved.get("dr_aggr", "aggr1_dr"),
            noaccess_policy = saved["noaccess_policy"],
            ssh_backend     = saved["ssh_backend"],
            ssh_user        = saved["ssh_user"],
            log_file        = saved.get("log_file"),
            timeout         = saved["timeout"],
            poll_interval   = saved["poll_interval"],
            dry_run         = saved.get("dry_run", False),
        )

        if not resume_args.log_file:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            resume_args.log_file = f"migration_resume_{args.job_id}_{stamp}.log"

        logger = setup_logging(resume_args.log_file)
        logger.info("================================================================")
        logger.info("Action '%s' for job %s", args.action, args.job_id)
        logger.info("Log file: %s", resume_args.log_file)
        logger.info("================================================================")

        executor = OntapCliExecutor(
            logger=logger,
            ssh_backend=resume_args.ssh_backend,
            ssh_user=resume_args.ssh_user,
            dry_run=resume_args.dry_run,
        )
        orchestrator = MigrationOrchestrator(executor, resume_args)

        try:
            if args.action == "resume":
                orchestrator.action_resume(job_data)
            else:
                orchestrator.action_check_status(job_data)
            logger.info("SUCCESS: action '%s' completed without error.", args.action)
            return 0
        except OntapCliError as exc:
            logger.error("ONTAP FAILURE: %s", exc)
            logger.error("Execution interrupted. Check the log: %s", resume_args.log_file)
            return 2
        except Exception as exc:
            logger.exception("UNEXPECTED FAILURE: %s", exc)
            return 3

    # ---- Normal actions --------------------------------------------------
    if not args.log_file:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.log_file = f"migration_{args.action}_{stamp}.log"

    logger = setup_logging(args.log_file)
    logger.info("================================================================")
    logger.info("Starting NetApp ONTAP orchestration - action '%s'.", args.action)
    logger.info("Source=%s | Pivot=%s | Destination=%s | Volume=%s",
                args.source_cluster, args.pivot_cluster,
                args.dest_cluster, args.volume)
    logger.info("Log file: %s", args.log_file)
    if args.dry_run:
        logger.info("DRY-RUN MODE ACTIVE: no command will actually be executed.")
    logger.info("================================================================")

    executor = OntapCliExecutor(
        logger=logger,
        ssh_backend=args.ssh_backend,
        ssh_user=args.ssh_user,
        dry_run=args.dry_run,
    )
    orchestrator = MigrationOrchestrator(executor, args)

    try:
        if args.action == "create":
            orchestrator.action_create()
        elif args.action == "clone":
            orchestrator.action_clone()
        elif args.action == "cleanup":
            orchestrator.action_cleanup()
        logger.info("SUCCESS: action '%s' completed without error.", args.action)
        return 0

    except OntapCliError as exc:
        # ONTAP business error: already logged exhaustively by the executor.
        logger.error("ONTAP FAILURE: %s", exc)
        logger.error("Execution interrupted. Check the log: %s", args.log_file)
        return 2
    except Exception as exc:  # safety net: any other error is still logged.
        logger.exception("UNEXPECTED FAILURE: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
