from flask import Blueprint, abort, render_template, request

from ..analytics import (
    get_available_report_years,
    get_branch_device_year_table,
    get_branch_item_trend,
    resolve_report_year,
)
from ..cctv_metrics import build_month_metrics_by_branch, summarize_by_period, summarize_cctv_rows
from ..charts import per_series_trend_payloads, trend_chart_payload
from ..db import get_connection
from ..exports import (
    add_solo_item_charts,
    add_stacked_total_chart,
    build_asset_rows_workbook,
    build_cctv_rows_workbook,
    dated_download_name,
    send_workbook,
    write_trend_matrix_sheet,
)
from ..paths import safe_filename
from ..queries import get_branch, get_cctv_items_by_branch_period, get_current_assets, search_cctv

bp = Blueprint("branch_detail", __name__, url_prefix="/branch")


def _device_status_breakdown(rows) -> dict:
    """Device Type Breakdown, split by Status - one row per device type,
    one column per status seen among `rows` (this branch's current assets,
    or its current CCTV items - both have device_name/status columns), so
    e.g. "how many PCs are actually BROKEN vs. still USING LOCAL" is
    visible at a glance instead of only the device's overall count.

    Rows are sorted by each device's own total (busiest device type first,
    matching the old status-less breakdown's order); status columns are
    sorted alphabetically since there's no inherent ranking between them."""
    counts: dict[str, dict[str, int]] = {}
    statuses_seen: set[str] = set()
    for a in rows:
        device = a["device_name"] or "(UNKNOWN)"
        status = a["status"] or "(UNKNOWN)"
        statuses_seen.add(status)
        device_row = counts.setdefault(device, {})
        device_row[status] = device_row.get(status, 0) + 1

    statuses = sorted(statuses_seen)
    devices = sorted(counts, key=lambda d: sum(counts[d].values()), reverse=True)
    rows = [
        {
            "device": device,
            "cells": [counts[device].get(status, 0) for status in statuses],
            "total": sum(counts[device].values()),
        }
        for device in devices
    ]
    column_totals = [sum(counts[device].get(status, 0) for device in devices) for status in statuses]
    return {
        "statuses": statuses,
        "rows": rows,
        "column_totals": column_totals,
        "grand_total": sum(column_totals),
    }


@bp.route("/<branch_no>")
def detail(branch_no):
    conn = get_connection()
    try:
        branch = get_branch(conn, branch_no)
        if not branch:
            abort(404, description="Branch not found.")
        assets = get_current_assets(conn, branch_no=branch_no)
        # Chart: full history. Table: full Jan-Dec of the selected year, so
        # a month can be compared against the same month in a different
        # year, not just against whichever month happened to precede it -
        # see get_branch_device_year_table.
        periods, items, matrix = get_branch_item_trend(conn, branch_no)
        available_years = get_available_report_years(conn)
        selected_year = resolve_report_year(request.args.get("year"), available_years)
        trend_periods, trend_rows, trend_column_totals, trend_column_added, trend_column_removed = (
            get_branch_device_year_table(conn, branch_no, selected_year, items)
        )

        # CCTV section - same idea as the asset one above, over cctv_items
        # instead: current items (Manage CCTV's own row shape), a device-
        # type trend chart (full history), and a By Month breakdown of the
        # 4 headline metrics (recorder count, parsed camera/HDD totals)
        # for the same selected_year the asset table above uses, so one
        # year selector drives both instead of two independent ones.
        cctv_items, _cctv_total = search_cctv(conn, {"branch_no": [branch_no]}, per_page=None)
        cctv_summary = summarize_cctv_rows(cctv_items)
        cctv_periods, _cctv_items, cctv_matrix = get_branch_item_trend(conn, branch_no, table="cctv_items")
        cctv_period_rows = get_cctv_items_by_branch_period(conn, branch_no=branch_no)
        cctv_month_metrics = build_month_metrics_by_branch(cctv_period_rows, trend_periods)
        cctv_month_cells = cctv_month_metrics.get(branch_no, [None] * len(trend_periods))
        # Same "Cameras" line added to the CCTV Dashboard's all-branches
        # chart, scoped to this one branch - a best-effort sum of this
        # branch's own recorders' free-text camera count field, not a
        # device count like the other series.
        camera_totals_by_period = summarize_by_period(cctv_period_rows)
    finally:
        conn.close()

    chart_data = trend_chart_payload(periods, matrix)
    device_trend_charts = per_series_trend_payloads(periods, matrix)
    cctv_matrix["Cameras"] = {
        period: (data["camera_total"] or 0) for period, data in camera_totals_by_period.items()
    }
    cctv_chart_data = trend_chart_payload(cctv_periods, cctv_matrix)
    cctv_device_trend_charts = per_series_trend_payloads(cctv_periods, cctv_matrix)
    device_status_breakdown = _device_status_breakdown(assets)
    # Same shape, same helper - a cctv_items row also has device_name/status.
    cctv_status_breakdown = _device_status_breakdown(cctv_items)

    return render_template(
        "branch_detail.html",
        active_page="assets",
        branch=branch,
        assets=assets,
        device_status_breakdown=device_status_breakdown,
        cctv_status_breakdown=cctv_status_breakdown,
        chart_data=chart_data,
        device_trend_charts=device_trend_charts,
        available_years=available_years,
        selected_year=selected_year,
        trend_periods=trend_periods,
        trend_rows=trend_rows,
        trend_column_totals=trend_column_totals,
        trend_column_added=trend_column_added,
        trend_column_removed=trend_column_removed,
        cctv_items=cctv_items,
        cctv_summary=cctv_summary,
        cctv_chart_data=cctv_chart_data,
        cctv_device_trend_charts=cctv_device_trend_charts,
        cctv_month_cells=cctv_month_cells,
    )


def _add_trend_sheet(wb, title, periods, matrix):
    """Shared by both export routes below - a period x device-type matrix
    sheet plus a stacked total chart and one solo chart per device type,
    skipped gracefully (no sheet at all) when there's under 2 periods of
    history, same guard charts.trend_chart_payload()/per_series_trend_payloads()
    already apply to the on-page charts."""
    items = list(matrix.keys())
    if len(periods) < 2 or not items:
        return
    ws = write_trend_matrix_sheet(wb, title, periods, items, matrix)
    add_stacked_total_chart(ws, title, len(periods), len(items), "A" + str(len(periods) + 3))
    add_solo_item_charts(ws, items, len(periods), len(periods) + 20)


@bp.route("/<branch_no>/export")
def export(branch_no):
    conn = get_connection()
    try:
        branch = get_branch(conn, branch_no)
        if not branch:
            abort(404, description="Branch not found.")
        assets = get_current_assets(conn, branch_no=branch_no)
        periods, _items, matrix = get_branch_item_trend(conn, branch_no)
    finally:
        conn.close()

    safe_name = safe_filename(branch["eng_name"] or branch_no, fallback=branch_no)
    wb = build_asset_rows_workbook(assets, sheet_title="Current Assets")
    _add_trend_sheet(wb, "Item Count Trend", periods, matrix)
    return send_workbook(wb, dated_download_name(f"{safe_name} - assets"))


@bp.route("/<branch_no>/export-cctv")
def export_cctv(branch_no):
    conn = get_connection()
    try:
        branch = get_branch(conn, branch_no)
        if not branch:
            abort(404, description="Branch not found.")
        cctv_items, _total = search_cctv(conn, {"branch_no": [branch_no]}, per_page=None)
        cctv_periods, _cctv_items, cctv_matrix = get_branch_item_trend(conn, branch_no, table="cctv_items")
        camera_totals_by_period = summarize_by_period(get_cctv_items_by_branch_period(conn, branch_no=branch_no))
        cctv_matrix["Cameras"] = {
            period: (data["camera_total"] or 0) for period, data in camera_totals_by_period.items()
        }
    finally:
        conn.close()

    safe_name = safe_filename(branch["eng_name"] or branch_no, fallback=branch_no)
    wb = build_cctv_rows_workbook(cctv_items, sheet_title="Current CCTV")
    _add_trend_sheet(wb, "CCTV Item Count Trend", cctv_periods, cctv_matrix)
    return send_workbook(wb, dated_download_name(f"{safe_name} - cctv"))
