"""Shared fixtures: a fully controllable fake ONTAP cluster estate.

No test in this suite ever touches a real cluster or the network. FakeClient
implements the OntapClient contract over in-memory dictionaries, so a test
can describe exactly the situation to reproduce (missing peering, relationship
still transferring, leftover volume…) and assert what the tool does about it.
"""

import logging
from typing import Dict, List, Optional

import pytest

from netapp_migration.models import (AggregateInfo, ExportRule,
                                     MigrationParams, OntapError,
                                     PeerInfo, SnapMirrorInfo, SvmInfo,
                                     VolumeInfo)
from netapp_migration.transport.base import OntapClient


@pytest.fixture
def logger():
    log = logging.getLogger("tests")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


@pytest.fixture
def params():
    return MigrationParams(
        source_cluster="SRC", pivot_cluster="PIV",
        dest_cluster="PRD", dr_cluster="DRC",
        volume="vol_prod_01",
        source_vserver="svm_source", pivot_vserver="svm_pivot",
        dest_vserver="svm_dest", dr_vserver="svm_dr",
        pivot_aggr="aggr_piv", dest_aggr="aggr_prd", dr_aggr="aggr_dr",
        timeout=1, poll_interval=0,
    )


class FakeClient(OntapClient):
    """In-memory ONTAP estate, healthy by default.

    Every mutation is recorded in `self.calls` so tests can assert that an
    action stopped BEFORE mutating anything when a pre-flight check failed.
    """

    def __init__(self):
        self.calls: List[str] = []

        # (cluster, svm) -> SvmInfo
        self.svms: Dict[tuple, SvmInfo] = {}
        for cluster, svm in (("SRC", "svm_source"), ("PIV", "svm_pivot"),
                             ("PRD", "svm_dest"), ("DRC", "svm_dr")):
            self.svms[(cluster, svm)] = SvmInfo(name=svm, exists=True,
                                                state="running",
                                                cifs_enabled=True, uuid="u")

        # (cluster, svm, volume) -> VolumeInfo   (source volume only)
        self.volumes: Dict[tuple, VolumeInfo] = {
            ("SRC", "svm_source", "vol_prod_01"):
                VolumeInfo(name="vol_prod_01", svm="svm_source",
                           size_bytes=10 * 1024 ** 3, security_style="ntfs",
                           aggregate="aggr_src"),
        }

        # cluster -> {aggregate: available bytes}
        self.aggregates: Dict[str, Dict[str, int]] = {
            "PIV": {"aggr_piv": 500 * 1024 ** 3, "aggr_piv2": 400 * 1024 ** 3},
            "PRD": {"aggr_prd": 500 * 1024 ** 3, "aggr_prd2": 900 * 1024 ** 3},
            "DRC": {"aggr_dr": 500 * 1024 ** 3, "aggr_dr2": 900 * 1024 ** 3},
            "SRC": {"aggr_src": 500 * 1024 ** 3},
        }

        # cluster -> peers
        self.cluster_peers: Dict[str, List[PeerInfo]] = {
            "PIV": [PeerInfo("SRC", "available"), PeerInfo("PRD", "available"),
                    PeerInfo("DRC", "available")],
            "PRD": [PeerInfo("PIV", "available"), PeerInfo("DRC", "available")],
            "DRC": [PeerInfo("PIV", "available"), PeerInfo("PRD", "available")],
            "SRC": [PeerInfo("PIV", "available")],
        }
        self.svm_peers: Dict[str, List[PeerInfo]] = {
            "PIV": [PeerInfo("svm_source", "peered", local_svm="svm_pivot",
                             peer_svm="svm_source", peer_cluster="SRC")],
            "PRD": [PeerInfo("svm_pivot", "peered", local_svm="svm_dest",
                             peer_svm="svm_pivot", peer_cluster="PIV"),
                    PeerInfo("svm_dr", "peered", local_svm="svm_dest",
                             peer_svm="svm_dr", peer_cluster="DRC")],
            "DRC": [PeerInfo("svm_pivot", "peered", local_svm="svm_dr",
                             peer_svm="svm_pivot", peer_cluster="PIV"),
                    PeerInfo("svm_dest", "peered", local_svm="svm_dr",
                             peer_svm="svm_dest", peer_cluster="PRD")],
        }

        self.policies: Dict[str, List[str]] = {
            c: ["MirrorAllSnapshots"] for c in ("PIV", "PRD", "DRC")}
        self.schedules: Dict[str, List[str]] = {
            c: ["hourly", "daily"] for c in ("PIV", "PRD", "DRC")}

        # dest_path -> SnapMirrorInfo
        self.relationships: Dict[str, SnapMirrorInfo] = {}
        # (cluster, svm, volume) -> {snapshot names}
        self.snapshots: Dict[tuple, set] = {}
        self.qtrees: Dict[tuple, List[str]] = {
            ("SRC", "svm_source", "vol_prod_01"): ["q_fin", "q_hr", "q_ops"],
        }
        # (cluster, svm) -> {policy name: [ExportRule]}. ep_noaccess is
        # deliberately absent everywhere: cleanup has to create it. The
        # source SVM carries one real policy so clone has clients to copy.
        self.export_policies: Dict[tuple, Dict[str, List[ExportRule]]] = {
            ("SRC", "svm_source"): {
                "ep_source": [ExportRule(clients=["10.0.0.0/8", "@admins"],
                                         ro_rule=["sys"], rw_rule=["sys"],
                                         superuser=["none"],
                                         protocols=["nfs"], index=1)],
                "default": [],
            },
        }
        # (cluster, svm, volume, qtree) -> policy name. Absent means the
        # qtree inherits the SVM default, exactly like a real cluster.
        self.qtree_policies: Dict[tuple, str] = {
            ("SRC", "svm_source", "vol_prod_01", "q_fin"): "ep_source",
            ("SRC", "svm_source", "vol_prod_01", "q_hr"): "ep_source",
            ("SRC", "svm_source", "vol_prod_01", "q_ops"): "ep_source",
        }
        self.shares: Dict[tuple, Dict[str, str]] = {
            ("SRC", "svm_source"): {"fin_share": "/vol_prod_01/q_fin",
                                    "hr_share": "/vol_prod_01/q_hr"},
        }
        self.junctions: Dict[tuple, List[str]] = {}

    # ---- helpers used by the tests ------------------------------------- #
    def add_volume(self, cluster, svm, volume, size=10 * 1024 ** 3,
                   aggregate="aggr_x", security_style="ntfs",
                   is_flexclone=False, move_state=""):
        self.volumes[(cluster, svm, volume)] = VolumeInfo(
            name=volume, svm=svm, size_bytes=size,
            security_style=security_style, aggregate=aggregate,
            is_flexclone=is_flexclone, move_state=move_state)
        self.junctions.setdefault((cluster, svm), []).append(f"/{volume}")

    def add_relationship(self, dest_path, state="snapmirrored",
                         transfer_state="idle"):
        self.relationships[dest_path] = SnapMirrorInfo(
            dest_path=dest_path, state=state, transfer_state=transfer_state)

    # ---- volumes -------------------------------------------------------- #
    def get_volume(self, cluster, svm, volume) -> VolumeInfo:
        try:
            return self.volumes[(cluster, svm, volume)]
        except KeyError:
            raise OntapError(cluster, f"volume show {svm}:{volume}",
                             "volume not found")

    def volume_exists(self, cluster, svm, volume) -> bool:
        return (cluster, svm, volume) in self.volumes

    def create_dp_volume(self, cluster, svm, volume, aggregate, size_bytes,
                         security_style, idempotent=False):
        self.calls.append(f"create_dp_volume {cluster} {svm}:{volume}")
        self.add_volume(cluster, svm, volume, size=size_bytes or 0,
                        aggregate=aggregate, security_style=security_style)

    def create_clone(self, cluster, svm, clone_name, parent_volume,
                     parent_snapshot):
        self.calls.append(f"create_clone {cluster} {svm}:{clone_name}")
        parent = self.volumes.get((cluster, svm, parent_volume))
        self.add_volume(cluster, svm, clone_name,
                        size=parent.size_bytes if parent else 0,
                        aggregate=parent.aggregate if parent else "aggr_x",
                        is_flexclone=True)
        # A FlexClone copies the WHOLE parent, qtrees included — which is
        # exactly what makes pruning necessary.
        source = self.qtrees.get(("SRC", "svm_source", parent_volume))
        if source is None:
            source = self.qtrees.get((cluster, svm, parent_volume), [])
        self.qtrees[(cluster, svm, clone_name)] = list(source)

    def start_volume_move(self, cluster, svm, volume, dest_aggregate):
        info = self.volumes.get((cluster, svm, volume))
        if info is not None:                    # the move splits the clone
            info.is_flexclone = False
            info.move_state = "success"
        self.calls.append(f"volume_move {cluster} {svm}:{volume} "
                          f"-> {dest_aggregate}")

    # ---- aggregates ----------------------------------------------------- #
    def get_aggregate_available(self, cluster, aggregate):
        return self.aggregates.get(cluster, {}).get(aggregate)

    def list_aggregates(self, cluster) -> List[AggregateInfo]:
        return [AggregateInfo(name=n, available_bytes=v)
                for n, v in self.aggregates.get(cluster, {}).items()]

    def aggregate_exists(self, cluster, aggregate) -> bool:
        return aggregate in self.aggregates.get(cluster, {})

    # ---- snapshots ------------------------------------------------------ #
    def create_snapshot(self, cluster, svm, volume, snapshot):
        self.calls.append(f"create_snapshot {cluster} {svm}:{volume}@{snapshot}")
        # A snapshot created on the source is visible everywhere once the
        # cascade transferred it; tests drive propagation explicitly.
        for key in list(self.volumes):
            if key[2] == volume:
                self.snapshots.setdefault(key, set()).add(snapshot)

    def snapshot_exists(self, cluster, svm, volume, snapshot) -> bool:
        return snapshot in self.snapshots.get((cluster, svm, volume), set())

    # ---- qtrees --------------------------------------------------------- #
    def list_qtrees(self, cluster, svm, volume) -> List[str]:
        return list(self.qtrees.get((cluster, svm, volume), []))

    def set_qtree_export_policy(self, cluster, svm, volume, qtree, policy):
        # Cluster included: like the rename, this may only ever happen on
        # PROD — the DR clone is a mirror destination and thus read-only.
        self.calls.append(f"export_policy {cluster} {volume}/{qtree}={policy}")
        self.qtree_policies[(cluster, svm, volume, qtree)] = policy

    def get_qtree_export_policy(self, cluster, svm, volume, qtree) -> str:
        if qtree not in self.qtrees.get((cluster, svm, volume), []):
            raise OntapError(cluster, f"qtree lookup {volume}/{qtree}",
                             "qtree not found")
        return self.qtree_policies.get((cluster, svm, volume, qtree),
                                       "default")

    def rename_qtree(self, cluster, svm, volume, qtree, new_name):
        # Cluster included: renaming must happen on PROD only — the DR
        # clone is a mirror destination and therefore read-only.
        self.calls.append(f"rename_qtree {cluster} {volume}/{qtree} "
                          f"-> {new_name}")
        qtrees = self.qtrees.setdefault((cluster, svm, volume), [])
        if qtree in qtrees:
            qtrees[qtrees.index(qtree)] = new_name

    # ---- CIFS ----------------------------------------------------------- #
    def export_policy_exists(self, cluster, svm, policy) -> bool:
        return policy in self.export_policies.get((cluster, svm), {})

    def get_export_policy_rules(self, cluster, svm, policy) -> List[ExportRule]:
        policies = self.export_policies.get((cluster, svm), {})
        if policy not in policies:
            raise OntapError(cluster, f"export policy '{policy}' on {svm}",
                             "policy not found")
        return list(policies[policy])

    def create_export_policy(self, cluster, svm, policy, rules=None):
        self.calls.append(f"create_export_policy {cluster} {svm}:{policy} "
                          f"rules={len(rules or [])}")
        self.export_policies.setdefault((cluster, svm), {})[policy] = \
            list(rules or [])

    def delete_qtree(self, cluster, svm, volume, qtree):
        self.calls.append(f"delete_qtree {cluster} {volume}/{qtree}")
        qtrees = self.qtrees.get((cluster, svm, volume), [])
        if qtree in qtrees:
            qtrees.remove(qtree)

    # ---- CIFS ----------------------------------------------------------- #
    def find_cifs_shares(self, cluster, svm, path_fragment) -> List[str]:
        return [name for name, path
                in self.shares.get((cluster, svm), {}).items()
                if path_fragment in path]

    def delete_cifs_share(self, cluster, svm, share):
        self.calls.append(f"delete_share {share}")
        self.shares.get((cluster, svm), {}).pop(share, None)

    # ---- SnapMirror ----------------------------------------------------- #
    def snapmirror_create(self, cluster, source_path, dest_path,
                          policy="MirrorAllSnapshots", schedule="hourly",
                          idempotent=False):
        self.calls.append(f"snapmirror_create {source_path} -> {dest_path}")
        if policy not in self.policies.get(cluster, []):
            raise OntapError(cluster, "snapmirror create",
                             f'Policy "{policy}" not found')
        if schedule and schedule not in self.schedules.get(cluster, []):
            raise OntapError(cluster, "snapmirror create",
                             f'Schedule "{schedule}" not found in the '
                             f'Administrative SVM')
        self.add_relationship(dest_path, state="uninitialized",
                              transfer_state="idle")

    def snapmirror_initialize(self, cluster, dest_path):
        self.calls.append(f"snapmirror_initialize {dest_path}")
        self.add_relationship(dest_path, state="snapmirrored",
                              transfer_state="idle")

    def snapmirror_update(self, cluster, dest_path):
        self.calls.append(f"snapmirror_update {dest_path}")

    def snapmirror_resync(self, cluster, dest_path):
        self.calls.append(f"snapmirror_resync {dest_path}")
        self.add_relationship(dest_path, state="snapmirrored",
                              transfer_state="idle")

    def get_snapmirror(self, cluster, dest_path) -> SnapMirrorInfo:
        return self.relationships.get(
            dest_path,
            SnapMirrorInfo(dest_path=dest_path, exists=False, state="absent",
                           transfer_state="unknown"))

    # ---- file security -------------------------------------------------- #
    def apply_file_security(self, cluster, svm, path, groups, rights):
        self.calls.append(f"apply_acl {path} {groups} {rights}")

    # ---- introspection -------------------------------------------------- #
    def get_svm(self, cluster, svm) -> SvmInfo:
        return self.svms.get((cluster, svm),
                             SvmInfo(name=svm, exists=False, state="absent"))

    def list_cluster_peers(self, cluster) -> List[PeerInfo]:
        return list(self.cluster_peers.get(cluster, []))

    def list_svm_peers(self, cluster) -> List[PeerInfo]:
        return list(self.svm_peers.get(cluster, []))

    def snapmirror_policy_exists(self, cluster, policy) -> bool:
        return policy in self.policies.get(cluster, [])

    def schedule_exists(self, cluster, schedule) -> bool:
        return schedule in self.schedules.get(cluster, [])

    def junction_path_exists(self, cluster, svm, path) -> bool:
        wanted = "/" + path.strip("/")
        for junction in self.junctions.get((cluster, svm), []):
            junction = junction.rstrip("/")
            if wanted == junction or wanted.startswith(junction + "/"):
                return True
        return False


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def store(tmp_path):
    from netapp_migration.core.jobs import JobStore
    return JobStore(str(tmp_path))


@pytest.fixture
def engine(client, params, store, logger):
    from netapp_migration.core.engine import MigrationEngine
    return MigrationEngine(client, params, store, logger)


def vmap(*qtrees):
    """Convenience qtree -> target volume mapping for the tests."""
    return {q: f"vol_{q}" for q in qtrees}


def cascade_ready(client, params):
    """Bring the fake estate to a fully replicated, healthy cascade."""
    for cluster, svm, aggr in ((params.pivot_cluster, params.pivot_vserver,
                                params.pivot_aggr),
                               (params.dest_cluster, params.dest_vserver,
                                params.dest_aggr),
                               (params.dr_cluster, params.dr_vserver,
                                params.dr_aggr)):
        client.add_volume(cluster, svm, params.volume, aggregate=aggr)
        client.add_relationship(params.path(svm, params.volume),
                                state="snapmirrored", transfer_state="idle")
