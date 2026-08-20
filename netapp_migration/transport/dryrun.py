"""Dry-run transport: logs every intended operation, touches nothing.

Returns canned values shaped so that the engine walks its happy path:
snapmirror always idle/snapmirrored, snapshots always present, plenty of
aggregate space. Used with --dry-run whatever the selected transport.
"""

import logging
from typing import List

from ..models import (VolumeInfo, AggregateInfo, SnapMirrorInfo,
                      SvmInfo, PeerInfo)
from .base import OntapClient

_FAKE_SIZE = 1024 ** 3          # 1 GiB source volume
_FAKE_AVAILABLE = 100 * 1024 ** 4   # 100 TiB free everywhere


class DryRunClient(OntapClient):
    """OntapClient that only logs; no cluster is ever contacted."""

    def __init__(self, logger: logging.Logger):
        self.log = logger
        # Simulated inventory: objects this run has "created". Without it
        # every existence check would answer yes, and the pre-flight report
        # would claim that target names are already taken.
        self._volumes: set = set()
        self._snapshots: set = set()

    def _trace(self, cluster: str, operation: str):
        self.log.info("[DRY-RUN] %-12s %s", cluster, operation)

    # ---- Volumes ---------------------------------------------------------
    def get_volume(self, cluster, svm, volume) -> VolumeInfo:
        self._trace(cluster, f"volume show {svm}:{volume}")
        # Simulated as already split and moved, so a prune rehearsal is not
        # blocked by a guard that could never be satisfied in simulation.
        return VolumeInfo(name=volume, svm=svm, size_bytes=_FAKE_SIZE,
                          security_style="ntfs",
                          aggregate="aggr_dryrun_parent",
                          is_flexclone=False, move_state="success")

    def volume_exists(self, cluster, svm, volume) -> bool:
        found = (cluster, svm, volume) in self._volumes
        self._trace(cluster, f"volume exists? {svm}:{volume} -> "
                             f"{'yes' if found else 'no'}")
        return found

    def create_dp_volume(self, cluster, svm, volume, aggregate, size_bytes,
                         security_style, idempotent=False):
        self._trace(cluster, f"volume create {svm}:{volume} (DP, aggr={aggregate})")
        self._volumes.add((cluster, svm, volume))

    def create_clone(self, cluster, svm, clone_name, parent_volume,
                     parent_snapshot):
        self._trace(cluster, f"volume clone create {svm}:{clone_name} "
                             f"(parent={parent_volume}@{parent_snapshot})")
        self._volumes.add((cluster, svm, clone_name))

    def start_volume_move(self, cluster, svm, volume, dest_aggregate):
        self._trace(cluster, f"volume move start {svm}:{volume} -> {dest_aggregate}")

    # ---- Aggregates ------------------------------------------------------
    def get_aggregate_available(self, cluster, aggregate):
        self._trace(cluster, f"aggregate space {aggregate}")
        return _FAKE_AVAILABLE

    def list_aggregates(self, cluster) -> List[AggregateInfo]:
        self._trace(cluster, "aggregate list")
        return [AggregateInfo(name="aggr_dryrun", available_bytes=_FAKE_AVAILABLE)]

    # ---- Snapshots -------------------------------------------------------
    def create_snapshot(self, cluster, svm, volume, snapshot):
        self._trace(cluster, f"snapshot create {svm}:{volume}@{snapshot}")
        # A snapshot created on the source is assumed propagated everywhere.
        self._snapshots.add(snapshot)

    def snapshot_exists(self, cluster, svm, volume, snapshot) -> bool:
        found = snapshot in self._snapshots
        self._trace(cluster, f"snapshot exists? {svm}:{volume}@{snapshot} -> "
                             f"{'yes' if found else 'no'}")
        return found

    # ---- Qtrees ----------------------------------------------------------
    def list_qtrees(self, cluster, svm, volume) -> List[str]:
        self._trace(cluster, f"qtree list {svm}:{volume}")
        return ["qtree_dryrun1", "qtree_dryrun2"]

    def export_policy_exists(self, cluster, svm, policy) -> bool:
        self._trace(cluster, f"export-policy exists? {svm}:{policy} -> yes")
        return True

    def create_export_policy(self, cluster, svm, policy):
        self._trace(cluster, f"export-policy create {svm}:{policy} (no rules)")

    def set_qtree_export_policy(self, cluster, svm, volume, qtree, policy):
        self._trace(cluster, f"qtree modify {volume}/{qtree} export-policy={policy}")

    def rename_qtree(self, cluster, svm, volume, qtree, new_name):
        self._trace(cluster, f"qtree rename {volume}/{qtree} -> {new_name}")

    def delete_qtree(self, cluster, svm, volume, qtree):
        self._trace(cluster, f"qtree delete {volume}/{qtree} (force)")

    # ---- CIFS shares -----------------------------------------------------
    def find_cifs_shares(self, cluster, svm, path_fragment) -> List[str]:
        self._trace(cluster, f"cifs share lookup path contains '{path_fragment}'")
        return []

    def delete_cifs_share(self, cluster, svm, share):
        self._trace(cluster, f"cifs share delete {share}")

    # ---- SnapMirror ------------------------------------------------------
    def snapmirror_create(self, cluster, source_path, dest_path,
                          policy="MirrorAllSnapshots", schedule="hourly",
                          idempotent=False):
        self._trace(cluster, f"snapmirror create {source_path} -> {dest_path}")

    def snapmirror_initialize(self, cluster, dest_path):
        self._trace(cluster, f"snapmirror initialize {dest_path}")

    def snapmirror_update(self, cluster, dest_path):
        self._trace(cluster, f"snapmirror update {dest_path}")

    def snapmirror_resync(self, cluster, dest_path):
        self._trace(cluster, f"snapmirror resync {dest_path}")

    def get_snapmirror(self, cluster, dest_path) -> SnapMirrorInfo:
        self._trace(cluster, f"snapmirror show {dest_path} -> snapmirrored/idle")
        return SnapMirrorInfo(dest_path=dest_path, state="snapmirrored",
                              transfer_state="idle", last_transfer_size="0")

    # ---- File security ----------------------------------------------------
    def apply_file_security(self, cluster, svm, path, groups, rights):
        self._trace(cluster, f"file-security apply {path} "
                             f"groups={groups} rights={rights}")

    # ---- Read-only introspection (pre-flight) -----------------------------
    # In simulation every prerequisite is reported as satisfied; the engine
    # marks such reports as `simulated` so they are informational only.
    def get_svm(self, cluster, svm) -> SvmInfo:
        self._trace(cluster, f"svm show {svm}")
        return SvmInfo(name=svm, exists=True, state="running", cifs_enabled=True)

    def aggregate_exists(self, cluster, aggregate) -> bool:
        self._trace(cluster, f"aggregate exists? {aggregate} -> yes")
        return True

    def list_cluster_peers(self, cluster) -> List[PeerInfo]:
        self._trace(cluster, "cluster peer show")
        return [PeerInfo(name="*", state="available")]

    def list_svm_peers(self, cluster) -> List[PeerInfo]:
        self._trace(cluster, "svm peer show")
        return [PeerInfo(name="*", state="peered", local_svm="*", peer_svm="*")]

    def snapmirror_policy_exists(self, cluster, policy) -> bool:
        self._trace(cluster, f"snapmirror policy exists? {policy} -> yes")
        return True

    def schedule_exists(self, cluster, schedule) -> bool:
        self._trace(cluster, f"schedule exists? {schedule} -> yes")
        return True

    def junction_path_exists(self, cluster, svm, path) -> bool:
        self._trace(cluster, f"junction path exists? {svm}:{path} -> yes")
        return True
