"""The main Dashboard shows every device type (no OTHER bucket), while
analytics' default still folds past MAX_SERIES for the other charts; colors
stay unique past the 8-slot palette."""

import unittest

from app import analytics
from app.charts import PALETTE, _assign_colors, trend_chart_payload


def _rows(n_items, periods=("2026-07", "2026-08")):
    return [{"period": p, "item": f"DEV{i:02d}", "cnt": 100 - i} for p in periods for i in range(n_items)]


class BuildTrendTests(unittest.TestCase):
    def test_default_folds_into_other(self):
        _periods, items, matrix = analytics._build_trend(_rows(12))
        self.assertEqual(len(items), analytics.MAX_SERIES + 1)
        self.assertEqual(items[-1], "OTHER")
        self.assertEqual(matrix["OTHER"]["2026-07"], sum(100 - i for i in range(7, 12)))

    def test_no_cap_keeps_every_item(self):
        _periods, items, matrix = analytics._build_trend(_rows(43), max_series=None)
        self.assertEqual(len(items), 43)
        self.assertNotIn("OTHER", matrix)
        self.assertEqual(items[0], "DEV00")  # still busiest first


class ColorTests(unittest.TestCase):
    def test_many_series_get_unique_colors(self):
        names = [f"DEV{i:02d}" for i in range(43)]
        colors = _assign_colors(names)
        self.assertEqual(len(colors), 43)
        self.assertEqual(len(set(colors.values())), 43)
        # The top 7 still come from the validated palette (minus OTHER's slot).
        self.assertTrue(all(colors[n] in PALETTE[:-1] for n in names[:7]))

    def test_payload_with_many_series(self):
        _periods, _items, matrix = analytics._build_trend(_rows(20), max_series=None)
        payload = trend_chart_payload(["2026-07", "2026-08"], matrix)
        self.assertEqual(len(payload["series"]), 20)


if __name__ == "__main__":
    unittest.main()
