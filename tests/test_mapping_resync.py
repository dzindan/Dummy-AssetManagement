"""Mapping changes in Settings reach every month already imported, not just
the next import (found on real data 2026-10-06: CARD READER renamed to ID
CARD READER only changed the alias table, so Jan-Aug kept CARD READER):
re-mapping an alias, renaming and merging a standard name. Also: default
standard names seed a new database only, so a renamed/merged default
doesn't come back on the next startup. Fixture pattern from
tests/test_hand_fix.py."""
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection, init_db  # noqa: E402
from app.importer import import_asset_report  # noqa: E402

PERIODS = ("2026-06", "2026-07", "2026-08")


def _workbook(path: str) -> None:
    wb = openpyxl.Workbook()
    oa = wb.active
    oa.title = "OA EQUIPMENT"
    oa.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
    oa.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
    oa.append([1, "TEST BRANCH", "CHIP READER ", "1001", "A", "", "SAMSUNG LS22A330NHEXXV", "SN-1", "USING LOCAL", ""])
    oa.append([2, "TEST BRANCH", "CHIP READER", "1002", "B", "", "SAMSUNG LS22A330NHEXXV", "SN-2", "IN USE", ""])
    wb.save(path)


class MappingResyncTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_mapsync_")
        self.app = create_app()
        self.client = self.app.test_client()
        tmp = tempfile.mkdtemp(prefix="am_mapsync_files_")
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))")
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("mapper", "mappass123", admin)
        self.client.post("/login", data={"username": "mapper", "password": "mappass123"})
        for period in PERIODS:
            path = os.path.join(tmp, f"{period}.xlsx")
            _workbook(path)
            import_asset_report(path, source_label=f"{period}.xlsx", period=period)

    def _values(self, col):
        conn = get_connection()
        try:
            return {r[0] for r in conn.execute(f"SELECT {col} FROM asset_items")}
        finally:
            conn.close()

    def _post(self, url, **form):
        resp = self.client.post(url, data=form)
        self.assertIn(resp.status_code, (200, 302))

    def test_remap_alias_updates_every_month(self):
        self.assertEqual(self._values("device_name"), {"CHIP READER"})
        self._post("/settings/device-alias/map", alias="CHIP READER", canonical_name="CARD READER")
        self.assertEqual(self._values("device_name"), {"CARD READER"})
        # Re-map: the rows the first mapping produced must follow (the bug).
        self._post("/settings/device-alias/map", alias="CHIP READER", canonical_name="ID CARD READER")
        self.assertEqual(self._values("device_name"), {"ID CARD READER"})

    def test_remap_leaves_hand_edited_rows(self):
        self._post("/settings/device-alias/map", alias="CHIP READER", canonical_name="CARD READER")
        conn = get_connection()
        try:
            conn.execute("UPDATE asset_items SET device_name = 'PINPAD' WHERE serial_tag = 'SN-2'")
            conn.commit()
        finally:
            conn.close()
        self._post("/settings/device-alias/map", alias="CHIP READER", canonical_name="ID CARD READER")
        self.assertEqual(self._values("device_name"), {"ID CARD READER", "PINPAD"})

    def test_rename_standard_updates_every_month(self):
        self._post("/settings/device-alias/map", alias="CHIP READER", canonical_name="CARD READER")
        self._post("/settings/device-standard/rename", old_name="CARD READER", new_name="SMART READER")
        self.assertEqual(self._values("device_name"), {"SMART READER"})

    def test_merge_standard_updates_every_month(self):
        self._post("/settings/model-alias/map", alias="SAMSUNG LS22A330NHEXXV", canonical_name="SAMSUNG S22A330")
        self.assertEqual(self._values("model_device"), {"SAMSUNG S22A330"})
        # SAMSUNG S22A330NHE is a default standard name, so this is a merge.
        self._post("/settings/model-standard/rename", old_name="SAMSUNG S22A330", new_name="SAMSUNG S22A330NHE")
        self.assertEqual(self._values("model_device"), {"SAMSUNG S22A330NHE"})

    def test_status_remap(self):
        self._post("/settings/status-alias/map", alias="IN USE", canonical_name="USING INTERNET")
        self._post("/settings/status-alias/map", alias="IN USE", canonical_name="USING LOCAL")
        self.assertEqual(self._values("status"), {"USING LOCAL"})

    def test_renamed_default_does_not_come_back_on_startup(self):
        self._post("/settings/device-standard/rename", old_name="CARD READER", new_name="ID CARD READER")
        init_db()  # what every app start runs
        conn = get_connection()
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM device_standard_names")}
        finally:
            conn.close()
        self.assertIn("ID CARD READER", names)
        self.assertNotIn("CARD READER", names)


if __name__ == "__main__":
    unittest.main()
