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
from ..queries import get_current_branch_breakdown, get_current_cctv_count, get_latest_batch

bp = Blueprint("cctv_dashboard", __name__, url_prefix="/cctv/dashboard")

# Same idea as dashboard.py, over cctv_items instead of asset_items - see
# analytics.py's table/kinds parameters (added alongside this page) for how
# the shared trend/month-change/year-comparison logic serves both.
TABLE = "cctv_items"
YEARS_KINDS = ("cctv_report",)


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
