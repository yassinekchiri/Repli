"""Command-line interface.

Same actions and flags as the historical single-file script, plus the
transport selection:

    --transport rest|ssh     (default: rest — ONTAP REST API, basic auth)
    --config PATH            JSON file with per-cluster REST credentials
    --api-user USER          username override (password via env)
    --insecure               skip TLS verification (self-signed certs)
    --job-dir PATH           where job files live (default: CWD)

Console shows the high-level progress only; the log file receives the
full DEBUG trace (every REST call / SSH command with its payloads).

Exit codes:
    0  success
    1  bad invocation (unknown job, unreadable credentials file)
    2  ONTAP failure during execution
    3  unexpected failure
    4  action refused by the pre-flight checks (nothing was modified)
"""

import argparse
import datetime
import logging
import os
import sys
from typing import List, Optional

from ..config import CredentialsResolver, job_dir
from ..core.engine import MigrationEngine
from ..core.jobs import JobStore, JobNotFound
from ..models import (ConfigError, MigrationParams, OntapError, ConfirmationRequired,
                      PreflightFailed, AuthError, ForbiddenError, Principal,
                      SUPER_ADMIN)
from ..security import csvio
from ..security.tokens import TokenStore, default_store_path
from ..transport import build_client


# =============================================================================
# LOGGING
# =============================================================================

class _ConsoleFormatter(logging.Formatter):
    """Compact console format: time only, level prefix only for non-INFO."""
    _PREFIX = {logging.WARNING: "[WARN]  ", logging.ERROR: "[ERROR] ",
               logging.CRITICAL: "[CRIT]  "}

    def format(self, record):
        prefix = self._PREFIX.get(record.levelno, "")
        return f"{self.formatTime(record, '%H:%M:%S')}  {prefix}{record.getMessage()}"


def setup_logging(log_file: str) -> logging.Logger:
    """File handler: DEBUG (full trace). Console handler: INFO (progress)."""
    logger = logging.getLogger("netapp_migration.cli")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(_ConsoleFormatter())
    logger.addHandler(console)
    return logger


# =============================================================================
# ARGUMENTS
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netapp_cascade_migration",
        description="NetApp ONTAP cascading migration (Y fan-out: "
                    "Source -> Pivot -> PROD + DR).",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--action", required=True,
                        choices=["create", "clone", "test", "acl", "cleanup",
                                 "resume", "check-status", "retry",
                                 "tokens-init", "tokens-import",
                                 "tokens-list", "tokens-revoke",
                                 "tokens-set-scope", "api-unlock"])

    # Topology (create / clone-without-job / cleanup)
    parser.add_argument("--source-cluster")
    parser.add_argument("--pivot-cluster")
    parser.add_argument("--dest-cluster")
    parser.add_argument("--dr-cluster")
    parser.add_argument("--volume")
    parser.add_argument("--source-vserver", default="svm_source")
    parser.add_argument("--pivot-vserver", default="svm_pivot")
    parser.add_argument("--dest-vserver", default="svm_dest")
    parser.add_argument("--dr-vserver", default="svm_dr")
    parser.add_argument("--pivot-aggr", default="aggr1_pivot")
    parser.add_argument("--dest-aggr", default="aggr1_dest")
    parser.add_argument("--dr-aggr", default="aggr1_dr")
    parser.add_argument("--noaccess-policy", default="ep_noaccess")

    # Action-specific
    parser.add_argument("--create-mode", choices=["full", "pivot-only"],
                        default="full")
    parser.add_argument("--job-id",
                        help="(resume / check-status / retry / clone / test / "
                             "acl) job ID of a previous create run.")
    parser.add_argument("--qtrees",
                        help="(clone / test / cleanup) CSV list 'q1,q2' or "
                             "'all'.")
    parser.add_argument("--qtree",
                        help="(cleanup) one qtree — the singular form of "
                             "--qtrees, kept for existing scripts.")
    parser.add_argument("--ad-groups",
                        help="(acl) CSV AD groups, e.g. 'DOM\\\\grp1,DOM\\\\grp2'.")
    parser.add_argument("--acl-path",
                        help="(acl) target path on the destination vserver, "
                             "e.g. '/v_q_fin_8072b8/projects'. Required.")
    parser.add_argument("--acl-rights", default="full-control",
                        choices=["no-access", "read", "write", "modify",
                                 "full-control"])
    parser.add_argument("--test-validity-days", type=int, default=7,
                        help="(test) validity of the test environment in "
                             "days (default: 7).")
    parser.add_argument("--fresh", action="store_true",
                        help="(clone) start from a clean base even if a test "
                             "environment exists: the full flow runs and the "
                             "old test clones are left to delete manually.")
    parser.add_argument("--no-prune", action="store_true",
                        help="(test / clone) keep, in each clone, the qtrees "
                             "it inherited from the source volume. By default "
                             "a clone keeps only the qtree it was created "
                             "for — the others are deleted from the CLONE "
                             "(never from the source).")
    parser.add_argument("--yes", action="store_true",
                        help="(resume) skip the interactive confirmation.")
    parser.add_argument("--volume-map",
                        help="(test / clone) CSV naming, per qtree, the target "
                             "volume and optionally the qtree's new name "
                             "inside it: header 'qtree,volume[,new_qtree]'.")

    # Authentication
    parser.add_argument("--token",
                        help="API token. Omit to be prompted (never echoed). "
                             "The global token grants super-admin rights.")
    parser.add_argument("--token-store", default=None,
                        help=f"encrypted token store "
                             f"(default: {default_store_path()}).")
    parser.add_argument("--scope-csv",
                        help="(tokens-import) CSV: qtree,token,actions[,label] "
                             "— use NEW_TOKEN to have one generated.")
    parser.add_argument("--scope-out",
                        help="(tokens-import) write the answer CSV here "
                             "(contains the generated tokens).")
    parser.add_argument("--token-id",
                        help="(tokens-revoke / tokens-set-scope) target token.")
    parser.add_argument("--grant-qtrees",
                        help="(tokens-set-scope) new qtree list, comma-separated.")
    parser.add_argument("--grant-actions",
                        help="(tokens-set-scope) new action list, comma-separated.")
    parser.add_argument("--unlock-socket", default=None,
                        help="(api-unlock) unlock socket of a running API "
                             "started with --start-locked (default: "
                             "unlock.sock beside the token store).")

    # Transport
    parser.add_argument("--transport", choices=["rest", "ssh"], default="rest",
                        help="ONTAP transport (default: rest).")
    parser.add_argument("--config",
                        help="(rest) JSON credentials file "
                             "(default: $NETAPP_MIGRATION_CONFIG).")
    parser.add_argument("--api-user",
                        help="(rest) username override "
                             "(password via $NETAPP_API_PASSWORD or config).")
    parser.add_argument("--insecure", action="store_true",
                        help="(rest) skip TLS certificate verification.")
    parser.add_argument("--ssh-backend", choices=["subprocess", "paramiko"],
                        default="subprocess")
    parser.add_argument("--ssh-user", default=None)

    # Execution
    parser.add_argument("--job-dir", default=None,
                        help="Job files directory "
                             "(default: $NETAPP_MIGRATION_JOB_DIR or CWD).")
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--poll-interval", type=int, default=30)
    return parser


def validate_args(args, parser):
    job_based = ("resume", "check-status", "retry", "test", "acl")
    if args.action in job_based and not args.job_id:
        parser.error(f"--job-id is required for --action {args.action}.")
    if args.action == "test":
        if not args.qtrees:
            parser.error("--qtrees is required for --action test.")
        if not args.volume_map:
            parser.error("--volume-map is required for --action test "
                         "(CSV 'qtree,volume': the target volume name for "
                         "each qtree).")
    if args.action == "acl":
        if not args.ad_groups:
            parser.error("--ad-groups is required for --action acl.")
        if not args.acl_path:
            parser.error("--acl-path is required for --action acl "
                         "(target path on the destination vserver).")
    if args.action == "clone":
        if not args.qtrees:
            parser.error("--qtrees is required for --action clone.")
        if not args.job_id:
            _require_topology(args, parser)
    if args.action == "create":
        _require_topology(args, parser)
        if not args.dr_cluster:
            parser.error("--dr-cluster is required for --action create.")
    if args.action == "cleanup":
        _require_topology(args, parser)
        if not args.qtree:
            parser.error("--qtree is required for --action cleanup.")


def _require_topology(args, parser):
    for flag in ("source_cluster", "pivot_cluster", "dest_cluster", "volume"):
        if not getattr(args, flag, None):
            parser.error(f"--{flag.replace('_', '-')} is required "
                         f"for --action {args.action}.")


def params_from_args(args) -> MigrationParams:
    return MigrationParams(
        source_cluster=args.source_cluster, pivot_cluster=args.pivot_cluster,
        dest_cluster=args.dest_cluster, dr_cluster=args.dr_cluster or "",
        volume=args.volume,
        source_vserver=args.source_vserver, pivot_vserver=args.pivot_vserver,
        dest_vserver=args.dest_vserver, dr_vserver=args.dr_vserver,
        pivot_aggr=args.pivot_aggr, dest_aggr=args.dest_aggr,
        dr_aggr=args.dr_aggr, noaccess_policy=args.noaccess_policy,
        timeout=args.timeout, poll_interval=args.poll_interval,
        dry_run=args.dry_run, transport=args.transport,
        ssh_backend=args.ssh_backend, ssh_user=args.ssh_user,
        log_file=args.log_file)


# =============================================================================
# ENTRY POINT
# =============================================================================

TOKEN_ACTIONS = ("tokens-init", "tokens-import", "tokens-list",
                 "tokens-revoke", "tokens-set-scope")

# Which scope name guards each migration action.
ACTION_SCOPE = {"create": "create", "resume": "resume", "retry": "retry",
                "check-status": "status", "test": "test", "clone": "clone",
                "acl": "acl", "cleanup": "cleanup"}


def _prompt_token(prompt: str) -> str:
    """Read a token without echoing it (never in argv, never in history)."""
    import getpass
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def _authenticate(args) -> Principal:
    """Resolve the CLI caller into a Principal.

    Enforcement starts as soon as a token store exists on the machine: a
    freshly installed tool with no store keeps working unauthenticated for
    the local admin, which is what makes 'tokens-init' possible.
    """
    store = TokenStore(args.token_store)
    if not store.exists:
        return SUPER_ADMIN
    token = args.token or _prompt_token("Token: ")
    try:
        # The global token both unlocks the store and identifies the super
        # admin; a delegated token cannot decrypt it, so we try that first.
        store.unlock(token)
        return store.authenticate(token)
    except AuthError:
        pass
    # Delegated token: the store can only be opened with the global one, so
    # a scoped caller must go through the API instead.
    raise AuthError(
        "this token cannot open the local token store",
        hint="delegated tokens are meant for the REST API; only the super "
             "admin (global token) can drive the CLI directly")


def _run_api_unlock(args) -> int:
    """Hand the global token to a locked, already-running API."""
    from ..interfaces.api.unlock import request_unlock, default_socket_path

    store = TokenStore(args.token_store)
    path = args.unlock_socket or default_socket_path(store.path)

    token = args.token or _prompt_token("Global token (super admin): ")
    if not token:
        print("ERROR: no token supplied.", file=sys.stderr)
        return 1

    try:
        answer = request_unlock(path, token)
    except FileNotFoundError:
        print(f"ERROR: no unlock socket at {path}", file=sys.stderr)
        print("       the API is not running, or was not started with "
              "--start-locked.", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"ERROR: not allowed to open {path}", file=sys.stderr)
        print("       run this as the service account or as root.",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: cannot reach {path}: {exc}", file=sys.stderr)
        return 1

    if answer.startswith("OK"):
        print(f"API unlocked ({answer[3:].strip()}).")
        return 0
    print(f"ERROR: {answer[4:].strip() or 'unlock refused'}", file=sys.stderr)
    return 1


def _run_token_action(args) -> int:
    """tokens-* actions: local administration of the encrypted store."""
    action = args.action
    store = TokenStore(args.token_store)

    if action == "tokens-init":
        if store.exists:
            print(f"ERROR: a token store already exists at {store.path}",
                  file=sys.stderr)
            return 1
        first = _prompt_token("New global token (super admin): ")
        again = _prompt_token("Confirm global token: ")
        if not first or first != again:
            print("ERROR: the two entries differ (or were empty).",
                  file=sys.stderr)
            return 1
        try:
            store.initialise(first)
        except AuthError as exc:
            print(f"ERROR: {exc.message}", file=sys.stderr)
            return 1
        print(f"Token store created: {store.path}")
        print("Keep the global token safe: it is never written anywhere and "
              "cannot be recovered.")
        print("Start the API with:")
        print("  python3 -m netapp_migration.interfaces.api.serve")
        return 0

    if not store.exists:
        print(f"ERROR: no token store at {store.path}", file=sys.stderr)
        print("       create it first: --action tokens-init", file=sys.stderr)
        return 1
    try:
        store.unlock(args.token or _prompt_token("Global token: "))
    except AuthError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return 1

    try:
        if action == "tokens-list":
            scopes = store.list_scopes()
            if not scopes:
                print("No delegated token.")
                return 0
            width = max(len(s.token_id) for s in scopes)
            print(f"{'TOKEN ID'.ljust(width)}  {'QTREES':<28}  ACTIONS")
            for scope in scopes:
                print(f"{scope.token_id.ljust(width)}  "
                      f"{','.join(scope.qtrees):<28}  "
                      f"{','.join(scope.actions)}"
                      + (f"   [{scope.label}]" if scope.label else ""))
            return 0

        if action == "tokens-import":
            if not args.scope_csv:
                print("ERROR: --scope-csv is required.", file=sys.stderr)
                return 1
            rows = csvio.parse_scope_csv(csvio.read_file(args.scope_csv))
            results = []
            for row in rows:
                outcome = store.upsert(row["qtree"], row["actions"],
                                       row["token"], row["label"])
                outcome["label"] = row["label"]
                results.append(outcome)
            answer = csvio.render_scope_csv(results)
            if args.scope_out:
                fd = os.open(args.scope_out,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(answer)
                print(f"Answer CSV written to {args.scope_out} (mode 0600).")
                print("It contains the generated tokens in clear: hand them "
                      "to their owners, then delete the file.")
            else:
                print(answer, end="")
            created = sum(1 for r in results if r["status"] == "created")
            print(f"\n{created} token(s) created, "
                  f"{len(results) - created} scope(s) updated.")
            return 0

        if action == "tokens-revoke":
            if not args.token_id:
                print("ERROR: --token-id is required.", file=sys.stderr)
                return 1
            store.revoke(args.token_id)
            print(f"Token {args.token_id} revoked.")
            return 0

        if action == "tokens-set-scope":
            if not args.token_id:
                print("ERROR: --token-id is required.", file=sys.stderr)
                return 1
            qtrees = ([q.strip() for q in args.grant_qtrees.split(",")]
                      if args.grant_qtrees else None)
            actions = ([a.strip() for a in args.grant_actions.split(",")]
                       if args.grant_actions else None)
            if qtrees is None and actions is None:
                print("ERROR: give --grant-qtrees and/or --grant-actions.",
                      file=sys.stderr)
                return 1
            scope = store.set_scope(args.token_id, qtrees=qtrees,
                                    actions=actions)
            print(f"{scope.token_id}: qtrees={','.join(scope.qtrees)} "
                  f"actions={','.join(scope.actions)}")
            return 0
    except (AuthError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        print(f"ERROR: {message}", file=sys.stderr)
        hint = getattr(exc, "hint", "")
        if hint:
            print(f"       {hint}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.action == "api-unlock":
        return _run_api_unlock(args)

    if args.action in TOKEN_ACTIONS:
        return _run_token_action(args)

    validate_args(args, parser)

    # Authentication: enforced as soon as a token store exists.
    try:
        principal = _authenticate(args)
    except AuthError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"       {exc.hint}", file=sys.stderr)
        return 5
    try:
        scope_qtrees = ([q.strip() for q in (args.qtrees or "").split(",")
                         if q.strip() and q.strip().lower() != "all"]
                        or ([args.qtree] if args.qtree else []))
        principal.authorise(ACTION_SCOPE.get(args.action, args.action),
                            scope_qtrees)
    except ForbiddenError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"       {exc.hint}", file=sys.stderr)
        return 5

    store = JobStore(args.job_dir or job_dir())

    # ---- Resolve parameters: from the job file or from the CLI -----------
    job = None
    if args.job_id:
        try:
            job = store.load(args.job_id)
        except (JobNotFound, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        params = store.params_of(job)
        # Runtime knobs may be overridden on the command line.
        params.dry_run = params.dry_run or args.dry_run
        if args.transport != "rest" or params.transport not in ("rest", "ssh"):
            params.transport = args.transport
    else:
        params = params_from_args(args)

    # A simulated run must never rewrite the state of a real job.
    if params.dry_run:
        store = JobStore(args.job_dir or job_dir(), read_only=True)

    if not args.log_file:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.log_file = f"migration_{args.action}_{stamp}.log"
    logger = setup_logging(args.log_file)

    logger.info("=" * 64)
    logger.info("NetApp ONTAP orchestration — action '%s'", args.action)
    logger.info("Transport: %s%s | Log file: %s",
                params.transport, " (DRY-RUN)" if params.dry_run else "",
                args.log_file)
    logger.info("=" * 64)

    try:
        resolver = CredentialsResolver(args.config,
                                       username_override=args.api_user,
                                       insecure=args.insecure)
    except ConfigError as exc:
        # Same failure the API reports as 503: a file the operator must fix.
        print(f"ERROR: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"       {exc.hint}", file=sys.stderr)
        return 1

    # Names chosen by the client: the target volume per qtree, and
    # optionally the name the qtree takes inside it.
    volume_map = None
    qtree_map = None
    if args.volume_map:
        try:
            clone_map = csvio.parse_clone_map_csv(
                csvio.read_file(args.volume_map))
        except (OSError, ValueError) as exc:
            logger.error("Cannot read --volume-map: %s", exc)
            return 1
        volume_map, qtree_map = csvio.split_clone_map(clone_map)
        logger.info("Volume mapping: %s",
                    ", ".join(f"{k} -> {v}" for k, v in volume_map.items()))
        if qtree_map:
            logger.info("Qtree renaming: %s",
                        ", ".join(f"{k} -> {v}" for k, v in qtree_map.items()))

    client = build_client(params, logger, resolver)
    engine = MigrationEngine(client, params, store, logger)

    try:
        if args.action == "create":
            engine.create(create_mode=args.create_mode)
        elif args.action == "resume":
            _run_resume(engine, job, args, logger)
        elif args.action == "check-status":
            engine.check_status(job)
        elif args.action == "retry":
            engine.retry(job)
        elif args.action == "clone":
            if job is None:
                engine.check_clone_prerequisites()
            engine.clone(args.qtrees, job=job, fresh=args.fresh,
                         volume_map=volume_map, qtree_map=qtree_map,
                         prune=not args.no_prune)
        elif args.action == "test":
            engine.test(args.qtrees, job=job,
                        validity_days=args.test_validity_days,
                        volume_map=volume_map, qtree_map=qtree_map,
                        prune=not args.no_prune)
        elif args.action == "acl":
            engine.acl(args.ad_groups, acl_path=args.acl_path,
                       acl_rights=args.acl_rights, job=job)
        elif args.action == "cleanup":
            engine.cleanup(args.qtrees or args.qtree, job=job)
        logger.info("SUCCESS: action '%s' completed without error.", args.action)
        return 0

    except PreflightFailed as exc:
        # The report itself was already rendered as a table by the engine.
        logger.error("")
        logger.error("ACTION REFUSED — %s", exc.report.summary())
        logger.error("Nothing was modified on any cluster.")
        logger.error("Fix the checks marked FAIL above, then run the same "
                     "command again.")
        _report_job_id(engine, logger)
        return 4
    except OntapError as exc:
        logger.error("ONTAP FAILURE: %s", exc)
        _report_job_id(engine, logger)
        logger.error("Execution interrupted. Check the log: %s", args.log_file)
        return 2
    except Exception as exc:  # noqa: BLE001 — last-resort catch for the CLI
        logger.exception("UNEXPECTED FAILURE: %s", exc)
        _report_job_id(engine, logger)
        return 3


def _run_resume(engine, job, args, logger):
    """resume with interactive confirmation (or --yes)."""
    try:
        engine.resume(job, confirm=args.yes)
    except ConfirmationRequired:
        try:
            answer = input("  Proceed with destination replication? [y/N] ")
        except EOFError:
            answer = "n"
        if answer.strip().lower() == "y":
            engine.resume(job, confirm=True)
        else:
            logger.info("  Not proceeding. Job ID: %s", job["job_id"])


def _report_job_id(engine, logger):
    if not engine.job_id:
        return
    script = os.path.basename(sys.argv[0])
    logger.error("=" * 64)
    logger.error("SCRIPT INTERRUPTED — job file preserved.")
    logger.error("Job ID : %s", engine.job_id)
    logger.error("Retry  : python3 %s --action retry --job-id %s",
                 script, engine.job_id)
    logger.error("=" * 64)


if __name__ == "__main__":
    sys.exit(main())
