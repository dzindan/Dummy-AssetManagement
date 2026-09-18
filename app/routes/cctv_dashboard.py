from flask import Blueprint, render_template, request

from ..analytics import (
    get_all_branches_item_trend,
    get_available_report_years,
    get_branch_month_change_table,
    get_year_comparison_table,
    resolve_report_year,
)
from ..cctv_metrics import build_month_metrics_by_branch, summarize_cctv_rows
from ..charts import trend_chart_payload
from ..db import get_connection
from ..exports import build_workbook, send_workbook, style_header_row
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


def _build_branch_cctv_tree(rows):
    """Groups get_current_cctv_items_for_tree's flat rows into one entry
    per branch (sorted by recorder count, most equipment first - same
    convention as get_current_branch_breakdown) with per-item rows nested
    underneath, plus a summarize_cctv_rows() total for the branch-level
    summary row."""
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
        tree.append(
            {
                "bkey": branch["bkey"],
                "branch_no": branch["branch_no"],
                "display_name": branch["display_name"],
                "rows": branch["rows"],
                **summarize_cctv_rows(branch["rows"]),
            }
        )
    tree.sort(key=lambda b: b["recorder_count"], reverse=True)
    return tree


def _build_branch_tree_with_months(conn, month_periods):
    """Shared by index() and export_compare() - the "Compare by Branch"
    tree plus its per-branch month_cells, for whatever `month_periods` the
    caller already resolved (so both stay on the exact same selected year
    without a second, possibly-inconsistent, query for it)."""
    branch_tree = _build_branch_cctv_tree(get_current_cctv_items_for_tree(conn))
    branch_month_metrics = build_month_metrics_by_branch(get_cctv_items_by_branch_period(conn), month_periods)
    for b in branch_tree:
        b["month_cells"] = branch_month_metrics.get(b["bkey"], [None] * len(month_periods))
    return branch_tree


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
        branch_tree = _build_branch_tree_with_months(conn, month_periods)
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


@bp.route("/export-compare")
def export_compare():
    """The "Compare by Branch" panel's own export - current DVR/Recorder,
    Cameras, HDD Count, HDD Capacity totals per branch on one sheet, the
    same 4 metrics broken out by month (for the selected year, with each
    metric's own delta column) on a second - covers what that panel shows
    on screen that neither the flat by-month export above (item count
    only, no DVR/Camera/HDD split) nor Manage CCTV's own export (row-level
    detail, no per-branch totals) does."""
    conn = get_connection()
    try:
        available_years = get_available_report_years(conn, kinds=YEARS_KINDS)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        month_periods, _month_table, _totals, _added, _removed = get_branch_month_change_table(
            conn, selected_year, table=TABLE
        )
        branch_tree = _build_branch_tree_with_months(conn, month_periods)
    finally:
        conn.close()

    wb = build_workbook(
        "Current by Branch",
        [
            ("Branch", "display_name"),
            ("DVR/Recorder", "recorder_count"),
            ("Cameras", lambda b: b["camera_total"] if b["camera_total"] is not None else ""),
            ("HDD Count", lambda b: b["hdd_count_total"] if b["hdd_count_total"] is not None else ""),
            (
                "HDD Capacity (TB)",
                lambda b: round(b["hdd_capacity_total_tb"], 2) if b["hdd_capacity_total_tb"] is not None else "",
            ),
        ],
        branch_tree,
    )

    month_ws = wb.create_sheet(f"By Month {selected_year}")
    month_ws.append(
        ["Branch", "Period", "DVR/Recorder", "DVR/Recorder Δ", "Cameras", "Cameras Δ",
         "HDD Count", "HDD Count Δ", "HDD Capacity (TB)", "HDD Capacity Δ (TB)"]
    )
    for b in branch_tree:
        for period, cell in zip(month_periods, b["month_cells"]):
            if cell is None:
                continue
            month_ws.append(
                [
                    b["display_name"], period,
                    cell["recorder_count"], cell["recorder_delta"],
                    cell["camera_total"], cell["camera_delta"],
                    cell["hdd_count_total"], cell["hdd_count_delta"],
                    round(cell["hdd_capacity_total_tb"], 2) if cell["hdd_capacity_total_tb"] is not None else None,
                    round(cell["hdd_capacity_delta"], 2) if cell["hdd_capacity_delta"] is not None else None,
                ]
            )
    style_header_row(month_ws)

    return send_workbook(wb, f"cctv_compare_by_branch_{selected_year}.xlsx")
