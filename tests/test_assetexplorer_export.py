# -*- coding: utf-8 -*-
"""Manage Assets -> Export for AssetExplorer (user, 2026-11-23): for
ManageEngine AssetExplorer's import - since 2026-11-27 one .xlsx with a sheet
per AE group, laid out like the user's AE template. Same isolated-DB pattern
as tests/test_as_of_month.py."""
import io
import os
import sys
import tempfile
import unittest
import zipfile

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.assetexplorer import (GROUP_COLUMNS, _has_serial, _text, asset_state, group_of,  # noqa: E402
                               user_field)
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402


def read_export(data: bytes) -> list[dict]:
    """The workbook's sheets -> every row as a dict, plus "_group" (also used by test_asset_os /
    test_user_department). Each sheet's header must be its group's template columns."""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    out = []
    for ws in wb.worksheets:
        lines = list(ws.iter_rows(values_only=True))
        assert list(lines[0]) == GROUP_COLUMNS[ws.title], ws.title
        out += [{**dict(zip(lines[0], ("" if v is None else v for v in line))), "_group": ws.title}
                for line in lines[1:]]
    return out


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
        self.assertIn(".xlsx", resp.headers["Content-Disposition"])
        return read_export(resp.data)

    def test_rows(self):
        rows = self._csv()
        self.assertEqual({r["_group"] for r in rows}, {"Computers", "Components"})   # PC, LCD; no empty sheet
        by_name = {r["Asset Name"]: r for r in rows}
        self.assertEqual(len(by_name), 3)                      # names unique
        pc = by_name["001-PC-SN1"]
        self.assertEqual((pc["_group"], pc["Product"], pc["Org Serial Number"], pc["Asset State"], pc["Site"],
                          pc["Location"], pc["User"], pc["User Login Name"], pc["IP Address"]),
                         ("Computers", "DELL OPTIPLEX 7090", "SN1", "In Use", "ZETA BRANCH", "ZETA BRANCH",
                          "NGUYỄN VĂN A(111)", "111", "10.0.0.5"))
        self.assertTrue(pc["Description"].startswith("Device: PC; AM status: USING LOCAL"))
        self.assertIn("AM period: 2026-03", pc["Description"])
        self.assertEqual(by_name["001-PC-SN1-2"]["Asset State"], "In Repair")   # duplicate serial
        lcd = [r for r in rows if r["_group"] == "Components"][0]
        self.assertTrue(lcd["Asset Name"].startswith("001-LCD-NOSN-"))
        self.assertEqual((lcd["Org Serial Number"], lcd["Product"], lcd["Asset State"], lcd["User Login Name"]),
                         ("", "LCD - UNKNOWN MODEL", "In Store", ""))
        self.assertTrue(lcd["Description"].startswith("Device: LCD"))

    def test_template_columns_and_groups(self):
        # the user's AE template (XLSForAssets.xls): every group's columns, as there
        common = ["Product", "Asset Name", "Location", "Asset State", "User Login Name", "User Domain", "User",
                  "Department", "Site", "Vendor", "Org Serial Number", "Barcode", "Asset Tag", "Purchase Cost",
                  "Acquisition Date", "Warranty Expiry Date", "Expiry Date"]
        self.assertEqual(list(GROUP_COLUMNS), ["Computers", "IT Assets", "Components", "Non IT Assets",
                                               "Mobile Devices"])
        for group, columns in GROUP_COLUMNS.items():
            self.assertEqual(columns[:17], common, group)
        self.assertEqual(len(GROUP_COLUMNS["Computers"]), 45)
        self.assertEqual(GROUP_COLUMNS["IT Assets"][17:], ["Description", "MAC Address", "IP Address", "Firmware",
                                                           "Firmware Revision"])
        self.assertEqual(GROUP_COLUMNS["Components"][17:], ["Description"])
        self.assertEqual(len(GROUP_COLUMNS["Mobile Devices"]), 29)
        for device, group in (("PC", "Computers"), ("notebook", "Computers"), ("SERVER PC", "Computers"),
                              ("TABLET", "Mobile Devices"), ("LCD", "Components"), ("COMPUTER MOUSE", "Components"),
                              ("PAPER SHREDDER", "Non IT Assets"), ("IP PHONE", "IT Assets"),
                              ("PRINTER", "IT Assets"), ("SOMETHING NEW", "IT Assets"), ("", "IT Assets")):
            self.assertEqual(group_of(device), group, device)
        # nothing to export: still a file that opens
        empty = self.client.get("/assets/export-assetexplorer?device_name=NOTHING")
        self.assertEqual([ws.title for ws in openpyxl.load_workbook(io.BytesIO(empty.data)).worksheets],
                         ["IT Assets"])

    def test_unknown_values_are_empty_cells(self):
        # not "" (an empty string cell an importer may take as a bad number / date)
        data = self.client.get("/assets/export-assetexplorer").data
        ws = openpyxl.load_workbook(io.BytesIO(data))["Components"]   # the LCD: no user, serial, model
        head = [c.value for c in ws[1]]
        for column in ("Purchase Cost", "Acquisition Date", "Warranty Expiry Date", "User Login Name",
                       "Org Serial Number", "User"):
            cell = ws.cell(2, head.index(column) + 1)
            self.assertIsNone(cell.value, column)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.startswith("xl/worksheets/"):
                    self.assertNotIn(b't="inlineStr"></c>', z.read(name), name)

    def test_a_branch_formula_stays_text(self):
        conn = get_connection()
        try:
            conn.execute("UPDATE asset_items SET remark = '=HYPERLINK(\"http://x\",\"y\")', "
                         "model_device = '=1+1' WHERE device_name = 'LCD'")
            conn.commit()
        finally:
            conn.close()
        data = self.client.get("/assets/export-assetexplorer").data
        ws = openpyxl.load_workbook(io.BytesIO(data))["Components"]
        head = [c.value for c in ws[1]]
        product, description = ws.cell(2, head.index("Product") + 1), ws.cell(2, head.index("Description") + 1)
        self.assertEqual((product.value, product.data_type), ("=1+1", "s"))
        self.assertEqual(description.data_type, "s")
        self.assertIn('Remark: =HYPERLINK("http://x","y")', description.value)

    def test_follows_filters_and_page_button(self):
        self.assertEqual([r["Product"] for r in self._csv("?device_name=LCD")], ["LCD - UNKNOWN MODEL"])
        page = self.client.get("/assets/?device_name=LCD").get_data(as_text=True)
        self.assertIn("/assets/export-assetexplorer?device_name=LCD", page)

    def _import(self, period):
        path = os.path.join(tempfile.mkdtemp(prefix="am_ae_n_"), f"Zeta Branch {period}.xlsx")
        with open(path, "wb") as f:
            f.write(_report("Zeta Branch", [
                ("LCD", "", "", "", "", "WAREHOUSE"), ("LCD", "333", "C", "", "N/A", "USING LOCAL"),
                ("PC", "222", "B", "DELL OPTIPLEX 7090", "SN1", "BROKEN"),
                ("PC", "111", "A", "DELL OPTIPLEX 7090", "SN1", "USING LOCAL"),
                ("PC", "444", "D", "DELL OPTIPLEX 7090", "SN1-2", "USING LOCAL")]))
        import_asset_report(path, source_label=os.path.basename(path), period=period)

    def test_names_stay_the_same(self):
        before = {r["Asset Name"] for r in self._csv() if r["_group"] == "Components"}
        self._import("2026-04")
        rows = self._csv()
        names = {r["Asset Name"]: r["User"] for r in rows}
        self.assertEqual(len(names), 5)
        self.assertTrue(before <= set(names))                       # new month, new row ids
        # the second SN1 may not take SN1-2, another PC's own name
        self.assertEqual({k for k in names if "-PC-" in k}, {"001-PC-SN1", "001-PC-SN1-2", "001-PC-SN1-3"})
        self.assertTrue(names["001-PC-SN1-2"].endswith("(444)"))
        for qs in ("?sort=-user_id", "?sort=user_id", "?status=BROKEN", "?q=222"):
            for r in self._csv(qs):
                self.assertEqual(names.get(r["Asset Name"]), r["User"], qs)

    def test_state_map(self):
        self.assertEqual(asset_state("LOST", "1"), "Disposed")
        self.assertEqual(asset_state("", "123"), "In Use")
        self.assertEqual(asset_state("weird", ""), "In Store")

    def test_login_name(self):
        from app.assetexplorer import login_name
        self.assertEqual([login_name(v) for v in ("26509059", " 111 ", "WAREHOUSE", "CAN THO BRANCH",
                                                  "1234 5678", "9999xxxx", "NA", "")],
                         ["26509059", "111", "", "", "", "", "", ""])

    def test_user_field(self):
        self.assertEqual(user_field("TRAN B", "123"), "TRAN B(123)")
        self.assertEqual(user_field("", "123"), "123")
        self.assertEqual(user_field("FLOOR 1", "NA"), "FLOOR 1")
        self.assertEqual(user_field("NA", ""), "")
        self.assertEqual(user_field("-", "-"), "")

    def test_one_line_and_serial_remarks(self):
        self.assertEqual(_text(" a\r\n b\tc "), "a b c")
        for s in ("", "//", "-", "N/A", "n/aa", "MẤT SERI", "LOST OF SERVICE TAG"):
            self.assertFalse(_has_serial(s), s)
        for s in ("5CD1", "NACVWVZV", "SN 123"):
            self.assertTrue(_has_serial(s), s)


if __name__ == "__main__":
    unittest.main()
