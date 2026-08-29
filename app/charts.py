"""Small dependency-free SVG stacked-bar chart renderer.

Everything else in this app avoids CDN/JS dependencies so it keeps working
fully offline once packaged; charting is no exception, so this hand-rolls a
stacked bar chart as inline SVG (plus an HTML legend) instead of pulling in a
JS charting library. Each bar is one time period; its segments are that
period's series (item counts), stacked bottom-up so the bar's total height
reads as "how many assets that month" and its segments read as composition -
matching a part-to-whole-over-time story better than a multi-line chart did.

Colors are the validated 8-slot categorical palette from the dataviz
skill's reference instance (references/palette.md), snapped to this app's
own light-only chart surface (--panel: #ffffff in style.css - see
app/analytics.py's MAX_SERIES comment for why the cap is 7 real series +
"OTHER" rather than the full 10 the chart used to allow).
"""

from __future__ import annotations

import html
import math

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

GRIDLINE_COLOR = "#e5e7eb"
AXIS_TEXT_COLOR = "#6b7280"
TOTAL_LABEL_COLOR = "#1f2937"  # matches this app's --text token (style.css) - a direct label, not chrome

BAR_MAX_THICKNESS = 24  # px - never fill the whole slot; let air breathe around it
SEGMENT_GAP = 2  # px surface-color gap between stacked segments and between bars
CORNER_RADIUS = 4  # px, top of the topmost segment only - square everywhere else


def _nice_step(max_val: float) -> float:
    raw_step = (max_val / 4) or 1
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mult in (1, 2, 5, 10):
        step = mult * magnitude
        if step >= raw_step:
            return step
    return magnitude * 10


def _top_rounded_rect_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A rect from (x, y) to (x+w, y+h) with its top two corners rounded and
    its bottom two corners square - the "4px rounded data-end, square at the
    baseline" spec, applied here to a stacked segment's outward end rather
    than a literal chart baseline (the segment beneath it plays that role)."""
    r = max(0, min(r, w / 2, h))
    if r == 0:
        return f"M{x},{y} H{x + w} V{y + h} H{x} Z"
    return (
        f"M{x},{y + h} "
        f"L{x},{y + r} "
        f"Q{x},{y} {x + r},{y} "
        f"L{x + w - r},{y} "
        f"Q{x + w},{y} {x + w},{y + r} "
        f"L{x + w},{y + h} "
        f"Z"
    )


def render_bar_chart(
    periods: list[str],
    series: dict[str, dict[str, int]],
    width: int = 900,
    height: int = 400,
) -> str:
    """`series` maps a series name to a dict of period -> count (missing
    periods are treated as 0). Renders one stacked bar per period - segment
    order matches `series`' own iteration order (callers already put the
    busiest items first and "OTHER" last; this never re-sorts by value, so a
    series keeps its color and stack position across periods - see
    color-formula.md's "color follows the entity, never its rank").
    Returns an HTML fragment (SVG + legend)."""
    if not periods or not series:
        return '<p class="muted">Not enough historical data yet - import at least two months to see a trend.</p>'

    pad_left, pad_right, pad_top, pad_bottom = 46, 16, 26, 34  # extra top padding: room for each bar's total label
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    names = list(series.keys())
    stack_totals = [sum(series[name].get(p, 0) for name in names) for p in periods]
    max_val = max(stack_totals) if stack_totals else 0
    step = _nice_step(max_val) if max_val > 0 else 1
    y_max = step * 4 if max_val > 0 else 4

    n = len(periods)
    band_w = plot_w / n
    bar_w = min(BAR_MAX_THICKNESS, band_w * 0.6)

    def band_x(i: int) -> float:
        return pad_left + band_w * i + (band_w - bar_w) / 2

    def y_for(v: float) -> float:
        return pad_top + plot_h * (1 - (v / y_max if y_max else 0))

    baseline_y = y_for(0)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Stacked bar chart of item counts by month" '
        f'style="width:100%;height:auto;font-family:Segoe UI, Arial, sans-serif;">'
    ]

    for gi in range(5):
        val = y_max * gi / 4
        y = y_for(val)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="{GRIDLINE_COLOR}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" font-size="11" fill="{AXIS_TEXT_COLOR}" '
            f'text-anchor="end">{int(round(val))}</text>'
        )

    for i, p in enumerate(periods):
        x = band_x(i) + bar_w / 2
        parts.append(
            f'<text x="{x:.1f}" y="{height - pad_bottom + 18}" font-size="11" fill="{AXIS_TEXT_COLOR}" '
            f'text-anchor="middle">{html.escape(p)}</text>'
        )

    for i, p in enumerate(periods):
        x = band_x(i)
        cum = 0
        # Bottom-up so slot 1 sits at the baseline, matching the legend/table
        # order (top_items sorted by total desc) reading top-to-bottom there
        # as "biggest series at the bottom of its own stack."
        for idx, name in enumerate(names):
            value = series[name].get(p, 0)
            if value <= 0:
                continue
            color = PALETTE[idx % len(PALETTE)]
            y_bottom = y_for(cum)
            y_top = y_for(cum + value)
            cum += value
            is_top_segment = idx == len(names) - 1 or all(series[n2].get(p, 0) <= 0 for n2 in names[idx + 1 :])
            seg_h = y_bottom - y_top
            # Leave the gap below this segment (i.e. shrink its own bottom
            # edge) so touching segments never share a hard-edged border -
            # the segment resting on the true baseline keeps its full height
            # since the axis line, not another mark, is beneath it.
            gap = 0 if y_bottom >= baseline_y - 0.5 else SEGMENT_GAP
            draw_h = max(0.0, seg_h - gap)
            title = html.escape(f"{name}: {value} ({p})")
            if is_top_segment:
                path = _top_rounded_rect_path(x, y_top, bar_w, draw_h, CORNER_RADIUS)
                parts.append(f'<path d="{path}" fill="{color}"><title>{title}</title></path>')
            else:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{draw_h:.1f}" '
                    f'fill="{color}"><title>{title}</title></rect>'
                )

    # The one number that's worth labeling directly on a stacked bar: its
    # total (see marks-and-anatomy.md's "Columns -> value on the cap") -
    # every segment's own value is still one hover/title away, so this
    # doesn't repeat marks-and-anatomy.md's "never a number on every point".
    for i, total in enumerate(stack_totals):
        if total <= 0:
            continue
        x = band_x(i) + bar_w / 2
        y = y_for(total) - 6
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="12" font-weight="600" fill="{TOTAL_LABEL_COLOR}" '
            f'text-anchor="middle">{total}</text>'
        )

    parts.append("</svg>")

    legend = ['<div class="chart-legend">']
    for idx, name in enumerate(names):
        color = PALETTE[idx % len(PALETTE)]
        legend.append(
            f'<span class="chart-legend-item">'
            f'<span class="chart-legend-swatch" style="background:{color}"></span>{html.escape(name)}</span>'
        )
    legend.append("</div>")

    return "".join(parts) + "".join(legend)
