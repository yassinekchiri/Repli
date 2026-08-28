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

from ..models import (VolumeInfo, AggregateInfo, SnapMirrorInfo, SvmInfo,
                      PeerInfo, ExportRule, ExportPolicyInfo, QtreeInfo,
                      QuotaRule)


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

    @abstractmethod
    def get_qtree_export_policy(self, cluster: str, svm: str, volume: str,
                                qtree: str) -> str:
        """Name of the export policy currently applied to a qtree.

        A qtree that was never exported explicitly reports the policy it
        inherits, usually the SVM's 'default'.
        """

    @abstractmethod
    def export_policy_exists(self, cluster: str, svm: str,
                             policy: str) -> bool:
        """Is this export policy defined on the SVM?"""

    @abstractmethod
    def get_export_policy_rules(self, cluster: str, svm: str,
                                policy: str) -> List[ExportRule]:
        """The rules of an export policy, in order.

        An empty list is a real answer, not a failure: a policy with no rule
        exists and denies every client.
        """

    @abstractmethod
    def create_export_policy(self, cluster: str, svm: str, policy: str,
                             rules: Optional[List[ExportRule]] = None) -> None:
        """Create an export policy, with the given rules or none at all.

        No rules is a deliberate, useful case: an empty policy denies every
        client, which is exactly what 'cut the source access' means. Passing
        rules is the other case — carrying a source qtree's clients over to
        the destination so they still reach their data after the migration.
        """

    @abstractmethod
    def delete_qtree(self, cluster: str, svm: str, volume: str,
                     qtree: str) -> None:
        """Delete a qtree AND ITS CONTENTS. Irreversible.

        Only used to prune the qtrees a FlexClone inherited from its parent
        volume but does not own. Every caller must have established first
        that the volume is detached from its parent and that the qtree is
        not the one the volume was created for.
        """

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

    # =====================================================================
    # READ-ONLY INTROSPECTION — used exclusively by the pre-flight checks.
    # These must never mutate anything and must not raise when the object
    # is simply absent: they answer "is this feasible?".
    # =====================================================================

    @abstractmethod
    def get_svm(self, cluster: str, svm: str) -> SvmInfo:
        """SVM state (exists / running / CIFS enabled). Never raises when
        the SVM is absent: returns SvmInfo(exists=False)."""

    @abstractmethod
    def aggregate_exists(self, cluster: str, aggregate: str) -> bool:
        """Explicit existence check (get_aggregate_available cannot tell an
        absent aggregate from an unknown capacity)."""

    @abstractmethod
    def list_cluster_peers(self, cluster: str) -> List[PeerInfo]:
        """Cluster peering entries seen from `cluster`."""

    @abstractmethod
    def list_svm_peers(self, cluster: str) -> List[PeerInfo]:
        """SVM peering entries seen from `cluster`."""

    @abstractmethod
    def snapmirror_policy_exists(self, cluster: str, policy: str) -> bool:
        """Whether the SnapMirror policy is visible to the API user."""

    @abstractmethod
    def schedule_exists(self, cluster: str, schedule: str) -> bool:
        """Whether the cron schedule is visible to the API user.

        Visibility matters as much as existence: ONTAP resolves referenced
        objects with the caller's permissions, so an object the role cannot
        read is rejected as 'not found' at creation time.
        """

    @abstractmethod
    def junction_path_exists(self, cluster: str, svm: str, path: str) -> bool:
        """Whether an absolute NAS path is reachable on the SVM (ACL target)."""

    # =====================================================================
    # READ-ONLY INVENTORY — what the destination actually looks like once a
    # clone has run. Reporting only: nothing here drives a migration, and no
    # caller may depend on these to decide whether an action is feasible.
    # =====================================================================

    @abstractmethod
    def list_qtree_details(self, cluster: str, svm: str,
                           volume: str) -> List[QtreeInfo]:
        """Every qtree of a volume with its id, path and export policy.

        Unlike list_qtrees, which answers 'which names are there', this one
        carries the handles an operator needs to find the object again.
        """

    @abstractmethod
    def get_export_policy(self, cluster: str, svm: str,
                          policy: str) -> Optional[ExportPolicyInfo]:
        """One export policy with its id and rules; None when absent."""

    @abstractmethod
    def list_quota_rules(self, cluster: str, svm: str,
                         volume: str) -> List[QuotaRule]:
        """Quota rules defined on a volume, with their limits.

        An empty list means no quota rule, which is a real answer: a volume
        with quotas switched on but no rule limits nothing.
        """

    @abstractmethod
    def get_quota_policy(self, cluster: str, svm: str) -> str:
        """Name of the SVM's active quota policy, '' when not exposed.

        The REST API applies quota rules to the SVM's active policy without
        naming it, so the REST transport legitimately answers '' — the
        inventory reports that as 'not exposed by REST' rather than as a
        missing policy.
        """
