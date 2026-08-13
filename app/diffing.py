"""Per-branch snapshot diffing: compares a branch's two most recent import
batches as full replacements of that branch's equipment list (see
queries.py for why "most recent batch per branch" is the right unit)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .queries import get_batch_assets_for_branch, get_branch, get_branches_in_batch, get_previous_batch_for_branch

COMPARE_FIELDS = ["status", "user_id_norm", "full_name", "position", "device_name", "model_device"]
FIELD_LABELS = {
    "status": "Status",
    "user_id_norm": "User ID",
    "full_name": "Full Name",
    "position": "Position",
    "device_name": "Device",
    "model_device": "Model",
}


@dataclass
class BranchDiff:
    branch_no: str
    branch_label: str
    new_batch_id: int
    previous_batch_id: int | None
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)  # each: {"old":row,"new":row,"diffs":{field:(old,new)}}
    unchanged_count: int = 0

    @property
    def has_previous(self) -> bool:
        return self.previous_batch_id is not None


def diff_branch(conn, branch_no: str, new_batch_id: int) -> BranchDiff:
    branch_row = get_branch(conn, branch_no)
    branch_label = branch_row["eng_name"] if branch_row else (branch_no or "(unresolved branch)")

    previous_batch_id = get_previous_batch_for_branch(conn, branch_no, new_batch_id)
    diff = BranchDiff(
        branch_no=branch_no, branch_label=branch_label,
        new_batch_id=new_batch_id, previous_batch_id=previous_batch_id,
    )

    new_rows = get_batch_assets_for_branch(conn, new_batch_id, branch_no)
    new_by_key = {r["asset_key"]: r for r in new_rows}

    if previous_batch_id is None:
        diff.added = list(new_rows)
        return diff

    old_rows = get_batch_assets_for_branch(conn, previous_batch_id, branch_no)
    old_by_key = {r["asset_key"]: r for r in old_rows}

    for key, new_row in new_by_key.items():
        old_row = old_by_key.get(key)
        if old_row is None:
            diff.added.append(new_row)
            continue
        diffs = {}
        for f in COMPARE_FIELDS:
            old_val = old_row[f] or ""
            new_val = new_row[f] or ""
            if old_val != new_val:
                diffs[f] = (old_val, new_val)
        if diffs:
            diff.changed.append({"old": old_row, "new": new_row, "diffs": diffs})
        else:
            diff.unchanged_count += 1

    for key, old_row in old_by_key.items():
        if key not in new_by_key:
            diff.removed.append(old_row)

    return diff


def diff_batch(conn, batch_id: int, fallback_branch_no: str = "") -> list[BranchDiff]:
    """Diff every branch touched by `batch_id` against each branch's previous batch."""
    branch_nos = get_branches_in_batch(conn, batch_id)
    if not branch_nos and fallback_branch_no:
        branch_nos = [fallback_branch_no]

    # get_branches_in_batch excludes branch_no='' (unresolved). A batch can
    # still carry unresolved rows alongside resolved ones (e.g. the Total
    # Asset history import resolves each row independently) - without this,
    # those rows are silently skipped by every diff below instead of showing
    # up as "(unresolved branch)" (the label diff_branch already renders for
    # branch_no=''), making them look like they were never diffed at all.
    has_unresolved = conn.execute(
        "SELECT 1 FROM asset_items WHERE batch_id = ? AND branch_no = '' LIMIT 1", (batch_id,)
    ).fetchone() is not None
    if has_unresolved and "" not in branch_nos:
        branch_nos = [*branch_nos, ""]

    return [diff_branch(conn, b, batch_id) for b in branch_nos]
