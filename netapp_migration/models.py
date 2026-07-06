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


class ConfirmationRequired(Exception):
    """Raised by the engine when an action needs an explicit human go-ahead.

    The CLI catches it to prompt interactively; the REST API translates it
    into an HTTP 409 asking the caller to re-POST with {"confirm": true}.
    """


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
    snapmirror_schedule: str = "hourly"   # cron schedule name; "none" = no schedule
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


@dataclass
class AggregateInfo:
    name: str
    available_bytes: int = 0


@dataclass
class SnapMirrorInfo:
    """Normalised SnapMirror relationship state.

    state          : 'snapmirrored' | 'uninitialized' | 'broken_off' | ...
    transfer_state : 'idle' | 'transferring'
    """
    dest_path: str
    state: str = "unknown"
    transfer_state: str = "unknown"
    last_transfer_size: str = "-"
    exists: bool = True

    @property
    def is_ready(self) -> bool:
        return self.state in ("snapmirrored", "idle", "in_sync")

    @property
    def is_idle(self) -> bool:
        return self.transfer_state != "transferring"
