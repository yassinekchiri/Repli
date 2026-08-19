"""Pre-flight feasibility checks.

Every action asks "is this doable?" BEFORE touching a cluster. Each answer is
an individual named CheckResult (code, what was observed, how to fix it), so
that both the CLI and the REST API can show the operator exactly which
prerequisite is missing instead of relaying a raw ONTAP error.

Design rules:

* read-only — a checker never mutates anything;
* never abort on the first problem: the whole report is collected so the
  operator fixes everything in one pass;
* a cluster/permission error during a check becomes a failed check, not a
  crash (`_safe`);
* under the dry-run transport the report is flagged `simulated` and never
  blocks, since the simulated world cannot reflect real cluster state.

Check codes are stable identifiers, safe to consume by automation.
"""

import datetime
import logging
import re
from typing import Callable, Dict, List, Optional, Sequence

from ..models import (CheckResult, MigrationParams, OntapError,
                      PreflightReport, SEVERITY_ERROR, SEVERITY_WARNING,
                      SnapMirrorInfo)
from ..security import csvio
from ..transport.base import OntapClient
from .jobs import CREATE_STATUS_ORDER

# ONTAP volume names: letters, digits, underscore; 203 characters maximum.
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VOLUME_NAME_MAX = 203

# AD group accepted as DOMAIN\group, domain\group@realm or a bare group name.
_AD_GROUP_RE = re.compile(r"^[^\\/:*?\"<>|]+(\\[^\\/:*?\"<>|]+)?$")

# Statuses in which the replication cascade is fully in place.
_CASCADE_READY_STATUSES = ("dest_initialized", "completed")

# Kept here so the checks and the engine cannot drift apart.
SNAPMIRROR_POLICY = "MirrorAllSnapshots"
SNAPMIRROR_SCHEDULE = "hourly"


class PreflightChecker:
    """Builds a PreflightReport for each action of the migration."""

    def __init__(self, client: OntapClient, params: MigrationParams,
                 logger: logging.Logger, simulated: bool = False):
        self.c = client
        self.p = params
        self.log = logger
        self.simulated = simulated

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    def _report(self, action: str) -> PreflightReport:
        return PreflightReport(action=action, simulated=self.simulated)

    @staticmethod
    def _add(report: PreflightReport, code: str, title: str, passed: bool,
             detail: str = "", hint: str = "", target: str = "",
             severity: str = SEVERITY_ERROR) -> CheckResult:
        return report.add(CheckResult(code=code, title=title, passed=passed,
                                      severity=severity, detail=detail,
                                      hint=hint, target=target))

    def _safe(self, fn: Callable, default):
        """Run an introspection call; on ONTAP error return `default`.

        The error text is kept so the check can explain what went wrong
        (typically an RBAC restriction on the API user).
        """
        try:
            return fn(), ""
        except OntapError as exc:
            self.log.debug("Pre-flight probe failed: %s", exc)
            return default, exc.detail or str(exc)
        except Exception as exc:                      # noqa: BLE001
            self.log.debug("Pre-flight probe raised: %s", exc)
            return default, str(exc)

    # ---- reusable check groups ---------------------------------------- #
    def _check_svm(self, report: PreflightReport, cluster: str, svm: str,
                   role: str, need_cifs: bool = False):
        info, err = self._safe(lambda: self.c.get_svm(cluster, svm), None)
        target = f"{cluster} / {svm}"
        if info is None:
            self._add(report, "SVM_UNREADABLE", f"{role} SVM readable", False,
                      detail=err or "SVM could not be read",
                      hint="check the API user's role grants readonly on "
                           "/api/svm/svms",
                      target=target)
            return
        if not info.exists:
            self._add(report, "SVM_MISSING", f"{role} SVM exists", False,
                      detail=f"vserver '{svm}' not found on {cluster}",
                      hint="create the SVM or fix the --*-vserver parameter",
                      target=target)
            return
        self._add(report, "SVM_MISSING", f"{role} SVM exists", True,
                  detail=f"state={info.state}", target=target)
        if info.state not in ("running", "unknown"):
            self._add(report, "SVM_NOT_RUNNING", f"{role} SVM is running",
                      False, detail=f"state={info.state}",
                      hint=f"vserver start -vserver {svm}", target=target)
        if need_cifs and info.cifs_enabled is False:
            self._add(report, "SVM_CIFS_DISABLED", f"{role} SVM serves CIFS",
                      False, detail="CIFS is not enabled on this SVM",
                      hint="enable CIFS or target another SVM",
                      target=target, severity=SEVERITY_WARNING)

    def _check_aggregate(self, report: PreflightReport, cluster: str,
                         aggregate: str, role: str,
                         required_bytes: Optional[int]):
        target = f"{cluster} / {aggregate}"
        exists, err = self._safe(
            lambda: self.c.aggregate_exists(cluster, aggregate), False)
        if not exists:
            self._add(report, "AGGREGATE_MISSING",
                      f"{role} aggregate exists", False,
                      detail=err or f"aggregate '{aggregate}' not found",
                      hint="fix the --*-aggr parameter or check the role's "
                           "readonly access on /api/storage/aggregates",
                      target=target)
            return
        self._add(report, "AGGREGATE_MISSING", f"{role} aggregate exists",
                  True, target=target)

        available, err = self._safe(
            lambda: self.c.get_aggregate_available(cluster, aggregate), None)
        if available is None or required_bytes is None:
            self._add(report, "AGGREGATE_SPACE", f"{role} aggregate capacity",
                      False,
                      detail=f"could not compare (available={available}, "
                             f"required={required_bytes})",
                      hint="verify capacity manually before proceeding",
                      target=target, severity=SEVERITY_WARNING)
            return
        ok = available >= required_bytes
        self._add(report, "AGGREGATE_SPACE", f"{role} aggregate capacity", ok,
                  detail=f"available={_gib(available)}, "
                         f"required={_gib(required_bytes)}",
                  hint="free space or choose another aggregate" if not ok else "",
                  target=target)

    def _check_volume_absent(self, report: PreflightReport, cluster: str,
                             svm: str, volume: str, role: str,
                             hint: str = "") -> bool:
        """Check a volume does NOT exist yet. Returns True when absent."""
        target = f"{cluster} / {svm}:{volume}"
        exists, err = self._safe(
            lambda: self.c.volume_exists(cluster, svm, volume), False)
        if err:
            self._add(report, "VOLUME_UNREADABLE",
                      f"{role} volume state readable", False, detail=err,
                      hint="check the role's readonly access on "
                           "/api/storage/volumes", target=target)
            return False
        self._add(report, "VOLUME_ALREADY_EXISTS",
                  f"{role} volume is free", not exists,
                  detail=f"volume '{volume}' already exists" if exists
                         else "name available",
                  hint=hint, target=target)
        return not exists

    def _check_volume_present(self, report: PreflightReport, cluster: str,
                              svm: str, volume: str, role: str) -> bool:
        target = f"{cluster} / {svm}:{volume}"
        exists, err = self._safe(
            lambda: self.c.volume_exists(cluster, svm, volume), False)
        self._add(report, "VOLUME_MISSING", f"{role} volume exists", exists,
                  detail=err or ("found" if exists else
                                 f"volume '{volume}' not found"),
                  hint="" if exists else
                       "run the previous action of the workflow first",
                  target=target)
        return exists

    def _check_peering(self, report: PreflightReport, local_cluster: str,
                       local_svm: str, remote_cluster: str, remote_svm: str,
                       leg: str):
        """Cluster peering + SVM peering for one replication leg."""
        target = f"{local_cluster} -> {remote_cluster}"

        peers, err = self._safe(
            lambda: self.c.list_cluster_peers(local_cluster), None)
        if peers is None:
            self._add(report, "CLUSTER_PEER_UNREADABLE",
                      f"{leg}: cluster peering readable", False,
                      detail=err, hint="grant readonly on /api/cluster/peers",
                      target=target, severity=SEVERITY_WARNING)
        else:
            match = [p for p in peers
                     if p.name == remote_cluster or p.name == "*"]
            if not match:
                self._add(report, "CLUSTER_PEER_MISSING",
                          f"{leg}: cluster peering exists", False,
                          detail=f"no peer entry for '{remote_cluster}' on "
                                 f"{local_cluster} (peers seen: "
                                 f"{', '.join(p.name for p in peers) or 'none'})",
                          hint=f"cluster peer create -peer-addrs "
                               f"<{remote_cluster} intercluster LIFs>",
                          target=target)
            else:
                usable = any(p.is_usable for p in match)
                self._add(report, "CLUSTER_PEER_MISSING",
                          f"{leg}: cluster peering exists", True,
                          detail=f"state={match[0].state}", target=target)
                if not usable:
                    self._add(report, "CLUSTER_PEER_UNAVAILABLE",
                              f"{leg}: cluster peering healthy", False,
                              detail=f"peer state={match[0].state}",
                              hint="cluster peer show / check intercluster "
                                   "LIF connectivity",
                              target=target)

        svm_peers, err = self._safe(
            lambda: self.c.list_svm_peers(local_cluster), None)
        if svm_peers is None:
            self._add(report, "SVM_PEER_UNREADABLE",
                      f"{leg}: SVM peering readable", False, detail=err,
                      hint="grant readonly on /api/svm/peers",
                      target=target, severity=SEVERITY_WARNING)
            return
        match = [p for p in svm_peers
                 if p.name == "*"
                 or (p.peer_svm == remote_svm
                     and p.local_svm in (local_svm, "*"))]
        if not match:
            self._add(report, "SVM_PEER_MISSING",
                      f"{leg}: SVM peering exists", False,
                      detail=f"no SVM peer {local_svm} -> {remote_svm} on "
                             f"{local_cluster}",
                      hint=f"vserver peer create -vserver {local_svm} "
                           f"-peer-vserver {remote_svm} -peer-cluster "
                           f"{remote_cluster} -applications snapmirror",
                      target=f"{local_svm} -> {remote_svm}")
            return
        self._add(report, "SVM_PEER_MISSING", f"{leg}: SVM peering exists",
                  True, detail=f"state={match[0].state}",
                  target=f"{local_svm} -> {remote_svm}")
        if not any(p.is_usable for p in match):
            self._add(report, "SVM_PEER_NOT_PEERED",
                      f"{leg}: SVM peering healthy", False,
                      detail=f"state={match[0].state}",
                      hint="vserver peer accept if the peer is pending",
                      target=f"{local_svm} -> {remote_svm}")

    def _check_policy_and_schedule(self, report: PreflightReport,
                                   cluster: str, policy: str, schedule: str,
                                   role: str):
        """Objects referenced by 'snapmirror create' must be VISIBLE.

        ONTAP resolves them with the caller's permissions, so an object the
        API role cannot read is rejected as 'not found' at creation time.
        """
        found, err = self._safe(
            lambda: self.c.snapmirror_policy_exists(cluster, policy), False)
        self._add(report, "SNAPMIRROR_POLICY_MISSING",
                  f"{role}: SnapMirror policy visible", found,
                  detail=err or (f"policy '{policy}' found" if found else
                                 f"policy '{policy}' not visible to the API user"),
                  hint="" if found else
                       "create the policy, or grant readonly on "
                       "/api/snapmirror/policies to the API role",
                  target=f"{cluster} / {policy}")

        found, err = self._safe(
            lambda: self.c.schedule_exists(cluster, schedule), False)
        self._add(report, "SCHEDULE_MISSING",
                  f"{role}: transfer schedule visible", found,
                  detail=err or (f"schedule '{schedule}' found" if found else
                                 f"schedule '{schedule}' not visible to the "
                                 f"API user"),
                  hint="" if found else
                       f"job schedule cron create -name {schedule} -minute 5, "
                       f"or grant readonly on /api/cluster/schedules",
                  target=f"{cluster} / {schedule}")

    def _check_relationship_absent(self, report: PreflightReport,
                                   cluster: str, dest_path: str, leg: str):
        sm, err = self._safe(
            lambda: self.c.get_snapmirror(cluster, dest_path), None)
        target = f"{cluster} / {dest_path}"
        if sm is None:
            self._add(report, "SNAPMIRROR_UNREADABLE",
                      f"{leg}: relationship state readable", False,
                      detail=err, hint="grant readonly on "
                                       "/api/snapmirror/relationships",
                      target=target)
            return
        self._add(report, "SNAPMIRROR_ALREADY_EXISTS",
                  f"{leg}: relationship is free", not sm.exists,
                  detail=("relationship already declared "
                          f"({sm.describe()})") if sm.exists else "none",
                  hint="use action 'retry' to resume the existing job"
                       if sm.exists else "",
                  target=target)

    def _check_relationship_ready(self, report: PreflightReport, cluster: str,
                                  dest_path: str, leg: str,
                                  require_idle: bool = True) -> Optional[SnapMirrorInfo]:
        sm, err = self._safe(
            lambda: self.c.get_snapmirror(cluster, dest_path), None)
        target = f"{cluster} / {dest_path}"
        if sm is None:
            self._add(report, "SNAPMIRROR_UNREADABLE",
                      f"{leg}: relationship state readable", False,
                      detail=err, hint="grant readonly on "
                                       "/api/snapmirror/relationships",
                      target=target)
            return None
        ready = sm.is_ready if require_idle else sm.is_mirrored
        self._add(report, "SNAPMIRROR_NOT_READY", f"{leg}: replication ready",
                  ready,
                  detail=sm.describe() if ready
                         else f"{sm.describe()} — {sm.unhealthy_reason}",
                  hint="" if ready else
                       "wait for the transfer to finish, or repair the "
                       "relationship before continuing",
                  target=target)
        return sm

    def resolve_qtrees(self, qtrees) -> List[str]:
        """Normalise a qtree argument: 'all', a CSV string or a list.

        'all' expands to the source volume's qtrees. Never raises: an
        unreachable source yields an empty list, which the checks report.
        """
        if isinstance(qtrees, str):
            items = ([qtrees] if qtrees.strip().lower() == "all"
                     else qtrees.split(","))
        else:
            items = list(qtrees)
        items = [q.strip() for q in items if q and q.strip()]
        if len(items) == 1 and items[0].lower() == "all":
            discovered, _ = self._safe(
                lambda: self.c.list_qtrees(self.p.source_cluster,
                                           self.p.source_vserver,
                                           self.p.volume), [])
            return list(discovered or [])
        return items

    def _check_qtrees(self, report: PreflightReport,
                      qtrees: Sequence[str]) -> List[str]:
        """Validate the requested qtrees against the source volume.

        Accepts 'all', a CSV string or a list. Returns the normalised list
        (order preserved, duplicates removed).
        """
        p = self.p
        target = f"{p.source_cluster} / {p.source_vserver}:{p.volume}"

        requested = self.resolve_qtrees(qtrees)
        self._add(report, "QTREES_EMPTY", "At least one qtree requested",
                  bool(requested),
                  detail=f"{len(requested)} qtree(s)" if requested
                         else "empty qtree list",
                  hint="pass --qtrees q1,q2 or --qtrees all", target=target)
        if not requested:
            return []

        duplicates = sorted({q for q in requested if requested.count(q) > 1})
        self._add(report, "QTREES_DUPLICATED", "No duplicate qtree",
                  not duplicates,
                  detail=f"duplicated: {', '.join(duplicates)}" if duplicates
                         else "all distinct",
                  hint="remove the duplicates from --qtrees" if duplicates else "",
                  target=target)
        normalised = list(dict.fromkeys(requested))

        available, err = self._safe(
            lambda: self.c.list_qtrees(p.source_cluster, p.source_vserver,
                                       p.volume), None)
        if available is None:
            self._add(report, "QTREES_UNREADABLE",
                      "Source qtrees readable", False, detail=err,
                      hint="grant readonly on /api/storage/qtrees",
                      target=target)
        else:
            missing = [q for q in normalised if q not in available]
            self._add(report, "QTREES_MISSING",
                      "Requested qtrees exist on the source volume",
                      not missing,
                      detail=f"unknown qtree(s): {', '.join(missing)} "
                             f"(source has: {', '.join(available) or 'none'})"
                             if missing else
                             f"all {len(normalised)} found",
                      hint="fix the qtree names or use --qtrees all"
                           if missing else "",
                      target=target)

        # The derived clone name must be a legal ONTAP volume name.
        for qtree in normalised:
            candidate = f"v_{qtree}_" + "0" * 6
            legal = (_VOLUME_NAME_RE.match(candidate) is not None
                     and len(candidate) <= _VOLUME_NAME_MAX)
            if not legal:
                self._add(report, "CLONE_NAME_ILLEGAL",
                          "Derived clone name is a valid ONTAP volume name",
                          False,
                          detail=f"qtree '{qtree}' yields '{candidate}' "
                                 f"({len(candidate)} chars)",
                          hint="ONTAP volume names allow letters, digits and "
                               "underscore only, 203 characters maximum",
                          target=target)
        return normalised

    def _check_qtree_map(self, report: PreflightReport,
                         qtrees: Sequence[str],
                         qtree_map: Optional[Dict[str, str]],
                         job: Optional[dict]):
        """Renaming a qtree inside its clone: optional, but must be possible.

        The clone is a copy of the source volume, so it starts out holding
        every qtree the source holds. A new name that already exists there
        would make ONTAP refuse the rename halfway through the run, after
        the clones have been created — hence the check up front.
        """
        p = self.p
        mapping = dict((job or {}).get("qtree_map") or {})
        mapping.update({k: v for k, v in (qtree_map or {}).items() if v})
        lowered = {k.lower(): v for k, v in mapping.items()}

        renames: Dict[str, str] = {}
        for qtree in qtrees:
            name = mapping.get(qtree) or lowered.get(qtree.lower())
            if name and name != qtree:
                renames[qtree] = name
        if not renames:
            return                      # renaming is opt-in: nothing to check

        for qtree, name in sorted(renames.items()):
            try:
                csvio.validate_qtree_name(name)
                legal, why = True, ""
            except ValueError as exc:
                legal, why = False, str(exc)
            self._add(report, "QTREE_NAME_ILLEGAL",
                      f"'{name}' is a valid qtree name", legal,
                      detail=why or f"qtree '{qtree}' -> '{name}'",
                      hint="ONTAP forbids / \\ : * ? \" < > | and names "
                           "longer than 64 characters" if not legal else "")

        # Two qtrees of the same source volume renamed identically would
        # collide the moment they landed in the same clone; they land in
        # different clones here, but the source names must still be distinct.
        collisions = sorted({v for v in renames.values()
                             if list(renames.values()).count(v) > 1})
        self._add(report, "QTREE_NAME_DUPLICATE",
                  "New qtree names are distinct", not collisions,
                  detail=f"reused: {', '.join(collisions)}" if collisions
                         else "all distinct",
                  hint="give each qtree its own new name" if collisions else "")

        # A clone inherits every qtree of the source volume: the new name
        # must not be one of them, or the rename cannot happen.
        existing, err = self._safe(
            lambda: self.c.list_qtrees(p.source_cluster, p.source_vserver,
                                       p.volume), None)
        if existing is None:
            self._add(report, "QTREE_LIST_UNREADABLE",
                      "Source qtrees readable", False,
                      severity=SEVERITY_WARNING,
                      detail=err or "could not list the source qtrees",
                      hint="grant readonly on /api/storage/qtrees",
                      target=f"{p.source_cluster} / {p.volume}")
            return
        present = {q.lower() for q in existing}
        for qtree, name in sorted(renames.items()):
            free = name.lower() not in present or name.lower() == qtree.lower()
            self._add(report, "QTREE_NAME_TAKEN",
                      f"'{name}' is free inside the clone", free,
                      detail=f"the source volume already holds a qtree named "
                             f"'{name}'" if not free
                             else f"qtree '{qtree}' -> '{name}'",
                      hint="the clone inherits every qtree of the source "
                           "volume: pick a name none of them uses"
                           if not free else "",
                      target=f"{p.dest_cluster} / {p.volume}")

    def _check_volume_map(self, report: PreflightReport,
                          qtrees: Sequence[str],
                          volume_map: Optional[Dict[str, str]],
                          job: Optional[dict]):
        """Every qtree must be given an explicit, legal, free volume name."""
        p = self.p
        mapping = dict((job or {}).get("volume_map") or {})
        mapping.update({k: v for k, v in (volume_map or {}).items() if v})
        lowered = {k.lower(): v for k, v in mapping.items()}

        resolved: Dict[str, str] = {}
        missing = []
        for qtree in qtrees:
            name = mapping.get(qtree) or lowered.get(qtree.lower())
            if name:
                resolved[qtree] = name
            else:
                missing.append(qtree)

        self._add(report, "VOLUME_MAP_MISSING",
                  "Every qtree has a target volume name", not missing,
                  detail=f"no name given for: {', '.join(missing)}" if missing
                         else f"{len(resolved)} name(s) provided",
                  hint="supply one line per qtree (qtree,volume) via "
                       "--volume-map, or the volume_map field of the API "
                       "request" if missing else "")
        if not resolved:
            return

        # Distinct names — two qtrees cannot land on the same volume.
        duplicates = sorted({v for v in resolved.values()
                             if list(resolved.values()).count(v) > 1})
        self._add(report, "VOLUME_MAP_DUPLICATE",
                  "Target volume names are distinct", not duplicates,
                  detail=f"reused: {', '.join(duplicates)}" if duplicates
                         else "all distinct",
                  hint="give each qtree its own volume name" if duplicates else "")

        for qtree, name in sorted(resolved.items()):
            legal = (_VOLUME_NAME_RE.match(name) is not None
                     and len(name) <= _VOLUME_NAME_MAX)
            self._add(report, "VOLUME_NAME_ILLEGAL",
                      f"'{name}' is a valid ONTAP volume name", legal,
                      detail=f"qtree '{qtree}' -> '{name}' "
                             f"({len(name)} chars)",
                      hint="letters, digits and underscore only, starting "
                           "with a letter or underscore, 203 characters "
                           "maximum" if not legal else "")
            if not legal:
                continue
            # The name must be free on BOTH destinations (unless this job
            # already created it, i.e. a promotion reusing its own clones).
            already_ours = name in set((job or {}).get("clone_volumes", []))
            for cluster, svm, role in ((p.dest_cluster, p.dest_vserver, "PROD"),
                                       (p.dr_cluster, p.dr_vserver, "DR")):
                taken, err = self._safe(
                    lambda c=cluster, sv=svm, n=name:
                        self.c.volume_exists(c, sv, n), False)
                free = (not taken) or already_ours
                self._add(report, "VOLUME_NAME_TAKEN",
                          f"{role}: volume '{name}' is available", free,
                          detail=err or ("already exists" if taken
                                         else "name available"),
                          hint="choose another name, or delete the leftover "
                               "volume" if not free else "",
                          target=f"{cluster} / {svm}:{name}")

    # ================================================================== #
    # ACTION: create
    # ================================================================== #
    def for_create(self) -> PreflightReport:
        p = self.p
        report = self._report("create")

        # 1. Topology parameters must all be present (an empty cluster name
        #    would otherwise be silently turned into a bogus URL).
        for label, value in (("source cluster", p.source_cluster),
                            ("pivot cluster", p.pivot_cluster),
                            ("PROD cluster", p.dest_cluster),
                            ("DR cluster", p.dr_cluster),
                            ("volume", p.volume)):
            self._add(report, "PARAM_MISSING", f"{label} provided",
                      bool(value and value.strip()),
                      detail="empty" if not (value or "").strip() else value,
                      hint="all four clusters and the volume are required "
                           "for this action")
        if report.failures:
            return report

        # 2. Distinct roles: the same cluster twice would create a relationship
        #    onto itself.
        pairs = {"source/pivot": (p.source_cluster, p.pivot_cluster),
                 "pivot/PROD": (p.pivot_cluster, p.dest_cluster),
                 "pivot/DR": (p.pivot_cluster, p.dr_cluster),
                 "PROD/DR": (p.dest_cluster, p.dr_cluster)}
        for label, (a, b) in pairs.items():
            same = (a == b)
            self._add(report, "TOPOLOGY_DUPLICATE_CLUSTER",
                      f"{label} are distinct clusters", not same,
                      detail=f"both are '{a}'" if same else f"{a} / {b}",
                      hint="the Y topology needs four distinct clusters"
                           if same else "")

        # 3. SVMs.
        self._check_svm(report, p.source_cluster, p.source_vserver, "Source")
        self._check_svm(report, p.pivot_cluster, p.pivot_vserver, "Pivot")
        self._check_svm(report, p.dest_cluster, p.dest_vserver, "PROD")
        self._check_svm(report, p.dr_cluster, p.dr_vserver, "DR")

        # 4. Source volume + its characteristics (drives the space checks).
        src, err = self._safe(
            lambda: self.c.get_volume(p.source_cluster, p.source_vserver,
                                      p.volume), None)
        required = None
        if src is None:
            self._add(report, "SOURCE_VOLUME_MISSING", "Source volume exists",
                      False, detail=err or "volume not found",
                      hint="check --volume and --source-vserver",
                      target=f"{p.source_cluster} / "
                             f"{p.source_vserver}:{p.volume}")
        else:
            required = src.size_bytes
            self._add(report, "SOURCE_VOLUME_MISSING", "Source volume exists",
                      True,
                      detail=f"size={_gib(src.size_bytes)}, "
                             f"security-style={src.security_style or 'unknown'}",
                      target=f"{p.source_cluster} / "
                             f"{p.source_vserver}:{p.volume}")

        # 5. Target aggregates: exist and large enough.
        self._check_aggregate(report, p.pivot_cluster, p.pivot_aggr,
                              "Pivot", required)
        self._check_aggregate(report, p.dest_cluster, p.dest_aggr,
                              "PROD", required)
        self._check_aggregate(report, p.dr_cluster, p.dr_aggr, "DR", required)

        # 6. Destination volumes must NOT exist yet.
        hint = "delete the leftover volume, or resume the original job with "\
               "action 'retry' instead of creating a new one"
        self._check_volume_absent(report, p.pivot_cluster, p.pivot_vserver,
                                  p.volume, "Pivot DP", hint)
        self._check_volume_absent(report, p.dest_cluster, p.dest_vserver,
                                  p.volume, "PROD DP", hint)
        self._check_volume_absent(report, p.dr_cluster, p.dr_vserver,
                                  p.volume, "DR DP", hint)

        # 7. Peering for the three legs of the Y.
        self._check_peering(report, p.pivot_cluster, p.pivot_vserver,
                            p.source_cluster, p.source_vserver,
                            "source -> pivot")
        self._check_peering(report, p.dest_cluster, p.dest_vserver,
                            p.pivot_cluster, p.pivot_vserver, "pivot -> PROD")
        self._check_peering(report, p.dr_cluster, p.dr_vserver,
                            p.pivot_cluster, p.pivot_vserver, "pivot -> DR")

        # 8. Policy and schedule, on every cluster that will host a
        #    relationship (this is what silently broke the first real run).
        for cluster, role in ((p.pivot_cluster, "Pivot"),
                              (p.dest_cluster, "PROD"),
                              (p.dr_cluster, "DR")):
            self._check_policy_and_schedule(report, cluster,
                                            SNAPMIRROR_POLICY,
                                            SNAPMIRROR_SCHEDULE, role)

        # 9. Relationships must not already exist.
        self._check_relationship_absent(report, p.pivot_cluster,
                                        p.path(p.pivot_vserver, p.volume),
                                        "source -> pivot")
        self._check_relationship_absent(report, p.dest_cluster,
                                        p.path(p.dest_vserver, p.volume),
                                        "pivot -> PROD")
        self._check_relationship_absent(report, p.dr_cluster,
                                        p.path(p.dr_vserver, p.volume),
                                        "pivot -> DR")
        return report

    # ================================================================== #
    # ACTION: resume
    # ================================================================== #
    def for_resume(self, job: dict) -> PreflightReport:
        p = self.p
        report = self._report("resume")
        status = job.get("status", "unknown")

        self._add(report, "JOB_STATUS_INVALID",
                  "Job is waiting for the destination fan-out",
                  status == "pivot_initialized",
                  detail=f"status={status}",
                  hint={"completed": "nothing to resume, the job is done",
                        "dest_initialized": "PROD and DR are already "
                                            "launched, use 'check-status'",
                        }.get(status,
                              "run action 'retry' first to finish the create "
                              "phases"),
                  target=job.get("job_id", ""))
        if report.failures:
            return report

        # Pivot must have finished its baseline: this is the strict rule of
        # the cascade.
        self._check_relationship_ready(report, p.pivot_cluster,
                                       job["pivot_dest_path"], "pivot")

        # PROD and DR volumes and relationships must exist, and must NOT be
        # initialized yet — otherwise resume would fire a second initialize.
        for cluster, svm, dest_path, role in (
                (p.dest_cluster, p.dest_vserver, job["dest_dest_path"], "PROD"),
                (p.dr_cluster, p.dr_vserver, job.get("dr_dest_path", ""), "DR")):
            if not dest_path:
                continue
            self._check_volume_present(report, cluster, svm, p.volume,
                                       f"{role} DP")
            sm, err = self._safe(
                lambda c=cluster, d=dest_path: self.c.get_snapmirror(c, d),
                None)
            target = f"{cluster} / {dest_path}"
            if sm is None:
                self._add(report, "SNAPMIRROR_UNREADABLE",
                          f"{role}: relationship state readable", False,
                          detail=err, target=target)
                continue
            self._add(report, "SNAPMIRROR_MISSING",
                      f"{role}: relationship declared", sm.exists,
                      detail=sm.describe(),
                      hint="" if sm.exists else
                           "run action 'retry' to re-declare it",
                      target=target)
            if sm.exists and (sm.is_mirrored or sm.is_transferring):
                self._add(report, "SNAPMIRROR_ALREADY_INITIALIZED",
                          f"{role}: not initialized yet", False,
                          detail=f"already {sm.describe()}",
                          hint="this destination is already running; use "
                               "'check-status' to follow it instead of "
                               "resuming",
                          target=target)
        return report

    # ================================================================== #
    # ACTION: retry
    # ================================================================== #
    def for_retry(self, job: dict) -> PreflightReport:
        report = self._report("retry")
        status = job.get("status", "unknown")
        known = status in CREATE_STATUS_ORDER
        self._add(report, "JOB_STATUS_UNKNOWN", "Job status is recognised",
                  known, detail=f"status={status}",
                  hint="the job file may be corrupted or written by another "
                       "tool version" if not known else "",
                  target=job.get("job_id", ""))
        if not known:
            return report

        idx = CREATE_STATUS_ORDER.index(status)
        # Phases still to run need the same prerequisites as 'create'.
        if idx < CREATE_STATUS_ORDER.index("relationships_created"):
            p = self.p
            for cluster, role in ((p.pivot_cluster, "Pivot"),
                                  (p.dest_cluster, "PROD"),
                                  (p.dr_cluster, "DR")):
                self._check_policy_and_schedule(report, cluster,
                                                SNAPMIRROR_POLICY,
                                                SNAPMIRROR_SCHEDULE, role)
            self._check_peering(report, p.pivot_cluster, p.pivot_vserver,
                                p.source_cluster, p.source_vserver,
                                "source -> pivot")
            self._check_peering(report, p.dest_cluster, p.dest_vserver,
                                p.pivot_cluster, p.pivot_vserver,
                                "pivot -> PROD")
            self._check_peering(report, p.dr_cluster, p.dr_vserver,
                                p.pivot_cluster, p.pivot_vserver,
                                "pivot -> DR")
        return report

    # ================================================================== #
    # ACTIONS: test / clone
    # ================================================================== #
    def _check_cascade_healthy(self, report: PreflightReport, job: dict):
        """The three cascade relationships must be mirrored and idle."""
        p = self.p
        self._check_relationship_ready(report, p.pivot_cluster,
                                       job["pivot_dest_path"], "pivot")
        self._check_relationship_ready(report, p.dest_cluster,
                                       job["dest_dest_path"], "PROD")
        if job.get("dr_dest_path"):
            self._check_relationship_ready(report, p.dr_cluster,
                                           job["dr_dest_path"], "DR")

    def for_test(self, job: dict, qtrees: Sequence[str],
                 volume_map: Optional[Dict[str, str]] = None,
                 qtree_map: Optional[Dict[str, str]] = None,
                 prune: bool = True) -> PreflightReport:
        p = self.p
        report = self._report("test")
        status = job.get("status", "unknown")

        self._add(report, "CASCADE_NOT_READY",
                  "Replication cascade is complete",
                  status in _CASCADE_READY_STATUSES,
                  detail=f"job status={status}",
                  hint="finish the replication first (create / resume / "
                       "check-status) before building a test environment",
                  target=job.get("job_id", ""))

        existing = (job.get("clone_volumes") or []) if job.get("test_env") \
            else []
        self._add(report, "TEST_ENV_ALREADY_EXISTS",
                  "No test environment already in place", not existing,
                  detail=f"test clones {', '.join(existing)} created "
                         f"{job.get('test_created_at', 'unknown')}"
                         if existing else "none",
                  hint="promote it with action 'clone', or delete its clones "
                       "before building a new one" if existing else "",
                  target=job.get("job_id", ""))

        if status in _CASCADE_READY_STATUSES:
            self._check_cascade_healthy(report, job)

        normalised = self._check_qtrees(report, qtrees)
        self._check_volume_map(report, normalised, volume_map, job)
        self._check_qtree_map(report, normalised, qtree_map, job)
        self._check_prune_plan(report, normalised, volume_map, qtree_map,
                               job, prune)

        # The clone mirror PROD -> DR needs its own peering, policy and
        # schedule on the DR cluster.
        self._check_peering(report, p.dr_cluster, p.dr_vserver,
                            p.dest_cluster, p.dest_vserver,
                            "clone PROD -> clone DR")
        self._check_policy_and_schedule(report, p.dr_cluster,
                                        SNAPMIRROR_POLICY,
                                        SNAPMIRROR_SCHEDULE, "clone mirror")
        return report

    def for_clone(self, job: dict, qtrees: Sequence[str],
                  fresh: bool = False,
                  volume_map: Optional[Dict[str, str]] = None,
                  qtree_map: Optional[Dict[str, str]] = None,
                  prune: bool = True) -> PreflightReport:
        p = self.p
        report = self._report("clone")
        promoting = bool(job.get("test_env") and job.get("clone_volumes")
                         and not fresh)

        if promoting:
            recorded = dict(job.get("volume_map") or {})
            test_qtrees = job.get("test_qtrees") or list(recorded)
            requested = list(dict.fromkeys(self.resolve_qtrees(qtrees)))
            exact = sorted(requested) == sorted(dict.fromkeys(test_qtrees))
            self._add(report, "PROMOTION_QTREE_MISMATCH",
                      "Requested qtrees match the test environment", exact,
                      detail=f"requested={sorted(requested)}, "
                             f"test={sorted(set(test_qtrees))}",
                      hint="promote the exact same qtrees as the test run, or "
                           "use --fresh to rebuild from scratch"
                           if not exact else "",
                      target=job.get("job_id", ""))

            # The clones themselves and their mirrors must be healthy.
            for qtree in test_qtrees:
                clone = recorded.get(qtree, "")
                if not clone:
                    continue
                self._check_volume_present(report, p.dest_cluster,
                                           p.dest_vserver, clone,
                                           f"PROD clone {clone}")
                self._check_volume_present(report, p.dr_cluster, p.dr_vserver,
                                           clone, f"DR clone {clone}")
                self._check_relationship_ready(
                    report, p.dr_cluster, p.path(p.dr_vserver, clone),
                    f"clone {clone}")

            expiry = job.get("test_expires_at")
            if expiry:
                try:
                    expired = (datetime.datetime.fromisoformat(expiry)
                               < datetime.datetime.now())
                except ValueError:
                    expired = False
                self._add(report, "TEST_ENV_EXPIRED",
                          "Test environment still within validity", not expired,
                          detail=f"expired on {expiry}" if expired
                                 else f"valid until {expiry}",
                          hint="promoting an expired environment is allowed "
                               "but confirm the data is still relevant"
                               if expired else "",
                          target=job.get("job_id", ""),
                          severity=SEVERITY_WARNING)
        else:
            status = job.get("status", "unknown") if job else "unknown"
            if job:
                self._add(report, "CASCADE_NOT_READY",
                          "Replication cascade is complete",
                          status in _CASCADE_READY_STATUSES,
                          detail=f"job status={status}",
                          hint="finish the replication before cloning",
                          target=job.get("job_id", ""))
                if status in _CASCADE_READY_STATUSES:
                    self._check_cascade_healthy(report, job)
            normalised = self._check_qtrees(report, qtrees)
            self._check_volume_map(report, normalised, volume_map,
                                   None if fresh else job)
            self._check_qtree_map(report, normalised, qtree_map,
                                  None if fresh else job)
            self._check_prune_plan(report, normalised, volume_map, qtree_map,
                                   None if fresh else job, prune)
            self._check_peering(report, p.dr_cluster, p.dr_vserver,
                                p.dest_cluster, p.dest_vserver,
                                "clone PROD -> clone DR")
            self._check_policy_and_schedule(report, p.dr_cluster,
                                            SNAPMIRROR_POLICY,
                                            SNAPMIRROR_SCHEDULE,
                                            "clone mirror")
            if fresh and job and job.get("test_env"):
                self._add(report, "FRESH_ABANDONS_TEST_ENV",
                          "Fresh mode abandons the existing test environment",
                          False,
                          detail=f"test clones "
                                 f"{', '.join(job.get('clone_volumes', []))} "
                                 f"will be left in place",
                          hint="its clones must be deleted manually once the "
                               "new clones are validated",
                          target=job.get("job_id", ""),
                          severity=SEVERITY_WARNING)

        # Volume moves need a destination aggregate other than the parent's.
        for cluster, svm, aggr_role in ((p.dest_cluster, p.dest_vserver, "PROD"),
                                        (p.dr_cluster, p.dr_vserver, "DR")):
            aggrs, err = self._safe(lambda c=cluster: self.c.list_aggregates(c),
                                    None)
            if aggrs is None:
                self._add(report, "AGGREGATE_LIST_UNREADABLE",
                          f"{aggr_role}: aggregates readable", False,
                          detail=err,
                          hint="grant readonly on /api/storage/aggregates",
                          target=cluster)
                continue
            parent, _ = self._safe(
                lambda c=cluster, s=svm: self.c.get_volume(c, s, p.volume),
                None)
            parent_aggr = parent.aggregate if parent else None
            candidates = [a for a in aggrs
                          if not parent_aggr
                          or a.name.lower() != parent_aggr.lower()]
            self._add(report, "MOVE_TARGET_AGGREGATE_MISSING",
                      f"{aggr_role}: an aggregate is available for the move",
                      bool(candidates),
                      detail=f"{len(candidates)} candidate(s), parent "
                             f"aggregate excluded: {parent_aggr or 'unknown'}",
                      hint="the clone must move to an aggregate different "
                           "from its parent volume's to be detached"
                           if not candidates else "",
                      target=cluster)
        return report

    # ================================================================== #
    # ACTION: acl
    # ================================================================== #
    def for_acl(self, job: Optional[dict], acl_path: str,
                groups: Sequence[str], rights: str) -> PreflightReport:
        p = self.p
        report = self._report("acl")

        raw = (acl_path or "").strip()
        self._add(report, "ACL_PATH_MISSING", "A target path is provided",
                  bool(raw), detail="empty path" if not raw else raw,
                  hint="pass --acl-path /<clone_volume>[/subdir]")
        if not raw:
            return report

        absolute = raw.startswith("/")
        self._add(report, "ACL_PATH_NOT_ABSOLUTE", "Path is absolute",
                  absolute, detail=raw,
                  hint="the path must start with '/' and be relative to the "
                       "SVM namespace")

        traversal = ".." in raw.split("/") or "\\" in raw
        self._add(report, "ACL_PATH_TRAVERSAL", "Path has no traversal segment",
                  not traversal, detail=raw,
                  hint="remove '..' segments and backslashes" if traversal else "")

        canonical = "/" + "/".join(s for s in raw.split("/") if s)
        is_root = canonical == "/"
        self._add(report, "ACL_PATH_IS_ROOT",
                  "Path is not the SVM namespace root", not is_root,
                  detail=canonical,
                  hint="forcing a DACL on '/' would rewrite every volume of "
                       "the SVM; target a specific volume" if is_root else "")
        if report.failures:
            return report

        # The first segment must be a volume of this job (a clone), so an
        # operator cannot rewrite ACLs outside the migration perimeter.
        first = canonical.strip("/").split("/")[0]
        known = list(job.get("clone_volumes", []) or []) if job else []
        if known:
            inside = first in known
            self._add(report, "ACL_PATH_OUTSIDE_JOB",
                      "Path belongs to a volume of this job", inside,
                      detail=f"'{first}' vs job volumes: {', '.join(known)}",
                      hint="target one of the job's clone volumes"
                           if not inside else "",
                      target=job.get("job_id", "") if job else "")
        else:
            self._add(report, "ACL_PATH_UNVERIFIABLE",
                      "Path can be matched against the job's clones", False,
                      detail="this job has no recorded clone volume "
                             "(run 'test' or 'clone' first)",
                      hint="the path is accepted but its perimeter cannot be "
                           "verified — double-check it before confirming",
                      target=job.get("job_id", "") if job else "",
                      severity=SEVERITY_WARNING)

        # The path must actually resolve on the destination SVM.
        self._check_svm(report, p.dest_cluster, p.dest_vserver, "PROD",
                        need_cifs=True)
        exists, err = self._safe(
            lambda: self.c.junction_path_exists(p.dest_cluster,
                                                p.dest_vserver, canonical),
            False)
        self._add(report, "ACL_PATH_NOT_FOUND", "Path exists on the PROD SVM",
                  exists,
                  detail=err or (canonical if exists else
                                 f"no mounted volume matches '{canonical}'"),
                  hint="check the junction path of the clone volume"
                       if not exists else "",
                  target=f"{p.dest_cluster} / {p.dest_vserver}")

        # NTFS security style is required for DACL forcing to mean anything.
        volume_info, _ = self._safe(
            lambda: self.c.get_volume(p.dest_cluster, p.dest_vserver, first),
            None)
        if volume_info and volume_info.security_style:
            style = volume_info.security_style.lower()
            ntfs = style in ("ntfs", "mixed")
            self._add(report, "ACL_SECURITY_STYLE",
                      "Volume security style supports NTFS ACLs", ntfs,
                      detail=f"security-style={style}",
                      hint="DACL forcing requires an NTFS (or mixed) volume"
                           if not ntfs else "",
                      target=f"{p.dest_cluster} / {first}")

        # Groups.
        cleaned = [g.strip() for g in groups if g.strip()]
        self._add(report, "ACL_GROUPS_EMPTY", "At least one AD group provided",
                  bool(cleaned),
                  detail=f"{len(cleaned)} group(s)" if cleaned else "none",
                  hint="pass --ad-groups 'DOMAIN\\\\group'")
        for group in cleaned:
            valid = _AD_GROUP_RE.match(group) is not None
            self._add(report, "ACL_GROUP_SYNTAX",
                      f"AD group '{group}' is well formed", valid,
                      detail=group,
                      hint="expected DOMAIN\\group (escape the backslash in "
                           "the shell)" if not valid else "")
        return report

    # ================================================================== #
    # ACTION: cleanup
    # ================================================================== #
    def _check_prune_plan(self, report: PreflightReport,
                          qtrees: Sequence[str],
                          volume_map: Optional[Dict[str, str]],
                          qtree_map: Optional[Dict[str, str]],
                          job: Optional[dict], enabled: bool):
        """Announce what pruning will delete, before the clones exist.

        A clone inherits exactly the qtrees the source volume holds, so the
        plan is knowable up front — which is the point: the operator sees the
        deletions listed before running the action, not afterwards.
        """
        p = self.p
        volumes = dict((job or {}).get("volume_map") or {})
        volumes.update({k: v for k, v in (volume_map or {}).items() if v})
        renames = dict((job or {}).get("qtree_map") or {})
        renames.update({k: v for k, v in (qtree_map or {}).items() if v})

        if not enabled:
            self._add(report, "PRUNE_DISABLED",
                      "Clones keep every qtree of the source volume", False,
                      severity=SEVERITY_WARNING,
                      detail="pruning is switched off for this run",
                      hint="each client's volume will also hold the other "
                           "clients' qtrees")
            return

        source, err = self._safe(
            lambda: self.c.list_qtrees(p.source_cluster, p.source_vserver,
                                       p.volume), None)
        if source is None:
            self._add(report, "PRUNE_SOURCE_UNREADABLE",
                      "Source qtrees readable (to plan the pruning)", False,
                      severity=SEVERITY_WARNING,
                      detail=err or "could not list the source qtrees",
                      hint="grant readonly on /api/storage/qtrees",
                      target=f"{p.source_cluster} / {p.volume}")
            return

        for qtree in qtrees:
            volume = volumes.get(qtree) or next(
                (v for k, v in volumes.items() if k.lower() == qtree.lower()),
                None)
            if not volume:
                continue                       # VOLUME_MAP_MISSING covers it
            kept = renames.get(qtree, qtree)
            surplus = [q for q in source
                       if q.lower() != qtree.lower() and q not in ("", "-")]
            self._add(report, "PRUNE_PLAN",
                      f"'{volume}': inherited qtrees that will be DELETED",
                      True,
                      severity=SEVERITY_WARNING if surplus else SEVERITY_ERROR,
                      detail=f"keeps '{kept}', deletes {len(surplus)}: "
                             f"{', '.join(surplus)}" if surplus
                             else f"keeps '{kept}', the source holds nothing "
                                  f"else",
                      target=f"{p.dest_cluster} / {p.dest_vserver}:{volume}")

    def for_cleanup(self, job: Optional[dict], qtree: str) -> PreflightReport:
        p = self.p
        report = self._report("cleanup")

        name = (qtree or "").strip()
        self._add(report, "CLEANUP_QTREE_MISSING", "A qtree is provided",
                  bool(name), detail="empty qtree name" if not name else name,
                  hint="pass --qtree <name>; an empty value would match "
                       "every CIFS share of the SVM")
        if not name:
            return report

        self._add(report, "CLEANUP_QTREE_SEPARATOR",
                  "Qtree name has no path separator", "/" not in name,
                  detail=name, hint="pass the qtree name, not a path")

        available, err = self._safe(
            lambda: self.c.list_qtrees(p.source_cluster, p.source_vserver,
                                       p.volume), None)
        target = f"{p.source_cluster} / {p.source_vserver}:{p.volume}"
        if available is None:
            self._add(report, "QTREES_UNREADABLE", "Source qtrees readable",
                      False, detail=err, target=target)
        else:
            exists = name in available
            self._add(report, "CLEANUP_QTREE_NOT_FOUND",
                      "Qtree exists on the source volume", exists,
                      detail=f"source has: {', '.join(available) or 'none'}"
                             if not exists else name,
                      hint="check the qtree name (it may already have been "
                           "renamed by a previous cleanup)" if not exists else "",
                      target=target)

        # Cutting the source is only safe once the target is really in place.
        if job:
            status = job.get("status", "unknown")
            promoted = bool(job.get("clone_promoted_at"))
            self._add(report, "CLEANUP_MIGRATION_INCOMPLETE",
                      "Replication is complete", status == "completed",
                      detail=f"job status={status}",
                      hint="do not cut source access before the migration is "
                           "confirmed complete",
                      target=job.get("job_id", ""))
            self._add(report, "CLEANUP_CLONES_NOT_PROMOTED",
                      "Definitive clones have been promoted", promoted,
                      detail="clone_promoted_at is not set on this job"
                             if not promoted else
                             job.get("clone_promoted_at", ""),
                      hint="run action 'clone' and let the volume moves finish "
                           "before cutting the source",
                      target=job.get("job_id", ""),
                      severity=SEVERITY_WARNING)
        else:
            self._add(report, "CLEANUP_NO_JOB_CONTEXT",
                      "Migration state can be verified", False,
                      detail="no job id provided: the tool cannot confirm the "
                             "migration finished",
                      hint="pass --job-id so the migration state is checked",
                      severity=SEVERITY_WARNING)

        # Show exactly which shares would be deleted (no guessing).
        shares, err = self._safe(
            lambda: self.c.find_cifs_shares(p.source_cluster,
                                            p.source_vserver, f"/{name}"),
            None)
        if shares is None:
            self._add(report, "CIFS_SHARES_UNREADABLE",
                      "CIFS shares readable", False, detail=err,
                      hint="grant readonly on /api/protocols/cifs/shares",
                      target=p.source_cluster, severity=SEVERITY_WARNING)
        else:
            self._add(report, "CLEANUP_SHARES_PREVIEW",
                      "CIFS shares that will be deleted", True,
                      detail=", ".join(shares) if shares
                             else "none matched this qtree",
                      target=f"{p.source_cluster} / {p.source_vserver}",
                      severity=SEVERITY_WARNING if shares else SEVERITY_ERROR)
        return report


def _gib(value: Optional[int]) -> str:
    """Human-readable byte count for check details."""
    if value is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024.0
    return str(value)
