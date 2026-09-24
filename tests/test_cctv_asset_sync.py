# -*- coding: utf-8 -*-
"""CCTV <-> asset_items link (cctv_items.asset_item_id): set at import,
backfilled for older data, and used to copy an edit made in Manage CCTV
to the mirrored Manage Assets row and back. Same isolated-DB pattern as
tests/test_cctv_import.py.
"""
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import _backfill_cctv_asset_links, get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402
from app.routes.asset_edit import EDITABLE_FIELDS as ASSET_EDITABLE_FIELDS  # noqa: E402
from app.routes.cctv_edit import EDITABLE_FIELDS as CCTV_EDITABLE_FIELDS  # noqa: E402


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_cctvsynctest_")
    return create_app()


def _build_workbook(path: str) -> None:
    """OA sheet with one PC and one DVR (SN-SHARED), CCTV sheet with that
    same DVR, one DVR only on the CCTV sheet, and one monitor with no
    serial."""
    wb = openpyxl.Workbook()
    oa = wb.active
    oa.title = "OA EQUIPMENT"
    oa.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
    oa.append(
        ["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
         "SERIAL/ SERVICE TAG", "STATUS", "REMARK"]
    )
    oa.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", ""])
    oa.append([2, "TEST BRANCH", "DVR", "", "", "", "DS-7316", "SN-SHARED", "USING LOCAL", ""])

    cctv = wb.create_sheet("CCTV REPORT")
    cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
    cctv.append(
        ["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
         "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
         "LOCATION", "REMARK"]
    )
    cctv.append([1, "TEST BRANCH", "DVR", "10.0.1.1", "HIK VISION", "DS-7316", "SN-SHARED",
                 "USING LOCAL", 16, "3", "24TB", "IT ROOM", ""])
    cctv.append([2, "TEST BRANCH", "DVR", "10.0.1.2", "HIK VISION", "DS-7208", "SN-CCTV-ONLY",
                 "USING LOCAL", 8, "1", "4TB", "IT ROOM", ""])
    cctv.append([3, "TEST BRANCH", "MONITOR", "", "SAMSUNG", "LS20", "",
                 "USING LOCAL", None, None, None, "IT ROOM", ""])
    wb.save(path)


class CctvAssetSyncTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        self.client = self.app.test_client()
        tmpdir = tempfile.mkdtemp(prefix="am_cctvsynctest_import_")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
            )
            admin_role_id = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("synctester", "syncpass1", admin_role_id)
        self.client.post("/login", data={"username": "synctester", "password": "syncpass1"})

        path = os.path.join(tmpdir, "report.xlsx")
        _build_workbook(path)
        reports = import_asset_report(path, source_label="report.xlsx", period="2026-03")
        by_kind = {r.kind: r for r in reports}
        self.asset_batch_id = by_kind["asset_report"].batch_id
        self.cctv_batch_id = by_kind["cctv_report"].batch_id

    def _cctv(self, serial=None, device=None):
        conn = get_connection()
        try:
            if serial is not None:
                return conn.execute("SELECT * FROM cctv_items WHERE serial_tag = ?", (serial,)).fetchone()
            return conn.execute("SELECT * FROM cctv_items WHERE device_name = ?", (device,)).fetchone()
        finally:
            conn.close()

    def _asset(self, asset_id):
        conn = get_connection()
        try:
            return conn.execute("SELECT * FROM asset_items WHERE id = ?", (asset_id,)).fetchone()
        finally:
            conn.close()

    def _links(self):
        """{cctv serial or device name: linked asset row's serial/device}."""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT c.serial_tag AS c_serial, c.device_name AS c_device, a.id AS a_id, a.serial_tag AS a_serial, "
                "a.device_name AS a_device, a.batch_id AS a_batch "
                "FROM cctv_items c LEFT JOIN asset_items a ON a.id = c.asset_item_id"
            ).fetchall()
        finally:
            conn.close()
        return {(r["c_serial"] or r["c_device"]): r for r in rows}

    def _assert_all_linked(self):
        links = self._links()
        self.assertEqual(len(links), 3)
        for key, r in links.items():
            self.assertIsNotNone(r["a_id"], key)
            self.assertEqual(r["a_batch"], self.asset_batch_id)
        self.assertEqual(links["SN-SHARED"]["a_serial"], "SN-SHARED")
        self.assertEqual(links["SN-CCTV-ONLY"]["a_serial"], "SN-CCTV-ONLY")
        # No serial - "MONITOR" is normalized to LCD (default alias) on both sides.
        self.assertEqual(links["LCD"]["a_device"], "LCD")
        self.assertIsNone(links["LCD"]["a_serial"] or None)

    def test_import_links_every_cctv_row(self):
        self._assert_all_linked()
        # The shared DVR links to the OA sheet's own row, not a second copy.
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM asset_items WHERE serial_tag = 'SN-SHARED'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_backfill_restores_links_for_older_data(self):
        conn = get_connection()
        try:
            conn.execute("UPDATE cctv_items SET asset_item_id = NULL")
            _backfill_cctv_asset_links(conn)
            conn.commit()
        finally:
            conn.close()
        self._assert_all_linked()

    def test_cctv_edit_is_copied_to_linked_asset(self):
        cctv = self._cctv(serial="SN-CCTV-ONLY")
        form = {f: cctv[f] or "" for f in CCTV_EDITABLE_FIELDS}
        form.update({"serial_tag": "SN-FIXED", "remark": "moved to vault", "camera_count": "12"})
        self.client.post(f"/cctv/{cctv['id']}/edit", data=form)

        asset = self._asset(cctv["asset_item_id"])
        self.assertEqual(asset["serial_tag"], "SN-FIXED")
        self.assertEqual(asset["remark"], "MOVED TO VAULT")

        conn = get_connection()
        try:
            logged = conn.execute(
                "SELECT field FROM logsdb.activity_log WHERE category = 'asset' AND target = ?",
                (f"Asset #{asset['id']}",),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(sorted(r["field"] for r in logged), ["remark", "serial_tag"])

    def test_asset_edit_is_copied_to_linked_cctv_row(self):
        cctv = self._cctv(serial="SN-SHARED")
        asset = self._asset(cctv["asset_item_id"])
        form = {f: asset[f] or "" for f in ASSET_EDITABLE_FIELDS}
        form.update({"model_device": "DS-7316-NEW", "full_name": "SOMEONE"})
        self.client.post(f"/assets/{asset['id']}/edit", data=form)

        updated = self._cctv(serial="SN-SHARED")
        self.assertEqual(updated["model_device"], "DS-7316-NEW")
        # Camera/HDD fields are CCTV-only and untouched.
        self.assertEqual(updated["camera_count"], cctv["camera_count"])

    def _add_other_branch(self):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('002', 'Other', 'OTHER TRANSACTION OFFICE', datetime('now'))"
            )
            conn.commit()
        finally:
            conn.close()

    def test_editing_branch_dept_moves_row_and_linked_row_to_that_branch(self):
        self._add_other_branch()
        cctv = self._cctv(serial="SN-CCTV-ONLY")
        self.assertEqual(cctv["branch_no"], "001")
        form = {f: cctv[f] or "" for f in CCTV_EDITABLE_FIELDS}
        form["branch_dept"] = "Other Transaction Office"
        resp = self.client.post(f"/cctv/{cctv['id']}/edit", data=form, follow_redirects=True)
        self.assertIn(b"Moved to branch 002", resp.data)

        moved = self._cctv(serial="SN-CCTV-ONLY")
        self.assertEqual(moved["branch_no"], "002")
        self.assertEqual(self._asset(moved["asset_item_id"])["branch_no"], "002")  # synced
        conn = get_connection()
        try:
            fields = {r["field"] for r in conn.execute(
                "SELECT field FROM logsdb.activity_log WHERE field = 'branch_no'")}
        finally:
            conn.close()
        self.assertEqual(fields, {"branch_no"})

    def test_editing_asset_branch_dept_also_moves_linked_cctv_row(self):
        self._add_other_branch()
        cctv = self._cctv(serial="SN-SHARED")
        asset = self._asset(cctv["asset_item_id"])
        form = {f: asset[f] or "" for f in ASSET_EDITABLE_FIELDS}
        form["branch_dept"] = "OTHER"
        self.client.post(f"/assets/{asset['id']}/edit", data=form)
        self.assertEqual(self._asset(asset["id"])["branch_no"], "002")
        self.assertEqual(self._cctv(serial="SN-SHARED")["branch_no"], "002")

    def test_unknown_branch_dept_keeps_branch_and_warns(self):
        cctv = self._cctv(serial="SN-CCTV-ONLY")
        form = {f: cctv[f] or "" for f in CCTV_EDITABLE_FIELDS}
        form["branch_dept"] = "NO SUCH PLACE XYZ"
        resp = self.client.post(f"/cctv/{cctv['id']}/edit", data=form, follow_redirects=True)
        self.assertIn(b"doesn&#39;t match any branch", resp.data)
        after = self._cctv(serial="SN-CCTV-ONLY")
        self.assertEqual(after["branch_no"], "001")
        self.assertEqual(after["branch_dept"], "NO SUCH PLACE XYZ")  # the text edit itself is kept

    def test_edit_on_unlinked_row_still_works(self):
        cctv = self._cctv(serial="SN-CCTV-ONLY")
        conn = get_connection()
        try:
            conn.execute("UPDATE cctv_items SET asset_item_id = NULL WHERE id = ?", (cctv["id"],))
            conn.commit()
        finally:
            conn.close()
        form = {f: cctv[f] or "" for f in CCTV_EDITABLE_FIELDS}
        form["remark"] = "solo"
        self.client.post(f"/cctv/{cctv['id']}/edit", data=form)
        self.assertEqual(self._cctv(serial="SN-CCTV-ONLY")["remark"], "SOLO")


if __name__ == "__main__":
    unittest.main()
