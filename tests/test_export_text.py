# -*- coding: utf-8 -*-
"""Exports keep outside text as text: openpyxl writes a string starting
with "=" as a formula (a remark "=HYPERLINK(...)" in an imported report
would run when the export is opened), and a control character an xlsx
can't hold failed the whole export."""
import io
import os
import sys
import time
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import exports  # noqa: E402


class ExportTextTests(unittest.TestCase):
    def test_formulas_stay_text_and_bad_characters_go(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        rows = [{"remark": '=HYPERLINK("http://evil.example","click")', "name": "PC\x07-01", "n": 3}]
        exports.write_sheet(ws, [("Remark", "remark"), ("Name", "name"), ("N", "n")], rows)
        buf = io.BytesIO()
        wb.save(buf)
        cell = openpyxl.load_workbook(io.BytesIO(buf.getvalue())).active
        self.assertEqual(cell["A2"].data_type, "s")
        self.assertEqual(cell["A2"].value, '=HYPERLINK("http://evil.example","click")')
        self.assertEqual(cell["B2"].value, "PC-01")
        self.assertEqual(cell["C2"].value, 3)
        self.assertEqual(cell["A1"].value, "Remark")

    def test_big_export_is_not_quadratic(self):
        rows = [{str(c): "=x" if r % 50 == 0 and c == 3 else f"text {r}" for c in range(18)} for r in range(6000)]
        start = time.monotonic()
        ws = exports.build_workbook("t", [(str(c), str(c)) for c in range(18)], rows).active
        self.assertLess(time.monotonic() - start, 15)  # was over a minute
        self.assertEqual((ws.max_row, ws["D2"].value, ws["D2"].data_type, ws["D3"].value), (6001, "=x", "s", "text 1"))


if __name__ == "__main__":
    unittest.main()
