# -*- coding: utf-8 -*-
"""SQLite's built-in UPPER() is ASCII-only; db.get_connection overrides it
with Python's Unicode str.upper(). Without that, mapping a Vietnamese
device/model/status name in Settings left existing rows unchanged, and
searches missed lowercase Vietnamese text. Same isolated-DB pattern as
tests/test_settings_tabs.py."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.queries import search_assets  # noqa: E402


class UnicodeUpperTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_unicodeupper_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('8009', 'HCMC', 'HO CHI MINH BRANCH', datetime('now'))")
            batch = conn.execute("INSERT INTO import_batches (imported_at, kind, label, period) "
                                 "VALUES (datetime('now'), 'asset_report', 'HCMC', '2026-08')").lastrowid
            # As the importer stores them: *_raw as typed, the shown value uppercased in Python.
            conn.execute("""INSERT INTO asset_items (batch_id, asset_key, branch_dept, branch_no, device_name,
                            device_name_raw, model_device, model_device_raw, status, status_raw, remark)
                            VALUES (?, 'k1', 'HCMC', '8009', 'MÁY CUỐN THẾP', 'Máy cuốn thếp',
                                    'MÁY ĐẾM TIỀN', 'Máy đếm tiền', 'ĐANG DÙNG', 'Đang dùng', 'không sử dụng')""",
                         (batch,))
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("uni", "unipass123", admin)
        self.client.post("/login", data={"username": "uni", "password": "unipass123"})

    def _row(self):
        conn = get_connection()
        try:
            return conn.execute("SELECT device_name, model_device, status FROM asset_items").fetchone()
        finally:
            conn.close()

    def test_upper_handles_vietnamese(self):
        conn = get_connection()
        try:
            self.assertEqual(conn.execute("SELECT UPPER('Không sử dụng'), UPPER(NULL), UPPER(12)").fetchone()[:],
                             ("KHÔNG SỬ DỤNG", None, "12"))
        finally:
            conn.close()

    def test_mapping_and_unmapping_a_vietnamese_device_name_updates_existing_rows(self):
        self.client.post("/settings/device-alias/map",
                         data={"alias": "MÁY CUỐN THẾP", "canonical_name": "BANKNOTE BANDING MACHINE"})
        self.assertEqual(self._row()["device_name"], "BANKNOTE BANDING MACHINE")
        self.client.post("/settings/device-alias/unmap", data={"alias": "MÁY CUỐN THẾP"})
        self.assertEqual(self._row()["device_name"], "MÁY CUỐN THẾP")

    def test_mapping_vietnamese_model_and_status_updates_existing_rows(self):
        self.client.post("/settings/model-alias/map", data={"alias": "MÁY ĐẾM TIỀN", "canonical_name": "XINDA BC-35"})
        self.assertEqual(self._row()["model_device"], "XINDA BC-35")
        self.client.post("/settings/status-alias/map", data={"alias": "ĐANG DÙNG", "canonical_name": "USING LOCAL"})
        self.assertEqual(self._row()["status"], "USING LOCAL")

    def test_search_finds_lowercase_vietnamese_text(self):
        conn = get_connection()
        try:
            _rows, total = search_assets(conn, {"q": "KHÔNG SỬ DỤNG"})
        finally:
            conn.close()
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
