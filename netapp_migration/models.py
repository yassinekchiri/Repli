"""Shared data models for the NetApp cascade migration tool.

These dataclasses are the contract between the three layers:

    interfaces (CLI / REST API)  ->  core engine  ->  transport (REST / SSH)

Nothing in here depends on argparse, FastAPI or any transport library, so
every layer can import this module without pulling extra dependencies.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional


# =============================================================================
# ERRORS
# =============================================================================

class OntapError(Exception):
    """Any failure while talking to an ONTAP cluster (REST or SSH).

    Carries enough context for the operator: target cluster, the operation
    attempted, and the underlying detail (HTTP body / CLI stderr).
    """

    def __init__(self, cluster: str, operation: str, detail: str = ""):
        self.cluster = cluster
        self.operation = operation
        self.detail = detail
        super().__init__(self.__str__())

    def __str__(self) -> str:
        msg = f"[{self.cluster}] {self.operation}"
        if self.detail:
            msg += f" -> {self.detail}"
        return msg


class ConfigError(Exception):
    """The tool is misconfigured: no credentials file, or an unusable one.

    Distinct from OntapError, which means a cluster answered badly. This one
    never reaches a cluster at all, so it must be reported as an operator
    problem with the file to fix — not as a storage failure.
    """

    def __init__(self, message: str, hint: str = "", path: str = ""):
        self.message = message
        self.hint = hint
        self.path = path
        super().__init__(message)


class ConfirmationRequired(Exception):
    """Raised by the engine when an action needs an explicit human go-ahead.

    The CLI catches it to prompt interactively; the REST API translates it
    into an HTTP 409 asking the caller to re-POST with {"confirm": true}.
    """


class PreflightFailed(Exception):
    """Raised when the pre-flight checks of an action did not pass.

    Carries the full report so that interfaces can render every individual
    check (CLI table / HTTP 422 body) instead of a single opaque message.
    No cluster mutation has taken place when this is raised.
    """

    def __init__(self, report: "PreflightReport"):
        self.report = report
        super().__init__(report.summary())


# =============================================================================
# PRE-FLIGHT CHECKS
# =============================================================================

SEVERITY_ERROR = "error"      # blocks the action
SEVERITY_WARNING = "warning"  # reported, does not block


@dataclass
class CheckResult:
    """Outcome of one individual feasibility check.

    code   : stable machine-readable identifier (for automation)
    title  : short human label of what was verified
    detail : what was actually observed on the cluster
    hint   : how the operator can make the check pass
    target : the object concerned, e.g. 'PIV110 / svm_pivot:vol_prod_01'
    """
    code: str
    title: str
    passed: bool
    severity: str = SEVERITY_ERROR
    detail: str = ""
    hint: str = ""
    target: str = ""

    @property
    def blocking(self) -> bool:
        return (not self.passed) and self.severity == SEVERITY_ERROR

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PreflightReport:
    """All checks run before an action, with the overall verdict.

    simulated=True means the checks ran against the dry-run transport: they
    are informational only and never block.
    """
    action: str
    checks: List[CheckResult] = field(default_factory=list)
    simulated: bool = False

    def add(self, check: CheckResult) -> CheckResult:
        self.checks.append(check)
        return check

    @property
    def failures(self) -> List[CheckResult]:
        return [c for c in self.checks if c.blocking]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks
                if (not c.passed) and c.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing blocks. A simulated report never blocks."""
        return self.simulated or not self.failures

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return (f"pre-flight for action '{self.action}': "
                    f"{len(self.checks)} check(s) passed")
        if self.ok:
            return (f"pre-flight for action '{self.action}': passed with "
                    f"{len(self.warnings)} warning(s)")
        codes = ", ".join(c.code for c in self.failures)
        return (f"pre-flight for action '{self.action}': "
                f"{len(self.failures)} check(s) failed ({codes})")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ok": self.ok,
            "simulated": self.simulated,
            "summary": self.summary(),
            "failed_count": len(self.failures),
            "warning_count": len(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
        }



# =============================================================================
# CREDENTIALS (REST transport, basic auth)
# =============================================================================

@dataclass
class ClusterCredentials:
    """Basic-auth credentials for one ONTAP cluster management interface."""
    username: str
    password: str
    verify_ssl: bool = True
    port: int = 443


# =============================================================================
# MIGRATION PARAMETERS
# =============================================================================

@dataclass
class MigrationParams:
    """Full parameter set of a migration (one source volume, Y fan-out).

    Serialised into the job file under the 'params' key, with the same key
    names as the historical single-file script so that existing job files
    keep working.
    """
    source_cluster:  str
    pivot_cluster:   str
    dest_cluster:    str
    dr_cluster:      str
    volume:          str
    source_vserver:  str = "svm_source"
    pivot_vserver:   str = "svm_pivot"
    dest_vserver:    str = "svm_dest"
    dr_vserver:      str = "svm_dr"
    pivot_aggr:      str = "aggr1_pivot"
    dest_aggr:       str = "aggr1_dest"
    dr_aggr:         str = "aggr1_dr"
    noaccess_policy: str = "ep_noaccess"
    timeout:         int = 3600
    poll_interval:   int = 30
    dry_run:         bool = False
    transport:       str = "rest"          # "rest" (default) or "ssh"
    ssh_backend:     str = "subprocess"    # ssh transport only
    ssh_user:        Optional[str] = None  # ssh transport only
    log_file:        Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MigrationParams":
        """Tolerant loader: unknown keys ignored, missing keys -> defaults.

        Keeps compatibility with job files written by older script versions
        (which had no 'transport' key, for instance).
        """
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def path(self, svm: str, volume: str) -> str:
        """SnapMirror path notation 'svm:volume'."""
        return f"{svm}:{volume}"


# =============================================================================
# NORMALISED ONTAP OBJECTS (returned by the transport layer)
# =============================================================================

@dataclass
class VolumeInfo:
    """Normalised view of a volume, identical for REST and SSH transports."""
    name: str
    svm: str
    size_bytes: Optional[int] = None
    security_style: Optional[str] = None
    aggregate: Optional[str] = None
    uuid: Optional[str] = None          # REST only
    # A FlexClone still attached to its parent. The volume move detaches it;
    # until then the clone shares blocks with the parent and pruning it frees
    # nothing. None means the transport could not tell.
    is_flexclone: Optional[bool] = None
    # ONTAP's volume-move state: 'success', 'failed', 'replicating',
    # 'cutover'... Empty when no move was ever run on this volume.
    move_state: str = ""


@dataclass
class AggregateInfo:
    name: str
    available_bytes: int = 0


@dataclass
class SvmInfo:
    """Normalised SVM (vserver) view, used by the pre-flight checks."""
    name: str
    exists: bool = True
    state: str = "unknown"          # 'running' | 'stopped' | ...
    cifs_enabled: Optional[bool] = None
    uuid: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self.exists and self.state == "running"


@dataclass
class PeerInfo:
    """Normalised peering entry (cluster peer or SVM peer)."""
    name: str                       # remote cluster (or remote SVM for SVM peers)
    state: str = "unknown"          # 'available' / 'peered' / 'unavailable' ...
    local_svm: str = ""             # SVM peers only
    peer_svm: str = ""              # SVM peers only
    peer_cluster: str = ""          # SVM peers only

    @property
    def is_usable(self) -> bool:
        return self.state in ("available", "peered", "ok")


# --- SnapMirror state vocabulary -------------------------------------------
# Explicit sets: anything not listed is 'unknown' and never treated as sane.

TRANSFER_IDLE = "idle"
TRANSFER_ACTIVE = frozenset({"queued", "preparing", "transferring",
                             "finalizing"})
TRANSFER_FAILED = frozenset({"failed", "aborted", "hard_aborted"})

MIRROR_HEALTHY = frozenset({"snapmirrored", "in_sync"})
MIRROR_UNINITIALIZED = frozenset({"uninitialized", "uninitialised"})
MIRROR_BROKEN = frozenset({"broken_off", "broken-off", "out_of_sync"})
MIRROR_ABSENT = "absent"


@dataclass
class SnapMirrorInfo:
    """Normalised SnapMirror relationship state.

    state          : mirror state — 'snapmirrored', 'in_sync',
                     'uninitialized', 'broken_off', 'out_of_sync',
                     'absent' (no relationship) or 'unknown'
    transfer_state : 'idle', 'queued', 'preparing', 'transferring',
                     'finalizing', 'failed', 'aborted' or 'unknown'

    Every predicate is deliberately strict: an absent, failed or unknown
    relationship is NEVER reported as idle or ready. Callers that need a
    reason to show the operator use `unhealthy_reason`.
    """
    dest_path: str
    state: str = "unknown"
    transfer_state: str = "unknown"
    last_transfer_size: str = "-"
    exists: bool = True
    last_error: str = ""

    # ---- transfer-level predicates ---------------------------------------
    @property
    def is_transferring(self) -> bool:
        return self.transfer_state in TRANSFER_ACTIVE

    @property
    def transfer_failed(self) -> bool:
        return self.transfer_state in TRANSFER_FAILED

    @property
    def is_idle(self) -> bool:
        """No transfer in flight AND the relationship is in a sane state.

        Requires the relationship to exist and its last transfer not to have
        failed: waiting for 'idle' must never be satisfied by an absent or
        broken relationship.
        """
        return (self.exists
                and self.transfer_state == TRANSFER_IDLE
                and not self.is_broken)

    # ---- mirror-level predicates -----------------------------------------
    @property
    def is_broken(self) -> bool:
        return self.state in MIRROR_BROKEN or self.transfer_failed

    @property
    def is_uninitialized(self) -> bool:
        return self.exists and self.state in MIRROR_UNINITIALIZED

    @property
    def is_mirrored(self) -> bool:
        return self.exists and self.state in MIRROR_HEALTHY

    @property
    def is_ready(self) -> bool:
        """Baseline done, data in place and nothing in flight."""
        return self.is_mirrored and self.transfer_state == TRANSFER_IDLE

    @property
    def unhealthy_reason(self) -> str:
        """Human explanation of why the relationship is not ready ('' if ok)."""
        if not self.exists:
            return "relationship does not exist"
        if self.transfer_failed:
            return (f"last transfer {self.transfer_state}"
                    + (f": {self.last_error}" if self.last_error else ""))
        if self.state in MIRROR_BROKEN:
            return f"mirror state '{self.state}'"
        if self.is_transferring:
            return f"transfer in progress ({self.transfer_state})"
        if self.is_uninitialized:
            return "relationship declared but never initialized"
        if not self.is_mirrored:
            return f"unrecognised mirror state '{self.state}'"
        return ""

    def describe(self) -> str:
        """One-line status for logs and API payloads."""
        if not self.exists:
            return "absent"
        return f"{self.state}/{self.transfer_state}"


# =============================================================================
# AUTHENTICATION / AUTHORISATION
# =============================================================================

class AuthError(Exception):
    """Authentication failure: no token, unknown token, or locked store."""

    def __init__(self, message: str, hint: str = ""):
        self.message = message
        self.hint = hint
        super().__init__(message)


class ForbiddenError(Exception):
    """The token is valid but its scope does not cover this request."""

    def __init__(self, message: str, hint: str = ""):
        self.message = message
        self.hint = hint
        super().__init__(message)


# Actions a scoped (per-qtree) token may be granted.
ACTIONS_QTREE_SCOPED = frozenset({"test", "clone", "acl", "cleanup",
                                  "prune"})
# Read-only actions, always grantable to a scoped token.
ACTIONS_READ = frozenset({"status", "preflight", "read"})
# Actions that act on the whole cascade: super-admin only, never delegated.
ACTIONS_SUPER_ONLY = frozenset({"create", "resume", "retry", "refresh",
                                "tokens"})
GRANTABLE_ACTIONS = ACTIONS_QTREE_SCOPED | ACTIONS_READ


@dataclass
class TokenScope:
    """What one scoped token is allowed to do, and on which qtrees."""
    token_id: str
    qtrees: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    label: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Principal:
    """The authenticated caller behind a request.

    A super admin bypasses every scope check; a scoped principal may only
    run the actions it was granted, on the qtrees it owns.
    """
    is_super_admin: bool = False
    token_id: str = ""
    qtrees: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    label: str = ""

    @property
    def name(self) -> str:
        return "super-admin" if self.is_super_admin else (
            self.label or self.token_id)

    def may(self, action: str) -> bool:
        if self.is_super_admin:
            return True
        return action in self.actions

    def owns(self, qtrees) -> bool:
        if self.is_super_admin:
            return True
        allowed = {q.lower() for q in self.qtrees}
        return all(q.strip().lower() in allowed for q in qtrees if q.strip())

    def authorise(self, action: str, qtrees=None) -> None:
        """Raise ForbiddenError unless the action is within scope."""
        if self.is_super_admin:
            return
        if action in ACTIONS_SUPER_ONLY:
            raise ForbiddenError(
                f"action '{action}' is reserved to the super admin",
                hint="ask the super admin to run it, or to widen this "
                     "token's scope (only qtree-level actions can be "
                     "delegated)")
        if not self.may(action):
            raise ForbiddenError(
                f"token '{self.name}' is not allowed to run '{action}'",
                hint=f"granted actions: {', '.join(sorted(self.actions)) or 'none'}")
        requested = [q for q in (qtrees or []) if q and q.strip()]
        if requested and not self.owns(requested):
            outside = sorted({q for q in requested
                              if q.strip().lower() not in
                              {o.lower() for o in self.qtrees}})
            raise ForbiddenError(
                f"token '{self.name}' has no access to qtree(s): "
                f"{', '.join(outside)}",
                hint=f"granted qtrees: {', '.join(sorted(self.qtrees)) or 'none'}")

    def to_dict(self) -> dict:
        return {"principal": self.name, "super_admin": self.is_super_admin,
                "token_id": self.token_id, "qtrees": sorted(self.qtrees),
                "actions": sorted(self.actions)}


SUPER_ADMIN = Principal(is_super_admin=True, token_id="super", label="super-admin")
