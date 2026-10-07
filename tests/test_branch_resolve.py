# -*- coding: utf-8 -*-
"""Branch labels map only through the branch list (no guessing): an alias,
a branch code written in the label / file name, or a branch name equal to
the label (ignoring case, accents, spaces, punctuation). Anything else
stays unresolved for manual mapping. The old substring rule sent the HO CHI
MINH BRANCH 8009 report (label "HCMC") to HCM CARD CENTER 8079. Same
isolated-DB pattern as tests/test_custom_branch.py."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import _file_branch, resolve_branch  # noqa: E402

BRANCHES = [
    ("8009", "HO CHI MINH BRANCH", "HO CHI MINH BRANCH"),
    ("8079", "HCM Card Center", "HCM CARD CENTER"),
    ("8175", "DISTRICT 11 BRANCH", "DISTRICT 11 BRANCH"),
    ("7912", "CAN THO BRANCH", "CAN THO BRANCH"),
    ("8090", "HAI PHONG BRANCH", "HAI PHONG BRANCH"),
    ("8168", "HAI PHONG CENTRAL BRANCH", "HAI PHONG CENTRAL BRANCH"),
    ("8155", "SOUTH SAI GON TRANSACTION OFFICE", "SOUTH SAI GON TRANSACTION OFFICE"),
    ("8179", "MY DINH TRANSACTION OFFICE", "MY DINH TRANSACTION OFFICE"),
    ("8017", "HA NOI BRANCH", "HA NOI BRANCH"),
    ("8131", "RISK MANAGEMENT DIVISION", "RISK MANAGEMENT DIVISION"),
    ("8133", "RISK MANAGEMENT DIVISION", "RISK MANAGEMENT DIVISION"),
]


class BranchResolveTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_branchresolve_")
        create_app()
        self.conn = get_connection()
        self.conn.executemany("INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                              "VALUES (?, ?, ?, datetime('now'))", BRANCHES)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def no(self, text):
        return resolve_branch(self.conn, text)[0]

    def test_names_from_the_list_resolve(self):
        self.assertEqual(self.no("HAI PHONG BRANCH"), "8090")
        self.assertEqual(self.no("Hai Phong Central Branch"), "8168")
        self.assertEqual(self.no("South saigon T.O"), "8155")   # T.O = TRANSACTION OFFICE
        self.assertEqual(self.no("My Dinh T/O"), "8179")
        self.assertEqual(self.no("Hà Nội Branch"), "8017")
        self.assertEqual(self.no("hcm card center"), "8079")

    def test_branch_code_in_the_label_resolves(self):
        self.assertEqual(self.no("8009"), "8009")
        self.assertEqual(self.no("HCMC 8009"), "8009")
        self.assertEqual(self.no("Report 2026"), "")              # not a branch code

    def test_anything_else_stays_unresolved(self):
        for text in ("HCMC", "ICT", "HO", "HCM", "Hai Phong", "HANOI", "Pham Hung", "My Dinh"):
            self.assertEqual(self.no(text), "", text)

    def test_file_sources_are_cross_checked(self):
        name = "HCMC BRANCH 8009 - IT ASSET MONTHLY REPORT_08.2026.xlsx"
        # Label/column not in the list: the file name code decides.
        self.assertEqual(_file_branch(self.conn, name, "HCMC", "HCMC")[:2], ("8009", "HO CHI MINH BRANCH"))
        # Everything that maps agrees.
        branch_no, _, checks, conflict = _file_branch(self.conn, "C:/x/" + name, "HO CHI MINH BRANCH", "8009")
        self.assertEqual((branch_no, conflict), ("8009", False))
        self.assertEqual(len(checks), 3)
        # File name says 8009, the label says HCM CARD CENTER: unresolved.
        branch_no, _, checks, conflict = _file_branch(self.conn, name, "HCM CARD CENTER")
        self.assertEqual((branch_no, conflict), ("", True))
        self.assertIn("Branch label: HCM CARD CENTER -> 8079", checks)
        # Label vs BRANCH/DEPT column disagree, no code in the name.
        self.assertEqual(_file_branch(self.conn, "report.xlsx", "HAI PHONG BRANCH", "HAI PHONG CENTRAL BRANCH")[1:], ("", ["File name: report.xlsx -> no branch code", "Branch label: HAI PHONG BRANCH -> 8090", "BRANCH/DEPT column: HAI PHONG CENTRAL BRANCH -> 8168"], True))
        # Nothing maps: unresolved, not a conflict.
        self.assertEqual(_file_branch(self.conn, "report.xlsx", "Pham Hung")[0::3], ("", False))
        # Two different codes in the name: the name is not used.
        self.assertEqual(_file_branch(self.conn, "8009 vs 8079.xlsx", "HO CHI MINH BRANCH")[0], "8009")

    def test_two_branches_with_the_same_name_stay_unresolved(self):
        self.assertEqual(self.no("RISK MANAGEMENT DIVISION"), "")

    def test_alias_still_wins(self):
        self.conn.execute("INSERT INTO branch_aliases (alias, branch_no) VALUES ('HCMC', '8009')")
        self.assertEqual(self.no("HCMC"), "8009")


if __name__ == "__main__":
    unittest.main()
