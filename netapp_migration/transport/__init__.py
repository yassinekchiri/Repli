"""Transport factory: pick the right OntapClient for the run."""

import logging
from typing import Callable

from ..models import MigrationParams, ClusterCredentials
from .base import OntapClient
from .dryrun import DryRunClient
from .rest import RestClient
from .ssh import SshClient


def build_client(params: MigrationParams, logger: logging.Logger,
                 credentials_for: Callable[[str], ClusterCredentials]
                 ) -> OntapClient:
    """Instantiate the transport selected by the parameters.

    Priority: dry_run beats everything, then params.transport
    ('rest' default / 'ssh' fallback).
    """
    if params.dry_run:
        logger.info("DRY-RUN MODE: no cluster will be contacted.")
        return DryRunClient(logger)
    if params.transport == "ssh":
        logger.info("Transport: ONTAP CLI over SSH (backend=%s).",
                    params.ssh_backend)
        return SshClient(logger, ssh_backend=params.ssh_backend,
                         ssh_user=params.ssh_user)
    logger.info("Transport: ONTAP REST API (basic auth).")
    return RestClient(logger, credentials_for)
