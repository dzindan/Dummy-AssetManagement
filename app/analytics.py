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


VISIBLE_MONTHS = 6  # how many recent months Branch Detail's rolling-window table displays


def get_available_report_years(conn) -> list[str]:
    """Distinct calendar years ("YYYY") with at least one asset-report/
    baseline import on file, newest first - backs the Dashboard's year
    selector and the year comparison table below."""
    rows = conn.execute(
        "SELECT DISTINCT substr(period, 1, 4) AS y FROM import_batches "
        "WHERE period IS NOT NULL AND period != '' ORDER BY y DESC"
    ).fetchall()
    return [r["y"] for r in rows]


def _month_periods_for_year(year: str) -> list[str]:
    return [f"{year}-{m:02d}" for m in range(1, 13)]


def _prepare_year_columns(by_group: dict[str, dict], year: str):
    """Slice a _walk_period_changes result down to the 12 fixed months of
    `year`, unlike Branch Detail's rolling _limit_to_recent_months window:
    every shown column keeps its own added/removed (diffed against
    whatever period actually preceded it - possibly outside the visible
    year, e.g. January's delta lands against December of the year before),
    not just the single most recent column. A group with no data at all in
    `year` is dropped entirely rather than shown as an all-blank row."""
    visible_periods = _month_periods_for_year(year)
    prepared: dict[str, dict] = {}
    for key, data in by_group.items():
        cells = []
        last_count = 0
        any_data = False
        for period in visible_periods:
            cell = data["periods"].get(period)
            if cell is None:
                cells.append(None)
                continue
            any_data = True
            cells.append({"count": cell["count"], "added": cell["added"], "removed": cell["removed"]})
            last_count = cell["count"]
        if any_data:
            prepared[key] = {"cells": cells, "current_count": last_count}
    return visible_periods, prepared


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


def get_branch_month_change_table(conn, year: str):
    """Dashboard summary table: one row per branch, showing its asset count
    for each of the 12 months of `year`, sorted by current (latest-in-year)
    count. Every column carries its own added/removed indicator (diffed
    against whatever period actually precedes it, which may fall in the
    prior year for January) - see _walk_period_changes for why that's
    computed from actual asset identity rather than a naive count
    difference, and _prepare_year_columns for why every column (not just
    the last) gets a delta here, unlike Branch Detail's rolling window."""
    rows = conn.execute(BRANCH_MONTH_CHANGES_SQL).fetchall()
    by_group = _walk_period_changes((r["grp"] or "", r["period"], r["asset_key"]) for r in rows)
    visible_periods, prepared = _prepare_year_columns(by_group, year)

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
    column_added = [
        sum((c["added"] or 0) for c in (r["cells"][i] for r in table) if c) for i in range(len(visible_periods))
    ]
    column_removed = [
        sum((c["removed"] or 0) for c in (r["cells"][i] for r in table) if c) for i in range(len(visible_periods))
    ]

    return visible_periods, table, column_totals, column_added, column_removed


YEAR_SNAPSHOT_SQL = """
WITH branch_key AS (
    SELECT ai.*, COALESCE(NULLIF(ai.branch_no, ''), 'UNRESOLVED:' || ai.branch_dept) AS bkey
    FROM asset_items ai
),
branch_batches AS (
    SELECT DISTINCT bk.bkey, bk.batch_id, ib.period
    FROM branch_key bk
    JOIN import_batches ib ON ib.id = bk.batch_id
    WHERE ib.period IS NOT NULL AND ib.period != '' AND ib.period <= ?
),
latest_period AS (
    SELECT bkey, MAX(period) AS period
    FROM branch_batches
    GROUP BY bkey
),
latest_batch AS (
    SELECT bb.bkey, MAX(bb.batch_id) AS batch_id
    FROM branch_batches bb
    JOIN latest_period lp ON lp.bkey = bb.bkey AND lp.period IS bb.period
    GROUP BY bb.bkey
)
SELECT COUNT(*) AS c
FROM branch_key bk
JOIN latest_batch lb ON bk.bkey = lb.bkey AND bk.batch_id = lb.batch_id
"""


def get_year_comparison_table(conn) -> list[dict]:
    """Dashboard year-comparison panel: one row per calendar year with any
    reporting data, oldest first, showing that year's end-of-year asset
    snapshot (same per-branch "latest period up to and including this
    year's December" logic as CURRENT_ASSETS_CTE in queries.py, just bounded
    by a "<= YYYY-12" cutoff instead of unbounded) and its change versus the
    year immediately before it. `change` is None for the first year on
    record, since there's nothing earlier to compare against."""
    years = get_available_report_years(conn)
    years.sort()  # oldest first, opposite of get_available_report_years' newest-first default

    table = []
    prev_count: int | None = None
    for year in years:
        cutoff = f"{year}-12"
        count = conn.execute(YEAR_SNAPSHOT_SQL, (cutoff,)).fetchone()["c"]
        change = None if prev_count is None else count - prev_count
        table.append({"year": year, "count": count, "change": change})
        prev_count = count
    return table


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
