import re

from flask import Blueprint, render_template, request

from ..analytics import (
    get_all_branches_item_trend,
    get_available_report_years,
    get_branch_month_change_table,
    get_year_comparison_table,
    resolve_report_year,
)
from ..charts import trend_chart_payload
from ..db import get_connection
from ..exports import build_workbook, send_workbook
from ..queries import (
    get_cctv_items_by_branch_period,
    get_current_branch_breakdown,
    get_current_cctv_count,
    get_current_cctv_items_for_tree,
    get_latest_batch,
)

bp = Blueprint("cctv_dashboard", __name__, url_prefix="/cctv/dashboard")

# Same idea as dashboard.py, over cctv_items instead of asset_items - see
# analytics.py's table/kinds parameters (added alongside this page) for how
# the shared trend/month-change/year-comparison logic serves both.
TABLE = "cctv_items"
YEARS_KINDS = ("cctv_report",)

_LEADING_INT_RE = re.compile(r"\d+")
# First "<number> TB/GB/T" in a capacity string - real files write this in
# every shape from "24TB" to "16TB (2X8TB) Total" to "7452.04 GB" to
# "21.86T" (see cctv_items.hdd_capacity in production); this only needs the
# headline total, not to parse every parenthetical breakdown.
_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|T)\b", re.IGNORECASE)


def _parse_leading_int(text: str) -> int | None:
    match = _LEADING_INT_RE.search(text or "")
    return int(match.group()) if match else None


def _parse_capacity_tb(text: str) -> float | None:
    match = _CAPACITY_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    return value / 1024 if match.group(2).upper() == "GB" else value


def _build_branch_cctv_tree(rows):
    """Groups get_current_cctv_items_for_tree's flat rows into one entry
    per branch (sorted by recorder count, most equipment first - same
    convention as get_current_branch_breakdown) with per-item rows nested
    underneath, plus best-effort numeric totals for the branch-level
    summary row. A branch's total is left as None (rendered "-") rather
    than silently undercounted when NONE of its items parsed - it's an
    accurate reflection of "count/HDD size wasn't recorded this way for
    any of them" rather than a wrong zero, but a PARTIAL parse still sums
    what it can (that's the common case: one HDD row with a stray unit
    typo alongside several clean ones)."""
    # Note: the per-branch dict below deliberately never uses the key name
    # "items" - Jinja's `b.items` attribute lookup finds dict.items (the
    # builtin method) before ever trying `b["items"]`, so a real "items"
    # key would only ever be reachable in the template via `b["items"]`,
    # not the `b.items` dot-syntax the rest of this app's templates use
    # for every other dict key.
    by_branch: dict[str, dict] = {}
    for row in rows:
        branch = by_branch.setdefault(
            row["bkey"],
            {"bkey": row["bkey"], "branch_no": row["branch_no"], "display_name": row["display_name"], "rows": []},
        )
        branch["rows"].append(row)

    tree = []
    for branch in by_branch.values():
        rows_for_branch = branch["rows"]
        cameras = [v for v in (_parse_leading_int(i["camera_count"]) for i in rows_for_branch) if v is not None]
        hdd_counts = [v for v in (_parse_leading_int(i["hdd_count"]) for i in rows_for_branch) if v is not None]
        hdd_capacities = [
            v for v in (_parse_capacity_tb(i["hdd_capacity"]) for i in rows_for_branch) if v is not None
        ]
        tree.append(
            {
                "bkey": branch["bkey"],
                "branch_no": branch["branch_no"],
                "display_name": branch["display_name"],
                "recorder_count": len(rows_for_branch),
                "camera_total": sum(cameras) if cameras else None,
                "hdd_count_total": sum(hdd_counts) if hdd_counts else None,
                "hdd_capacity_total_tb": sum(hdd_capacities) if hdd_capacities else None,
                "rows": rows_for_branch,
            }
        )
    tree.sort(key=lambda b: b["recorder_count"], reverse=True)
    return tree


def _delta(cur, prev):
    return cur - prev if cur is not None and prev is not None else None


def _build_branch_month_metrics(rows, month_periods):
    """Same per-item parsing as _build_branch_cctv_tree, but grouped by
    (branch, period) instead of just branch - the month-by-month table
    each branch's tree row expands to, alongside its item-level detail.
    Each cell also gets a *_delta vs whatever period actually precedes it
    on record (which may fall outside `month_periods`, e.g. the prior
    year's December for January) - same "diff against the real previous
    report, not just the previous visible column" rule
    get_branch_month_change_table already uses for the flat by-month
    table below, so both tables' +/- agree with each other. Unlike that
    table's identity-based asset added/removed, these are plain numeric
    deltas (this month's parsed sum minus last month's) since there's no
    per-camera/per-HDD identity to track, only a running total.

    `month_periods` is the selected year's 12 "YYYY-MM" strings (same list
    get_branch_month_change_table already produced for this request, so
    the year selector controls both) - a branch/period with nothing on
    file at all gets None (rendered "-" across the row) rather than a row
    of zeros, same reasoning as the tree's own per-branch totals."""
    by_branch_period: dict[tuple[str, str], list] = {}
    for row in rows:
        by_branch_period.setdefault((row["bkey"], row["period"]), []).append(row)

    by_branch: dict[str, dict[str, dict]] = {}
    for (bkey, period), items in by_branch_period.items():
        cameras = [v for v in (_parse_leading_int(i["camera_count"]) for i in items) if v is not None]
        hdd_counts = [v for v in (_parse_leading_int(i["hdd_count"]) for i in items) if v is not None]
        hdd_capacities = [v for v in (_parse_capacity_tb(i["hdd_capacity"]) for i in items) if v is not None]
        by_branch.setdefault(bkey, {})[period] = {
            "recorder_count": len(items),
            "camera_total": sum(cameras) if cameras else None,
            "hdd_count_total": sum(hdd_counts) if hdd_counts else None,
            "hdd_capacity_total_tb": sum(hdd_capacities) if hdd_capacities else None,
        }

    result: dict[str, list] = {}
    for bkey, by_period in by_branch.items():
        prev = None
        for period in sorted(by_period):
            cur = by_period[period]
            cur["recorder_delta"] = _delta(cur["recorder_count"], prev["recorder_count"] if prev else None)
            cur["camera_delta"] = _delta(cur["camera_total"], prev["camera_total"] if prev else None)
            cur["hdd_count_delta"] = _delta(cur["hdd_count_total"], prev["hdd_count_total"] if prev else None)
            cur["hdd_capacity_delta"] = _delta(
                cur["hdd_capacity_total_tb"], prev["hdd_capacity_total_tb"] if prev else None
            )
            prev = cur
        result[bkey] = [by_period.get(period) for period in month_periods]
    return result


@bp.route("/")
def index():
    conn = get_connection()
    try:
        branch_count = len(get_current_branch_breakdown(conn, table=TABLE))
        cctv_count = get_current_cctv_count(conn)
        latest_cctv_batch = get_latest_batch(conn, kind="cctv_report")

        all_periods, all_items, all_matrix = get_all_branches_item_trend(conn, table=TABLE)

        available_years = get_available_report_years(conn, kinds=YEARS_KINDS)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        month_periods, month_table, month_column_totals, month_column_added, month_column_removed = (
            get_branch_month_change_table(conn, selected_year, table=TABLE)
        )

        year_comparison = get_year_comparison_table(conn, table=TABLE, years_kinds=YEARS_KINDS)
        branch_tree = _build_branch_cctv_tree(get_current_cctv_items_for_tree(conn))

        branch_month_metrics = _build_branch_month_metrics(
            get_cctv_items_by_branch_period(conn), month_periods
        )
        for b in branch_tree:
            b["month_cells"] = branch_month_metrics.get(b["bkey"], [None] * len(month_periods))
    finally:
        conn.close()

    all_branches_chart_data = trend_chart_payload(all_periods, all_matrix)

    return render_template(
        "cctv_dashboard.html",
        active_page="cctv_dashboard",
        branch_count=branch_count,
        cctv_count=cctv_count,
        latest_cctv_batch=latest_cctv_batch,
        all_branches_chart_data=all_branches_chart_data,
        available_years=available_years,
        selected_year=selected_year,
        month_periods=month_periods,
        month_table=month_table,
        month_column_totals=month_column_totals,
        month_column_added=month_column_added,
        month_column_removed=month_column_removed,
        year_comparison=year_comparison,
        branch_tree=branch_tree,
    )


@bp.route("/export")
def export():
    """Branch x month CCTV-count table for the selected year - same idea as
    dashboard.export, re-computed rather than reusing state from index()."""
    conn = get_connection()
    try:
        available_years = get_available_report_years(conn, kinds=YEARS_KINDS)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        month_periods, month_table, _totals, _added, _removed = get_branch_month_change_table(
            conn, selected_year, table=TABLE
        )
    finally:
        conn.close()

    columns = [("Branch", "label")]
    for i, period in enumerate(month_periods):
        columns.append((period, lambda r, i=i: (r["cells"][i]["count"] if r["cells"][i] else 0)))

    wb = build_workbook(f"CCTV by Branch by Month {selected_year}", columns, month_table)
    return send_workbook(wb, f"cctv_by_branch_by_month_{selected_year}.xlsx")
