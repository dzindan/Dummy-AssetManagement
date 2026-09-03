"""Data shaping for the interactive line-chart-with-slicers widget
(app/static/trend_chart.js) - the SVG itself is drawn client-side now, so
this module's only job is turning a period/series matrix into a small JSON
payload the widget can render and letting the user click device/branch
chips on/off without the chart repainting colors out from under them.

Colors are the validated 8-slot categorical palette from the dataviz
skill's reference instance (references/palette.md), snapped to this app's
own light-only chart surface (--panel: #ffffff in style.css). Unlike the
stacked-bar chart this replaced, each series' color is a stable hash of its
own name, not its rank in the current chart - see dataviz's "color follows
the entity, never its rank": the old rank-based scheme repainted every
surviving series' color the moment a filter changed which series were
visible (or even just changed their relative order), which is exactly what
breaks a chart meant to be filtered interactively. A name-based hash can't
give every device/branch a provably unique color once there are more
entities than palette slots (there are ~25 standard device names against
8 slots), but it keeps any *given* entity's color the same everywhere it
appears in the app, which is what a slicer needs.
"""

from __future__ import annotations

# Validated categorical order (light surface #ffffff) - worst adjacent CVD
# Delta E 9.1 (OKLab x100, protanopia/deuteranopia simulated), worst adjacent
# normal-vision Delta E 19.6. Never reorder or cycle past slot 8 - see this
# module's docstring and app/analytics.py's MAX_SERIES.
PALETTE = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red - reserved for "OTHER", the fold-of-remainder bucket
]


def stable_color_for(name: str) -> str:
    """Deterministic PALETTE slot for `name` - same process or a different
    one, same page or a different page, so e.g. "PC" is always the same
    color on both the Dashboard's all-branches chart and every Branch
    Detail page's chart. A plain polynomial hash rather than Python's
    built-in hash(): str hashing is salted per-process (PYTHONHASHSEED) by
    default specifically to make it *not* stable, which is exactly wrong
    here."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PALETTE[h % len(PALETTE)]


def trend_chart_payload(periods: list[str], series: dict[str, dict[str, int]]) -> dict:
    """JSON-serializable payload for TrendChart.init() (trend_chart.js).
    `series` maps a series name to a dict of period -> count (missing
    periods treated as 0) - same shape the old render_bar_chart() took.
    Display order (top-N by total first, "OTHER" last) is still whatever
    order the caller's `series` dict iterates in; only each series' color
    is decoupled from that order now."""
    if not periods or not series:
        return {"periods": [], "series": []}
    return {
        "periods": periods,
        "series": [
            {
                "name": name,
                "color": stable_color_for(name),
                "values": [series[name].get(p, 0) for p in periods],
            }
            for name in series
        ],
    }
