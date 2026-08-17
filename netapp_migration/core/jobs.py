"""Job file store.

Same on-disk format as the historical single-file script, so existing job
files keep working:

    netapp_migration_<YYYYMMDD_HHMMSS_6hex>.json

    {
      "job_id": ..., "created_at": ..., "status": ...,
      "create_mode": ..., "pivot_dest_path": ..., "dest_dest_path": ...,
      "dr_dest_path": ..., "params": {...},
      "clone_uid": ..., "clone_volumes": [...]        # after clone/test
    }
"""

import datetime
import json
import os
import re
import uuid
from typing import List, Optional

from ..models import MigrationParams

_JOB_FILE_PREFIX = "netapp_migration_"
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# Ordered checkpoints written by action 'create'; 'retry' uses this list
# to skip phases that already completed.
CREATE_STATUS_ORDER = [
    "started",
    "space_checked",
    "volumes_created",
    "relationships_created",
    "pivot_initialized",
    "dest_initialized",
    "completed",
]


class JobNotFound(FileNotFoundError):
    pass


class JobStore:
    """Reads/writes migration job files in a single directory.

    read_only=True turns every write into a no-op: used by simulated
    (--dry-run) runs so a simulation can never rewrite the state of a real
    job. Reads keep working normally.
    """

    def __init__(self, directory: str, read_only: bool = False):
        self.directory = directory
        self.read_only = read_only
        if not read_only and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

    # ------------------------------------------------------------------ #
    def _path(self, job_id: str) -> str:
        # Reject path separators and anything exotic: the job id is used to
        # build a file name (path-traversal hardening).
        if not _JOB_ID_RE.match(job_id):
            raise ValueError(f"invalid job id: {job_id!r}")
        return os.path.join(self.directory, f"{_JOB_FILE_PREFIX}{job_id}.json")

    @staticmethod
    def generate_job_id() -> str:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ #
    def create(self, params: MigrationParams, create_mode: str = "full") -> dict:
        """Create and persist a new job record; returns the job dict."""
        job_id = self.generate_job_id()
        job = {
            "job_id": job_id,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "started",
            "create_mode": create_mode,
            "pivot_dest_path": params.path(params.pivot_vserver, params.volume),
            "dest_dest_path": params.path(params.dest_vserver, params.volume),
            "dr_dest_path": params.path(params.dr_vserver, params.volume),
            "params": params.to_dict(),
        }
        self.save(job)
        return job

    def save(self, job: dict) -> str:
        path = self._path(job["job_id"])
        if self.read_only:
            # Simulated run: keep the in-memory job up to date, never touch
            # the file on disk.
            return path
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job, fh, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def load(self, job_id: str) -> dict:
        path = self._path(job_id)
        if not os.path.isfile(path):
            raise JobNotFound(
                f"job file not found for job id '{job_id}' "
                f"(expected: {path}). Make sure you are using the same "
                f"job directory as the original run.")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def set_status(self, job: dict, status: str) -> None:
        job["status"] = status
        self.save(job)

    def list_jobs(self) -> List[dict]:
        """All job records in the directory, newest first."""
        jobs = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        for name in sorted(names, reverse=True):
            if not (name.startswith(_JOB_FILE_PREFIX) and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(self.directory, name),
                          "r", encoding="utf-8") as fh:
                    jobs.append(json.load(fh))
            except (OSError, ValueError):
                continue
        return jobs

    def params_of(self, job: dict) -> MigrationParams:
        return MigrationParams.from_dict(job.get("params", {}))
