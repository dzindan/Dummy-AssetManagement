from flask import Blueprint, abort, render_template
from markupsafe import Markup

from ..analytics import get_branch_device_month_changes, get_branch_item_trend
from ..charts import render_line_chart
from ..db import get_connection
from ..exports import build_asset_rows_workbook, send_workbook
from ..paths import safe_filename
from ..queries import get_branch, get_current_assets

bp = Blueprint("branch_detail", __name__, url_prefix="/branch")


@bp.route("/<branch_no>")
def detail(branch_no):
    conn = get_connection()
    try:
        branch = get_branch(conn, branch_no)
        if not branch:
            abort(404, description="Branch not found.")
        assets = get_current_assets(conn, branch_no=branch_no)
        # Chart: full history. Table: only the most recent months (with a
        # change indicator on just the latest one) - see analytics.py.
        periods, items, matrix = get_branch_item_trend(conn, branch_no)
        trend_periods, changes_by_device = get_branch_device_month_changes(conn, branch_no, items)
    finally:
        conn.close()

    chart_html = Markup(render_line_chart(periods, matrix))

    device_counts: dict[str, int] = {}
    for a in assets:
        key = a["device_name"] or "(UNKNOWN)"
        device_counts[key] = device_counts.get(key, 0) + 1

    empty_cells = [None] * len(trend_periods)
    trend_rows = [
        {"item": item, "cells": changes_by_device.get(item, {}).get("cells", empty_cells)}
        for item in items
    ]
    trend_column_totals = [
        sum((c["count"] if c else 0) for c in (r["cells"][i] for r in trend_rows)) for i in range(len(trend_periods))
    ]
    latest_added_total = sum((r["cells"][-1]["added"] or 0) for r in trend_rows if r["cells"] and r["cells"][-1])
    latest_removed_total = sum((r["cells"][-1]["removed"] or 0) for r in trend_rows if r["cells"] and r["cells"][-1])

    return render_template(
        "branch_detail.html",
        active_page="assets",
        branch=branch,
        assets=assets,
        device_counts=sorted(device_counts.items(), key=lambda kv: kv[1], reverse=True),
        chart_html=chart_html,
        trend_periods=trend_periods,
        trend_rows=trend_rows,
        trend_column_totals=trend_column_totals,
        trend_latest_added=latest_added_total,
        trend_latest_removed=latest_removed_total,
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
    return send_workbook(wb, f"{safe_name} - assets.xlsx")
