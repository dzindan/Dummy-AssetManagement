from flask import Blueprint, render_template
from markupsafe import Markup

from ..analytics import get_all_branches_item_trend, get_branch_month_change_table
from ..charts import render_line_chart
from ..db import get_connection
from ..queries import get_current_asset_count, get_current_branch_breakdown, get_latest_batch

bp = Blueprint("dashboard", __name__)


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
        month_periods, month_table, month_column_totals, month_grand_added, month_grand_removed = (
            get_branch_month_change_table(conn)
        )

        unmapped_device_count = conn.execute("SELECT COUNT(*) c FROM device_unmapped").fetchone()["c"]
    finally:
        conn.close()

    all_branches_chart_html = Markup(render_line_chart(all_periods, all_matrix))

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
        all_branches_chart_html=all_branches_chart_html,
        unmapped_device_count=unmapped_device_count,
        month_periods=month_periods,
        month_table=month_table,
        month_column_totals=month_column_totals,
        month_grand_added=month_grand_added,
        month_grand_removed=month_grand_removed,
    )
