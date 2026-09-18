# -*- coding: utf-8 -*-
"""Multi-sheet asset-report import + CCTV ingest verification.

Follows the same isolated-DB pattern as tests/test_auth.py: override
%LOCALAPPDATA% to a fresh temp directory before create_app(), so this test's
database is a disposable throwaway, never the real one.

Regression target: a monthly branch report workbook that packs a regular
OA/PC equipment sheet AND a separate CCTV equipment sheet used to only ever
import the single best-scoring sheet (see importer.detect_equipment_sheet's
old behavior) - the other sheet was silently dropped, no error shown. This
verifies both sheets now import, into their own tables/batches.
"""
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import detect_equipment_sheets, import_asset_report, reresolve_unresolved_assets  # noqa: E402


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_cctvtest_")
    return create_app()


def _build_two_sheet_workbook(path: str) -> None:
    wb = openpyxl.Workbook()
    oa = wb.active
    oa.title = "OA EQUIPMENT"
    oa.append([None, None, None, None, None, None, None, "Branch/TO/Center Name:", "Test Branch"])
    oa.append(
        ["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
         "SERIAL/ SERVICE TAG", "STATUS", "REMARK"]
    )
    oa.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", ""])
    oa.append([2, "TEST BRANCH", "PC", "1002", "NGUYEN VAN B", "10.0.0.2", "DELL 3060", "SN-PC-2", "USING LOCAL", ""])

    cctv = wb.create_sheet("CCTV REPORT")
    cctv.append([None, None, None, None, None, None, None, None, None, None, "Branch/TO/Center Name:", "Test Branch"])
    cctv.append(
        ["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
         "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
         "LOCATION", "REMARK"]
    )
    cctv.append(
        [1, "TEST BRANCH", "CCTV RECORDING 1", "10.0.1.1", "HIK VISION", "DS-7316", "SN-CCTV-1",
         "USING LOCAL", 16, "3", "24TB", "IT ROOM", ""]
    )
    cctv.append(
        [2, "TEST BRANCH", "MONITOR", "DYNAMIC IP", "SAMSUNG", "LS20", "SN-CCTV-2",
         "USING LOCAL", None, None, None, "IT ROOM", ""]
    )
    wb.save(path)


class DetectEquipmentSheetsTests(unittest.TestCase):
    def test_classifies_oa_and_cctv_sheets_separately(self):
        tmpdir = tempfile.mkdtemp(prefix="am_cctvtest_wb_")
        path = os.path.join(tmpdir, "report.xlsx")
        _build_two_sheet_workbook(path)

        wb = openpyxl.load_workbook(path, data_only=True)
        matches = detect_equipment_sheets(wb)
        wb.close()

        self.assertEqual(len(matches), 2)
        kinds = {m.sheet_name: m.kind for m in matches}
        self.assertEqual(kinds["OA EQUIPMENT"], "asset")
        self.assertEqual(kinds["CCTV REPORT"], "cctv")


class ImportAssetReportMultiSheetTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        self.tmpdir = tempfile.mkdtemp(prefix="am_cctvtest_import_")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
            )
            conn.commit()
        finally:
            conn.close()

    def test_two_sheet_file_produces_two_batches_in_their_own_tables(self):
        path = os.path.join(self.tmpdir, "report.xlsx")
        _build_two_sheet_workbook(path)

        reports = import_asset_report(path, source_label="report.xlsx", period="2026-03")
        self.assertEqual(len(reports), 2)

        by_kind = {r.kind: r for r in reports}
        self.assertIn("asset_report", by_kind)
        self.assertIn("cctv_report", by_kind)

        asset_report = by_kind["asset_report"]
        self.assertEqual(asset_report.error, "")
        self.assertEqual(asset_report.rows_imported, 2)
        self.assertEqual(asset_report.branch_no, "001")

        cctv_report = by_kind["cctv_report"]
        self.assertEqual(cctv_report.error, "")
        self.assertEqual(cctv_report.rows_imported, 2)
        self.assertEqual(cctv_report.branch_no, "001")

        conn = get_connection()
        try:
            asset_rows = conn.execute(
                "SELECT * FROM asset_items WHERE batch_id = ? ORDER BY serial_tag", (asset_report.batch_id,)
            ).fetchall()
            cctv_rows = conn.execute(
                "SELECT * FROM cctv_items WHERE batch_id = ? ORDER BY serial_tag", (cctv_report.batch_id,)
            ).fetchall()

            # asset_items must never contain the CCTV batch's rows, and vice
            # versa - the whole point of the separate table.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM asset_items").fetchone()["c"], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM cctv_items").fetchone()["c"], 2
            )
        finally:
            conn.close()

        self.assertEqual([r["serial_tag"] for r in asset_rows], ["SN-PC-1", "SN-PC-2"])
        self.assertEqual(asset_rows[0]["device_name"], "PC")
        self.assertEqual(asset_rows[0]["user_id_raw"], "1001")

        self.assertEqual([r["serial_tag"] for r in cctv_rows], ["SN-CCTV-1", "SN-CCTV-2"])
        dvr_row = cctv_rows[0]
        self.assertEqual(dvr_row["manufacturer"], "HIK VISION")
        self.assertEqual(dvr_row["camera_count"], "16")
        self.assertEqual(dvr_row["hdd_count"], "3")
        self.assertEqual(dvr_row["hdd_capacity"], "24TB")
        self.assertEqual(dvr_row["location"], "IT ROOM")
        self.assertEqual(dvr_row["ip"], "10.0.1.1")

    def test_single_oa_sheet_file_still_imports_as_one_report(self):
        """Regression guard: a normal file with only one equipment sheet
        (the overwhelmingly common case) must still behave exactly like
        before - one report, one batch."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "REPORT"
        ws.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
        ws.append(
            ["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
             "SERIAL/ SERVICE TAG", "STATUS", "REMARK"]
        )
        ws.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", ""])
        path = os.path.join(self.tmpdir, "single.xlsx")
        wb.save(path)

        reports = import_asset_report(path, source_label="single.xlsx", period="2026-03")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].kind, "asset_report")
        self.assertEqual(reports[0].rows_imported, 1)


class ReresolveCctvUnresolvedTests(unittest.TestCase):
    """reresolve_unresolved_assets used to only touch asset_items - a CCTV
    sheet's own garbled/unresolvable branch hint stayed stuck on
    branch_no = '' forever after assigning it a branch alias in Settings,
    even though the exact same fix worked instantly for a plain asset
    report. Both tables must resync."""

    def setUp(self):
        self.app = _fresh_app()
        self.tmpdir = tempfile.mkdtemp(prefix="am_cctvtest_reresolve_")

    def test_assigning_a_branch_alias_backfills_cctv_items_too(self):
        wb = openpyxl.Workbook()
        cctv = wb.active
        cctv.title = "CCTV REPORT"
        # No "Branch/TO/Center Name:" label row and an empty BRANCH/DEPT
        # column - same shape as a real file whose branch-name cell got
        # left blank, which is what produces an unresolvable garbled hint.
        cctv.append(
            ["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
             "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
             "LOCATION", "REMARK"]
        )
        cctv.append(
            [1, "NOTABRANCH", "CCTV RECORDING 1", "10.0.1.1", "HIK VISION", "DS-7316", "SN-CCTV-1",
             "USING LOCAL", 16, "3", "24TB", "IT ROOM", ""]
        )
        path = os.path.join(self.tmpdir, "report.xlsx")
        wb.save(path)

        reports = import_asset_report(path, source_label="report.xlsx", period="2026-03")
        self.assertEqual(len(reports), 1)
        cctv_report = reports[0]
        self.assertEqual(cctv_report.kind, "cctv_report")
        self.assertEqual(cctv_report.branch_no, "")

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
            )
            row = conn.execute(
                "SELECT branch_no FROM cctv_items WHERE batch_id = ?", (cctv_report.batch_id,)
            ).fetchone()
            self.assertEqual(row["branch_no"], "")

            fixed_count = reresolve_unresolved_assets(conn, cctv_report.branch_hint, "001")
            conn.commit()

            row = conn.execute(
                "SELECT branch_no FROM cctv_items WHERE batch_id = ?", (cctv_report.batch_id,)
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(fixed_count, 1)
        self.assertEqual(row["branch_no"], "001")


if __name__ == "__main__":
    unittest.main()
