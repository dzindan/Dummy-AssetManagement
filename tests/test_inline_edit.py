# -*- coding: utf-8 -*-
"""Excel-style inline editing in Manage Assets / Manage CCTV: one cell saved
per request (POST /assets/<id>/cell, /cctv/<id>/cell) with the same rules as
the Edit page. Fixture pattern from tests/test_cctv_asset_sync.py."""
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


def _workbook(path: str) -> None:
    wb = openpyxl.Workbook()
    oa = wb.active
    oa.title = "OA EQUIPMENT"
    oa.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
    oa.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK", "HANDOVER DATE"])
    oa.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", "",
               "01/01/2024"])
    oa.append([2, "TEST BRANCH", "DVR", "", "", "", "DS-7316", "SN-DVR-1", "USING LOCAL", "", ""])
    cctv = wb.create_sheet("CCTV REPORT")
    cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
    cctv.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
                 "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
                 "LOCATION", "REMARK"])
    cctv.append([1, "TEST BRANCH", "DVR", "10.0.1.1", "HIK VISION", "DS-7316", "SN-DVR-1", "USING LOCAL", 16, "3",
                 "24TB", "IT ROOM", ""])
    wb.save(path)


class InlineEditTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_inline_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            for no, name in (("001", "TEST BRANCH"), ("002", "OTHER BRANCH")):
                conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                             "VALUES (?, ?, ?, datetime('now'))", (no, name, name))
            roles = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM roles")}
            conn.commit()
        finally:
            conn.close()
        create_account("editor", "editpass123", roles["Admin"])
        create_account("viewer", "viewpass123", roles["Viewer"])
        self.client.post("/login", data={"username": "editor", "password": "editpass123"})
        path = os.path.join(tempfile.mkdtemp(), "report.xlsx")
        _workbook(path)
        import_asset_report(path, source_label="report.xlsx", period="2026-08")

    def _one(self, sql, *args):
        conn = get_connection()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def _cell(self, kind, row_id, field, value, client=None):
        return (client or self.client).post(f"/{kind}/{row_id}/cell", json={"field": field, "value": value},
                                            headers={"X-Requested-With": "fetch"})

    def test_saves_one_cell_uppercased_and_logged(self):
        pc = self._one("SELECT * FROM asset_items WHERE serial_tag = 'SN-PC-1'")
        res = self._cell("assets", pc["id"], "remark", "  spare in warehouse ").get_json()
        self.assertTrue(res["ok"])
        self.assertEqual(res["value"], "SPARE IN WAREHOUSE")
        after = self._one("SELECT * FROM asset_items WHERE id = ?", pc["id"])
        self.assertEqual(after["remark"], "SPARE IN WAREHOUSE")
        self.assertEqual(after["full_name"], pc["full_name"])   # other fields untouched
        log = self._one("SELECT * FROM logsdb.activity_log WHERE action = 'Edited asset' AND field = 'remark'")
        self.assertEqual(log["new_value"], "SPARE IN WAREHOUSE")

    def test_handover_date_returns_usage_duration(self):
        pc = self._one("SELECT * FROM asset_items WHERE serial_tag = 'SN-PC-1'")
        res = self._cell("assets", pc["id"], "handover_date", "15/03/2020").get_json()
        self.assertTrue(res["ok"])
        self.assertTrue(res["usage_duration"])

    def test_branch_edit_moves_the_row_and_returns_its_label(self):
        pc = self._one("SELECT * FROM asset_items WHERE serial_tag = 'SN-PC-1'")
        res = self._cell("assets", pc["id"], "branch_dept", "other branch").get_json()
        self.assertEqual(res["branch_label"], "OTHER BRANCH")
        self.assertEqual(self._one("SELECT branch_no FROM asset_items WHERE id = ?", pc["id"])[0], "002")
        self.assertTrue(any("Moved to branch 002" in m["text"] for m in res["messages"]))

    def test_cctv_cell_syncs_to_the_linked_asset(self):
        dvr = self._one("SELECT * FROM cctv_items WHERE serial_tag = 'SN-DVR-1'")
        res = self._cell("cctv", dvr["id"], "model_device", "ds-7316hqhi-k4").get_json()
        self.assertTrue(res["ok"])
        self.assertEqual(self._one("SELECT model_device FROM asset_items WHERE id = ?", dvr["asset_item_id"])[0],
                         "DS-7316HQHI-K4")

    def test_bad_column_missing_row_and_viewer_are_refused(self):
        pc = self._one("SELECT * FROM asset_items WHERE serial_tag = 'SN-PC-1'")
        self.assertEqual(self._cell("assets", pc["id"], "branch_no", "002").status_code, 400)
        self.assertEqual(self._cell("assets", 999999, "remark", "x").status_code, 404)
        viewer = self.app.test_client()
        viewer.post("/login", data={"username": "viewer", "password": "viewpass123"})
        self.assertEqual(self._cell("assets", pc["id"], "remark", "x", client=viewer).status_code, 403)
        self.assertEqual(self._cell("cctv", 1, "remark", "x", client=viewer).status_code, 403)

    def test_tables_are_editable_only_for_editors(self):
        for url in ("/assets/", "/cctv/"):
            page = self.client.get(url).data.decode()
            self.assertIn("data-inline-edit", page, url)
            self.assertIn('data-field="device_name"', page, url)
            self.assertIn('<datalist id="dl-status">', page, url)
        viewer = self.app.test_client()
        viewer.post("/login", data={"username": "viewer", "password": "viewpass123"})
        page = viewer.get("/assets/").data.decode()
        self.assertNotIn("data-inline-edit", page)

    def test_edit_page_still_works(self):
        pc = self._one("SELECT * FROM asset_items WHERE serial_tag = 'SN-PC-1'")
        from app.routes.asset_edit import EDITABLE_FIELDS
        form = {f: (pc[f] or "") for f in EDITABLE_FIELDS}
        form["status"] = "broken"
        self.client.post(f"/assets/{pc['id']}/edit", data=form)
        self.assertEqual(self._one("SELECT status FROM asset_items WHERE id = ?", pc["id"])[0], "BROKEN")


if __name__ == "__main__":
    unittest.main()
