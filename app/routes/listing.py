"""What Manage Assets (asset_edit.py) and Manage CCTV (cctv_edit.py) share:
server-side paging, the one-branch filter banner, the lists for the Edit form.

Manage Assets used to load every matching row at once (see search_assets'
docstring) - fine at a few hundred rows, but painfully slow to render once
a deployment's asset_items grew into the thousands. Paginated server-side
now, Manage CCTV the same way.
"""

from urllib.parse import urlencode

from flask import request

from ..queries import UNRESOLVED_BRANCH_FILTER, get_branch, get_report_periods, parse_as_of

PER_PAGE_OPTIONS = [100, 200, 300, 400, 500, 1000, 2000, 3000, 4000, 5000]
DEFAULT_PER_PAGE = 200


def parse_per_page() -> int:
    try:
        value = int(request.args.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    return value if value in PER_PAGE_OPTIONS else DEFAULT_PER_PAGE


def as_of_month(conn) -> tuple[str, list[str]]:
    """(the "As of month" picked - "" for the latest - , every month on
    file newest first). A month at or after the newest report is the
    latest view itself, so it stays "" there (and the page stays editable)."""
    periods = get_report_periods(conn)
    as_of = parse_as_of(request.args.get("as_of"))
    if as_of and (not periods or as_of >= periods[0]):
        as_of = ""
    return as_of, periods


def parse_page() -> int:
    try:
        value = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        return 1
    return max(value, 1)


def pagination_qs() -> str:
    """Current query string (filters + per_page) with `page` stripped out -
    Prev/Next links append their own `&page=N` onto this rather than onto
    the raw request query string, so paging never accidentally carries a
    stale page number along for the ride."""
    pairs = [(k, v) for k in request.args if k != "page" for v in request.args.getlist(k)]
    return urlencode(pairs)


def selected_branch(conn, filters: dict):
    """The branch for the "filtered to this one branch" message / link (see
    the templates) - only when exactly one branch is selected: with several
    selected at once there's no single branch to link to."""
    if len(filters["branch_no"]) != 1:
        return None
    only_branch = filters["branch_no"][0]
    if only_branch == UNRESOLVED_BRANCH_FILTER:
        return {"branch_no": "", "eng_name": "Unresolved / unmatched branch"}
    return get_branch(conn, only_branch)


def standard_lists(conn) -> tuple[list[str], list[str]]:
    """(standard device names, standard statuses) for an Edit form's choices."""
    names = [r["name"] for r in conn.execute("SELECT name FROM device_standard_names ORDER BY name").fetchall()]
    statuses = [r["name"] for r in conn.execute("SELECT name FROM status_standard_names ORDER BY name").fetchall()]
    return names, statuses
