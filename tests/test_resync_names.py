# -*- coding: utf-8 -*-
"""resync_full_names: pushing an updated users table onto already-imported
asset_items, since full_name is a snapshot taken at import time and nothing
else re-touches it afterward (see importer.resync_full_names's docstring).

Follows the same isolated-DB pattern as tests/test_auth.py.
"""
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report, resync_full_names  # noqa: E402


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_resynctest_")
    return create_app()


def _build_workbook(path: str, rows: list[tuple]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "REPORT"
    ws.append([None] * 7 + ["Branch/TO/Center Name:", "Test Branch"])
    ws.append(
        ["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
         "SERIAL/ SERVICE TAG", "STATUS", "REMARK"]
    )
    for i, (user_id, full_name, serial) in enumerate(rows, start=1):
        ws.append([i, "TEST BRANCH", "PC", user_id, full_name, "10.0.0.1", "DELL 3060", serial, "USING LOCAL", ""])
    wb.save(path)


class ResyncFullNamesTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        self.tmpdir = tempfile.mkdtemp(prefix="am_resynctest_import_")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
            )
            conn.commit()
        finally:
            conn.close()

    def _import(self, rows):
        path = os.path.join(self.tmpdir, "report.xlsx")
        _build_workbook(path, rows)
        reports = import_asset_report(path, source_label="report.xlsx", period="2026-03")
        self.assertEqual(reports[0].error, "")
        return reports[0].batch_id

    def test_renaming_a_known_user_updates_already_imported_rows(self):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO users (user_no, user_no_norm, branch_no, user_name, updated_at) "
                "VALUES ('1001', '1001', '001', 'NGUYEN VAN A CU', datetime('now'))"
            )
            conn.commit()
        finally:
            conn.close()

        self._import([("1001", "typed differently in the report", "SN-1")])

        conn = get_connection()
        try:
            row = conn.execute("SELECT full_name FROM asset_items WHERE serial_tag = 'SN-1'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["full_name"], "NGUYEN VAN A CU")

        # User IDs re-imported with a corrected/renamed spelling.
        conn = get_connection()
        try:
            conn.execute("UPDATE users SET user_name = 'NGUYEN VAN A MOI' WHERE user_no = '1001'")
            conn.commit()
            changed = resync_full_names(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(changed, 1)

        conn = get_connection()
        try:
            row = conn.execute("SELECT full_name FROM asset_items WHERE serial_tag = 'SN-1'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["full_name"], "NGUYEN VAN A MOI")

    def test_user_added_after_import_gets_picked_up_on_resync(self):
        # No matching row in `users` yet at import time - full_name falls
        # back to the report's own raw text (uppercased), same as import.
        self._import([("2002", "raw report name", "SN-2")])

        conn = get_connection()
        try:
            row = conn.execute("SELECT full_name FROM asset_items WHERE serial_tag = 'SN-2'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["full_name"], "RAW REPORT NAME")

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO users (user_no, user_no_norm, branch_no, user_name, updated_at) "
                "VALUES ('2002', '2002', '001', 'TRAN THI B', datetime('now'))"
            )
            conn.commit()
            changed = resync_full_names(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(changed, 1)

        conn = get_connection()
        try:
            row = conn.execute("SELECT full_name FROM asset_items WHERE serial_tag = 'SN-2'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["full_name"], "TRAN THI B")

    def test_row_with_no_matching_user_is_left_alone(self):
        self._import([("9999", "nobody knows this person", "SN-3")])

        conn = get_connection()
        try:
            changed = resync_full_names(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(changed, 0)

        conn = get_connection()
        try:
            row = conn.execute("SELECT full_name FROM asset_items WHERE serial_tag = 'SN-3'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["full_name"], "NOBODY KNOWS THIS PERSON")


if __name__ == "__main__":
    unittest.main()
