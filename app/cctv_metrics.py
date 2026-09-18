"""Parsing/aggregation shared by the CCTV Dashboard's per-branch tree and
Branch Detail's own CCTV section - both need the same "sum whatever parses
out of camera_count/hdd_count/hdd_capacity's free text" logic and the same
month-over-month numeric delta, just grouped differently (dashboard: one
summary per branch; branch detail: one summary for a single branch, split
by month).
"""

from __future__ import annotations

import re

_LEADING_INT_RE = re.compile(r"\d+")
# First "<number> TB/GB/T" in a capacity string - real files write this in
# every shape from "24TB" to "16TB (2X8TB) Total" to "7452.04 GB" to
# "21.86T" (see cctv_items.hdd_capacity in production); this only needs the
# headline total, not to parse every parenthetical breakdown.
_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|T)\b", re.IGNORECASE)


def parse_leading_int(text: str) -> int | None:
    match = _LEADING_INT_RE.search(text or "")
    return int(match.group()) if match else None


def parse_capacity_tb(text: str) -> float | None:
    match = _CAPACITY_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    return value / 1024 if match.group(2).upper() == "GB" else value


def numeric_delta(cur, prev):
    return cur - prev if cur is not None and prev is not None else None


def summarize_cctv_rows(rows) -> dict:
    """Recorder count + best-effort camera/HDD totals for one flat list of
    cctv_items-shaped rows (one branch's worth, or one branch/period's
    worth) - a total is left None (rendered "-") rather than silently
    undercounted when NONE of the rows parsed, but a partial parse still
    sums what it can (the common case: one stray unit typo alongside
    several clean rows)."""
    cameras = [v for v in (parse_leading_int(r["camera_count"]) for r in rows) if v is not None]
    hdd_counts = [v for v in (parse_leading_int(r["hdd_count"]) for r in rows) if v is not None]
    hdd_capacities = [v for v in (parse_capacity_tb(r["hdd_capacity"]) for r in rows) if v is not None]
    return {
        "recorder_count": len(rows),
        "camera_total": sum(cameras) if cameras else None,
        "hdd_count_total": sum(hdd_counts) if hdd_counts else None,
        "hdd_capacity_total_tb": sum(hdd_capacities) if hdd_capacities else None,
    }


def build_month_metrics_by_branch(rows, month_periods):
    """Groups rows (each needs `bkey`, `period`, `camera_count`,
    `hdd_count`, `hdd_capacity`) into one summarize_cctv_rows() result per
    (branch, period), with each metric's own delta vs whatever period
    actually precedes it on record (which may fall outside `month_periods`,
    e.g. the prior year's December for January - same rule
    analytics.get_branch_month_change_table already uses for its identity-
    based asset added/removed, just a plain numeric diff here since
    there's no per-camera/per-HDD identity to track, only a running
    total). Returns {bkey: [month_periods-shaped list of summary dicts or
    None]}."""
    by_branch_period: dict[tuple[str, str], list] = {}
    for row in rows:
        by_branch_period.setdefault((row["bkey"], row["period"]), []).append(row)

    by_branch: dict[str, dict[str, dict]] = {}
    for (bkey, period), items in by_branch_period.items():
        by_branch.setdefault(bkey, {})[period] = summarize_cctv_rows(items)

    result: dict[str, list] = {}
    for bkey, by_period in by_branch.items():
        prev = None
        for period in sorted(by_period):
            cur = by_period[period]
            cur["recorder_delta"] = numeric_delta(cur["recorder_count"], prev["recorder_count"] if prev else None)
            cur["camera_delta"] = numeric_delta(cur["camera_total"], prev["camera_total"] if prev else None)
            cur["hdd_count_delta"] = numeric_delta(
                cur["hdd_count_total"], prev["hdd_count_total"] if prev else None
            )
            cur["hdd_capacity_delta"] = numeric_delta(
                cur["hdd_capacity_total_tb"], prev["hdd_capacity_total_tb"] if prev else None
            )
            prev = cur
        result[bkey] = [by_period.get(period) for period in month_periods]
    return result
