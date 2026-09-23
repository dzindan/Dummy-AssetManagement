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

            # cctv_items keeps the full detailed rows (camera_count/hdd_*/
            # location/manufacturer) - those never leak into asset_items.
            # But the CCTV sheet's own devices (different serials than the
            # OA sheet's PCs) DO get mirrored into asset_items too, as bare
            # equipment, so Manage Assets' inventory includes them.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM asset_items").fetchone()["c"], 4
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM cctv_items").fetchone()["c"], 2
            )
        finally:
            conn.close()

        self.assertEqual(
            [r["serial_tag"] for r in asset_rows], ["SN-CCTV-1", "SN-CCTV-2", "SN-PC-1", "SN-PC-2"]
        )
        pc_rows = {r["serial_tag"]: r for r in asset_rows if r["serial_tag"].startswith("SN-PC")}
        self.assertEqual(pc_rows["SN-PC-1"]["device_name"], "PC")
        self.assertEqual(pc_rows["SN-PC-1"]["user_id_raw"], "1001")

        mirrored_rows = {r["serial_tag"]: r for r in asset_rows if r["serial_tag"].startswith("SN-CCTV")}
        self.assertEqual(mirrored_rows["SN-CCTV-1"]["user_id_raw"], "")
        self.assertEqual(mirrored_rows["SN-CCTV-1"]["full_name"], "")
        self.assertEqual(mirrored_rows["SN-CCTV-1"]["branch_no"], "001")

        self.assertEqual([r["serial_tag"] for r in cctv_rows], ["SN-CCTV-1", "SN-CCTV-2"])
        dvr_row = cctv_rows[0]
        self.assertEqual(dvr_row["manufacturer"], "HIK VISION")
        self.assertEqual(dvr_row["camera_count"], "16")
        self.assertEqual(dvr_row["hdd_count"], "3")
        self.assertEqual(dvr_row["hdd_capacity"], "24TB")
        self.assertEqual(dvr_row["location"], "IT ROOM")
        self.assertEqual(dvr_row["ip"], "10.0.1.1")

    def test_cctv_row_sharing_a_serial_with_an_oa_sheet_row_is_not_duplicated(self):
        """The same physical DVR can legitimately appear as its own row on
        both the OA sheet (someone typed it into the generic equipment
        list) and the CCTV sheet (with its full camera/HDD detail) - the
        mirror must not create a second asset_items row for it."""
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
        cctv.append(
            [1, "TEST BRANCH", "CCTV RECORDING 1", "10.0.1.1", "HIK VISION", "DS-7316", "SN-SHARED",
             "USING LOCAL", 16, "3", "24TB", "IT ROOM", ""]
        )
        path = os.path.join(self.tmpdir, "shared_serial.xlsx")
        wb.save(path)

        reports = import_asset_report(path, source_label="shared_serial.xlsx", period="2026-03")
        by_kind = {r.kind: r for r in reports}
        asset_batch_id = by_kind["asset_report"].batch_id

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT serial_tag FROM asset_items WHERE batch_id = ?", (asset_batch_id,)
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(sorted(r["serial_tag"] for r in rows), ["SN-PC-1", "SN-SHARED"])

    def test_cctv_sheets_footer_legend_table_is_not_imported_as_equipment(self):
        """Real CCTV sheets (see every file sampled from the June-Aug 2026
        reports) follow their real DVR rows with a blank row, then a second
        "NUMBER OF CAMERA CONNECTED / Qty" summary mini-table repeating each
        DVR's name/manufacturer/model down to a "Grand Total" row - not more
        equipment. That footer used to get ingested as a string of garbage
        cctv_items/asset_items rows (bare numbers, "NUMBER OF CAMERA
        CONNECTED" as a device name, a device name landing in the
        BRANCH/DEPT column and failing branch resolution, ...)."""
        wb = openpyxl.Workbook()
        cctv = wb.active
        cctv.title = "CCTV REPORT"
        cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
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
            [2, "TEST BRANCH", "CCTV RECORDING 2", "10.0.1.2", "HIK VISION", "DS-7324", "SN-CCTV-2",
             "USING LOCAL", 8, "2", "12TB", "IT ROOM", ""]
        )
        cctv.append([None] * 13)  # the real-world blank divider row
        # The footer legend, reproduced from a real file: a header row, then
        # per-DVR name/manufacturer/model rows with no relation to the real
        # DEVICE NAME/BRANCH/SERIAL columns above, ending in a stray total.
        cctv.append([None, None, "NUMBER  OF CAMERA CONNECTED", "Qty", None, None, None, None, None, None, None, None, None])
        cctv.append([None, "CCTV RECORDING 1", 16, 1, None, None, None, None, None, None, None, None, None])
        cctv.append([None, "HIK VISION", None, None, None, None, None, None, None, None, None, None, None])
        cctv.append([None, "DS-7316", None, None, None, None, None, None, None, None, None, None, None])
        cctv.append([None, "CCTV RECORDING 2", 8, 1, None, None, None, None, None, None, None, None, None])
        cctv.append([None, "Grand Total", 24, 2, None, None, None, None, None, None, None, None, None])
        path = os.path.join(self.tmpdir, "footer_legend.xlsx")
        wb.save(path)

        reports = import_asset_report(path, source_label="footer_legend.xlsx", period="2026-03")
        self.assertEqual(len(reports), 2)
        by_kind = {r.kind: r for r in reports}
        cctv_report = by_kind["cctv_report"]
        asset_report = by_kind["asset_report"]

        self.assertEqual(cctv_report.rows_imported, 2)
        # Only the two real device names - none of the footer's garbage
        # ("NUMBER  OF CAMERA CONNECTED", "Grand Total", a bare "16", ...).
        self.assertEqual(cctv_report.unrecognized_devices, ["CCTV RECORDING 1", "CCTV RECORDING 2"])
        self.assertEqual(cctv_report.branch_no, "001")

        conn = get_connection()
        try:
            cctv_rows = conn.execute(
                "SELECT serial_tag, device_name FROM cctv_items WHERE batch_id = ? ORDER BY serial_tag",
                (cctv_report.batch_id,),
            ).fetchall()
            asset_rows = conn.execute(
                "SELECT serial_tag FROM asset_items WHERE batch_id = ?", (asset_report.batch_id,)
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual([r["serial_tag"] for r in cctv_rows], ["SN-CCTV-1", "SN-CCTV-2"])
        self.assertEqual([r["serial_tag"] for r in asset_rows], ["SN-CCTV-1", "SN-CCTV-2"])

    def test_footer_legend_with_no_blank_row_gap_is_still_excluded(self):
        """Same footer-legend problem as the test above, but reproducing a
        real file (DISTRICT 7 T.O's Jan-2026 report) where the legend table
        starts on the very next row after the last real one - no blank
        divider at all. The row-number ("NO") column going blank, not a
        blank row, is what actually has to stop the scan."""
        wb = openpyxl.Workbook()
        cctv = wb.active
        cctv.title = "CCTV REPORT"
        cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
        cctv.append(
            ["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
             "STATUS", "NUMBER  OF CAMERA CONNECTED"]
        )
        cctv.append(
            [1, "TEST BRANCH", "CCTV RECORDING 1", "10.0.1.1", "HIK VISION", "DS-7324HQHI-K4", "SN-CCTV-1",
             "USING LOCAL", 19]
        )
        cctv.append(
            [2, "TEST BRANCH", "CCTV RECORDING 2", "10.0.1.2", "HIK VISION", "DS-7104HQHI-K1", "SN-CCTV-2",
             "USING LOCAL", 1]
        )
        # No blank row here - the legend starts immediately.
        cctv.append([None, None, " NUMBER  OF CAMERA CONNECTED", "Qty", None, None, None, None, None])
        cctv.append([None, "CCTV RECORDING 1", 19, 1, None, None, None, None, None])
        cctv.append([None, "HIK VISION", None, None, None, None, None, None, None])
        cctv.append([None, "DS-7324HQHI-K4", None, None, None, None, None, None, None])
        cctv.append([None, "CCTV RECORDING 2", 1, 1, None, None, None, None, None])
        cctv.append([None, "Grand Total", 20, 2, None, None, None, None, None])
        path = os.path.join(self.tmpdir, "no_gap_legend.xlsx")
        wb.save(path)

        reports = import_asset_report(path, source_label="no_gap_legend.xlsx", period="2026-03")
        by_kind = {r.kind: r for r in reports}
        cctv_report = by_kind["cctv_report"]
        self.assertEqual(cctv_report.rows_imported, 2)

        conn = get_connection()
        try:
            cctv_rows = conn.execute(
                "SELECT serial_tag FROM cctv_items WHERE batch_id = ?", (cctv_report.batch_id,)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(sorted(r["serial_tag"] for r in cctv_rows), ["SN-CCTV-1", "SN-CCTV-2"])

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

    def test_repeated_header_rows_are_not_imported_as_devices(self):
        """Both shapes seen in real files: an exact copy of the header row
        below it (DIST 10 T.O's OA sheet), and a template sample row with
        its own spelling (CMC's CCTV sheet: Device Name / MODEL DIVECE /
        Serial Number / Branch Name)."""
        wb = openpyxl.Workbook()
        oa = wb.active
        oa.title = "OA EQUIPMENT"
        oa.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
        header = ["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                  "SERIAL/ SERVICE TAG", "STATUS", "REMARK"]
        oa.append(header)
        oa.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        oa.append([1, "TEST BRANCH", "PC", "1001", "NGUYEN VAN A", "10.0.0.1", "DELL 3060", "SN-PC-1", "USING LOCAL", ""])

        cctv = wb.create_sheet("CCTV REPORT")
        cctv.append([None] * 10 + ["Branch/TO/Center Name:", "Test Branch"])
        cctv.append(
            ["NO", "BRANCH / DEPT", "DEVICE NAME", "IP ADDRESS", "PRODUCTION", "MODEL DEVICE", "SERIAL NO",
             "STATUS", "NUMBER  OF CAMERA CONNECTED", "NUMBER OF HARD DISK", "CAPACITY OF ALL HARD DISK",
             "LOCATION", "REMARK"]
        )
        cctv.append([1, "Branch Name", "Device Name", "", "", "MODEL DIVECE", "Serial Number",
                     "USING LOCAL", None, None, None, "IT ROOM", ""])
        cctv.append([2, "TEST BRANCH", "DVR", "10.0.1.1", "HIK VISION", "DS-7316", "SN-CCTV-1",
                     "USING LOCAL", 16, "3", "24TB", "IT ROOM", ""])
        path = os.path.join(self.tmpdir, "header_repeat.xlsx")
        wb.save(path)

        reports = {r.kind: r for r in import_asset_report(path, source_label="header_repeat.xlsx", period="2026-03")}
        self.assertEqual(reports["asset_report"].rows_skipped_header_repeat, 1)
        self.assertEqual(reports["cctv_report"].rows_skipped_header_repeat, 1)
        self.assertEqual(reports["cctv_report"].rows_imported, 1)

        conn = get_connection()
        try:
            asset_devices = sorted(r["device_name"] for r in conn.execute("SELECT device_name FROM asset_items"))
            cctv_devices = [r["device_name"] for r in conn.execute("SELECT device_name FROM cctv_items")]
        finally:
            conn.close()
        self.assertEqual(asset_devices, ["DVR", "PC"])  # the PC + the mirrored DVR, no header rows
        self.assertEqual(cctv_devices, ["DVR"])


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

        # A CCTV-only file (no OA sheet) now also gets a synthetic
        # asset_report batch created to hold the mirrored DVR row.
        reports = import_asset_report(path, source_label="report.xlsx", period="2026-03")
        self.assertEqual(len(reports), 2)
        by_kind = {r.kind: r for r in reports}
        cctv_report = by_kind["cctv_report"]
        asset_report = by_kind["asset_report"]
        self.assertEqual(cctv_report.branch_no, "")

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
            )
            cctv_row = conn.execute(
                "SELECT branch_no FROM cctv_items WHERE batch_id = ?", (cctv_report.batch_id,)
            ).fetchone()
            self.assertEqual(cctv_row["branch_no"], "")

            fixed_count = reresolve_unresolved_assets(conn, cctv_report.branch_hint, "001")
            conn.commit()

            cctv_row = conn.execute(
                "SELECT branch_no FROM cctv_items WHERE batch_id = ?", (cctv_report.batch_id,)
            ).fetchone()
            asset_row = conn.execute(
                "SELECT branch_no FROM asset_items WHERE batch_id = ?", (asset_report.batch_id,)
            ).fetchone()
        finally:
            conn.close()

        # Both the cctv_items row and its mirrored asset_items row shared
        # the same unresolved hint, so one alias fixes both.
        self.assertEqual(fixed_count, 2)
        self.assertEqual(cctv_row["branch_no"], "001")
        self.assertEqual(asset_row["branch_no"], "001")


if __name__ == "__main__":
    unittest.main()
