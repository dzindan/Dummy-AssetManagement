# -*- coding: utf-8 -*-
"""Operating System from Network Check scans (user, 2026-11-25): saved per PC
serial, shown / filtered / exported in Manage Assets."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, scanner  # noqa: E402
from app.asset_os import NO_OS, save_scan, usable_serial_key  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402
from app.queries import search_assets, search_cctv  # noqa: E402
from app.routes import network_check  # noqa: E402

WIN10 = {"name": "Windows 10 Pro", "version": "10.0.19045", "arch": "64-bit", "install_date": "2024-01-02"}
WIN11 = {"name": "Windows 11 Pro", "version": "10.0.22631", "arch": "64-bit", "install_date": ""}


class ScannerTests(unittest.TestCase):
    def _uptime(self, code, out):
        with mock.patch.object(scanner, "_run", return_value=(code, out)):
            return scanner.get_uptime("10.0.0.1")

    def test_reads_os_with_uptime(self):
        raw = {"boot": "2026-01-01T08:00:00", "name": "Microsoft Windows 10 Pro ", "version": "10.0.19045",
               "arch": "64-bit", "install_date": "2024-01-02", "hostname": "PC01"}
        result = self._uptime(0, json.dumps(raw) + "\r\nsome stderr text")
        self.assertTrue(result["ok"])
        self.assertEqual(result["boot_time"], "2026-01-01 08:00:00")
        self.assertEqual(result["os"], {**WIN10, "hostname": "PC01"})

    def test_failures(self):
        for code, out in ((1, "Access is denied"), (0, ""), (0, "garbage"), (0, "{}"), (0, '{"boot": null}')):
            result = self._uptime(code, out)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["os"])

    def test_hardware_reads_os_too(self):
        raw = {"system": {"Serial": "S1"}, "os": {"name": "Microsoft Windows 10 Pro", "version": "10.0.19045",
                                                  "arch": "64-bit", "install_date": "2024-01-02", "hostname": "H"}}
        with mock.patch.object(scanner, "_run", return_value=(0, json.dumps(raw))):
            self.assertEqual(scanner.get_hardware("10.0.0.1")["os"], {**WIN10, "hostname": "H"})
        with mock.patch.object(scanner, "_run", return_value=(0, json.dumps({"system": {}}))):
            self.assertIsNone(scanner.get_hardware("10.0.0.1")["os"])
        # the uptime call timed out: the hardware one's answer counts
        self.assertEqual(network_check._live_os({"uptime": {"ok": False, "os": None}, "hardware": {"os": WIN10}}), WIN10)

    def test_clean_name(self):
        self.assertEqual(scanner._clean_os_name("Microsoft® Windows Server® 2008 Standard "),
                         "Windows Server 2008 Standard")
        self.assertEqual(scanner._clean_os_name("Microsoft Windows Server (R) 2012"), "Windows Server 2012")
        self.assertEqual(scanner._clean_os_name(None), "")


class AssetOsTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_os_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at)"
                         " VALUES ('001', 'ZETA BRANCH', 'ZETA BRANCH', datetime('now'))")
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.commit()
        finally:
            conn.close()
        create_account("os", "ospass123", admin)
        self.client.post("/login", data={"username": "os", "password": "ospass123"})
        self._import("2026-03")

    def _import(self, period):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([None] * 7 + ["Branch/TO/Center Name:", "Zeta Branch"])
        ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        for i, (device, serial) in enumerate((("PC", "abc-1234"), ("PC", "XYZ9999"), ("LCD", "ABC1234")), start=1):
            ws.append([i, "ZETA BRANCH", device, "111", "A", "10.0.0.5", "DELL", serial, "USING LOCAL", ""])
        path = os.path.join(tempfile.mkdtemp(prefix="am_os_f_"), f"Zeta Branch {period}.xlsx")
        wb.save(path)
        import_asset_report(path, source_label=os.path.basename(path), period=period)

    def _scan(self, serial, os_info, hardware=True):
        def fake(targets, on_result, **kw):
            on_result({"ip": targets[0], "alive": True, "hostname": "PC01", "sessions": None,
                       "uptime": {"ok": bool(os_info), "os": os_info},
                       "hardware": {"system": {"ok": True, "items": [{"Serial": serial, "Model": "M"}]}}
                       if hardware else None})
        with mock.patch("app.routes.network_check.run_scan", side_effect=fake):
            sid = self.client.post("/network-check/scan", json={
                "mode": "range", "targets": "10.0.0.5", "include_hardware": hardware}).get_json()["scan_id"]
            end = time.time() + 10
            while network_check.running_scans() and time.time() < end:
                time.sleep(0.05)
        return sid

    def _os(self, **filters):
        conn = get_connection()
        try:
            rows, total = search_assets(conn, filters)
            self.assertEqual(len(rows), total)
            return {(r["device_name"], r["serial_tag"]): r["os_name"] for r in rows}
        finally:
            conn.close()

    def test_scan_shows_in_manage_assets_and_survives_import(self):
        sid = self._scan("ABC1234", WIN10)
        data = self.client.get(f"/network-check/scan/{sid}").get_json()
        self.assertEqual(data["results"][0]["compare"]["live_os"], "Windows 10 Pro (10.0.19045) 64-bit")
        ws = openpyxl.load_workbook(io.BytesIO(
            self.client.get(f"/network-check/scan/{sid}/export.xlsx").data)).active
        self.assertIn("Operating System", [c.value for c in ws[1]])
        self._import("2026-04")
        rows = self._os()
        by_key = {(d, s.replace("-", "").upper()): v for (d, s), v in rows.items()}
        self.assertEqual(by_key, {("PC", "ABC1234"): "Windows 10 Pro", ("PC", "XYZ9999"): None,
                                  ("LCD", "ABC1234"): None})
        page = self.client.get("/assets/?sort=-os").get_data(as_text=True)
        self.assertIn("Windows 10 Pro (10.0.19045)", page)
        self.assertIn("PC01", page)

    def test_filter(self):
        self._scan("ABC1234", WIN10)
        self.assertEqual(list(self._os(os=["Windows 10 Pro"]).values()), ["Windows 10 Pro"])
        self.assertEqual([k for k in self._os(os=[NO_OS])], [("PC", "XYZ9999")])
        self.assertEqual(len(self._os(os=["Windows 10 Pro", NO_OS])), 2)
        page = self.client.get("/assets/", query_string={"os": NO_OS}).get_data(as_text=True)
        self.assertIn("XYZ9999", page)
        conn = get_connection()
        try:
            search_cctv(conn, {})  # shares the query builder, without the OS join
        finally:
            conn.close()

    def test_exports(self):
        self._scan("ABC1234", WIN10)
        from tests.test_assetexplorer_export import read_export
        rows = read_export(self.client.get("/assets/export-assetexplorer").data)
        self.assertEqual(sorted(r.get("Operating System", "") for r in rows), ["", "", "Windows 10 Pro (10.0.19045)"])
        ws = openpyxl.load_workbook(io.BytesIO(self.client.get("/assets/export").data)).active
        head = [c.value for c in ws[1]]
        col = head.index("OS")
        self.assertIn("Windows 10 Pro (10.0.19045)", [r[col] for r in ws.iter_rows(min_row=2, values_only=True)])

    def test_rescan_updates_but_failure_keeps(self):
        self._scan("ABC1234", WIN10)
        self._scan("ABC1234", None)
        self.assertIn("Windows 10 Pro", self._os().values())
        self._scan("ABC1234", WIN11)
        self.assertIn("Windows 11 Pro", self._os().values())
        self.assertNotIn("Windows 10 Pro", self._os().values())

    def test_not_saved_without_serial(self):
        self._scan("ABC1234", WIN10, hardware=False)
        self._scan("To be filled by O.E.M.", WIN10)
        self.assertEqual(set(self._os().values()), {None})
        for junk in ("", "0", "00000000", "Default string", "None", "System Serial Number"):
            self.assertEqual(usable_serial_key(junk), "")
        self.assertEqual(usable_serial_key("abc-1234"), "ABC1234")
        conn = get_connection()
        try:
            self.assertFalse(save_scan(conn, "ABC1234", "10.0.0.5", "", {"name": ""}))
        finally:
            conn.close()

    def test_database_problem_does_not_end_the_scan(self):
        with mock.patch("app.routes.network_check.asset_os.save_scan", side_effect=RuntimeError("locked")), \
                self.assertLogs("app.routes.network_check", "ERROR"):
            sid = self._scan("ABC1234", WIN10)
        self.assertEqual(self.client.get(f"/network-check/scan/{sid}").get_json()["done"], 1)


if __name__ == "__main__":
    unittest.main()
