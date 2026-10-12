# -*- coding: utf-8 -*-
"""Deleting a device (user's rule 2026-10-09): from Manage Assets or Manage
CCTV it goes on both sides (CCTV <-> asset mirror) and in every month;
from Duplicate Check / the Cleaning Report (no all_months) only the row and
its linked row. Same isolated-DB pattern as tests/test_custom_branch.py."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402


class DeleteSyncTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_delsync_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('8064', 'TDH', 'TRAN DUY HUNG BRANCH', datetime('now'))")
            self.ids = {}
            for period in ("2026-07", "2026-08"):
                ab = conn.execute("INSERT INTO import_batches (imported_at, kind, label, period) "
                                  "VALUES (datetime('now'), 'asset_report', 'TDH', ?)", (period,)).lastrowid
                cb = conn.execute("INSERT INTO import_batches (imported_at, kind, label, period) "
                                  "VALUES (datetime('now'), 'cctv_report', 'TDH', ?)", (period,)).lastrowid
                for key, dev in (("SN:DVR1", "DVR/CCTV RECORDER"), ("SN:DVR2", "DVR/CCTV RECORDER")):
                    a = conn.execute("INSERT INTO asset_items (batch_id, asset_key, branch_dept, branch_no, device_name, "
                                     "serial_tag) VALUES (?, ?, 'TDH', '8064', ?, ?)", (ab, key, dev, key[3:])).lastrowid
                    c = conn.execute("INSERT INTO cctv_items (batch_id, asset_key, branch_dept, branch_no, device_name, "
                                     "serial_tag, asset_item_id) VALUES (?, ?, 'TDH', '8064', ?, ?, ?)",
                                     (cb, key, dev, key[3:], a)).lastrowid
                    self.ids[(period, key)] = (a, c)
                # Two LCDs without serial for the same user: same key, one month.
                for n in (1, 2):
                    self.ids[(period, f"LCD{n}")] = (conn.execute(
                        "INSERT INTO asset_items (batch_id, asset_key, branch_dept, branch_no, device_name) "
                        "VALUES (?, 'FB:TDH|LCD||1', 'TDH', '8064', 'LCD')", (ab,)).lastrowid, None)
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("deleter", "deletepass123", admin)
        self.client.post("/login", data={"username": "deleter", "password": "deletepass123"})

    def _left(self):
        conn = get_connection()
        try:
            return ({r[0] for r in conn.execute("SELECT id FROM asset_items")},
                    {r[0] for r in conn.execute("SELECT id FROM cctv_items")})
        finally:
            conn.close()

    def test_delete_in_manage_cctv_goes_everywhere(self):
        aug_cctv = self.ids[("2026-08", "SN:DVR1")][1]
        resp = self.client.post("/cctv/bulk-delete", data={"asset_ids": [aug_cctv], "all_months": "1"},
                                follow_redirects=True)
        self.assertIn(b"2 row(s) in Manage Assets and 2 in Manage CCTV, all months included", resp.data)
        assets, cctv = self._left()
        for period in ("2026-07", "2026-08"):
            a, c = self.ids[(period, "SN:DVR1")]
            self.assertNotIn(a, assets)
            self.assertNotIn(c, cctv)
            a2, c2 = self.ids[(period, "SN:DVR2")]           # another DVR: untouched
            self.assertIn(a2, assets)
            self.assertIn(c2, cctv)
        conn = get_connection()
        try:
            logged = [r[0] for r in conn.execute("SELECT target FROM logsdb.activity_log WHERE action LIKE 'Deleted%'")]
        finally:
            conn.close()
        self.assertEqual(len(logged), 4)
        self.assertTrue(any("2026-07" in t and "other month" in t for t in logged))

    def test_delete_in_manage_assets_goes_to_cctv_too(self):
        jul_asset = self.ids[("2026-07", "SN:DVR2")][0]
        self.client.post("/assets/bulk-delete", data={"asset_ids": [jul_asset], "all_months": "1"})
        assets, cctv = self._left()
        for period in ("2026-07", "2026-08"):
            a, c = self.ids[(period, "SN:DVR2")]
            self.assertNotIn(a, assets)
            self.assertNotIn(c, cctv)
            self.assertIn(self.ids[(period, "SN:DVR1")][1], cctv)

    def test_same_key_twin_in_the_same_month_is_kept(self):
        lcd1_aug = self.ids[("2026-08", "LCD1")][0]
        self.client.post("/assets/bulk-delete", data={"asset_ids": [lcd1_aug], "all_months": "1"})
        assets, _ = self._left()
        self.assertNotIn(lcd1_aug, assets)
        self.assertIn(self.ids[("2026-08", "LCD2")][0], assets)   # its twin in August stays
        self.assertNotIn(self.ids[("2026-07", "LCD1")][0], assets)  # July rows of that key go
        self.assertNotIn(self.ids[("2026-07", "LCD2")][0], assets)

    def test_without_all_months_only_the_row_and_its_link(self):
        a, c = self.ids[("2026-08", "SN:DVR1")]
        resp = self.client.post("/assets/bulk-delete", data={"asset_ids": [a]}, follow_redirects=True)
        self.assertIn(b"Deleted 1 asset(s) and 1 matching row(s) in Manage CCTV.", resp.data)
        assets, cctv = self._left()
        self.assertNotIn(a, assets)
        self.assertNotIn(c, cctv)
        ja, jc = self.ids[("2026-07", "SN:DVR1")]
        self.assertIn(ja, assets)
        self.assertIn(jc, cctv)

    def test_single_delete_route(self):
        a, c = self.ids[("2026-08", "SN:DVR2")]
        self.client.post(f"/cctv/{c}/delete", data={"all_months": "1"})
        assets, cctv = self._left()
        self.assertNotIn(self.ids[("2026-07", "SN:DVR2")][0], assets)
        self.assertNotIn(a, assets)


if __name__ == "__main__":
    unittest.main()
