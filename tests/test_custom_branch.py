# -*- coding: utf-8 -*-
"""Settings > Branch Aliases: when a label is a unit the official branch list
doesn't have, a new unit name can be typed instead of picking a branch
(custom code C001, C002...). Same isolated-DB pattern as
tests/test_settings_tabs.py."""
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import create_custom_branch, import_branch_file, is_custom_branch  # noqa: E402


class CustomBranchTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_custombranch_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                         "VALUES ('8160', 'SCMC', 'SMART CREDIT MARKETING CENTER', datetime('now'))")
            batch = conn.execute("INSERT INTO import_batches (imported_at, kind, label, period) "
                                 "VALUES (datetime('now'), 'asset_report', 'HO', '2026-09')").lastrowid
            for i in range(3):
                conn.execute("INSERT INTO asset_items (batch_id, asset_key, branch_dept, branch_no, device_name) "
                             "VALUES (?, ?, 'SCMC PICO', '', 'PC')", (batch, f"k{i}"))
            conn.execute("INSERT INTO branch_unresolved (raw_hint, first_seen_at, last_seen_at, occurrences) "
                         "VALUES ('SCMC PICO', datetime('now'), datetime('now'), 3)")
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("mapper", "mappass123", admin)
        self.client.post("/login", data={"username": "mapper", "password": "mappass123"})

    def _branch_of_rows(self):
        conn = get_connection()
        try:
            return {r[0] for r in conn.execute("SELECT branch_no FROM asset_items")}
        finally:
            conn.close()

    def test_unresolved_label_becomes_a_new_unit(self):
        page = self.client.get("/settings/").data.decode()
        self.assertIn('name="new_branch_name" value="SCMC PICO"', page)
        resp = self.client.post("/settings/branch-hint/map", data={
            "raw_hint": "SCMC PICO", "branch_no": "", "new_branch_name": "scmc  pico"}, follow_redirects=True)
        self.assertIn(b"Fixed 3 already-imported", resp.data)
        conn = get_connection()
        try:
            unit = conn.execute("SELECT * FROM branches WHERE branch_no = 'C001'").fetchone()
            alias = conn.execute("SELECT branch_no FROM branch_aliases WHERE alias = 'SCMC PICO'").fetchone()
            unresolved = conn.execute("SELECT COUNT(*) FROM branch_unresolved").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(unit["eng_name"], "SCMC PICO")
        self.assertEqual(alias["branch_no"], "C001")
        self.assertEqual(unresolved, 0)
        self.assertEqual(self._branch_of_rows(), {"C001"})
        self.assertIn("C001, custom unit", self.client.get("/settings/").data.decode())

    def test_picking_a_branch_wins_over_the_name_box(self):
        self.client.post("/settings/branch-hint/map", data={
            "raw_hint": "SCMC PICO", "branch_no": "8160", "new_branch_name": "SCMC PICO"})
        self.assertEqual(self._branch_of_rows(), {"8160"})
        conn = get_connection()
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM branches WHERE branch_no LIKE 'C%'").fetchone())
        finally:
            conn.close()

    def test_manual_alias_to_a_new_unit_and_codes_count_up(self):
        self.client.post("/settings/branch-alias/add", data={"alias_text": "SCMC DALI", "branch_no": "",
                                                              "new_branch_name": "SCMC DALI"})
        self.client.post("/settings/branch-alias/add", data={"alias_text": "SCMC PICO", "branch_no": "",
                                                              "new_branch_name": "SCMC PICO"})
        conn = get_connection()
        try:
            units = dict(conn.execute("SELECT eng_name, branch_no FROM branches WHERE branch_no LIKE 'C%'").fetchall())
        finally:
            conn.close()
        self.assertEqual(units, {"SCMC DALI": "C001", "SCMC PICO": "C002"})

    def test_existing_name_is_reused_and_nothing_given_is_refused(self):
        conn = get_connection()
        try:
            self.assertEqual(create_custom_branch(conn, "Smart Credit Marketing Center"), "8160")
            first = create_custom_branch(conn, "SCMC PICO")
            self.assertEqual(create_custom_branch(conn, " scmc pico "), first)
            conn.commit()
        finally:
            conn.close()
        resp = self.client.post("/settings/branch-hint/map", data={"raw_hint": "SCMC PICO", "branch_no": "",
                                                                    "new_branch_name": ""}, follow_redirects=True)
        self.assertIn(b"type a new unit name", resp.data)
        self.assertTrue(is_custom_branch("C001"))
        self.assertFalse(is_custom_branch("8160"))

    def test_branch_file_import_keeps_custom_units(self):
        conn = get_connection()
        try:
            create_custom_branch(conn, "SCMC PICO")
            conn.commit()
        finally:
            conn.close()
        wb = openpyxl.Workbook()
        wb.active.append(["Branch No", "Local Branch Name", "Eng. Branch Name"])
        wb.active.append(["8160", "SCMC", "SHINHAN BANK VIETNAM SMART CREDIT MARKETING CENTER"])
        path = os.path.join(tempfile.mkdtemp(), "branches.xlsx")
        wb.save(path)
        import_branch_file(path)
        conn = get_connection()
        try:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM branches WHERE branch_no = 'C001'").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
