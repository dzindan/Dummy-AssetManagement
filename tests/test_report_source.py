# -*- coding: utf-8 -*-
"""Branch reports vs the HO consolidated Monthly Report: separate import
paths (each refuses the other's file), import_batches.report_source, and
"current state" / month-change tables with one report per unit. HO
report rules (user, 2026-11-11): the NORTHERN HO block -> Northern Head
Office, a block of a unit with its own report -> that unit (dropped when
its own report for the month is there), everything else -> Head Office with
Dept as Position; the file's date is the month after the one it reports.
Same isolated-DB pattern as tests/test_period_skip.py."""
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
    ("8061", "HEAD OFFICE"),
    ("8136", "NORTHERN HEAD OFFICE"),
    ("C001", "SCMC PICO"),
)
USERS = (("111", "8009"), ("222", "7904"), ("333", "8129"))


def _xlsx(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _branch_report(serials, label="HCMC BRANCH", unit="HO CHI MINH BRANCH") -> bytes:
    def build(wb):
        ws = wb.active
        ws.append([None] * 7 + ["Branch/TO/Center Name:", label])
        ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        for i, sn in enumerate(serials, start=1):
            ws.append([i, unit, "PC", "111", "A", "", "DELL", sn, "USING LOCAL", ""])
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
        self.app = create_app(csrf=False)
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
            conn.execute("INSERT INTO branch_aliases (alias, branch_no) VALUES ('NORTHERN HO', '8136')")
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

    def _positions(self, branch_no):
        conn = get_connection()
        try:
            return {r["serial_tag"]: r["position"] or "" for r in get_current_assets(conn, branch_no=branch_no)}
        finally:
            conn.close()

    def _import_ho(self, name, rows):
        path = self._write(name, _ho_report(rows))
        reports, summary = import_ho_report(path, source_label=os.path.basename(path))
        self.assertEqual(summary.error, "")
        return reports, summary

    def test_period_from_file_name(self):
        # sent in September -> the August report (it sits in the 08 folder)
        self.assertEqual(ho_report_period("Monthly Report 18092026.xlsx"), "2026-08")
        self.assertEqual(ho_report_period("Monthly Report 12012027.xlsx"), "2026-12")
        self.assertEqual(ho_report_period("Monthly Report Sep 26.xlsx"), "")

    def test_ho_import_rules(self):
        self._import_branch("2026-08", ["B1", "B2", "B3"])
        reports, summary = self._import_ho("Monthly Report 18092026.xlsx", [
            ("HEAD OFFICE", "222", "H1", "ICT PLANNING DEPARTMENT"),  # Head Office, Dept kept as Position
            ("HEAD OFFICE", "333", "H2", ""),        # the user's unit (8129) no longer counts
            ("SCMC PICO", "", "H3", ""),             # a site is Head Office too, its name as Position
            ("NORTHERN HO", "", "N1", "RETAIL"),     # the Northern block -> Northern Head Office
            ("HCMC", "", "H4", ""),                  # 8009's own August report is in: left out
            ("NOWHERE", "", "H5", ""),               # anything else: Head Office
        ])
        self.assertEqual(summary.period, "2026-08")
        self.assertEqual(summary.rows_read, 6)
        self.assertEqual(summary.skipped_own_report, {"HO CHI MINH BRANCH": 1})
        self.assertEqual(summary.unresolved_rows, 0)
        self.assertEqual(sorted(r.branch_no for r in reports), ["8061", "8136"])
        self.assertEqual(self._positions("8061"), {"H1": "ICT PLANNING DEPARTMENT", "H2": "", "H3": "SCMC PICO",
                                                   "H5": "NOWHERE"})
        self.assertEqual(self._positions("8136"), {"N1": "RETAIL"})
        self.assertEqual(self._current_serials("8009"), ["B1", "B2", "B3"])
        self.assertEqual(self._current_serials("8129"), [])
        self.assertEqual(self._current_serials("C001"), [])

    def test_own_report_replaces_the_ho_copy(self):
        """Northern's own file and the HO report's NORTHERN HO block are the
        same devices: never both."""
        self._import_ho("Monthly Report 21082026.xlsx", [("NORTHERN HO", "", "N0", "")])   # July
        self._import_ho("Monthly Report 18092026.xlsx", [("NORTHERN HO", "", "N1", ""),    # August
                                                         ("NORTHERN HO", "", "N2", ""),
                                                         ("HEAD OFFICE", "", "H1", "")])
        self.assertEqual(self._current_serials("8136"), ["N1", "N2"])
        # Northern's own August report comes in later: the HO copy for August goes.
        path = self._write("NORTHERN BUSINESS DIVISION - Aug.xlsx",
                           _branch_report(["N1", "N3"], label="NORTHERN HO", unit="NORTHERN HO"))
        reports = import_asset_report(path, source_label=os.path.basename(path), period="2026-08")
        self.assertEqual([r.branch_no for r in reports if not r.error], ["8136"])
        self.assertEqual(self._current_serials("8136"), ["N1", "N3"])
        self.assertEqual(self._current_serials("8061"), ["H1"])
        conn = get_connection()
        try:
            self.assertEqual(get_current_asset_count(conn), 3)
            periods, table, *_ = get_branch_month_change_table(conn, "2026")
            left = conn.execute("SELECT COUNT(*) FROM asset_items ai JOIN import_batches ib ON ib.id = ai.batch_id "
                                "WHERE ib.report_source = 'HO' AND ai.branch_no = '8136'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(left, 1)  # July's N0 stays: no own report for July
        cells = dict(zip(periods, next(r for r in table if r["branch_no"] == "8136")["cells"]))
        self.assertEqual(cells["2026-07"]["count"], 1)
        self.assertEqual(cells["2026-08"]["count"], 2)
        # A newer HO report while Northern has no own file for that month: it counts.
        self._import_ho("Monthly Report 12102026.xlsx", [("NORTHERN HO", "", "N9", "")])  # September
        self.assertEqual(self._current_serials("8136"), ["N9"])
        # The same month from both, HO first: the unit's own report wins.
        self._import_ho("Monthly Report 04112026.xlsx", [("NORTHERN HO", "", "N8", "")])  # October
        path = self._write("NORTHERN BUSINESS DIVISION - Oct.xlsx",
                           _branch_report(["N1"], label="NORTHERN HO", unit="NORTHERN HO"))
        import_asset_report(path, source_label=os.path.basename(path), period="2026-10")
        self.assertEqual(self._current_serials("8136"), ["N1"])
        # ... and an HO report imported after it leaves the block out.
        _, summary = self._import_ho("Monthly Report 05112026.xlsx", [("NORTHERN HO", "", "N7", "")])
        self.assertEqual(summary.skipped_own_report, {"NORTHERN HEAD OFFICE": 1})
        self.assertEqual(self._current_serials("8136"), ["N1"])

    def test_trends_and_previous_report_take_one_report_per_month(self):
        """Charts, the device table and "previous report" follow the same
        rule as current state: one report per unit and month, the unit's own
        before the HO report's copy, the series running across both."""
        from app.analytics import get_all_branches_item_trend, get_branch_device_year_table, get_branch_item_trend
        from app.queries import get_previous_batch_for_branch

        self._import_ho("Monthly Report 21082026.xlsx", [("NORTHERN HO", "", "N0", "")])  # July, HO only
        path = self._write("NORTHERN BUSINESS DIVISION - Aug.xlsx",
                           _branch_report(["N1", "N2"], label="NORTHERN HO", unit="NORTHERN HO"))
        own = import_asset_report(path, source_label=os.path.basename(path), period="2026-08")[0].batch_id
        conn = get_connection()
        try:
            # an HO copy for August left next to the own report (e.g. data from
            # before drop_ho_rows), with a device type the own report hasn't got
            cur = conn.execute("INSERT INTO import_batches (imported_at, kind, source_files_json, label, period, "
                               "report_source) VALUES (datetime('now'), 'asset_report', '[]', 'x', '2026-08', 'HO')")
            conn.execute("INSERT INTO asset_items (batch_id, asset_key, branch_no, device_name, serial_tag) "
                         "VALUES (?, 'k-ho', '8136', 'LAPTOP', 'L9')", (cur.lastrowid,))
            conn.commit()
            periods, items, matrix = get_branch_item_trend(conn, "8136")
            self.assertEqual({i: matrix[i].get("2026-08") for i in items}, {"PC": 2})
            periods, items, matrix = get_all_branches_item_trend(conn)
            self.assertEqual(matrix["PC"]["2026-08"], 2)
            self.assertNotIn("LAPTOP", matrix)
            periods, rows, *_ = get_branch_device_year_table(conn, "8136", "2026", ["PC", "LAPTOP"])
            self.assertEqual([r["item"] for r in rows], ["PC"])
            self.assertEqual(get_previous_batch_for_branch(conn, "8136", own), conn.execute(
                "SELECT id FROM import_batches WHERE period = '2026-07'").fetchone()[0])
        finally:
            conn.close()

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
        self.assertIn("HO report for 2026-07 - 1 rows into 1 unit(s)", body)
        self.assertEqual(self._sources(), [("HEAD OFFICE", "2026-07", "HO")])

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
