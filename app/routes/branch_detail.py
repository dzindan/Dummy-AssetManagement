from flask import Blueprint, abort, render_template, request

from ..analytics import (
    get_available_report_years,
    get_branch_device_year_table,
    get_branch_item_trend,
    resolve_report_year,
)
from ..charts import trend_chart_payload
from ..db import get_connection
from ..exports import build_asset_rows_workbook, dated_download_name, send_workbook
from ..paths import safe_filename
from ..queries import get_branch, get_current_assets

bp = Blueprint("branch_detail", __name__, url_prefix="/branch")


def _device_status_breakdown(assets) -> dict:
    """Device Type Breakdown, split by Status - one row per device type,
    one column per status seen among this branch's current assets, so e.g.
    "how many PCs are actually BROKEN vs. still USING LOCAL" is visible at a
    glance instead of only the device's overall count.

    Rows are sorted by each device's own total (busiest device type first,
    matching the old status-less breakdown's order); status columns are
    sorted alphabetically since there's no inherent ranking between them."""
    counts: dict[str, dict[str, int]] = {}
    statuses_seen: set[str] = set()
    for a in assets:
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
    finally:
        conn.close()

    chart_data = trend_chart_payload(periods, matrix)
    device_status_breakdown = _device_status_breakdown(assets)

    return render_template(
        "branch_detail.html",
        active_page="assets",
        branch=branch,
        assets=assets,
        device_status_breakdown=device_status_breakdown,
        chart_data=chart_data,
        available_years=available_years,
        selected_year=selected_year,
        trend_periods=trend_periods,
        trend_rows=trend_rows,
        trend_column_totals=trend_column_totals,
        trend_column_added=trend_column_added,
        trend_column_removed=trend_column_removed,
    )


@bp.route("/<branch_no>/export")
def export(branch_no):
    conn = get_connection()
    try:
        branch = get_branch(conn, branch_no)
        if not branch:
            abort(404, description="Branch not found.")
        assets = get_current_assets(conn, branch_no=branch_no)
    finally:
        conn.close()

    safe_name = safe_filename(branch["eng_name"] or branch_no, fallback=branch_no)
    wb = build_asset_rows_workbook(assets, sheet_title="Current Assets")
    return send_workbook(wb, dated_download_name(f"{safe_name} - assets"))
