"""CSV exchange formats.

Two files travel between the super admin and the tool:

1. **Scope CSV** — grants: one line per qtree, with the token that owns it
   and the actions it may run. `NEW_TOKEN` asks the API to mint one.

       qtree,token,actions,label
       q_fin,NEW_TOKEN,"test,clone,acl",Finance
       q_hr,NEW_TOKEN,test,HR
       q_ops,mtk_xxxxx...,"test,acl",Ops

   The answer is the same CSV with the `token` column filled in for every
   freshly generated token, plus `token_id` and `status`. That answer is the
   ONLY place a delegated token appears in clear: it is never stored.

2. **Volume-map CSV** — clone naming: the target volume name chosen by the
   client for each qtree (no generated suffix).

       qtree,volume
       q_fin,vol_finance_prod
       q_hr,vol_rh_prod
"""

import csv
import io
from typing import Dict, List, Optional, Sequence

from ..models import AuthError

NEW_TOKEN = "NEW_TOKEN"

SCOPE_COLUMNS = ("qtree", "token", "actions")
SCOPE_OUTPUT_COLUMNS = ("qtree", "token", "actions", "label", "token_id",
                        "status")
VOLUME_MAP_COLUMNS = ("qtree", "volume")
# Third column, optional: the name the qtree takes inside the clone volume.
# Left out or empty, the qtree keeps the name it has on the source.
CLONE_MAP_COLUMNS = ("qtree", "volume", "new_qtree")
# ONTAP rejects these outright; catching them here turns a cluster-side
# failure halfway through a run into a refusal before anything is created.
_ILLEGAL_QTREE_CHARS = set('/\\:*?"<>|')
_MAX_QTREE_NAME = 64


def _norm(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_").lstrip("﻿")


def _read_rows(text: str, required: Sequence[str], what: str) -> List[dict]:
    """Parse a CSV into normalised dict rows, with actionable errors."""
    if not (text or "").strip():
        raise ValueError(f"the {what} CSV is empty")
    # Sniff ';' vs ',' — French spreadsheets export semicolons by default.
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError(f"the {what} CSV has no header line")
    headers = {_norm(h): h for h in reader.fieldnames}
    missing = [c for c in required if c not in headers]
    if missing:
        raise ValueError(
            f"the {what} CSV is missing column(s): {', '.join(missing)} "
            f"(found: {', '.join(reader.fieldnames)}); expected header: "
            f"{','.join(required)}")

    rows: List[dict] = []
    for number, raw in enumerate(reader, start=2):      # line 1 is the header
        row = {key: (raw.get(original) or "").strip()
               for key, original in headers.items()}
        if not any(row.values()):
            continue                                    # skip blank lines
        row["_line"] = number
        rows.append(row)
    if not rows:
        raise ValueError(f"the {what} CSV contains no data row")
    return rows


# =============================================================================
# Scope CSV
# =============================================================================

def parse_scope_csv(text: str) -> List[dict]:
    """Rows of {qtree, token, actions[list], label, _line}."""
    rows = _read_rows(text, SCOPE_COLUMNS, "scope")
    parsed: List[dict] = []
    seen = set()
    for row in rows:
        qtree = row.get("qtree", "")
        if not qtree:
            raise ValueError(f"line {row['_line']}: the qtree column is empty")
        key = qtree.lower()
        if key in seen:
            raise ValueError(f"line {row['_line']}: qtree '{qtree}' appears "
                             f"more than once")
        seen.add(key)
        actions = [a.strip() for a in
                   row.get("actions", "").replace(";", ",").split(",")
                   if a.strip()]
        if not actions:
            raise ValueError(f"line {row['_line']}: no action listed for "
                             f"qtree '{qtree}'")
        parsed.append({"qtree": qtree,
                       "token": row.get("token", ""),
                       "actions": actions,
                       "label": row.get("label", ""),
                       "line": row["_line"]})
    return parsed


def render_scope_csv(results: Sequence[dict]) -> str:
    """Answer CSV: the generated tokens appear here and nowhere else."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(SCOPE_OUTPUT_COLUMNS),
                            lineterminator="\n")
    writer.writeheader()
    for item in results:
        writer.writerow({
            "qtree": item.get("qtree", ""),
            "token": item.get("token", ""),
            "actions": ",".join(item.get("actions", [])),
            "label": item.get("label", ""),
            "token_id": item.get("token_id", ""),
            "status": item.get("status", ""),
        })
    return out.getvalue()


# =============================================================================
# Volume-map CSV
# =============================================================================

def validate_qtree_name(name: str, where: str = "") -> str:
    """Reject a qtree name ONTAP would refuse, saying why."""
    prefix = f"{where}: " if where else ""
    if not name:
        raise ValueError(f"{prefix}the qtree name is empty")
    bad = sorted(_ILLEGAL_QTREE_CHARS & set(name))
    if bad:
        raise ValueError(f"{prefix}qtree name '{name}' contains "
                         f"{' '.join(repr(c) for c in bad)} — ONTAP allows "
                         f"none of / \\ : * ? \" < > |")
    if len(name) > _MAX_QTREE_NAME:
        raise ValueError(f"{prefix}qtree name '{name}' is {len(name)} "
                         f"characters, the maximum is {_MAX_QTREE_NAME}")
    return name


def parse_clone_map_csv(text: str) -> Dict[str, dict]:
    """Rows of qtree,volume[,new_qtree] -> {qtree: {volume, new_qtree}}.

    The third column is optional, both as a header and per row: an empty
    new_qtree means the qtree keeps its current name inside the clone.
    """
    rows = _read_rows(text, VOLUME_MAP_COLUMNS, "volume map")
    mapping: Dict[str, dict] = {}
    volumes: Dict[str, str] = {}
    for row in rows:
        qtree, volume = row.get("qtree", ""), row.get("volume", "")
        new_qtree = row.get("new_qtree", "")
        if not qtree:
            raise ValueError(f"line {row['_line']}: the qtree column is empty")
        if not volume:
            raise ValueError(f"line {row['_line']}: no target volume name for "
                             f"qtree '{qtree}'")
        if qtree.lower() in {k.lower() for k in mapping}:
            raise ValueError(f"line {row['_line']}: qtree '{qtree}' appears "
                             f"more than once")
        if volume.lower() in volumes:
            raise ValueError(
                f"line {row['_line']}: volume name '{volume}' is already used "
                f"for qtree '{volumes[volume.lower()]}' — each qtree needs a "
                f"distinct target volume")
        if new_qtree:
            validate_qtree_name(new_qtree, f"line {row['_line']}")
        volumes[volume.lower()] = qtree
        mapping[qtree] = {"volume": volume, "new_qtree": new_qtree}
    return mapping


def parse_volume_map_csv(text: str) -> Dict[str, str]:
    """Rows of qtree,volume -> {qtree: target volume name}.

    The volume half of parse_clone_map_csv, kept for callers that do not
    care about renaming.
    """
    return {qtree: entry["volume"]
            for qtree, entry in parse_clone_map_csv(text).items()}


def split_clone_map(mapping: Optional[Dict[str, dict]]):
    """{qtree: {volume, new_qtree}} -> ({qtree: volume}, {qtree: new_qtree}).

    Two flat dicts is what the engine and the job files carry: it keeps job
    files written by older versions readable, since they only ever held the
    volume half.
    """
    volumes: Dict[str, str] = {}
    renames: Dict[str, str] = {}
    for qtree, entry in (mapping or {}).items():
        if isinstance(entry, str):                 # tolerate the flat form
            volumes[qtree] = entry
            continue
        if entry.get("volume"):
            volumes[qtree] = entry["volume"]
        if entry.get("new_qtree"):
            renames[qtree] = entry["new_qtree"]
    return volumes, renames


def render_volume_map_csv(mapping: Dict[str, str],
                          renames: Optional[Dict[str, str]] = None) -> str:
    """Render the map; the third column appears only when a rename exists."""
    renames = renames or {}
    columns = CLONE_MAP_COLUMNS if renames else VOLUME_MAP_COLUMNS
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for qtree, volume in mapping.items():
        row = [qtree, volume]
        if renames:
            row.append(renames.get(qtree, ""))
        writer.writerow(row)
    return out.getvalue()


def read_file(path: str) -> str:
    """Read a CSV file, tolerating a UTF-8 BOM (Excel exports)."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()
