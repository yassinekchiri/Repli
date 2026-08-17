"""ONTAP REST API transport (default).

Talks to each cluster's management LIF over HTTPS with basic auth.
Requires ONTAP >= 9.9 (validated against 9.16.1). Endpoint map:

    volumes           /api/storage/volumes            (create, clone, move)
    aggregates        /api/storage/aggregates
    snapshots         /api/storage/volumes/{uuid}/snapshots
    qtrees            /api/storage/qtrees
    snapmirror        /api/snapmirror/relationships (+ /transfers)
    cifs shares       /api/protocols/cifs/shares
    file security     /api/protocols/file-security/permissions   (DACL forcing)
    jobs              /api/cluster/jobs/{uuid}

Long ONTAP operations answer '202 Accepted' with a job UUID:
  - object creations (volume, clone, snapshot) are waited on (fast jobs);
  - transfers, volume moves and DACL forcing are fire-and-forget — the
    engine polls the resource state instead, keeping the 'launch and exit'
    philosophy of the tool.
"""

import logging
import time
import urllib.parse
from typing import Callable, Dict, List, Optional

import requests
import urllib3

from ..models import (OntapError, ClusterCredentials, VolumeInfo,
                      AggregateInfo, SnapMirrorInfo, SvmInfo, PeerInfo,
                      TRANSFER_ACTIVE, TRANSFER_FAILED, TRANSFER_IDLE)
from .base import OntapClient

# How long we wait for a *creation* job (volume/clone/snapshot) to finish.
_CREATION_JOB_TIMEOUT = 600
_JOB_POLL_SECONDS = 2

# ONTAP messages that mean "the object is already there" (idempotent mode).
_ALREADY_EXISTS_MARKERS = ("already exists", "duplicate entry",
                           "entry already exists", "already has")


class RestClient(OntapClient):
    """OntapClient implementation over the ONTAP REST API (basic auth)."""

    def __init__(self, logger: logging.Logger,
                 credentials_for: Callable[[str], ClusterCredentials]):
        """:param credentials_for: cluster name -> ClusterCredentials."""
        self.log = logger
        self._creds_for = credentials_for
        self._sessions: Dict[str, requests.Session] = {}
        self._insecure_warned = False

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _session(self, cluster: str) -> requests.Session:
        if cluster not in self._sessions:
            creds = self._creds_for(cluster)
            s = requests.Session()
            s.auth = (creds.username, creds.password)
            s.verify = creds.verify_ssl
            s.headers.update({"Accept": "application/json",
                              "Content-Type": "application/json"})
            if not creds.verify_ssl and not self._insecure_warned:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                self.log.warning("TLS certificate verification DISABLED for "
                                 "cluster '%s' (verify_ssl=false).", cluster)
                self._insecure_warned = True
            # Stash the port on the session object for URL building.
            s._ontap_port = creds.port  # type: ignore[attr-defined]
            self._sessions[cluster] = s
        return self._sessions[cluster]

    def _url(self, cluster: str, path: str) -> str:
        port = getattr(self._session(cluster), "_ontap_port", 443)
        return f"https://{cluster}:{port}/api{path}"

    def _request(self, cluster: str, method: str, path: str,
                 params: Optional[dict] = None,
                 json_body: Optional[dict] = None,
                 wait_job: bool = True) -> dict:
        """Send one REST call; raise OntapError on any HTTP/ONTAP failure.

        202 responses carry an async job. With wait_job=True the job is
        polled to completion; with wait_job=False the job UUID is only
        logged (fire-and-forget) and the raw 202 body is returned.
        """
        url = self._url(cluster, path)
        t0 = time.monotonic()
        try:
            r = self._session(cluster).request(method, url, params=params,
                                               json=json_body, timeout=60)
        except requests.RequestException as exc:
            raise OntapError(cluster, f"{method} {path}",
                             f"connection failure: {exc}") from exc
        elapsed = time.monotonic() - t0

        body: dict = {}
        if r.content:
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:2000]}
        self.log.debug("REST %s %s -> %s (%.2fs)\nparams=%s\nbody=%s\nresponse=%s",
                       method, url, r.status_code, elapsed, params, json_body, body)

        if r.status_code >= 400:
            message = (body.get("error", {}) or {}).get("message", r.text[:500])
            raise OntapError(cluster, f"{method} {path}",
                             f"HTTP {r.status_code}: {message}")

        if r.status_code == 202 and wait_job:
            job_uuid = (body.get("job", {}) or {}).get("uuid")
            if job_uuid:
                self._wait_job(cluster, job_uuid)
        elif r.status_code == 202 and not wait_job:
            job_uuid = (body.get("job", {}) or {}).get("uuid")
            self.log.debug("Fire-and-forget job on %s: %s", cluster, job_uuid)
        return body

    def _wait_job(self, cluster: str, job_uuid: str):
        """Poll a cluster job until success; raise OntapError on failure."""
        deadline = time.monotonic() + _CREATION_JOB_TIMEOUT
        while True:
            body = self._request(cluster, "GET", f"/cluster/jobs/{job_uuid}",
                                 params={"fields": "state,message"})
            state = body.get("state", "unknown")
            if state == "success":
                return
            if state in ("failure", "error"):
                raise OntapError(cluster, f"job {job_uuid}",
                                 body.get("message", "job failed"))
            if time.monotonic() > deadline:
                raise OntapError(cluster, f"job {job_uuid}",
                                 f"timeout after {_CREATION_JOB_TIMEOUT}s "
                                 f"(state={state})")
            time.sleep(_JOB_POLL_SECONDS)

    @staticmethod
    def _is_already_exists(exc: OntapError) -> bool:
        detail = exc.detail.lower()
        return any(marker in detail for marker in _ALREADY_EXISTS_MARKERS)

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def _volume_uuid(self, cluster: str, svm: str, volume: str) -> str:
        body = self._request(cluster, "GET", "/storage/volumes",
                             params={"name": volume, "svm.name": svm,
                                     "fields": "uuid"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"volume lookup {svm}:{volume}",
                             "volume not found")
        return records[0]["uuid"]

    def _svm_uuid(self, cluster: str, svm: str) -> str:
        body = self._request(cluster, "GET", "/svm/svms",
                             params={"name": svm, "fields": "uuid"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"svm lookup {svm}", "vserver not found")
        return records[0]["uuid"]

    def _split_path(self, dest_path: str):
        svm, _, volume = dest_path.partition(":")
        return svm, volume

    # ------------------------------------------------------------------ #
    # Volumes
    # ------------------------------------------------------------------ #
    def get_volume(self, cluster: str, svm: str, volume: str) -> VolumeInfo:
        body = self._request(cluster, "GET", "/storage/volumes",
                             params={"name": volume, "svm.name": svm,
                                     "fields": "uuid,size,nas.security_style,"
                                               "aggregates.name"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"volume show {svm}:{volume}",
                             "volume not found")
        rec = records[0]
        aggregates = rec.get("aggregates", [])
        return VolumeInfo(
            name=volume, svm=svm,
            size_bytes=rec.get("size"),
            security_style=(rec.get("nas", {}) or {}).get("security_style"),
            aggregate=aggregates[0]["name"] if aggregates else None,
            uuid=rec.get("uuid"),
        )

    def volume_exists(self, cluster: str, svm: str, volume: str) -> bool:
        body = self._request(cluster, "GET", "/storage/volumes",
                             params={"name": volume, "svm.name": svm})
        return bool(body.get("records"))

    def create_dp_volume(self, cluster, svm, volume, aggregate, size_bytes,
                         security_style, idempotent=False):
        payload: dict = {
            "name": volume,
            "svm": {"name": svm},
            "aggregates": [{"name": aggregate}],
            "type": "dp",
            "guarantee": {"type": "none"},
        }
        if size_bytes:
            payload["size"] = size_bytes
        if security_style:
            payload["nas"] = {"security_style": security_style}
        try:
            self._request(cluster, "POST", "/storage/volumes",
                          json_body=payload)
        except OntapError as exc:
            if idempotent and self._is_already_exists(exc):
                self.log.warning("DP volume '%s' already exists on %s — skipping.",
                                 volume, cluster)
                return
            raise

    def create_clone(self, cluster, svm, clone_name, parent_volume,
                     parent_snapshot):
        payload = {
            "name": clone_name,
            "svm": {"name": svm},
            "clone": {
                "is_flexclone": True,
                "parent_volume": {"name": parent_volume},
                "parent_snapshot": {"name": parent_snapshot},
            },
            "nas": {"path": f"/{clone_name}"},
        }
        self._request(cluster, "POST", "/storage/volumes", json_body=payload)

    def start_volume_move(self, cluster, svm, volume, dest_aggregate):
        uuid = self._volume_uuid(cluster, svm, volume)
        # The PATCH job tracks the whole move: fire-and-forget on purpose.
        self._request(cluster, "PATCH", f"/storage/volumes/{uuid}",
                      json_body={"movement":
                                 {"destination_aggregate":
                                  {"name": dest_aggregate}}},
                      wait_job=False)

    # ------------------------------------------------------------------ #
    # Aggregates
    # ------------------------------------------------------------------ #
    def get_aggregate_available(self, cluster, aggregate):
        body = self._request(cluster, "GET", "/storage/aggregates",
                             params={"name": aggregate,
                                     "fields": "space.block_storage.available"})
        records = body.get("records", [])
        if not records:
            return None
        space = records[0].get("space", {}).get("block_storage", {})
        return space.get("available")

    def list_aggregates(self, cluster):
        body = self._request(cluster, "GET", "/storage/aggregates",
                             params={"fields": "space.block_storage.available"})
        out: List[AggregateInfo] = []
        for rec in body.get("records", []):
            space = rec.get("space", {}).get("block_storage", {})
            out.append(AggregateInfo(name=rec.get("name", ""),
                                     available_bytes=space.get("available", 0) or 0))
        return out

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    def create_snapshot(self, cluster, svm, volume, snapshot):
        uuid = self._volume_uuid(cluster, svm, volume)
        self._request(cluster, "POST",
                      f"/storage/volumes/{uuid}/snapshots",
                      json_body={"name": snapshot})

    def snapshot_exists(self, cluster, svm, volume, snapshot):
        uuid = self._volume_uuid(cluster, svm, volume)
        body = self._request(cluster, "GET",
                             f"/storage/volumes/{uuid}/snapshots",
                             params={"name": snapshot})
        return bool(body.get("records"))

    # ------------------------------------------------------------------ #
    # Qtrees
    # ------------------------------------------------------------------ #
    def list_qtrees(self, cluster, svm, volume):
        body = self._request(cluster, "GET", "/storage/qtrees",
                             params={"svm.name": svm, "volume.name": volume,
                                     "fields": "name"})
        return [rec["name"] for rec in body.get("records", [])
                if rec.get("name") not in ("", "-", None)]

    def _qtree_ref(self, cluster: str, svm: str, volume: str, qtree: str):
        body = self._request(cluster, "GET", "/storage/qtrees",
                             params={"svm.name": svm, "volume.name": volume,
                                     "name": qtree,
                                     "fields": "id,volume.uuid"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"qtree lookup {volume}/{qtree}",
                             "qtree not found")
        rec = records[0]
        return rec["volume"]["uuid"], rec["id"]

    def set_qtree_export_policy(self, cluster, svm, volume, qtree, policy):
        vol_uuid, qtree_id = self._qtree_ref(cluster, svm, volume, qtree)
        self._request(cluster, "PATCH",
                      f"/storage/qtrees/{vol_uuid}/{qtree_id}",
                      json_body={"export_policy": {"name": policy}})

    def rename_qtree(self, cluster, svm, volume, qtree, new_name):
        vol_uuid, qtree_id = self._qtree_ref(cluster, svm, volume, qtree)
        self._request(cluster, "PATCH",
                      f"/storage/qtrees/{vol_uuid}/{qtree_id}",
                      json_body={"name": new_name})

    # ------------------------------------------------------------------ #
    # CIFS shares
    # ------------------------------------------------------------------ #
    def find_cifs_shares(self, cluster, svm, path_fragment):
        body = self._request(cluster, "GET", "/protocols/cifs/shares",
                             params={"svm.name": svm, "fields": "name,path"})
        return [rec["name"] for rec in body.get("records", [])
                if path_fragment in (rec.get("path") or "")]

    def delete_cifs_share(self, cluster, svm, share):
        svm_uuid = self._svm_uuid(cluster, svm)
        self._request(cluster, "DELETE",
                      f"/protocols/cifs/shares/{svm_uuid}/"
                      f"{urllib.parse.quote(share, safe='')}")

    # ------------------------------------------------------------------ #
    # SnapMirror
    # ------------------------------------------------------------------ #
    def snapmirror_create(self, cluster, source_path, dest_path,
                          policy="MirrorAllSnapshots", schedule="hourly",
                          idempotent=False):
        payload = {
            "source": {"path": source_path},
            "destination": {"path": dest_path},
            "policy": {"name": policy},
            "transfer_schedule": {"name": schedule},
        }
        try:
            self._request(cluster, "POST", "/snapmirror/relationships",
                          json_body=payload)
        except OntapError as exc:
            if idempotent and self._is_already_exists(exc):
                self.log.warning("SnapMirror %s -> %s already exists — skipping.",
                                 source_path, dest_path)
                return
            if "not found" in exc.detail.lower():
                # ONTAP resolves referenced objects (schedule, policy, peer
                # SVM) with the CALLER's permissions: an object the role
                # cannot read is reported as missing even though it exists.
                raise OntapError(
                    cluster, exc.operation,
                    f"{exc.detail} — hint: the referenced object may exist "
                    f"but be invisible to the API user's role. Grant "
                    f"readonly access to /api/cluster/schedules, "
                    f"/api/snapmirror/policies and /api/svm/peers "
                    f"(see README section 2.5).") from exc
            raise

    def _relationship_uuid(self, cluster: str, dest_path: str) -> str:
        body = self._request(cluster, "GET", "/snapmirror/relationships",
                             params={"destination.path": dest_path,
                                     "fields": "uuid"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"snapmirror lookup {dest_path}",
                             "relationship not found")
        return records[0]["uuid"]

    def snapmirror_initialize(self, cluster, dest_path):
        uuid = self._relationship_uuid(cluster, dest_path)
        # The PATCH job tracks the whole baseline: fire-and-forget on purpose;
        # the engine polls get_snapmirror() instead.
        self._request(cluster, "PATCH", f"/snapmirror/relationships/{uuid}",
                      json_body={"state": "snapmirrored"}, wait_job=False)

    def snapmirror_update(self, cluster, dest_path):
        uuid = self._relationship_uuid(cluster, dest_path)
        self._request(cluster, "POST",
                      f"/snapmirror/relationships/{uuid}/transfers",
                      json_body={}, wait_job=False)

    def snapmirror_resync(self, cluster, dest_path):
        uuid = self._relationship_uuid(cluster, dest_path)
        self._request(cluster, "PATCH", f"/snapmirror/relationships/{uuid}",
                      json_body={"state": "snapmirrored"}, wait_job=False)

    # Raw ONTAP transfer states mapped onto our explicit vocabulary. Anything
    # absent from this table stays 'unknown' and is never treated as sane.
    _TRANSFER_STATE_MAP = {
        "success": TRANSFER_IDLE,
        "idle": TRANSFER_IDLE,
        "": TRANSFER_IDLE,             # no transfer object -> nothing in flight
        "queued": "queued",
        "preparing": "preparing",
        "transferring": "transferring",
        "finalizing": "finalizing",
        "failed": "failed",
        "aborted": "aborted",
        "hard_aborted": "hard_aborted",
    }

    def get_snapmirror(self, cluster, dest_path) -> SnapMirrorInfo:
        body = self._request(cluster, "GET", "/snapmirror/relationships",
                             params={"destination.path": dest_path,
                                     "fields": "state,healthy,unhealthy_reason,"
                                               "transfer.state,"
                                               "transfer.bytes_transferred"})
        records = body.get("records", [])
        if not records:
            # 'absent' with an explicit unknown transfer state: is_idle and
            # is_ready both stay false, so no wait can be satisfied by it.
            return SnapMirrorInfo(dest_path=dest_path, exists=False,
                                  state="absent", transfer_state="unknown")
        rec = records[0]
        transfer = rec.get("transfer", {}) or {}
        raw_transfer = (transfer.get("state") or "").lower()
        transfer_state = self._TRANSFER_STATE_MAP.get(raw_transfer, "unknown")
        if raw_transfer and raw_transfer not in self._TRANSFER_STATE_MAP:
            self.log.warning("Unrecognised SnapMirror transfer state '%s' on "
                             "%s — treated as unknown (not idle).",
                             raw_transfer, dest_path)

        # ONTAP exposes healthy/unhealthy_reason; a relationship reported
        # unhealthy must not pass as ready even if the state string looks fine.
        reasons = rec.get("unhealthy_reason") or []
        if isinstance(reasons, dict):
            reasons = [reasons]
        last_error = "; ".join(
            r.get("message", "") for r in reasons if isinstance(r, dict)
        ).strip("; ")
        state = (rec.get("state") or "unknown").lower()
        if rec.get("healthy") is False and transfer_state == TRANSFER_IDLE:
            # Idle but unhealthy: surface it as a failure, not as a clean idle.
            transfer_state = "failed"

        transferred = transfer.get("bytes_transferred")
        return SnapMirrorInfo(
            dest_path=dest_path,
            state=state,
            transfer_state=transfer_state,
            last_transfer_size=str(transferred) if transferred is not None else "-",
            last_error=last_error,
        )

    # ------------------------------------------------------------------ #
    # File security (DACL forcing)
    # ------------------------------------------------------------------ #
    def apply_file_security(self, cluster, svm, path, groups, rights):
        svm_uuid = self._svm_uuid(cluster, svm)
        rights_api = rights.replace("-", "_")   # full-control -> full_control
        payload = {
            "acls": [
                {
                    "user": group,
                    "access": "access_allow",
                    "rights": rights_api,
                    "apply_to": {"this_folder": True,
                                 "sub_folders": True,
                                 "files": True},
                }
                for group in groups
            ],
            "propagation_mode": "propagate",
        }
        encoded_path = urllib.parse.quote(path, safe="")
        # The apply job walks the whole tree: fire-and-forget on purpose.
        self._request(cluster, "POST",
                      f"/protocols/file-security/permissions/"
                      f"{svm_uuid}/{encoded_path}",
                      json_body=payload, wait_job=False)

    # ================================================================== #
    # READ-ONLY INTROSPECTION (pre-flight checks)
    # ================================================================== #
    def _try_get(self, cluster: str, path: str,
                 params: Optional[dict] = None) -> dict:
        """GET that returns {} instead of raising on 403/404.

        A pre-flight check must be able to report "not visible" without
        aborting the whole report.
        """
        try:
            return self._request(cluster, "GET", path, params=params)
        except OntapError as exc:
            self.log.debug("Introspection GET %s on %s unavailable: %s",
                           path, cluster, exc.detail)
            return {}

    def get_svm(self, cluster, svm) -> SvmInfo:
        body = self._try_get(cluster, "/svm/svms",
                             params={"name": svm,
                                     "fields": "uuid,state,cifs.enabled"})
        records = body.get("records", [])
        if not records:
            return SvmInfo(name=svm, exists=False, state="absent")
        rec = records[0]
        return SvmInfo(name=svm, exists=True,
                       state=(rec.get("state") or "unknown").lower(),
                       cifs_enabled=(rec.get("cifs", {}) or {}).get("enabled"),
                       uuid=rec.get("uuid"))

    def aggregate_exists(self, cluster, aggregate) -> bool:
        body = self._try_get(cluster, "/storage/aggregates",
                             params={"name": aggregate, "fields": "name"})
        return bool(body.get("records"))

    def list_cluster_peers(self, cluster) -> List[PeerInfo]:
        body = self._try_get(cluster, "/cluster/peers",
                             params={"fields": "name,status.state"})
        peers: List[PeerInfo] = []
        for rec in body.get("records", []):
            state = ((rec.get("status", {}) or {}).get("state")
                     or "unknown").lower()
            peers.append(PeerInfo(name=rec.get("name", ""), state=state))
        return peers

    def list_svm_peers(self, cluster) -> List[PeerInfo]:
        body = self._try_get(cluster, "/svm/peers",
                             params={"fields": "state,svm.name,peer.svm.name,"
                                               "peer.cluster.name"})
        peers: List[PeerInfo] = []
        for rec in body.get("records", []):
            peer = rec.get("peer", {}) or {}
            peers.append(PeerInfo(
                name=(peer.get("svm", {}) or {}).get("name", ""),
                state=(rec.get("state") or "unknown").lower(),
                local_svm=(rec.get("svm", {}) or {}).get("name", ""),
                peer_svm=(peer.get("svm", {}) or {}).get("name", ""),
                peer_cluster=(peer.get("cluster", {}) or {}).get("name", ""),
            ))
        return peers

    def snapmirror_policy_exists(self, cluster, policy) -> bool:
        body = self._try_get(cluster, "/snapmirror/policies",
                             params={"name": policy, "fields": "name"})
        return bool(body.get("records"))

    def schedule_exists(self, cluster, schedule) -> bool:
        body = self._try_get(cluster, "/cluster/schedules",
                             params={"name": schedule, "fields": "name"})
        return bool(body.get("records"))

    def junction_path_exists(self, cluster, svm, path) -> bool:
        """Resolve an absolute NAS path to a mounted volume on the SVM.

        The path is matched against volume junction paths: '/v_q_fin_ab12' or
        any sub-directory of it resolves to that volume.
        """
        body = self._try_get(cluster, "/storage/volumes",
                             params={"svm.name": svm,
                                     "fields": "name,nas.path"})
        wanted = "/" + path.strip("/")
        for rec in body.get("records", []):
            junction = ((rec.get("nas", {}) or {}).get("path") or "").rstrip("/")
            if not junction:
                continue
            if wanted == junction or wanted.startswith(junction + "/"):
                return True
        return False
