"""Server-side column sort for the paginated Manage Assets / Manage CCTV
tables (user's request 2026-10-06). Every other table is sorted in the
browser instead (app.js, "Click-to-sort tables") - these two can't be,
since a page only holds 100-5000 of the matching rows and sorting just that
page would look right while being wrong.

`?sort=<key>` sorts ascending, `?sort=-<key>` descending; clicking a header
cycles none -> ascending -> descending -> none (next_sort). Keys map to SQL
through each table's own whitelist (queries.ASSET_SORTS / CCTV_SORTS,
turned into an ORDER BY by queries.order_by_sql), so the URL never reaches
the ORDER BY as raw text. Blank values always sort last, whichever
direction.
"""

from __future__ import annotations

from urllib.parse import urlencode

from flask import request


def current_sort(sorts: dict) -> str:
    """The request's ?sort= if it names one of `sorts`' keys, else ""."""
    sort = request.args.get("sort", "")
    return sort if sort.removeprefix("-") in sorts else ""


def next_sort(current: str, key: str) -> str:
    if current == key:
        return "-" + key
    if current == "-" + key:
        return ""
    return key


def sort_url(key: str) -> str:
    """This page's URL with the sort for `key` advanced one step - every
    filter and per_page kept, page reset to 1."""
    args = [(k, v) for k in request.args if k not in ("sort", "page") for v in request.args.getlist(k)]
    new = next_sort(request.args.get("sort", ""), key)
    if new:
        args.append(("sort", new))
    return request.path + ("?" + urlencode(args) if args else "")
