# -*- coding: utf-8 -*-
"""Generating a hand-over form should stamp the form's own date onto every
included asset's handover_date in Manage Assets - see app/handover.py's
apply_handover_date(). Follows the same isolated-DB pattern as
tests/test_exports.py.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_handovertest_")
    return create_app()


def _role_id(conn, name):
    return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]


class HandoverDateStampingTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        conn = get_connection()
        try:
            admin_role_id = _role_id(conn, "Admin")
            conn.execute("INSERT INTO branches (branch_no, eng_name) VALUES ('001', 'Hanoi Branch')")
            conn.execute(
                "INSERT INTO import_batches (id, imported_at, kind, period) "
                "VALUES (1, datetime('now'), 'asset_report', '2026-01')"
            )
            conn.execute(
                "INSERT INTO asset_items (id, batch_id, asset_key, branch_no, branch_dept, device_name, "
                "device_name_raw, model_device, serial_tag, status, full_name, user_id_norm, user_id_raw, "
                "handover_date) VALUES (1, 1, 'k1', '001', 'Hanoi', 'PC', 'PC', 'M1', 'SN1', 'IN USE', "
                "'A', '1001', '1001', '2025-01-01')"
            )
            conn.commit()
        finally:
            conn.close()
        create_account("hotester", "hotesterpass1", admin_role_id)
        self.client = self.app.test_client()
        self.client.post("/login", data={"username": "hotester", "password": "hotesterpass1"})

    def _generate(self, ho_date):
        return self.client.post(
            "/lookup/generate",
            data={
                "user_no": "1001",
                "ho_date": ho_date,
                "ho_type": "ASSIGNMENT",
                "reason": "NEWCOMER",
                "asset_id": ["1"],
                "condition_1": "NEW",
                "receiving_name": "A",
                "receiving_title": "Staff",
                "receiving_dept": "Hanoi",
                "receiving_id": "1001",
            },
            follow_redirects=True,
        )

    def test_generate_stamps_handover_date_onto_asset(self):
        resp = self._generate("2026-03-15")
        self.assertEqual(resp.status_code, 200)

        conn = get_connection()
        try:
            row = conn.execute("SELECT handover_date FROM asset_items WHERE id = 1").fetchone()
            self.assertEqual(row["handover_date"], "15/03/2026")

            log_row = conn.execute(
                "SELECT * FROM activity_log WHERE field = 'handover_date' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(log_row)
            self.assertEqual(log_row["old_value"], "2025-01-01")
            self.assertEqual(log_row["new_value"], "15/03/2026")
            self.assertEqual(log_row["performed_by"], "hotester")
        finally:
            conn.close()

    def test_generating_again_with_same_date_does_not_log_again(self):
        self._generate("2026-03-15")
        conn = get_connection()
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM activity_log WHERE field = 'handover_date'"
            ).fetchone()["n"]
        finally:
            conn.close()

        self._generate("2026-03-15")

        conn = get_connection()
        try:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM activity_log WHERE field = 'handover_date'"
            ).fetchone()["n"]
            row = conn.execute("SELECT handover_date FROM asset_items WHERE id = 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(before, after)
        self.assertEqual(row["handover_date"], "15/03/2026")


class HandoverQrTests(HandoverDateStampingTests):
    """Every generated form carries a QR code + form number in its page
    header (app/handover.py's _add_header_qr) identifying its
    handover_records row, so a signed scan can be matched back to it."""

    # The date-stamping tests are inherited for setUp only.
    test_generate_stamps_handover_date_onto_asset = None
    test_generating_again_with_same_date_does_not_log_again = None

    def _record(self):
        conn = get_connection()
        try:
            return conn.execute("SELECT * FROM handover_records ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()

    def test_docx_header_has_qr_and_form_number(self):
        import io

        import segno
        from docx import Document

        from app.handover import form_number, qr_payload
        import datetime as dt

        self._generate("2026-03-15")
        rec = self._record()
        self.assertTrue(os.path.basename(rec["docx_path"]).startswith(f"HANDOVER_{form_number(rec['id'])}_"))

        doc = Document(rec["docx_path"])
        header = doc.sections[0].header
        self.assertIn(form_number(rec["id"]), header.paragraphs[0].text)

        ns = {"wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
        anchors = [a for a in header._element.findall(".//wp:anchor", ns)
                   if a.find("wp:docPr", ns).get("name") == "HandoverQR"]
        self.assertEqual(len(anchors), 1)
        blip = anchors[0].find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        image_bytes = header.part.related_parts[rid].blob

        payload = qr_payload(rec["id"], dt.date(2026, 3, 15), "1001", "ASSIGNMENT")
        self.assertEqual(payload, f"AMHO1|{rec['id']}|2026-03-15|1001|ASSIGNMENT")
        expected = io.BytesIO()
        segno.make_qr(payload, error="m").save(expected, kind="png", scale=10, border=2)
        self.assertEqual(image_bytes, expected.getvalue())

    def test_history_and_success_show_form_number(self):
        resp = self._generate("2026-03-15")
        rec = self._record()
        number = f"HO-{rec['id']:06d}"
        self.assertIn(number, resp.get_data(as_text=True))
        self.assertIn(number, self.client.get("/history/").get_data(as_text=True))

    def test_render_failure_leaves_no_history_row(self):
        from unittest import mock

        with mock.patch("app.handover._add_header_qr", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.app.config["PROPAGATE_EXCEPTIONS"] = True
                self._generate("2026-03-15")
        self.assertIsNone(self._record())


if __name__ == "__main__":
    unittest.main()
