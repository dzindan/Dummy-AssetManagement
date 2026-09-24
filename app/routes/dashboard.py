from flask import Blueprint, render_template, request

from ..analytics import (
    get_all_branches_item_trend,
    get_available_report_years,
    get_branch_month_change_table,
    get_year_comparison_table,
    resolve_report_year,
)
from ..charts import per_series_trend_payloads, trend_chart_payload
from ..db import get_connection
from ..exports import (
    add_solo_item_charts,
    add_trend_line_chart,
    build_workbook,
    send_workbook,
    write_trend_matrix_sheet,
)
from ..queries import get_current_asset_count, get_current_branch_breakdown, get_latest_batch

bp = Blueprint("dashboard", __name__)

# "Assets by Branch (by month)" sort, from the ?sort= query param. Missing /
# unknown = the default order get_branch_month_change_table already returns
# (current count, largest first). Clicking the Branch / Dept header cycles
# default -> A-Z -> Z-A -> default (see NEXT_BRANCH_SORT).
BRANCH_SORTS = {"branch": False, "-branch": True}  # value = descending
NEXT_BRANCH_SORT = {"": "branch", "branch": "-branch", "-branch": ""}


def _branch_sort_param() -> str:
    sort = request.args.get("sort", "")
    return sort if sort in BRANCH_SORTS else ""


def _sort_month_table(rows: list[dict], sort: str) -> list[dict]:
    """Sort by branch name; the "Unresolved" row (no branch_no) always stays
    last whichever direction, since it isn't a branch name at all."""
    if not sort:
        return rows
    named = sorted((r for r in rows if r["branch_no"]), key=lambda r: r["label"].upper(),
                   reverse=BRANCH_SORTS[sort])
    return named + [r for r in rows if not r["branch_no"]]


@bp.route("/")
def index():
    conn = get_connection()
    try:
        branch_count = conn.execute("SELECT COUNT(*) c FROM branches").fetchone()["c"]
        user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

        latest_asset_batch = get_latest_batch(conn)
        asset_count = get_current_asset_count(conn)
        branch_breakdown = get_current_branch_breakdown(conn)

        handover_count = conn.execute("SELECT COUNT(*) c FROM handover_records").fetchone()["c"]
        recent_handovers = conn.execute(
            "SELECT * FROM handover_records ORDER BY id DESC LIMIT 5"
        ).fetchall()

        all_periods, all_items, all_matrix = get_all_branches_item_trend(conn)

        available_years = get_available_report_years(conn)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        month_periods, month_table, month_column_totals, month_column_added, month_column_removed = (
            get_branch_month_change_table(conn, selected_year)
        )
        branch_sort = _branch_sort_param()
        month_table = _sort_month_table(month_table, branch_sort)

        year_comparison = get_year_comparison_table(conn)

        unmapped_device_count = conn.execute("SELECT COUNT(*) c FROM device_unmapped").fetchone()["c"]
    finally:
        conn.close()

    all_branches_chart_data = trend_chart_payload(all_periods, all_matrix)
    device_trend_charts = per_series_trend_payloads(all_periods, all_matrix)

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        branch_count=branch_count,
        user_count=user_count,
        asset_count=asset_count,
        handover_count=handover_count,
        latest_asset_batch=latest_asset_batch,
        branch_breakdown=branch_breakdown,
        recent_handovers=recent_handovers,
        all_branches_chart_data=all_branches_chart_data,
        device_trend_charts=device_trend_charts,
        unmapped_device_count=unmapped_device_count,
        available_years=available_years,
        selected_year=selected_year,
        month_periods=month_periods,
        month_table=month_table,
        branch_sort=branch_sort,
        next_branch_sort=NEXT_BRANCH_SORT[branch_sort],
        month_column_totals=month_column_totals,
        month_column_added=month_column_added,
        month_column_removed=month_column_removed,
        year_comparison=year_comparison,
    )


@bp.route("/export")
def export():
    """Branch x month asset-count table for the selected year, re-computed
    the same way as the Dashboard itself (see get_branch_month_change_table)
    rather than reusing state passed from index() - matches how every other
    export route in this app re-queries instead of caching across requests."""
    conn = get_connection()
    try:
        available_years = get_available_report_years(conn)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        month_periods, month_table, _totals, _added, _removed = get_branch_month_change_table(conn, selected_year)
        month_table = _sort_month_table(month_table, _branch_sort_param())  # same order as on screen
        all_periods, _all_items, all_matrix = get_all_branches_item_trend(conn)
    finally:
        conn.close()

    columns = [("Branch", "label")]
    for i, period in enumerate(month_periods):
        columns.append((period, lambda r, i=i: (r["cells"][i]["count"] if r["cells"][i] else 0)))

    wb = build_workbook(f"Assets by Branch by Month {selected_year}", columns, month_table)

    items = list(all_matrix.keys())
    if len(all_periods) >= 2 and items:
        ws = write_trend_matrix_sheet(wb, "Item Count Trend", all_periods, items, all_matrix)
        add_trend_line_chart(ws, "Item Count Trend - All Branches", len(all_periods), len(items), "A" + str(len(all_periods) + 3))
        add_solo_item_charts(ws, items, len(all_periods), len(all_periods) + 20)

    return send_workbook(wb, f"assets_by_branch_by_month_{selected_year}.xlsx")
