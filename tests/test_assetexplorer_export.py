# -*- coding: utf-8 -*-
"""Manage Assets -> Export for AssetExplorer (user, 2026-11-23): a CSV for
ManageEngine AssetExplorer's Import from CSV. Same isolated-DB pattern as
tests/test_as_of_month.py."""
import csv
import io
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.assetexplorer import COLUMNS, asset_state, user_field  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402


def _report(label: str, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 7 + ["Branch/TO/Center Name:", label])
    ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
    for i, (device, user, name, model, serial, status) in enumerate(rows, start=1):
        ws.append([i, label.upper(), device, user, name, "10.0.0.5", model, serial, status, ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class AssetExplorerExportTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_ae_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) VALUES (?, ?, ?, datetime('now'))",
                ("001", "ZETA BRANCH", "ZETA BRANCH"))
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        tmp = tempfile.mkdtemp(prefix="am_ae_files_")
        path = os.path.join(tmp, "Zeta Branch 2026-03.xlsx")
        with open(path, "wb") as f:
            f.write(_report("Zeta Branch", [
                ("PC", "111", "Nguyễn Văn A", "DELL OPTIPLEX 7090", "SN1", "USING LOCAL"),
                ("LCD", "", "", "", "", "WAREHOUSE"),
                ("PC", "222", "B", "DELL OPTIPLEX 7090", "SN1", "BROKEN"),
            ]))
        import_asset_report(path, source_label=os.path.basename(path), period="2026-03")
        create_account("ae", "aepass123", admin)
        self.client.post("/login", data={"username": "ae", "password": "aepass123"})

    def _csv(self, qs=""):
        resp = self.client.get("/assets/export-assetexplorer" + qs)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(".csv", resp.headers["Content-Disposition"])
        self.assertTrue(resp.data.startswith(b"\xef\xbb\xbf"))
        return list(csv.DictReader(io.StringIO(resp.data.decode("utf-8-sig"))))

    def test_rows(self):
        rows = self._csv()
        self.assertEqual(list(rows[0].keys()), COLUMNS)
        by_name = {r["Asset Name"]: r for r in rows}
        self.assertEqual(len(by_name), 3)                      # names unique
        pc = by_name["001-PC-SN1"]
        self.assertEqual((pc["Product Type"], pc["Product"], pc["Serial Number"], pc["Asset State"],
                          pc["Site"], pc["User"], pc["IP Address"]),
                         ("PC", "DELL OPTIPLEX 7090", "SN1", "In Use", "ZETA BRANCH", "NGUYỄN VĂN A(111)",
                          "10.0.0.5"))
        self.assertIn("AM status: USING LOCAL", pc["Description"])
        self.assertIn("AM period: 2026-03", pc["Description"])
        self.assertEqual(by_name["001-PC-SN1-2"]["Asset State"], "In Repair")   # duplicate serial
        lcd = [r for r in rows if r["Product Type"] == "LCD"][0]
        self.assertTrue(lcd["Asset Name"].startswith("001-LCD-NOSN-"))
        self.assertEqual((lcd["Serial Number"], lcd["Product"], lcd["Asset State"]),
                         ("", "LCD - UNKNOWN MODEL", "In Store"))

    def test_follows_filters_and_page_button(self):
        self.assertEqual([r["Product Type"] for r in self._csv("?device_name=LCD")], ["LCD"])
        page = self.client.get("/assets/?device_name=LCD").get_data(as_text=True)
        self.assertIn("/assets/export-assetexplorer?device_name=LCD", page)

    def test_state_map(self):
        self.assertEqual(asset_state("LOST", "1"), "Disposed")
        self.assertEqual(asset_state("", "123"), "In Use")
        self.assertEqual(asset_state("weird", ""), "In Store")

    def test_user_field(self):
        self.assertEqual(user_field("TRAN B", "123"), "TRAN B(123)")
        self.assertEqual(user_field("", "123"), "123")
        self.assertEqual(user_field("FLOOR 1", "NA"), "FLOOR 1")
        self.assertEqual(user_field("NA", ""), "")


if __name__ == "__main__":
    unittest.main()
