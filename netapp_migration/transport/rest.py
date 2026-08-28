"""ONTAP REST API transport (default).

Talks to each cluster's management LIF over HTTPS with basic auth.
Requires ONTAP >= 9.9 (validated against 9.16.1). Endpoint map:

    volumes           /api/storage/volumes            (create, clone, move)
    aggregates        /api/storage/aggregates
    snapshots         /api/storage/volumes/{uuid}/snapshots
    qtrees            /api/storage/qtrees
    snapmirror        /api/snapmirror/relationships (+ /transfers)
    cifs shares       /api/protocols/cifs/shares
    export policies   /api/protocols/nfs/export-policies
    quota rules       /api/storage/quota/rules            (inventory only)
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
                      ExportRule, ExportPolicyInfo, QtreeInfo, QuotaRule,
                      TRANSFER_ACTIVE, TRANSFER_FAILED, TRANSFER_IDLE)
from .base import OntapClient

# How long we wait for a *creation* job (volume/clone/snapshot) to finish.
_CREATION_JOB_TIMEOUT = 600
_JOB_POLL_SECONDS = 2


def _export_rule_from_rest(raw: dict) -> ExportRule:
    """One ONTAP rule record -> ExportRule.

    'clients' comes back as [{'match': '10.0.0.0/8'}, ...]; the access lists
    as [{'name': 'sys'}, ...]. Both are flattened to plain strings so the
    engine and the pre-flight never touch REST shapes.
    """
    def names(key: str, sub: str) -> List[str]:
        return [item[sub] for item in raw.get(key) or []
                if isinstance(item, dict) and item.get(sub)]

    return ExportRule(
        clients=names("clients", "match"),
        ro_rule=names("ro_rule", "name") or [],
        rw_rule=names("rw_rule", "name") or [],
        superuser=names("superuser", "name") or [],
        protocols=[p for p in raw.get("protocols") or [] if p],
        anonymous_user=raw.get("anonymous_user") or "",
        allow_suid=raw.get("allow_suid"),
        allow_device_creation=raw.get("allow_device_creation"),
        ntfs_unix_security=raw.get("ntfs_unix_security") or "",
        chown_mode=raw.get("chown_mode") or "",
        index=raw.get("index"))


def _export_rule_to_rest(rule: ExportRule) -> dict:
    """ExportRule -> the body ONTAP expects when creating a rule.

    'index' is deliberately dropped: rules are posted in order and ONTAP
    numbers them itself, so a gap in the source numbering — or the
    renumbering the one-client-per-rule split forces — never has to be
    reproduced on the destination.

    A field the source did not report is left out entirely rather than sent
    as a default: ONTAP then applies its own, which is what the source rule
    was doing too.
    """
    body: dict = {
        "clients": [{"match": c} for c in rule.clients],
        "ro_rule": [{"name": n} for n in rule.ro_rule],
        "rw_rule": [{"name": n} for n in rule.rw_rule],
        "protocols": list(rule.protocols),
    }
    if rule.superuser:
        body["superuser"] = [{"name": n} for n in rule.superuser]
    if rule.anonymous_user:
        body["anonymous_user"] = rule.anonymous_user
    if rule.allow_suid is not None:
        body["allow_suid"] = rule.allow_suid
    if rule.allow_device_creation is not None:
        body["allow_device_creation"] = rule.allow_device_creation
    if rule.ntfs_unix_security:
        body["ntfs_unix_security"] = rule.ntfs_unix_security
    if rule.chown_mode:
        body["chown_mode"] = rule.chown_mode
    return body


def _relationship_type_of(rec: dict) -> str:
    """XDP / DP / … from the policy type REST reports.

    REST has no relationship 'type' field: an async policy makes an XDP
    relationship, a sync one a sync relationship. Derived here so callers
    see the CLI's vocabulary rather than having to know that.
    """
    policy_type = ((rec.get("policy", {}) or {}).get("type", "") or "").lower()
    if not policy_type:
        return ""
    return "XDP" if policy_type.startswith("async") else policy_type.upper()


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
                                               "nas.path,"
                                               "aggregates.name,"
                                               "clone.is_flexclone,"
                                               "clone.parent_volume.name,"
                                               "movement.state,"
                                               "state,type,quota.state"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"volume show {svm}:{volume}",
                             "volume not found")
        rec = records[0]
        aggregates = rec.get("aggregates", [])
        nas = rec.get("nas", {}) or {}
        clone = rec.get("clone", {}) or {}
        return VolumeInfo(
            name=volume, svm=svm,
            size_bytes=rec.get("size"),
            security_style=nas.get("security_style"),
            aggregate=aggregates[0]["name"] if aggregates else None,
            uuid=rec.get("uuid"),
            is_flexclone=clone.get("is_flexclone"),
            move_state=(rec.get("movement", {}) or {}).get("state", "") or "",
            state=rec.get("state", "") or "",
            volume_type=rec.get("type", "") or "",
            junction_path=nas.get("path", "") or "",
            quota_state=(rec.get("quota", {}) or {}).get("state", "") or "",
            clone_parent=(clone.get("parent_volume", {}) or {}).get("name", ""),
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

    def get_qtree_export_policy(self, cluster, svm, volume, qtree) -> str:
        body = self._request(cluster, "GET", "/storage/qtrees",
                             params={"svm.name": svm, "volume.name": volume,
                                     "name": qtree,
                                     "fields": "export_policy.name"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"qtree lookup {volume}/{qtree}",
                             "qtree not found")
        return (records[0].get("export_policy") or {}).get("name", "")

    def export_policy_exists(self, cluster, svm, policy) -> bool:
        body = self._request(cluster, "GET", "/protocols/nfs/export-policies",
                             params={"name": policy, "svm.name": svm,
                                     "fields": "name"})
        return bool(body.get("records"))

    def get_export_policy_rules(self, cluster, svm, policy) -> List[ExportRule]:
        """Read a policy with its rules expanded.

        'rules' is not returned by default — it has to be asked for by name
        in 'fields', otherwise every policy comes back looking empty.
        """
        body = self._request(cluster, "GET", "/protocols/nfs/export-policies",
                             params={"name": policy, "svm.name": svm,
                                     "fields": "rules"})
        records = body.get("records", [])
        if not records:
            raise OntapError(cluster, f"export policy '{policy}' on {svm}",
                             "policy not found")
        return [_export_rule_from_rest(raw)
                for raw in records[0].get("rules") or []]

    def create_export_policy(self, cluster, svm, policy, rules=None):
        """Create the policy, with the given rules or none at all.

        No 'rules' key means no rule, means no access — which is what the
        cleanup action wants. Rules are posted with the policy in one call:
        ONTAP accepts them inline, and creating the policy first would leave
        a window where it exists and denies everyone.
        """
        payload = {"name": policy, "svm": {"name": svm}}
        if rules:
            payload["rules"] = [_export_rule_to_rest(r) for r in rules]
        self._request(cluster, "POST", "/protocols/nfs/export-policies",
                      json_body=payload)

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

    def delete_qtree(self, cluster, svm, volume, qtree):
        """Delete a qtree and everything in it. Irreversible.

        No 'force' parameter: unlike the CLI's `volume qtree delete -force`,
        the REST endpoint takes none and answers
        `HTTP 400: Unexpected Argument "force"` if given one (verified on
        9.16.1). The REST delete removes the qtree with its contents on its
        own.

        Waited to completion: ONTAP runs it as a job, and returning early
        would let the caller believe the space is already back.
        """
        vol_uuid, qtree_id = self._qtree_ref(cluster, svm, volume, qtree)
        self._request(cluster, "DELETE",
                      f"/storage/qtrees/{vol_uuid}/{qtree_id}",
                      wait_job=True)

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
                          policy, schedule, relationship_type="XDP",
                          idempotent=False):
        """Declare the relationship.

        The REST API has no 'type' field: the relationship kind follows from
        the POLICY's type — an async policy gives an XDP relationship, a sync
        one gives a sync relationship. So the requested type is not sent,
        it is VERIFIED after creation (_verify_relationship_type), which is
        the only honest way to promise it on this transport.
        """
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
                    f"{exc.detail} — hint: the referenced object '{policy}' / "
                    f"'{schedule}' may exist but be invisible to the API "
                    f"user's role. Grant readonly access to "
                    f"/api/cluster/schedules, /api/snapmirror/policies and "
                    f"/api/svm/peers (see README section 2.5).") from exc
            raise
        self._verify_relationship_type(cluster, dest_path, relationship_type)

    def _verify_relationship_type(self, cluster: str, dest_path: str,
                                  wanted: str) -> None:
        """Confirm ONTAP built the kind of relationship that was asked for.

        The type is decided by the policy, so the wrong policy silently
        yields the wrong kind of mirror. Checked rather than assumed, and
        raised loudly: a DP relationship where XDP was wanted replicates
        differently and would only be noticed at failover.
        """
        if not wanted:
            return
        body = self._try_get(cluster, "/snapmirror/relationships",
                             params={"destination.path": dest_path,
                                     "fields": "policy.type"})
        records = body.get("records", [])
        if not records:
            self.log.warning("Could not read back %s to confirm it is %s.",
                             dest_path, wanted)
            return
        actual = _relationship_type_of(records[0])
        if actual and actual != wanted.upper():
            raise OntapError(
                cluster, f"snapmirror create {dest_path}",
                f"relationship came out as '{actual}', not the '{wanted}' "
                f"that was asked for — check the policy's type")

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
                                     "fields": "uuid,state,healthy,"
                                               "unhealthy_reason,"
                                               "source.path,policy.name,"
                                               "policy.type,"
                                               "transfer_schedule.name,"
                                               "transfer.state,"
                                               "transfer.bytes_transferred,"
                                               "transfer.end_time"})
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
            uuid=rec.get("uuid", "") or "",
            source_path=(rec.get("source", {}) or {}).get("path", "") or "",
            policy=(rec.get("policy", {}) or {}).get("name", "") or "",
            schedule=(rec.get("transfer_schedule", {}) or {}).get("name", "") or "",
            relationship_type=_relationship_type_of(rec),
            last_transfer_end=transfer.get("end_time", "") or "",
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

    # ------------------------------------------------------------------ #
    # Read-only inventory (reporting)
    # ------------------------------------------------------------------ #
    def list_qtree_details(self, cluster, svm, volume) -> List[QtreeInfo]:
        body = self._request(cluster, "GET", "/storage/qtrees",
                             params={"svm.name": svm, "volume.name": volume,
                                     "fields": "name,id,path,volume.uuid,"
                                               "export_policy.name,"
                                               "security_style"})
        details = []
        for rec in body.get("records", []):
            if rec.get("name") in ("", "-", None):
                continue        # the volume's own root qtree, id 0
            details.append(QtreeInfo(
                name=rec["name"],
                id=rec.get("id"),
                volume=volume,
                volume_uuid=(rec.get("volume", {}) or {}).get("uuid", ""),
                path=rec.get("path", ""),
                export_policy=(rec.get("export_policy", {}) or {}).get("name", ""),
                security_style=rec.get("security_style", "") or ""))
        return details

    def get_export_policy(self, cluster, svm, policy) -> Optional[ExportPolicyInfo]:
        body = self._request(cluster, "GET", "/protocols/nfs/export-policies",
                             params={"name": policy, "svm.name": svm,
                                     "fields": "id,name,rules"})
        records = body.get("records", [])
        if not records:
            return None
        rec = records[0]
        return ExportPolicyInfo(
            name=rec.get("name", policy), id=rec.get("id"), svm=svm,
            rules=[_export_rule_from_rest(raw) for raw in rec.get("rules") or []])

    def list_quota_rules(self, cluster, svm, volume) -> List[QuotaRule]:
        """Quota rules of one volume, with their limits.

        REST attaches rules to the SVM's ACTIVE quota policy without naming
        it, so a rule here is always a rule of that policy — see
        get_quota_policy.
        """
        body = self._request(cluster, "GET", "/storage/quota/rules",
                             params={"svm.name": svm, "volume.name": volume,
                                     "fields": "uuid,type,qtree.name,"
                                               "users.name,group.name,"
                                               "space.hard_limit,"
                                               "space.soft_limit,"
                                               "files.hard_limit,"
                                               "files.soft_limit,"
                                               "user_mapping"})
        rules = []
        for rec in body.get("records", []):
            space = rec.get("space", {}) or {}
            files = rec.get("files", {}) or {}
            users = [u.get("name", "") for u in rec.get("users") or []
                     if isinstance(u, dict)]
            group = (rec.get("group", {}) or {}).get("name", "")
            rules.append(QuotaRule(
                uuid=rec.get("uuid", ""),
                type=rec.get("type", "") or "",
                qtree=(rec.get("qtree", {}) or {}).get("name", ""),
                target=", ".join(u for u in users if u) or group,
                space_hard_limit=space.get("hard_limit"),
                space_soft_limit=space.get("soft_limit"),
                files_hard_limit=files.get("hard_limit"),
                files_soft_limit=files.get("soft_limit"),
                user_mapping=rec.get("user_mapping")))
        return rules

    def get_quota_policy(self, cluster, svm) -> str:
        """Not exposed by the REST API.

        Quota rules are created against the SVM's active quota policy and no
        REST endpoint names it, so this answers '' rather than inventing a
        value. The SSH transport can read it (`vserver quota policy show`).
        """
        return ""
