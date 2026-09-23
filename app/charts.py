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
8 slots), so _assign_colors() resolves same-chart collisions by moving the
lower-ranked of two colliding series to the next free slot; a given
entity's color is therefore stable *unless* a chart's specific mix of
series forces it to move, which is still a big improvement over the old
scheme repainting on every filter change.
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


def _hash(name: str) -> int:
    """Plain polynomial hash rather than Python's built-in hash(): str
    hashing is salted per-process (PYTHONHASHSEED) by default specifically
    to make it *not* stable, which is exactly wrong here."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def stable_color_for(name: str) -> str:
    """Deterministic PALETTE slot for `name` in isolation - same process or
    a different one, same page or a different page, so e.g. "PC" is always
    the same color when it's the only thing asking. Real charts should go
    through _assign_colors() instead, which resolves same-chart collisions;
    this is kept as the building block that gives a name its *preferred*
    slot before collision resolution."""
    return PALETTE[_hash(name) % len(PALETTE)]


def _assign_colors(names: list[str]) -> dict[str, str]:
    """Collision-free color per name for a single chart's shown series.
    "OTHER" (the fold-of-remainder bucket) is pinned to the reserved last
    slot (see PALETTE) and never displaces or is displaced by a real
    series. The remaining names get their stable_color_for() slot as a
    *preference*; MAX_SERIES (app/analytics.py) caps real series at
    len(PALETTE) - 1, so there's always a free slot among the rest, but two
    preferences can still land on the same slot (there are ~25 standard
    device names against 7 non-reserved slots) - the second name to claim a
    taken slot moves to the next free one instead of sharing a color.
    Iteration order is the same top-N order the caller already put `names`
    in, so on a collision it's always the lower-ranked series that moves,
    keeping the busier series' color the more stable of the two."""
    other_slot = len(PALETTE) - 1
    assigned: dict[str, str] = {}
    used = {other_slot}
    if "OTHER" in names:
        assigned["OTHER"] = PALETTE[other_slot]
    for name in names:
        if name == "OTHER":
            continue
        slot = _hash(name) % other_slot
        while slot in used:
            slot = (slot + 1) % other_slot
        used.add(slot)
        assigned[name] = PALETTE[slot]
    return assigned


def trend_chart_payload(periods: list[str], series: dict[str, dict[str, int]]) -> dict:
    """JSON-serializable payload for TrendChart.init() (trend_chart.js).
    `series` maps a series name to a dict of period -> count (missing
    periods treated as 0) - same shape the old render_bar_chart() took.
    Display order (top-N by total first, "OTHER" last) is still whatever
    order the caller's `series` dict iterates in; colors are assigned from
    that same order via _assign_colors() so each chart's own shown series
    never collide, even though a given name's color can still shift chart
    to chart depending who else is shown alongside it. A single period
    isn't a trend, so it counts as no data too - trend_chart.js's own
    emptyMessage says "import at least two months to see a trend"."""
    if len(periods) < 2 or not series:
        return {"periods": [], "series": []}
    colors = _assign_colors(list(series))
    return {
        "periods": periods,
        "series": [
            {
                "name": name,
                "color": colors[name],
                "values": [series[name].get(p, 0) for p in periods],
            }
            for name in series
        ],
    }


def per_series_trend_payloads(periods: list[str], series: dict[str, dict[str, int]]) -> dict[str, dict]:
    """One single-series payload per item in `series`, for the "one chart
    per device type" grid alongside the combined chart. Colors come from
    the same _assign_colors() call the combined chart's trend_chart_payload()
    would make for this same `series` dict, so a device's solo chart always
    matches its segment color in the combined stacked chart above it. Empty
    dict under the same "not enough history" guard as trend_chart_payload."""
    if len(periods) < 2 or not series:
        return {}
    colors = _assign_colors(list(series))
    return {
        name: {
            "periods": periods,
            "series": [{"name": name, "color": colors[name], "values": [series[name].get(p, 0) for p in periods]}],
        }
        for name in series
    }
