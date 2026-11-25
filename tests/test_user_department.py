# -*- coding: utf-8 -*-
"""Department of each user from their Aither branch code (user, 2026-11-25)."""
import csv
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
from app.queries import NO_DEPARTMENT, department_options, search_assets  # noqa: E402


class UserDepartmentTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_dept_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            for no, name in (("8061", "HEAD OFFICE"), ("7901", "RETAIL DEPARTMENT"), ("7902", "SERVICE DEPARTMENT")):
                conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at)"
                             " VALUES (?, ?, ?, datetime('now'))", (no, name, name))
            # 00111 / 111: one normalized number, two Aither rows - the enabled one counts
            for no, norm, branch, status in (("00111", "111", "7902", "9-Disable"), ("111", "111", "7901", "1-Enable"),
                                             ("222", "222", "7902", "1-Enable"), ("333", "333", "9999", "1-Enable")):
                conn.execute("INSERT INTO users (user_no, user_no_norm, branch_no, user_name, status) VALUES (?,?,?,?,?)",
                             (no, norm, branch, f"USER {no}", status))
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("dept", "deptpass12", admin)
        self.client.post("/login", data={"username": "dept", "password": "deptpass12"})
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([None] * 7 + ["Branch/TO/Center Name:", "Head Office"])
        ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        for i, user in enumerate(("111", "222", "333", ""), start=1):
            ws.append([i, "HEAD OFFICE", "PC", user, "A", "", "DELL", f"SERIAL{i}", "USING LOCAL", ""])
        path = os.path.join(tempfile.mkdtemp(prefix="am_dept_f_"), "Head Office 2026-03.xlsx")
        wb.save(path)
        import_asset_report(path, source_label=os.path.basename(path), period="2026-03")

    def _rows(self, **kw):
        conn = get_connection()
        try:
            rows, total = search_assets(conn, kw.pop("filters", {}), **kw)
            self.assertEqual(len(rows), total)
            return [(r["serial_tag"], r["user_department"]) for r in rows]
        finally:
            conn.close()

    def test_mapping_filter_sort(self):
        self.assertEqual(sorted(self._rows()), [("SERIAL1", "RETAIL DEPARTMENT"), ("SERIAL2", "SERVICE DEPARTMENT"),
                                                ("SERIAL3", None), ("SERIAL4", None)])
        self.assertEqual(self._rows(filters={"department": ["SERVICE DEPARTMENT"]}),
                         [("SERIAL2", "SERVICE DEPARTMENT")])
        self.assertEqual(len(self._rows(filters={"department": [NO_DEPARTMENT]})), 2)
        self.assertEqual(self._rows(sort="-department")[0], ("SERIAL2", "SERVICE DEPARTMENT"))
        conn = get_connection()
        try:
            self.assertEqual(department_options(conn), [NO_DEPARTMENT, "RETAIL DEPARTMENT", "SERVICE DEPARTMENT"])
            conn.execute("UPDATE users SET branch_no = '7901' WHERE user_no = '222'")  # next ID import
            conn.commit()
        finally:
            conn.close()
        self.assertIn(("SERIAL2", "RETAIL DEPARTMENT"), self._rows())

    def test_page_and_exports(self):
        page = self.client.get("/assets/", query_string={"department": "RETAIL DEPARTMENT"}).get_data(as_text=True)
        self.assertIn('title="Branch code 7901"', page)
        self.assertNotIn("SERIAL2", page)
        ws = openpyxl.load_workbook(io.BytesIO(self.client.get("/assets/export").data)).active
        rows = [dict(zip([c.value for c in ws[1]], r)) for r in ws.iter_rows(min_row=2, values_only=True)]
        one = next(r for r in rows if r["Serial/Service Tag"] == "SERIAL1")
        self.assertEqual((one["User Branch Code"], one["Department"]), ("7901", "RETAIL DEPARTMENT"))
        conn = get_connection()
        try:
            conn.execute("UPDATE asset_items SET position = 'TEXT IN POSITION'")
            conn.commit()
        finally:
            conn.close()
        got = {r["Serial Number"]: r["Department"] for r in csv.DictReader(io.StringIO(
            self.client.get("/assets/export-assetexplorer").data.decode("utf-8-sig")))}
        self.assertEqual(got, {"SERIAL1": "RETAIL DEPARTMENT", "SERIAL2": "SERVICE DEPARTMENT",
                               "SERIAL3": "TEXT IN POSITION", "SERIAL4": "TEXT IN POSITION"})


if __name__ == "__main__":
    unittest.main()
