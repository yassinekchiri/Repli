"""Pydantic request/response models for the REST API."""

from typing import Dict, List, Optional, Union

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


def _mapping(value) -> Dict[str, str]:
    """Accept {"q": "vol"} or [{"qtree": "q", "volume": "vol"}] or CSV text."""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip() for k, v in value.items()}
    if isinstance(value, str):
        from ...security.csvio import parse_volume_map_csv
        return parse_volume_map_csv(value)
    out: Dict[str, str] = {}
    for item in value:
        if isinstance(item, dict):
            qtree = item.get("qtree") or item.get("q")
            volume = item.get("volume") or item.get("volume_name")
            if qtree and volume:
                out[str(qtree).strip()] = str(volume).strip()
    return out


class QtreesRequest(BaseModel):
    """Payload of the qtree-scoped actions.

    volume_map gives the target volume name chosen by the client for each
    qtree. Three shapes are accepted:

        {"volume_map": {"q_fin": "vol_finance_prod"}}
        {"volume_map": [{"qtree": "q_fin", "volume": "vol_finance_prod"}]}
        {"volume_map": "qtree,volume\nq_fin,vol_finance_prod\n"}
    """
    qtrees: Union[str, List[str]]
    volume_map: Union[Dict[str, str], List[dict], str, None] = None

    @property
    def qtrees_csv(self) -> str:
        return _csv(self.qtrees) or ""

    @property
    def mapping(self) -> Dict[str, str]:
        return _mapping(self.volume_map)


class TestRequest(QtreesRequest):
    """test payload: qtrees + validity of the test environment."""
    validity_days: int = Field(7, ge=1, le=365)


class CloneRequest(QtreesRequest):
    """clone payload: qtrees + optional fresh start.

    fresh=true ignores an existing test environment and runs the full flow
    on a clean base (the old test clones are left to delete manually).
    """
    fresh: bool = False


class AclRequest(BaseModel):
    """acl payload: decoupled from test/clone — one explicit path."""
    ad_groups: Union[str, List[str]]
    acl_path: str
    acl_rights: str = Field("full-control",
                            pattern="^(no-access|read|write|modify|full-control)$")

    @property
    def ad_groups_csv(self) -> str:
        return _csv(self.ad_groups) or ""


class CleanupRequest(BaseModel):
    qtree: str


class ActionAccepted(BaseModel):
    """202 answer for long-running background actions."""
    job_id: str
    action: str
    state: str = "running"
    detail: str = ""


class PreflightResponse(BaseModel):
    """Result of a feasibility check run on its own (no mutation)."""
    action: str
    ok: bool
    simulated: bool = False
    summary: str = ""
    failed_count: int = 0
    warning_count: int = 0
    checks: List[dict] = []


class PreflightCreateRequest(CreateMigrationRequest):
    """Same body as a create, but only the checks are run."""


class PreflightActionRequest(BaseModel):
    """Optional body of POST /migrations/{id}/preflight/{action}."""
    qtrees: Union[str, List[str], None] = None
    volume_map: Union[Dict[str, str], List[dict], str, None] = None
    acl_path: Optional[str] = None
    ad_groups: Union[str, List[str], None] = None
    qtree: Optional[str] = None
    fresh: bool = False

    @property
    def qtrees_csv(self) -> str:
        return _csv(self.qtrees) or ""

    @property
    def mapping(self) -> Dict[str, str]:
        return _mapping(self.volume_map)

    @property
    def ad_groups_list(self) -> List[str]:
        return [g.strip() for g in (_csv(self.ad_groups) or "").split(",")
                if g.strip()]


# =============================================================================
# Authentication / scopes
# =============================================================================

class ScopeCsvRequest(BaseModel):
    """Super-admin import of the qtree/token/actions CSV.

    `csv` is the file content. NEW_TOKEN in the token column asks the API to
    generate a token; the answer returns it in clear exactly once.
    """
    csv: str


class ScopeCsvResponse(BaseModel):
    csv: str
    created: int = 0
    updated: int = 0
    tokens: List[dict] = []


class ScopeUpdateRequest(BaseModel):
    """Dynamic scope change (super admin)."""
    qtrees: Union[str, List[str], None] = None
    actions: Union[str, List[str], None] = None
    label: Optional[str] = None

    def as_list(self, value) -> Optional[List[str]]:
        if value is None:
            return None
        text = _csv(value) or ""
        return [v.strip() for v in text.split(",") if v.strip()]


class ScopeResponse(BaseModel):
    token_id: str
    qtrees: List[str] = []
    actions: List[str] = []
    label: str = ""
    created_at: str = ""
    updated_at: str = ""


class WhoAmIResponse(BaseModel):
    principal: str
    super_admin: bool
    token_id: str = ""
    qtrees: List[str] = []
    actions: List[str] = []


class ActionResult(BaseModel):
    """200 answer for synchronous actions."""
    job_id: Optional[str] = None
    action: str
    result: dict = {}
    logs: List[str] = []
