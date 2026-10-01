# -*- coding: utf-8 -*-
"""Bulk asset-report upload where some branches already have an import for
the period: the warning lets each such file be skipped (the default) or
imported again, and the files without a conflict are always imported. Same
isolated-DB pattern as tests/test_dashboard_sort.py."""
import io
import os
import sys
import tempfile
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402

BRANCHES = (("001", "ZETA BRANCH"), ("002", "ALPHA BRANCH"), ("003", "MIDDLE BRANCH"))


def _report(label: str, serial_prefix: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 7 + ["Branch/TO/Center Name:", label])
    ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
               "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
    ws.append([1, label.upper(), "PC", "1", "A", "", "DELL", f"SN-{serial_prefix}", "USING LOCAL", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class PeriodSkipTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_periodskip_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            for no, name in BRANCHES:
                conn.execute(
                    "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) VALUES (?, ?, ?, datetime('now'))",
                    (no, name, name),
                )
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        # ZETA and ALPHA already have 2026-03; MIDDLE doesn't.
        tmp = tempfile.mkdtemp(prefix="am_periodskip_files_")
        for label in ("Zeta Branch", "Alpha Branch"):
            path = os.path.join(tmp, f"{label}.xlsx")
            with open(path, "wb") as f:
                f.write(_report(label, label[:1] + "-old"))
            import_asset_report(path, source_label=os.path.basename(path), period="2026-03")
        create_account("skipper", "skippass1", admin)
        self.client.post("/login", data={"username": "skipper", "password": "skippass1"})

    def _upload(self):
        files = [(io.BytesIO(_report(label, label[:1] + "-new")), f"{label}.xlsx")
                 for label in ("Zeta Branch", "Alpha Branch", "Middle Branch")]
        return self.client.post("/import/asset-reports", data={"period": "2026-03", "files": files},
                                content_type="multipart/form-data")

    def _warning_form(self, page: str) -> dict:
        """The hidden fields the warning page would post back."""
        import re
        fields = {"files": re.findall(r'name="files" value="([^"]+)"', page),
                  "conflict_files": re.findall(r'name="conflict_files" value="([^"]+)"', page),
                  "period": "2026-03", "source": "upload"}
        return fields

    def _batches(self):
        conn = get_connection()
        try:
            return [r[0] for r in conn.execute("SELECT label FROM import_batches ORDER BY id")]
        finally:
            conn.close()

    def test_warning_lists_conflicts_unticked_and_the_new_file(self):
        page = self._upload().data.decode()
        self.assertIn("Already Imported This Period?", page)
        self.assertEqual(page.count('name="import_conflict"'), 2)
        self.assertNotIn('name="import_conflict" value="" checked', page)
        self.assertIn("Not imported for this period yet", page)
        self.assertIn("Middle Branch.xlsx", page)

    def test_skip_conflicts_imports_only_the_new_file(self):
        form = self._warning_form(self._upload().data.decode())
        before = self._batches()
        resp = self.client.post("/import/asset-reports/confirm-period", data={**form, "mode": "selected"},
                                follow_redirects=True)
        added = self._batches()[len(before):]
        self.assertEqual(added, ["Middle Branch"])
        self.assertIn(b"Skipped 2 file(s)", resp.data)
        # The skipped temp uploads are cleaned up.
        for value in form["files"]:
            self.assertFalse(os.path.exists(value.split("::", 1)[0]))

    def test_ticked_conflict_is_imported_too(self):
        form = self._warning_form(self._upload().data.decode())
        zeta = next(p for p in form["conflict_files"] if p in next(f for f in form["files"] if "Zeta" in f))
        before = self._batches()
        self.client.post("/import/asset-reports/confirm-period",
                         data={**form, "mode": "selected", "import_conflict": [zeta]})
        self.assertEqual(sorted(self._batches()[len(before):]), ["Middle Branch", "Zeta Branch"])

    def test_import_all_anyway(self):
        form = self._warning_form(self._upload().data.decode())
        before = self._batches()
        self.client.post("/import/asset-reports/confirm-period", data={**form, "mode": "all"})
        self.assertEqual(len(self._batches()) - len(before), 3)

    def test_everything_skipped_imports_nothing(self):
        files = [(io.BytesIO(_report("Zeta Branch", "Z-new")), "Zeta Branch.xlsx")]
        page = self.client.post("/import/asset-reports", data={"period": "2026-03", "files": files},
                                content_type="multipart/form-data").data.decode()
        self.assertNotIn("Not imported for this period yet", page)
        before = self._batches()
        resp = self.client.post("/import/asset-reports/confirm-period",
                                data={**self._warning_form(page), "mode": "selected"}, follow_redirects=True)
        self.assertEqual(self._batches(), before)
        self.assertIn(b"Nothing imported - every file was skipped.", resp.data)


if __name__ == "__main__":
    unittest.main()
