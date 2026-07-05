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
from ..models import MigrationParams, OntapError, ConfirmationRequired
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
                                 "resume", "check-status", "retry"])

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
                        help="(clone / test / acl) CSV list 'q1,q2' or 'all'.")
    parser.add_argument("--qtree", help="(cleanup) single target qtree.")
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
    parser.add_argument("--yes", action="store_true",
                        help="(resume) skip the interactive confirmation.")

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
    if args.action == "test" and not args.qtrees:
        parser.error("--qtrees is required for --action test.")
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

def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

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
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

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
            engine.clone(args.qtrees, job=job)
        elif args.action == "test":
            engine.test(args.qtrees, job=job,
                        validity_days=args.test_validity_days)
        elif args.action == "acl":
            engine.acl(args.ad_groups, acl_path=args.acl_path,
                       acl_rights=args.acl_rights, job=job)
        elif args.action == "cleanup":
            engine.cleanup(args.qtree)
        logger.info("SUCCESS: action '%s' completed without error.", args.action)
        return 0

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
