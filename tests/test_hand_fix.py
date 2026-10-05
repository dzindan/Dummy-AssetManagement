# -*- coding: utf-8 -*-
"""Fixing an unmapped Device/Model/Status by hand (db.apply_hand_fix): the
fix reaches the same asset's other months and its linked CCTV/asset rows,
the value leaves Unmapped, and the asset's next monthly file is corrected
on import (db.apply_hand_fixes_to_batch). A normal change of a mapped value
stays a one-month change. Fixture pattern from tests/test_cctv_asset_sync.py.
"""
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
from app.routes.asset_edit import EDITABLE_FIELDS as ASSET_FIELDS  # noqa: E402
from app.routes.cctv_edit import EDITABLE_FIELDS as CCTV_FIELDS  # noqa: E402

BAD = "Không sử dụng"   # what the branch typed as the DVR's device name
BAD_UPPER = "KHÔNG SỬ DỤNG"


def _workbook(path: str) -> None:
    """OA sheet with a PC; CCTV sheet with a no-serial DVR typed as BAD."""
    wb = openpyxl.Workbook()
    oa = wb.active
    oa.title = "OA EQUIPMENT"
    oa.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
    oa.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
    oa.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", ""])
    cctv = wb.create_sheet("CCTV REPORT")
    cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
    cctv.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
                 "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
                 "LOCATION", "REMARK"])
    cctv.append([1, "TEST BRANCH", BAD, "", "GOLDEYE", "", "", "USING LOCAL", 14, "3", "2TB", "SERVER ROOM", ""])
    wb.save(path)


class HandFixTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_handfix_")
        self.app = create_app()
        self.client = self.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="am_handfix_files_")
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))")
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("fixer", "fixpass123", admin)
        self.client.post("/login", data={"username": "fixer", "password": "fixpass123"})
        for period in ("2026-06", "2026-07"):
            self._import(period)

    def _import(self, period):
        path = os.path.join(self.tmp, f"{period}.xlsx")
        _workbook(path)
        import_asset_report(path, source_label=f"{period}.xlsx", period=period)

    def _q(self, sql, *args):
        conn = get_connection()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _device_names(self, table):
        return {r[0] for r in self._q(f"SELECT device_name FROM {table} WHERE device_name_raw = ?", BAD)}

    def _queued(self):
        return bool(self._q("SELECT 1 FROM device_unmapped WHERE raw_name = ?", BAD_UPPER))

    def _edit_cctv_device(self, period, new_device):
        item = self._q("SELECT c.* FROM cctv_items c JOIN import_batches b ON b.id = c.batch_id "
                       "WHERE b.period = ? AND c.device_name_raw = ?", period, BAD)[0]
        form = {f: (item[f] or "") for f in CCTV_FIELDS}
        form["device_name"] = new_device
        return self.client.post(f"/cctv/{item['id']}/edit", data=form, follow_redirects=True)

    def test_fix_reaches_other_months_and_linked_assets_and_leaves_unmapped(self):
        self.assertTrue(self._queued())
        resp = self._edit_cctv_device("2026-07", "DVR/CCTV RECORDER")
        self.assertIn(b"other row(s)", resp.data)
        self.assertEqual(self._device_names("cctv_items"), {"DVR/CCTV RECORDER"})   # June too
        self.assertEqual(self._device_names("asset_items"), {"DVR/CCTV RECORDER"})  # mirrored rows, both months
        self.assertFalse(self._queued())
        # Not a global mapping: no alias was created.
        self.assertEqual(self._q("SELECT * FROM device_aliases WHERE alias = ?", BAD_UPPER), [])

    def test_next_months_file_is_corrected_on_import(self):
        self._edit_cctv_device("2026-07", "DVR/CCTV RECORDER")
        self._import("2026-08")
        self.assertEqual(self._device_names("cctv_items"), {"DVR/CCTV RECORDER"})
        self.assertEqual(self._device_names("asset_items"), {"DVR/CCTV RECORDER"})
        self.assertFalse(self._queued())
        # Same asset key as earlier months, so month-to-month tracking still matches.
        keys = self._q("SELECT DISTINCT asset_key FROM cctv_items WHERE device_name_raw = ?", BAD)
        self.assertEqual(len(keys), 1)

    def test_fix_from_the_asset_side_too(self):
        row = self._q("SELECT a.* FROM asset_items a JOIN import_batches b ON b.id = a.batch_id "
                      "WHERE b.period = '2026-07' AND a.device_name_raw = ?", BAD)[0]
        form = {f: (row[f] or "") for f in ASSET_FIELDS}
        form["device_name"] = "DVR/CCTV RECORDER"
        self.client.post(f"/assets/{row['id']}/edit", data=form)
        self.assertEqual(self._device_names("asset_items"), {"DVR/CCTV RECORDER"})
        self.assertEqual(self._device_names("cctv_items"), {"DVR/CCTV RECORDER"})
        self.assertFalse(self._queued())

    def test_changing_a_mapped_value_stays_one_month(self):
        pc = self._q("SELECT a.* FROM asset_items a JOIN import_batches b ON b.id = a.batch_id "
                     "WHERE b.period = '2026-07' AND a.serial_tag = 'SN-PC-1'")[0]
        form = {f: (pc[f] or "") for f in ASSET_FIELDS}
        form["status"] = "BROKEN"
        self.client.post(f"/assets/{pc['id']}/edit", data=form)
        statuses = dict(self._q("SELECT b.period, a.status FROM asset_items a JOIN import_batches b "
                                "ON b.id = a.batch_id WHERE a.serial_tag = 'SN-PC-1'"))
        self.assertEqual(statuses, {"2026-06": "USING LOCAL", "2026-07": "BROKEN"})
        self.assertEqual(self._q("SELECT * FROM hand_fixes"), [])


if __name__ == "__main__":
    unittest.main()
