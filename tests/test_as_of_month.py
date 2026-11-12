# -*- coding: utf-8 -*-
""""As of month" view (user, 2026-11-12): Manage Assets, Manage CCTV,
Lookup and Branch Detail can show each branch's newest report up to a month
instead of the latest - a branch with no report for that exact month shows
its report from before it - read-only. Same isolated-DB pattern as
tests/test_period_skip.py."""
import io
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402
from app.queries import assets_cte, get_current_assets, parse_as_of, search_assets  # noqa: E402

BRANCHES = (("001", "ZETA BRANCH"), ("002", "ALPHA BRANCH"))


def _report(label: str, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 7 + ["Branch/TO/Center Name:", label])
    ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
    for i, (user, serial) in enumerate(rows, start=1):
        ws.append([i, label.upper(), "PC", user, "A", "", "DELL", serial, "USING LOCAL", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class AsOfMonthTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_asof_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            for no, name in BRANCHES:
                conn.execute(
                    "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) VALUES (?, ?, ?, datetime('now'))",
                    (no, name, name))
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        tmp = tempfile.mkdtemp(prefix="am_asof_files_")
        # Zeta: Jan and Mar (user 111 hands Z1 over to 222 in March).
        # Alpha: Jan and Feb.
        for label, period, rows in (
            ("Zeta Branch", "2026-01", [("111", "Z1"), ("111", "Z2")]),
            ("Zeta Branch", "2026-03", [("222", "Z1"), ("111", "Z3")]),
            ("Alpha Branch", "2026-01", [("333", "A1")]),
            ("Alpha Branch", "2026-02", [("333", "A2")]),
        ):
            path = os.path.join(tmp, f"{label} {period}.xlsx")
            with open(path, "wb") as f:
                f.write(_report(label, rows))
            import_asset_report(path, source_label=os.path.basename(path), period=period)
        create_account("asof", "asofpass1", admin)
        self.client.post("/login", data={"username": "asof", "password": "asofpass1"})

    def _serials(self, as_of="", **kw):
        conn = get_connection()
        try:
            return sorted((r["serial_tag"], r["period"]) for r in get_current_assets(conn, as_of=as_of, **kw))
        finally:
            conn.close()

    def test_parse_as_of(self):
        self.assertEqual(parse_as_of("2026-02"), "2026-02")
        for bad in ("", "2026-13", "2026-2", "2026-02' OR 1=1 --", None):
            self.assertEqual(parse_as_of(bad), "")
        with self.assertRaises(ValueError):
            assets_cte("2026-02'; DROP TABLE x")

    def test_each_branch_newest_report_up_to_the_month(self):
        self.assertEqual(self._serials(), [("A2", "2026-02"), ("Z1", "2026-03"), ("Z3", "2026-03")])
        # February: Alpha's own Feb, Zeta has no Feb report -> its January one.
        self.assertEqual(self._serials("2026-02"), [("A2", "2026-02"), ("Z1", "2026-01"), ("Z2", "2026-01")])
        self.assertEqual(self._serials("2026-01"), [("A1", "2026-01"), ("Z1", "2026-01"), ("Z2", "2026-01")])
        self.assertEqual(self._serials("2025-12"), [])
        # who held Z1 then
        self.assertEqual(self._serials("2026-02", user_id_norm="111"), [("Z1", "2026-01"), ("Z2", "2026-01")])
        conn = get_connection()
        try:
            rows, total = search_assets(conn, {"branch_no": ["001"]}, as_of="2026-02")
        finally:
            conn.close()
        self.assertEqual((total, sorted(r["serial_tag"] for r in rows)), (2, ["Z1", "Z2"]))

    def test_manage_assets_page(self):
        latest = self.client.get("/assets/").get_data(as_text=True)
        self.assertIn('name="as_of"', latest)
        self.assertIn("data-inline-edit", latest)
        self.assertNotIn("As of 2026-", latest)
        page = self.client.get("/assets/?as_of=2026-02").get_data(as_text=True)
        self.assertIn("As of 2026-02", page)
        self.assertIn("Z2", page)
        self.assertNotIn("Z3", page)
        self.assertNotIn("data-inline-edit", page)      # read-only
        self.assertNotIn("Delete Selected", page)
        self.assertNotIn('value="2026-03" checked', page)
        # the newest month is the latest view itself (still editable)
        self.assertIn("data-inline-edit", self.client.get("/assets/?as_of=2026-03").get_data(as_text=True))
        # a bad value is ignored
        self.assertIn("Z3", self.client.get("/assets/?as_of=xx").get_data(as_text=True))
        resp = self.client.get("/assets/export?as_of=2026-02")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("as_of_2026-02", resp.headers["Content-Disposition"])
        ws = openpyxl.load_workbook(io.BytesIO(resp.data)).active
        self.assertEqual(ws.title, "Assets as of 2026-02")
        self.assertEqual(ws.max_row - 1, 3)  # A2 + Zeta's January Z1, Z2

    def test_lookup_and_branch_detail(self):
        page = self.client.get("/lookup/?q=111&search_by=user&as_of=2026-02").get_data(as_text=True)
        self.assertIn("Assets assigned as of 2026-02", page)
        self.assertIn("Z2", page)
        self.assertNotIn('name="asset_id"', page)        # no hand-over builder for a past month
        latest = self.client.get("/lookup/?q=111&search_by=user").get_data(as_text=True)
        self.assertIn('name="asset_id"', latest)
        self.assertNotIn("Z2", latest)
        serial = self.client.get("/lookup/?q=Z1&search_by=serial&as_of=2026-02").get_data(as_text=True)
        self.assertIn("View User", serial)
        self.assertIn("2026-01", serial)

        page = self.client.get("/branch/001?as_of=2026-02").get_data(as_text=True)
        self.assertIn("Assets as of 2026-02", page)
        self.assertIn("from the 2026-01 report", page)
        self.assertIn("Z2", page)
        self.assertNotIn("Z3", page)
        self.assertIn("as_of=2026-02", page)             # export links / breakdown links carry it
        resp = self.client.get("/branch/001/export?as_of=2026-02")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Assets as of 2026-02", openpyxl.load_workbook(io.BytesIO(resp.data)).sheetnames)
        self.assertEqual(self.client.get("/cctv/?as_of=2026-02").status_code, 200)


if __name__ == "__main__":
    unittest.main()
