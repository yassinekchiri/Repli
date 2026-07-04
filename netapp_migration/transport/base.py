"""Abstract ONTAP client: the single contract the engine relies on.

Every operation is SEMANTIC (create_clone, snapmirror_update, ...) and
returns normalised objects from models.py — never raw text or raw JSON.
Two implementations exist:

    rest.py   : ONTAP REST API (default, basic auth)      — production
    ssh.py    : ONTAP CLI over SSH (legacy fallback)      — --transport ssh
    dryrun.py : no-op simulation returning canned values  — --dry-run
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import VolumeInfo, AggregateInfo, SnapMirrorInfo


class OntapClient(ABC):
    """Contract between the migration engine and the ONTAP clusters."""

    # ---- Volumes ---------------------------------------------------------
    @abstractmethod
    def get_volume(self, cluster: str, svm: str, volume: str) -> VolumeInfo:
        """Return the volume attributes; raise OntapError if not found."""

    @abstractmethod
    def volume_exists(self, cluster: str, svm: str, volume: str) -> bool:
        """Cheap existence check (no exception when absent)."""

    @abstractmethod
    def create_dp_volume(self, cluster: str, svm: str, volume: str,
                         aggregate: str, size_bytes: Optional[int],
                         security_style: Optional[str],
                         idempotent: bool = False) -> None:
        """Create a DP (SnapMirror destination) volume, no space guarantee.

        With idempotent=True an 'already exists' answer is treated as success.
        """

    @abstractmethod
    def create_clone(self, cluster: str, svm: str, clone_name: str,
                     parent_volume: str, parent_snapshot: str) -> None:
        """Create a FlexClone from a given parent snapshot, junction-mounted
        at /<clone_name>."""

    @abstractmethod
    def start_volume_move(self, cluster: str, svm: str, volume: str,
                          dest_aggregate: str) -> None:
        """Launch (fire-and-forget) a volume move to another aggregate."""

    # ---- Aggregates ------------------------------------------------------
    @abstractmethod
    def get_aggregate_available(self, cluster: str,
                                aggregate: str) -> Optional[int]:
        """Available bytes on one aggregate (None if unknown)."""

    @abstractmethod
    def list_aggregates(self, cluster: str) -> List[AggregateInfo]:
        """All data aggregates with their available space."""

    # ---- Snapshots -------------------------------------------------------
    @abstractmethod
    def create_snapshot(self, cluster: str, svm: str, volume: str,
                        snapshot: str) -> None: ...

    @abstractmethod
    def snapshot_exists(self, cluster: str, svm: str, volume: str,
                        snapshot: str) -> bool: ...

    # ---- Qtrees ----------------------------------------------------------
    @abstractmethod
    def list_qtrees(self, cluster: str, svm: str, volume: str) -> List[str]:
        """Qtree names of a volume (the default '' / '-' qtree excluded)."""

    @abstractmethod
    def set_qtree_export_policy(self, cluster: str, svm: str, volume: str,
                                qtree: str, policy: str) -> None: ...

    @abstractmethod
    def rename_qtree(self, cluster: str, svm: str, volume: str,
                     qtree: str, new_name: str) -> None: ...

    # ---- CIFS shares -----------------------------------------------------
    @abstractmethod
    def find_cifs_shares(self, cluster: str, svm: str,
                         path_fragment: str) -> List[str]:
        """Share names whose path contains the given fragment."""

    @abstractmethod
    def delete_cifs_share(self, cluster: str, svm: str, share: str) -> None: ...

    # ---- SnapMirror ------------------------------------------------------
    @abstractmethod
    def snapmirror_create(self, cluster: str, source_path: str,
                          dest_path: str, policy: str = "MirrorAllSnapshots",
                          schedule: str = "hourly",
                          idempotent: bool = False) -> None:
        """Declare the relationship (no transfer). Runs on the cluster
        hosting the DESTINATION volume."""

    @abstractmethod
    def snapmirror_initialize(self, cluster: str, dest_path: str) -> None:
        """Launch the baseline transfer (fire-and-forget)."""

    @abstractmethod
    def snapmirror_update(self, cluster: str, dest_path: str) -> None:
        """Launch an incremental transfer (fire-and-forget)."""

    @abstractmethod
    def snapmirror_resync(self, cluster: str, dest_path: str) -> None:
        """Resynchronise the relationship from the shared baseline."""

    @abstractmethod
    def get_snapmirror(self, cluster: str, dest_path: str) -> SnapMirrorInfo:
        """Live relationship state, normalised."""

    # ---- File security (DACL forcing) -------------------------------------
    @abstractmethod
    def apply_file_security(self, cluster: str, svm: str, path: str,
                            groups: List[str], rights: str) -> None:
        """Force AD-group ACLs on a whole tree, server-side.

        rights: 'no-access' | 'read' | 'write' | 'modify' | 'full-control'.
        Propagation: this folder + sub-folders + files, mode 'propagate'.
        """
