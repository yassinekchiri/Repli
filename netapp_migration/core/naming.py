"""How migrated objects are named.

One rule lives here rather than in the engine because the pre-flight has to
predict exactly what the engine will do — it announces the new name before
the rename happens, and refuses if that name is already taken.
"""

from ..security.csvio import _MAX_QTREE_NAME

# What marks a source qtree whose data now lives elsewhere. Also what the
# pre-flight looks for to notice a qtree has already been cleaned up.
MIGRATED_MARK = "_MIG_"


def job_reference(job_id: str) -> str:
    """The short, still-unique tail of a job id, for use inside a name."""
    return (job_id or "unknown").split("_")[-1][:8] or "unknown"


def migrated_qtree_name(qtree: str, job_id: str, volume: str,
                        new_qtree: str = "") -> str:
    """Name a source qtree takes once its data lives elsewhere.

        q_finance  ->  q_finance_MIG_2da725__vol_fin_prod__finance

    Reads as: migrated by job …2da725, now 'finance' in 'vol_fin_prod'. The
    destination qtree is left out when the name did not change.

    ONTAP caps qtree names at 64 characters, so the parts are trimmed in
    order of usefulness — the original name first, the marker and the job
    reference last, since those are what make the qtree identifiable at all.
    """
    suffix = f"{MIGRATED_MARK}{job_reference(job_id)}__{volume}"
    if new_qtree and new_qtree != qtree:
        suffix += f"__{new_qtree}"

    budget = _MAX_QTREE_NAME - len(suffix)
    if budget < 4:
        # Even the suffix alone is too long: keep the marker and the job,
        # drop the rest rather than produce a name ONTAP would refuse.
        suffix = f"{MIGRATED_MARK}{job_reference(job_id)}"
        budget = _MAX_QTREE_NAME - len(suffix)
    return f"{qtree[:budget]}{suffix}"
