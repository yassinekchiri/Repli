"""Configuration: REST credentials resolution and job directory.

Credential lookup order for a given cluster:

    1. The per-cluster entry in the JSON config file
       (--config PATH, or env NETAPP_MIGRATION_CONFIG)
    2. The 'defaults' entry of the same config file
    3. Environment variables NETAPP_API_USER / NETAPP_API_PASSWORD

Config file format (see README):

    {
      "defaults": {"username": "admin", "password": "...",
                   "verify_ssl": false, "port": 443},
      "clusters": {
        "CMOPARPA4SFS100": {"username": "svc_migration", "password": "..."},
        "CMOPARDC5SFS100": {"verify_ssl": true}
      }
    }
"""

import json
import os
from typing import Optional

from .models import ClusterCredentials, ConfigError, OntapError

ENV_CONFIG = "NETAPP_MIGRATION_CONFIG"
ENV_USER = "NETAPP_API_USER"
ENV_PASSWORD = "NETAPP_API_PASSWORD"
ENV_JOB_DIR = "NETAPP_MIGRATION_JOB_DIR"


class CredentialsResolver:
    """Callable resolving basic-auth credentials per cluster (see module doc).

    :param username_override: forces the username for every cluster
        (CLI flag --api-user).
    :param insecure: forces verify_ssl=False for every cluster
        (CLI flag --insecure).
    """

    def __init__(self, config_path: Optional[str] = None,
                 username_override: Optional[str] = None,
                 insecure: bool = False):
        self._config: dict = {}
        self._username_override = username_override
        self._insecure = insecure
        path = config_path or os.environ.get(ENV_CONFIG)
        self.path = path
        if path:
            # Every failure below is an operator problem with a specific file,
            # so it is raised as ConfigError with the path and the fix — never
            # as a bare OSError that would surface as an opaque HTTP 500.
            if not os.path.isfile(path):
                raise ConfigError(
                    f"credentials file not found: {path}",
                    hint=f"create it (see README section 2.3), or point "
                         f"${ENV_CONFIG} at the right file",
                    path=path)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._config = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"credentials file is not valid JSON: {path} "
                    f"(line {exc.lineno}, column {exc.colno}: {exc.msg})",
                    hint="a trailing comma or an unquoted value is the usual "
                         "cause; check with: python3 -m json.tool " + path,
                    path=path) from exc
            except OSError as exc:
                raise ConfigError(
                    f"cannot read the credentials file {path}: {exc.strerror}",
                    hint="the API account must be able to read it "
                         "(mode 600, owned by the service user)",
                    path=path) from exc
            if not isinstance(self._config, dict):
                raise ConfigError(
                    f"credentials file must contain a JSON object: {path}",
                    hint='expected {"defaults": {...}, "clusters": {...}}',
                    path=path)

    def __call__(self, cluster: str) -> ClusterCredentials:
        defaults = self._config.get("defaults", {}) or {}
        per_cluster = (self._config.get("clusters", {}) or {}).get(cluster, {})
        merged = {**defaults, **per_cluster}
        if self._insecure:
            merged["verify_ssl"] = False

        username = (self._username_override or merged.get("username")
                    or os.environ.get(ENV_USER))
        password = merged.get("password") or os.environ.get(ENV_PASSWORD)
        if not username or not password:
            raise OntapError(
                cluster, "credentials lookup",
                f"no REST credentials found for cluster '{cluster}': provide a "
                f"config file entry (--config / ${ENV_CONFIG}) or set "
                f"${ENV_USER} and ${ENV_PASSWORD}")
        return ClusterCredentials(
            username=username,
            password=password,
            verify_ssl=bool(merged.get("verify_ssl", True)),
            port=int(merged.get("port", 443)),
        )


def job_dir() -> str:
    """Directory where job files are stored (default: current directory)."""
    return os.environ.get(ENV_JOB_DIR) or os.getcwd()
