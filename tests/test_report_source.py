# -*- coding: utf-8 -*-
"""Branch reports vs the HO consolidated Monthly Report: separate import
paths (each refuses the other's file), import_batches.report_source, and
"current state" / month-change tables keeping the two sources apart (a few
HO-report rows for a branch in a later month must not hide the branch's
own report). Same isolated-DB pattern as tests/test_period_skip.py."""
import io
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.analytics import get_branch_month_change_table  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import _add_missing_columns, get_connection  # noqa: E402
from app.importer import import_asset_report, import_ho_report, ho_report_period  # noqa: E402
from app.queries import get_current_asset_count, get_current_assets  # noqa: E402

UNITS = (
    ("8009", "HO CHI MINH BRANCH"),
    ("7904", "HO CHI MINH CORPORATE MARKETING CENTER"),
    ("8129", "ICT PLANNING DEPARTMENT"),
)
USERS = (("111", "8009"), ("222", "7904"), ("333", "8129"))


def _xlsx(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _branch_report(serials) -> bytes:
    def build(wb):
        ws = wb.active
        ws.append([None] * 7 + ["Branch/TO/Center Name:", "HCMC BRANCH"])
        ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        for i, sn in enumerate(serials, start=1):
            ws.append([i, "HO CHI MINH BRANCH", "PC", "111", "A", "", "DELL", sn, "USING LOCAL", ""])
    return _xlsx(build)


def _ho_report(rows) -> bytes:
    """rows: (branch/dept text, user id, serial, dept column)."""
    def build(wb):
        wb.active.title = "Department HO"
        ws = wb.create_sheet("Equipment list detail")
        ws.append(["No", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "Floor", "Dept", "Remark"])
        for i, (bd, uid, sn, dept) in enumerate(rows, start=1):
            ws.append([i, bd, "PC", uid, "X", "DELL", sn, "USING LOCAL", "5", dept, ""])
    return _xlsx(build)


class ReportSourceTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_reportsource_")
        self.app = create_app()
        self.files = tempfile.mkdtemp(prefix="am_reportsource_files_")
        conn = get_connection()
        try:
            for no, name in UNITS:
                conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                             "VALUES (?, ?, ?, datetime('now'))", (no, name, name))
            for user_no, branch_no in USERS:
                conn.execute("INSERT INTO users (user_no, user_no_norm, branch_no, user_name) VALUES (?, ?, ?, ?)",
                             (user_no, user_no, branch_no, f"USER {user_no}"))
            conn.execute("INSERT INTO branch_aliases (alias, branch_no) VALUES ('HCMC', '8009')")
            self.admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()

    def _write(self, name, data) -> str:
        path = os.path.join(self.files, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _import_branch(self, period, serials):
        path = self._write(f"HCMC BRANCH 8009 - {period}.xlsx", _branch_report(serials))
        reports = import_asset_report(path, source_label=os.path.basename(path), period=period)
        self.assertFalse([r.error for r in reports if r.error])
        return reports

    def _current_serials(self, branch_no):
        conn = get_connection()
        try:
            return sorted(r["serial_tag"] for r in get_current_assets(conn, branch_no=branch_no))
        finally:
            conn.close()

    def _sources(self):
        conn = get_connection()
        try:
            return [tuple(r) for r in conn.execute("SELECT label, period, report_source FROM import_batches ORDER BY id")]
        finally:
            conn.close()

    def test_period_from_file_name(self):
        self.assertEqual(ho_report_period("Monthly Report 18092026.xlsx"), "2026-09")
        self.assertEqual(ho_report_period("Monthly Report.xlsx"), "")

    def test_ho_import_splits_by_user_unit_and_skips_own_report_units(self):
        self._import_branch("2026-08", ["B1", "B2", "B3"])
        path = self._write("Monthly Report 18092026.xlsx", _ho_report([
            ("HEAD OFFICE", "222", "H1", ""),                      # user -> 7904
            ("HEAD OFFICE", "999", "H2", "ICT PLANNING DEPARTMENT"),  # unknown user -> Dept column
            ("HEAD OFFICE", "111", "H3", ""),                      # 8009 sends its own report
            ("HEAD OFFICE", "111", "H5", "ICT PLANNING DEPARTMENT"),  # ... so its Dept column wins
            ("NOWHERE", "", "H4", ""),                             # nothing maps
        ]))
        reports, summary = import_ho_report(path, source_label=os.path.basename(path))
        self.assertEqual(summary.error, "")
        self.assertEqual(summary.period, "2026-09")
        self.assertEqual(summary.rows_read, 5)
        self.assertEqual(summary.skipped_own_report, {"HO CHI MINH BRANCH": 1})
        self.assertEqual(summary.unresolved_rows, 1)
        self.assertEqual(sorted(r.branch_no for r in reports), ["", "7904", "8129"])
        self.assertEqual(
            [s for s in self._sources() if s[2] == "HO"],
            [("HO - Unresolved", "2026-09", "HO"), ("HO CHI MINH CORPORATE MARKETING CENTER", "2026-09", "HO"),
             ("ICT PLANNING DEPARTMENT", "2026-09", "HO")],
        )
        self.assertEqual(self._current_serials("7904"), ["H1"])
        self.assertEqual(self._current_serials("8129"), ["H2", "H5"])
        self.assertEqual(self._current_serials("8009"), ["B1", "B2", "B3"])

    def test_scmc_site_wins_over_user_unit(self):
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('C001', 'SCMC PICO', 'SCMC PICO', datetime('now'))")
            conn.commit()
        finally:
            conn.close()
        path = self._write("Monthly Report 18092026.xlsx", _ho_report([
            ("SCMC PICO", "222", "S1", ""),        # user -> 7904, but the SCMC site wins
            ("HEAD OFFICE", "222", "S2", ""),      # not a site: user's unit as usual
        ]))
        reports, summary = import_ho_report(path, source_label=os.path.basename(path))
        self.assertEqual(summary.error, "")
        self.assertEqual(self._current_serials("C001"), ["S1"])
        self.assertEqual(self._current_serials("7904"), ["S2"])

    def test_ho_rows_never_hide_the_branch_report(self):
        """The HCMC case: HO-report rows on 8009 dated September (from before
        8009 had its own report) used to replace its August report."""
        # Imported before 8009 had a report of its own, so its row was kept.
        path = self._write("Monthly Report 18092026.xlsx", _ho_report([("HEAD OFFICE", "111", "H9", "")]))
        reports, _ = import_ho_report(path, source_label=os.path.basename(path))
        self.assertEqual([r.branch_no for r in reports], ["8009"])
        self._import_branch("2026-07", ["B0", "B1"])
        self._import_branch("2026-08", ["B1", "B2", "B3"])

        self.assertEqual(self._current_serials("8009"), ["B1", "B2", "B3", "H9"])
        conn = get_connection()
        try:
            self.assertEqual(get_current_asset_count(conn), 4)
            periods, table, *_ = get_branch_month_change_table(conn, "2026")
        finally:
            conn.close()
        row = next(r for r in table if r["branch_no"] == "8009")
        self.assertEqual(row["current_count"], 4)
        cells = dict(zip(periods, row["cells"]))
        # September has only the HO report: its 1 row, nothing "removed".
        self.assertEqual((cells["2026-09"]["count"], cells["2026-09"]["removed"]), (1, None))
        self.assertEqual((cells["2026-08"]["added"], cells["2026-08"]["removed"]), (2, 1))

        # A newer branch report replaces only the branch part.
        self._import_branch("2026-09", ["B2", "B4"])
        self.assertEqual(self._current_serials("8009"), ["B2", "B4", "H9"])

    def test_each_form_refuses_the_other_report(self):
        ho_path = self._write("Monthly Report 18092026.xlsx", _ho_report([("HEAD OFFICE", "222", "H1", "")]))
        reports = import_asset_report(ho_path, source_label=os.path.basename(ho_path), period="2026-09")
        self.assertEqual(len(reports), 1)
        self.assertIn("HO Monthly Report", reports[0].error)

        branch_path = self._write("HCMC BRANCH 8009.xlsx", _branch_report(["B1"]))
        reports, summary = import_ho_report(branch_path)
        self.assertEqual(reports, [])
        self.assertIn("Not the HO consolidated Monthly Report", summary.error)
        self.assertEqual(self._sources(), [])

    def test_ho_upload_route(self):
        create_account("hoimporter", "hoimporterpass1", self.admin)
        client = self.app.test_client()
        client.post("/login", data={"username": "hoimporter", "password": "hoimporterpass1"})
        self.assertIn("HO Monthly Report", client.get("/import/").get_data(as_text=True))
        data = {"period": "", "files": [(io.BytesIO(_ho_report([("HEAD OFFICE", "222", "H1", "")])),
                                         "Monthly Report 21082026.xlsx")]}
        resp = client.post("/import/ho-reports", data=data, content_type="multipart/form-data",
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("HO report for 2026-08 - 1 rows into 1 unit(s)", body)
        self.assertEqual(self._sources(), [("HO CHI MINH CORPORATE MARKETING CENTER", "2026-08", "HO")])

    def test_migration_flags_old_ho_batches(self):
        conn = get_connection()
        try:
            conn.execute("ALTER TABLE import_batches DROP COLUMN report_source")
            conn.execute("INSERT INTO import_batches (imported_at, kind, branch_hint, period) "
                         "VALUES ('x', 'asset_report', 'HO Monthly Report', '2026-09'), "
                         "('x', 'asset_report', 'HCMC', '2026-08')")
            _add_missing_columns(conn)
            conn.commit()
            got = [r[0] for r in conn.execute("SELECT report_source FROM import_batches ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(got, ["HO", "BRANCH"])


if __name__ == "__main__":
    unittest.main()
