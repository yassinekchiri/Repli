"""Pydantic request/response models for the REST API."""

from typing import List, Optional, Union

from pydantic import BaseModel, Field


def _csv(value: Union[str, List[str], None]) -> Optional[str]:
    """Accept both 'q1,q2' and ['q1','q2']; normalise to CSV."""
    if value is None:
        return None
    if isinstance(value, list):
        return ",".join(value)
    return value


class CreateMigrationRequest(BaseModel):
    source_cluster: str
    pivot_cluster: str
    dest_cluster: str
    dr_cluster: str
    volume: str
    source_vserver: str = "svm_source"
    pivot_vserver: str = "svm_pivot"
    dest_vserver: str = "svm_dest"
    dr_vserver: str = "svm_dr"
    pivot_aggr: str = "aggr1_pivot"
    dest_aggr: str = "aggr1_dest"
    dr_aggr: str = "aggr1_dr"
    noaccess_policy: str = "ep_noaccess"
    create_mode: str = Field("full", pattern="^(full|pivot-only)$")
    timeout: int = 3600
    poll_interval: int = 30
    dry_run: bool = False
    transport: str = Field("rest", pattern="^(rest|ssh)$")


class ResumeRequest(BaseModel):
    confirm: bool = False


class QtreesRequest(BaseModel):
    """clone / test payload."""
    qtrees: Union[str, List[str]]

    @property
    def qtrees_csv(self) -> str:
        return _csv(self.qtrees) or ""


class AclRequest(BaseModel):
    ad_groups: Union[str, List[str]]
    acl_path: Optional[str] = None
    acl_rights: str = Field("full-control",
                            pattern="^(no-access|read|write|modify|full-control)$")
    qtrees: Union[str, List[str], None] = None

    @property
    def ad_groups_csv(self) -> str:
        return _csv(self.ad_groups) or ""

    @property
    def qtrees_csv(self) -> Optional[str]:
        return _csv(self.qtrees)


class CleanupRequest(BaseModel):
    qtree: str


class ActionAccepted(BaseModel):
    """202 answer for long-running background actions."""
    job_id: str
    action: str
    state: str = "running"
    detail: str = ""


class ActionResult(BaseModel):
    """200 answer for synchronous actions."""
    job_id: Optional[str] = None
    action: str
    result: dict = {}
    logs: List[str] = []
