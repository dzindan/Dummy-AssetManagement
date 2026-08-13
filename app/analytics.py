"""Month-over-month item-count trends, per branch and across all branches.

Each import_batches row carries a `period` ("YYYY-MM") representing the
month the data is *about* (not necessarily when it was imported - see
importer.py). For a given branch and period, several batches can exist (a
baseline import plus a later re-import for the same month); the most recent
batch_id for that (branch, period) pair is treated as that period's snapshot,
consistent with how "current state" is computed elsewhere (queries.py).
"""

from __future__ import annotations

MAX_SERIES = 10  # cap distinct item lines per chart; the rest fold into "OTHER"

BRANCH_TREND_SQL = """
WITH rows AS (
    SELECT ai.batch_id, ib.period AS period, ai.device_name AS item
    FROM asset_items ai
    JOIN import_batches ib ON ai.batch_id = ib.id
    WHERE ai.branch_no = ? AND ib.period IS NOT NULL AND ib.period != ''
),
latest AS (
    SELECT period, MAX(batch_id) AS batch_id FROM rows GROUP BY period
)
SELECT r.period, r.item, COUNT(*) AS cnt
FROM rows r
JOIN latest l ON r.period = l.period AND r.batch_id = l.batch_id
GROUP BY r.period, r.item
ORDER BY r.period, r.item
"""

ALL_BRANCHES_TREND_SQL = """
WITH rows AS (
    SELECT ai.batch_id, ai.branch_no AS branch_no, ib.period AS period, ai.device_name AS item
    FROM asset_items ai
    JOIN import_batches ib ON ai.batch_id = ib.id
    WHERE ib.period IS NOT NULL AND ib.period != ''
),
latest AS (
    SELECT branch_no, period, MAX(batch_id) AS batch_id FROM rows GROUP BY branch_no, period
)
SELECT r.period, r.item, COUNT(*) AS cnt
FROM rows r
JOIN latest l ON r.branch_no = l.branch_no AND r.period = l.period AND r.batch_id = l.batch_id
GROUP BY r.period, r.item
ORDER BY r.period, r.item
"""


def _build_trend(rows) -> tuple[list[str], list[str], dict[str, dict[str, int]]]:
    periods: list[str] = []
    totals: dict[str, int] = {}
    raw: dict[str, dict[str, int]] = {}  # item -> period -> count

    for row in rows:
        period, item, cnt = row["period"], row["item"] or "(UNKNOWN)", row["cnt"]
        if period not in periods:
            periods.append(period)
        raw.setdefault(item, {})[period] = cnt
        totals[item] = totals.get(item, 0) + cnt

    periods.sort()

    top_items = sorted(totals, key=lambda k: totals[k], reverse=True)[:MAX_SERIES]
    other_items = [i for i in totals if i not in top_items]

    matrix: dict[str, dict[str, int]] = {item: raw[item] for item in top_items}
    if other_items:
        other_series: dict[str, int] = {}
        for item in other_items:
            for period, cnt in raw[item].items():
                other_series[period] = other_series.get(period, 0) + cnt
        matrix["OTHER"] = other_series

    items = list(matrix.keys())
    return periods, items, matrix


def get_branch_item_trend(conn, branch_no: str):
    rows = conn.execute(BRANCH_TREND_SQL, (branch_no,)).fetchall()
    return _build_trend(rows)


def get_all_branches_item_trend(conn):
    rows = conn.execute(ALL_BRANCHES_TREND_SQL).fetchall()
    return _build_trend(rows)


def _walk_period_changes(rows: list[tuple[str, str, str]]) -> dict[str, dict]:
    """Core month-over-month diff engine shared by the branch-level (Dashboard)
    and device-level (Branch Detail) change tables.

    `rows` is (group_key, period, asset_key) triples - group_key is whatever
    the caller is breaking the table down by (branch_no, or device_name
    within one branch). For each group, periods are walked oldest-first and
    each period's asset_key set is diffed against the *previous* period's
    set for that same group - added = present now but not before, removed =
    present before but not now. This is deliberately NOT a plain count
    delta: swapping 5 devices for 5 different ones nets to a count change of
    zero but is still real asset movement, and only an identity-based diff
    (matching how Update & Compare and User History already work) catches
    that. The very first period on record for a group has no prior period to
    compare against, so its added/removed are left as None rather than
    misleadingly showing "everything was added this month".

    Stock counts (how many assets exist in a given month) are NOT
    meaningful to sum across months - a branch having 400 assets in both
    June and July is not "800 assets". Added/removed, by contrast, ARE flow
    quantities and can be meaningfully totaled, which is what backs each
    group's total_added/total_removed here.
    """
    by_group: dict[str, dict[str, set]] = {}
    for group_key, period, asset_key in rows:
        by_group.setdefault(group_key, {}).setdefault(period, set()).add(asset_key)

    result: dict[str, dict] = {}
    for group_key, period_sets in by_group.items():
        sorted_periods = sorted(period_sets.keys())
        per_period: dict[str, dict] = {}
        prev_keys: set | None = None
        total_added = 0
        total_removed = 0
        current_count = 0
        for period in sorted_periods:
            keys = period_sets[period]
            if prev_keys is None:
                added = removed = None
            else:
                added = len(keys - prev_keys)
                removed = len(prev_keys - keys)
                total_added += added
                total_removed += removed
            per_period[period] = {"count": len(keys), "added": added, "removed": removed}
            current_count = len(keys)
            prev_keys = keys
        result[group_key] = {
            "periods": per_period,
            "total_added": total_added,
            "total_removed": total_removed,
            "current_count": current_count,
        }
    return result


VISIBLE_MONTHS = 6  # how many recent months the change tables display


def _limit_to_recent_months(by_group: dict[str, dict], visible_months: int = VISIBLE_MONTHS):
    """Slice a _walk_period_changes result down to the N most recent months
    for display, and only keep the added/removed indicator on the last
    (most recent) of those columns - by design, the tables compare the two
    most recent months only, not a running chain across the whole visible
    window, so older columns show a plain count with no delta."""
    all_periods: set[str] = set()
    for data in by_group.values():
        all_periods.update(data["periods"].keys())
    visible_periods = sorted(all_periods)[-visible_months:]
    last_idx = len(visible_periods) - 1

    prepared: dict[str, dict] = {}
    for key, data in by_group.items():
        cells = []
        for i, period in enumerate(visible_periods):
            cell = data["periods"].get(period)
            if cell is None:
                cells.append(None)
                continue
            show_delta = i == last_idx
            cells.append(
                {
                    "count": cell["count"],
                    "added": cell["added"] if show_delta else None,
                    "removed": cell["removed"] if show_delta else None,
                }
            )
        prepared[key] = {"cells": cells, "current_count": data["current_count"]}
    return visible_periods, prepared


BRANCH_MONTH_CHANGES_SQL = """
WITH rows AS (
    SELECT ai.batch_id, ai.branch_no AS branch_no, ib.period AS period, ai.asset_key AS asset_key
    FROM asset_items ai
    JOIN import_batches ib ON ai.batch_id = ib.id
    WHERE ib.period IS NOT NULL AND ib.period != ''
),
latest AS (
    SELECT branch_no, period, MAX(batch_id) AS batch_id FROM rows GROUP BY branch_no, period
)
SELECT r.branch_no AS grp, r.period AS period, r.asset_key AS asset_key
FROM rows r
JOIN latest l ON r.branch_no = l.branch_no AND r.period = l.period AND r.batch_id = l.batch_id
"""


def get_branch_month_change_table(conn, visible_months: int = VISIBLE_MONTHS):
    """Dashboard summary table: one row per branch, showing its asset count
    for the `visible_months` most recent months, sorted by current (most
    recent) count. Only the most recent month's column carries an
    added/removed indicator, comparing it against the month right before it
    - see _walk_period_changes for why that's computed from actual asset
    identity rather than a naive count difference, and
    _limit_to_recent_months for why older columns don't also show a delta."""
    rows = conn.execute(BRANCH_MONTH_CHANGES_SQL).fetchall()
    by_group = _walk_period_changes((r["grp"] or "", r["period"], r["asset_key"]) for r in rows)
    visible_periods, prepared = _limit_to_recent_months(by_group, visible_months)

    names = {
        r["branch_no"]: r["eng_name"] for r in conn.execute("SELECT branch_no, eng_name FROM branches").fetchall()
    }

    table = []
    for branch_no, data in prepared.items():
        table.append(
            {
                "branch_no": branch_no,
                "label": names.get(branch_no, "") or ("Unresolved" if not branch_no else branch_no),
                "cells": data["cells"],
                "current_count": data["current_count"],
            }
        )
    table.sort(key=lambda r: r["current_count"], reverse=True)

    column_totals = [
        sum((c["count"] if c else 0) for c in (r["cells"][i] for r in table)) for i in range(len(visible_periods))
    ]
    latest_added_total = sum((r["cells"][-1]["added"] or 0) for r in table if r["cells"] and r["cells"][-1])
    latest_removed_total = sum((r["cells"][-1]["removed"] or 0) for r in table if r["cells"] and r["cells"][-1])

    return visible_periods, table, column_totals, latest_added_total, latest_removed_total


def get_branch_device_month_changes(conn, branch_no: str, top_items: list[str], visible_months: int = VISIBLE_MONTHS):
    """Same diff engine as get_branch_month_change_table, but scoped to one
    branch and broken down by device type instead of by branch - backs the
    Branch Detail item-trend table. Any device not in `top_items` (the same
    top-N list the trend chart already folds down to) is grouped into
    "OTHER", matching the chart/count table exactly. Returns
    (visible_periods, {device_name: {"cells": [...], "current_count": n}})."""
    sql = """
    WITH rows AS (
        SELECT ai.batch_id, ai.device_name AS device_name, ib.period AS period, ai.asset_key AS asset_key
        FROM asset_items ai
        JOIN import_batches ib ON ai.batch_id = ib.id
        WHERE ai.branch_no = ? AND ib.period IS NOT NULL AND ib.period != ''
    ),
    latest AS (
        SELECT period, MAX(batch_id) AS batch_id FROM rows GROUP BY period
    )
    SELECT r.device_name AS device_name, r.period AS period, r.asset_key AS asset_key
    FROM rows r
    JOIN latest l ON r.period = l.period AND r.batch_id = l.batch_id
    """
    raw = conn.execute(sql, (branch_no,)).fetchall()
    top_set = set(top_items)
    folded = (
        ((row["device_name"] or "(UNKNOWN)") if (row["device_name"] or "(UNKNOWN)") in top_set else "OTHER",
         row["period"], row["asset_key"])
        for row in raw
    )
    by_group = _walk_period_changes(folded)
    return _limit_to_recent_months(by_group, visible_months)
