# -*- coding: utf-8 -*-
"""User Asset History / Device Handover History (search by user ID or by
serial number) - both search modes and the from/to hand-over annotations
they share. Follows the same isolated-DB pattern as tests/test_exports.py.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.queries import get_serial_history  # noqa: E402
from app.routes.user_history import _build_custody_segments, _find_neighbor  # noqa: E402


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_userhistorytest_")
    return create_app()


def _role_id(conn, name):
    return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]


def _row(period, user_id_norm, user_id_raw="", full_name="", branch_dept="Hanoi", device_name="PC",
         model_device="M1", status="IN USE"):
    """A dict shaped like get_serial_history()'s sqlite3.Row output, for
    unit-testing _build_custody_segments/_find_neighbor without a DB."""
    return {
        "period": period, "user_id_norm": user_id_norm, "user_id_raw": user_id_raw,
        "full_name": full_name, "branch_dept": branch_dept, "device_name": device_name,
        "model_device": model_device, "status": status,
    }


class CustodySegmentTests(unittest.TestCase):
    """Pure-function tests for the walk that turns a serial's period-ordered
    snapshots into one entry per continuous stretch held by the same user -
    no DB needed."""

    def test_consecutive_same_user_merges_into_one_segment(self):
        rows = [_row("2026-01", "1001"), _row("2026-02", "1001"), _row("2026-03", "1001")]
        segments = _build_custody_segments(rows)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["from_period"], "2026-01")
        self.assertEqual(segments[0]["to_period"], "2026-03")

    def test_user_change_splits_into_two_segments(self):
        rows = [_row("2026-01", "1001", full_name="Alice"), _row("2026-02", "1002", full_name="Bob")]
        segments = _build_custody_segments(rows)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["user_id_norm"], "1001")
        self.assertEqual(segments[0]["to_period"], "2026-01")
        self.assertEqual(segments[1]["user_id_norm"], "1002")
        self.assertEqual(segments[1]["from_period"], "2026-02")

    def test_find_neighbor_start_returns_predecessor(self):
        segments = _build_custody_segments(
            [_row("2026-01", "1001", full_name="Alice"), _row("2026-02", "1002", full_name="Bob")]
        )
        neighbor = _find_neighbor(segments, "1002", "2026-02", "start")
        self.assertEqual(neighbor["full_name"], "Alice")

    def test_find_neighbor_end_returns_successor(self):
        segments = _build_custody_segments(
            [_row("2026-01", "1001", full_name="Alice"), _row("2026-02", "1002", full_name="Bob")]
        )
        neighbor = _find_neighbor(segments, "1001", "2026-01", "end")
        self.assertEqual(neighbor["full_name"], "Bob")

    def test_find_neighbor_returns_none_at_the_edges(self):
        segments = _build_custody_segments([_row("2026-01", "1001", full_name="Alice")])
        self.assertIsNone(_find_neighbor(segments, "1001", "2026-01", "start"))
        self.assertIsNone(_find_neighbor(segments, "1001", "2026-01", "end"))


class GetSerialHistoryTests(unittest.TestCase):
    """DB-level: latest-batch-per-period dedup, same convention as
    analytics.py/diffing.py."""

    def setUp(self):
        self.app = _fresh_app()

    def test_dedupes_to_latest_batch_within_a_period(self):
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, eng_name) VALUES ('001', 'Hanoi Branch')")
            conn.execute(
                "INSERT INTO import_batches (id, imported_at, kind, period) VALUES "
                "(1, '2026-01-01 00:00:00', 'asset_report', '2026-01'), "
                "(2, '2026-01-02 00:00:00', 'asset_report', '2026-01')"
            )
            conn.execute(
                "INSERT INTO asset_items (batch_id, asset_key, branch_no, branch_dept, device_name, "
                "device_name_raw, model_device, serial_tag, status, user_id_norm, user_id_raw) VALUES "
                "(1, 'SN:SN1', '001', 'Hanoi', 'PC', 'PC', 'M1', 'SN1', 'IN USE', '1001', '1001'), "
                "(2, 'SN:SN1', '001', 'Hanoi', 'PC', 'PC', 'M1', 'SN1', 'IN USE', '1002', '1002')"
            )
            conn.commit()
            rows = get_serial_history(conn, "SN1")
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id_norm"], "1002")


class UserHistoryRouteTests(unittest.TestCase):
    """Both search modes end to end through the Flask test client, and the
    from/to annotation they share."""

    def setUp(self):
        self.app = _fresh_app()
        conn = get_connection()
        try:
            admin_role_id = _role_id(conn, "Admin")
            conn.execute("INSERT INTO branches (branch_no, eng_name) VALUES ('001', 'Hanoi Branch')")
            conn.execute(
                "INSERT INTO users (user_no, user_no_norm, branch_no, user_name) VALUES "
                "('1001', '1001', '001', 'Alice'), ('1002', '1002', '001', 'Bob')"
            )
            conn.execute(
                "INSERT INTO import_batches (id, imported_at, kind, period) VALUES "
                "(1, '2026-01-01 00:00:00', 'asset_report', '2026-01'), "
                "(2, '2026-02-01 00:00:00', 'asset_report', '2026-02')"
            )
            # Same serial, reassigned from Alice (2026-01) to Bob (2026-02) -
            # a hand-over event purely from re-imported data, never a
            # generated hand-over form. Each of them also gets one unrelated
            # device in the *other* period, so get_user_asset_history's own
            # period-diff (_group_by_period, unmodified/pre-existing) has a
            # comparison point to detect the PC as removed/added against -
            # a user whose entire history is a single row in a single
            # period has no later/earlier group to diff against at all.
            conn.execute(
                "INSERT INTO asset_items (batch_id, asset_key, branch_no, branch_dept, device_name, "
                "device_name_raw, model_device, serial_tag, status, user_id_norm, user_id_raw, full_name) VALUES "
                "(1, 'SN:SN1', '001', 'Hanoi', 'PC', 'PC', 'M1', 'SN1', 'IN USE', '1001', '1001', 'Alice'), "
                "(2, 'SN:SN1', '001', 'Hanoi', 'PC', 'PC', 'M1', 'SN1', 'IN USE', '1002', '1002', 'Bob'), "
                "(1, 'FB:MONITOR-BOB', '001', 'Hanoi', 'MONITOR', 'MONITOR', 'M2', '', 'IN USE', "
                "'1002', '1002', 'Bob'), "
                "(2, 'FB:PRINTER-ALICE', '001', 'Hanoi', 'PRINTER', 'PRINTER', 'M3', '', 'IN USE', "
                "'1001', '1001', 'Alice')"
            )
            conn.commit()
        finally:
            conn.close()
        create_account("historytester", "historypass1", admin_role_id)
        self.client = self.app.test_client()
        self.client.post("/login", data={"username": "historytester", "password": "historypass1"})

    def test_serial_search_shows_both_custody_segments(self):
        resp = self.client.get("/user-history/?q=SN1&search_by=serial")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Alice", body)
        self.assertIn("Bob", body)
        self.assertIn("2026-01", body)
        self.assertIn("2026-02", body)

    def test_serial_search_no_match(self):
        resp = self.client.get("/user-history/?q=NOSUCHSERIAL&search_by=serial")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No history found", resp.get_data(as_text=True))

    def test_user_search_annotates_removed_item_with_next_holder(self):
        resp = self.client.get("/user-history/?q=1001")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # Alice's history: PC was Removed going into 2026-02 - the page
        # should say it went to Bob, without a second search.
        self.assertIn("Bob", body)

    def test_user_search_annotates_added_item_with_previous_holder(self):
        resp = self.client.get("/user-history/?q=1002")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # Bob's history: PC was Added in 2026-02 - the page should say it
        # came from Alice.
        self.assertIn("Alice", body)


if __name__ == "__main__":
    unittest.main()
