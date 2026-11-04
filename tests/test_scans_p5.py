# -*- coding: utf-8 -*-
"""P5: Network Check / CUCM scans run from the central server for several
people - each scan is its owner's, one Network Check scan per person and a
few at once, CUCM scans one at a time in the background (the page waits),
and every Update says who clicked it."""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, jobs  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.routes import network_check  # noqa: E402


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_p5_")
        self.app = create_app(csrf=False)
        admin = _role_id("Admin")
        self.clients = {}
        for name in ("ann", "bob", "cat", "dan"):
            create_account(name, name + "pass12", admin)
            c = self.app.test_client()
            c.post("/login?form=1", data={"username": name, "password": name + "pass12"})
            self.clients[name] = c
        self.gate = threading.Event()

    def tearDown(self):
        self.gate.set()


class NetworkCheckTests(Base):
    def fake_scan(self, targets, on_result, max_workers, include_hardware, should_stop):
        for ip in targets:
            on_result({"ip": ip, "alive": True, "hostname": "pc", "sessions": None,
                       "hardware": {"system": {"ok": True, "items": [{"Serial": "LIVE1", "Model": "M"}]}}})
        self.gate.wait(5)

    def start(self, who, targets="10.0.0.1"):
        return self.clients[who].post("/network-check/scan", json={"mode": "range", "targets": targets,
                                                                   "include_hardware": True})

    def test_scans_are_their_owners_and_limited(self):
        with mock.patch("app.routes.network_check.run_scan", side_effect=self.fake_scan):
            first = self.start("ann")
            self.assertEqual(first.status_code, 200)
            sid = first.get_json()["scan_id"]
            self.assertEqual(self.start("ann").status_code, 409)  # one at a time per person
            bob = self.clients["bob"]
            self.assertEqual(bob.get(f"/network-check/scan/{sid}").status_code, 404)
            self.assertEqual(bob.post(f"/network-check/scan/{sid}/stop").status_code, 404)
            self.assertEqual(bob.get(f"/network-check/scan/{sid}/export.xlsx").status_code, 404)
            self.assertEqual(bob.post(f"/network-check/scan/{sid}/apply", json={"updates": []}).status_code, 404)
            self.assertEqual(self.clients["ann"].get(f"/network-check/scan/{sid}").status_code, 200)
            self.assertEqual(self.start("bob").status_code, 200)
            self.assertEqual(self.start("cat").status_code, 200)
            self.assertEqual(self.start("dan").status_code, 429)  # MAX_RUNNING_SCANS = 3
            page = self.clients["dan"].get("/network-check/").get_data(as_text=True)
            self.assertNotIn(f'data-last-scan="{sid}"', page)
            self.assertIn(f'data-last-scan="{sid}"', self.clients["ann"].get("/network-check/").get_data(as_text=True))
            self.gate.set()
            self.assertTrue(_wait(lambda: not network_check.running_scans()))
        self.assertEqual(self.start("dan").status_code, 200)

    def test_update_records_who_applied_it(self):
        conn = get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")  # a bare row, no import batch behind it
            conn.execute("INSERT INTO asset_items (id, batch_id, asset_key, serial_tag) VALUES (900, 1, 'k', 'OLD1')")
            conn.commit()
        finally:
            conn.close()
        self.gate.set()
        with mock.patch("app.routes.network_check.run_scan", side_effect=self.fake_scan):
            sid = self.start("ann").get_json()["scan_id"]
            self.assertTrue(_wait(lambda: not network_check.running_scans()))
        resp = self.clients["ann"].post(f"/network-check/scan/{sid}/apply", json={"updates": [
            {"ip": "10.0.0.1", "field": "pc_serial", "asset_ids": [900], "value": "live1"}]})
        self.assertEqual(len(resp.get_json()["applied"]), 1)
        conn = get_connection()
        try:
            row = conn.execute("SELECT applied_by, new_value FROM logsdb.network_check_log").fetchone()
        finally:
            conn.close()
        self.assertEqual((row["applied_by"], row["new_value"]), ("ann", "LIVE1"))
        self.assertIn("<td>ann</td>", self.clients["bob"].get("/network-check/log").get_data(as_text=True))

    def test_finished_scans_are_trimmed(self):
        self.gate.set()
        with mock.patch("app.routes.network_check.run_scan", side_effect=self.fake_scan), \
                mock.patch.object(network_check, "KEEP_FINISHED_SCANS", 2):
            for _ in range(4):
                self.start("ann")
                self.assertTrue(_wait(lambda: not network_check.running_scans()))
            self.start("ann")
            self.assertTrue(_wait(lambda: not network_check.running_scans()))
        self.assertLessEqual(len(network_check._SCANS), 3)


class CucmScanTests(Base):
    PHONE = {"num": "1001", "ip": "10.1.1.5", "model": "Cisco 7942", "sn": "FCH1", "desc": "x"}

    def configure(self):
        from app.db import set_setting
        set_setting("cucm_ip", "10.0.0.9")
        set_setting("cucm_axluser", "axl")
        set_setting("cucm_axlpassword", "pw")

    def test_background_scan_waits_then_shows_result_to_its_owner(self):
        self.app.config["CUCM_SCAN_BACKGROUND"] = True
        self.configure()
        started = threading.Event()

        def slow_scan(**kw):
            started.set()
            self.gate.wait(5)
            return [dict(self.PHONE)]

        ann = self.clients["ann"]
        with mock.patch("app.routes.cucm_scan.scan_phones", side_effect=slow_scan):
            resp = ann.post("/cucm-scan/scan", data={"num": "1*", "model": "255"})
            self.assertEqual(resp.status_code, 302)
            loc = resp.headers["Location"]
            self.assertTrue(started.wait(5))
            self.assertIn("Scanning CUCM", ann.get(loc).get_data(as_text=True))
            second = self.clients["bob"].post("/cucm-scan/scan", data={"num": "2*", "model": "255"}).headers["Location"]
            waiting = self.clients["bob"].get(second).get_data(as_text=True)
            self.assertIn("Waiting to scan", waiting)
            self.assertIn("Now scanning: CUCM phones (number 1*, model Any) (ann", waiting)
            self.assertEqual(self.clients["bob"].get(loc).status_code, 404)
            page = ann.get("/cucm-scan/").get_data(as_text=True)
            self.assertIn("Now: CUCM phones", page)
            self.assertIn(f'<a href="{loc}">yours, open</a>', page)
            self.gate.set()
            job_id = int(loc.rsplit("/", 1)[1])
            self.assertTrue(_wait(lambda: jobs.cucm_runner.get(job_id).done))
            self.assertTrue(_wait(lambda: jobs.cucm_runner.get(job_id + 1).done))
        with mock.patch("app.is_server_mode", return_value=True):  # the templates' server_mode
            page = ann.get("/cucm-scan/").get_data(as_text=True)
        self.assertIn(f'Your last scan: <a href="{loc}">', page)  # the way back to a finished result
        html = ann.get(loc).get_data(as_text=True)
        self.assertIn("<strong>1</strong> phone(s) found", html)
        self.assertIn('data-scan-method="POST"', html)  # the page keeps this new result
        import re
        scan_id = re.search(r"/cucm-scan/scan/([0-9a-f]{32})/export", html).group(1)
        self.assertEqual(ann.get(f"/cucm-scan/scan/{scan_id}/export.xlsx").status_code, 200)
        self.assertEqual(self.clients["bob"].get(f"/cucm-scan/scan/{scan_id}/export.xlsx").status_code, 302)

    def test_desktop_scan_still_runs_in_the_request(self):
        self.configure()
        with mock.patch("app.routes.cucm_scan.scan_phones", return_value=[dict(self.PHONE)]):
            resp = self.clients["ann"].post("/cucm-scan/scan", data={"num": "1*", "model": "255"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<strong>1</strong> phone(s) found", resp.get_data(as_text=True))

    def test_a_crashing_scan_says_so(self):
        self.app.config["CUCM_SCAN_BACKGROUND"] = True
        self.configure()
        with mock.patch("app.routes.cucm_scan._with_import_crosscheck", side_effect=RuntimeError("db gone")), \
                mock.patch("app.routes.cucm_scan.scan_phones", return_value=[]):
            loc = self.clients["ann"].post("/cucm-scan/scan", data={"num": "1*", "model": "255"}).headers["Location"]
            job_id = int(loc.rsplit("/", 1)[1])
            self.assertTrue(_wait(lambda: jobs.cucm_runner.get(job_id).done))
        self.assertIn("Scan failed: RuntimeError: db gone", self.clients["ann"].get(loc).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
