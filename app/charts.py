"""Small dependency-free SVG line chart renderer.

Everything else in this app avoids CDN/JS dependencies so it keeps working
fully offline once packaged; charting is no exception, so this hand-rolls a
multi-series line chart as inline SVG (plus an HTML legend) instead of
pulling in a JS charting library.
"""

from __future__ import annotations

import math

PALETTE = [
    "#1d4ed8", "#dc2626", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#db2777", "#65a30d", "#4b5563", "#ea580c", "#0d9488",
]


def _nice_step(max_val: float) -> float:
    raw_step = (max_val / 4) or 1
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mult in (1, 2, 5, 10):
        step = mult * magnitude
        if step >= raw_step:
            return step
    return magnitude * 10


def render_line_chart(
    periods: list[str],
    series: dict[str, dict[str, int]],
    width: int = 900,
    height: int = 400,
) -> str:
    """`series` maps a series name to a dict of period -> count (missing
    periods are treated as 0). Returns an HTML fragment (SVG + legend)."""
    if not periods or not series:
        return '<p class="muted">Not enough historical data yet - import at least two months to see a trend.</p>'

    pad_left, pad_right, pad_top, pad_bottom = 46, 16, 16, 34
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    all_values = [series[name].get(p, 0) for name in series for p in periods]
    max_val = max(all_values) if all_values else 0
    step = _nice_step(max_val) if max_val > 0 else 1
    y_max = step * 4 if max_val > 0 else 4

    n = len(periods)

    def x_for(i: int) -> float:
        return pad_left if n == 1 else pad_left + plot_w * i / (n - 1)

    def y_for(v: float) -> float:
        return pad_top + plot_h * (1 - (v / y_max if y_max else 0))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;font-family:Segoe UI, Arial, sans-serif;">'
    ]

    for gi in range(5):
        val = y_max * gi / 4
        y = y_for(val)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" font-size="11" fill="#6b7280" '
            f'text-anchor="end">{int(round(val))}</text>'
        )

    for i, p in enumerate(periods):
        x = x_for(i)
        parts.append(
            f'<text x="{x:.1f}" y="{height - pad_bottom + 18}" font-size="11" fill="#6b7280" '
            f'text-anchor="middle">{p}</text>'
        )

    for idx, (name, values_by_period) in enumerate(series.items()):
        color = PALETTE[idx % len(PALETTE)]
        values = [values_by_period.get(p, 0) for p in periods]
        points = " ".join(f"{x_for(i):.1f},{y_for(v):.1f}" for i, v in enumerate(values))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for i, v in enumerate(values):
            parts.append(f'<circle cx="{x_for(i):.1f}" cy="{y_for(v):.1f}" r="3" fill="{color}"/>')

    parts.append("</svg>")

    legend = ['<div class="chart-legend">']
    for idx, name in enumerate(series.keys()):
        color = PALETTE[idx % len(PALETTE)]
        legend.append(
            f'<span class="chart-legend-item">'
            f'<span class="chart-legend-swatch" style="background:{color}"></span>{name}</span>'
        )
    legend.append("</div>")

    return "".join(parts) + "".join(legend)
