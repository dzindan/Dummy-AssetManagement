# -*- coding: utf-8 -*-
"""OS from Active Directory (user, 2026-11-25): reading, matching computers to
PC serials, and which of scan / AD Manage Assets shows."""
import base64
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ad_os, create_app, jobs  # noqa: E402
from app.asset_os import NO_OS, save_scan  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.importer import import_asset_report  # noqa: E402
from app.queries import os_coverage, search_assets  # noqa: E402

TODAY = dt.date(2026, 11, 25)


def comp(name, os_name="Windows 10 Pro", logon="2026-11-20 08:00:00", **kw):
    return {"name": name, "dns": f"{name}.corp".lower(), "description": "", "serial": "", "os": os_name,
            "version": "10.0.19045", "uac": "4096", "logon": logon, "ip": "", **kw}


class ReadTests(unittest.TestCase):
    def _answer(self, prefix, text):
        return 0, prefix + base64.b64encode(text.encode("utf-8")).decode() + "\r\nnoise on stderr"

    def test_parse(self):
        ft = str(int((dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
                      - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 10 ** 7))
        text = "\n".join([
            f"PC1\tpc1.corp\tPhòng A\t\tMicrosoft Windows 10 Pro\t10.0 (19045)\t4096\t{ft}\t10.0.0.9",
            "PC2\t\t\t\tWindows 11 Pro\t10.0 (22631)\t4098\t0\t",  # disabled
            "broken line",
            "PC3\t\t\t\tWindows 7\t6.1 (7601)\t4096\t\t"])
        with mock.patch.object(ad_os, "_run", return_value=self._answer("B64:", text)):
            got = ad_os.read_computers()
        self.assertEqual([c["name"] for c in got], ["PC1", "PC3"])
        self.assertEqual((got[0]["os"], got[0]["version"], got[0]["description"], got[0]["logon"][:7]),
                         ("Windows 10 Pro", "10.0.19045", "Phòng A", "2026-11"))
        self.assertEqual(got[1]["logon"], "")
        self.assertEqual(got[0]["ip"], "10.0.0.9")

    def test_errors(self):
        with mock.patch.object(ad_os, "_run", return_value=self._answer("ERR:", "no domain")):
            self.assertRaisesRegex(ad_os.AdReadError, "no domain", ad_os.read_computers)
        with mock.patch.object(ad_os, "_run", return_value=(-1, "Timeout")):
            self.assertRaisesRegex(ad_os.AdReadError, "Timeout", ad_os.read_computers)
        with mock.patch.object(ad_os, "_run") as run:
            for bad in ("a'; calc; '", "corp\\x", "a b", "$(x)", "-x"):
                self.assertRaises(ad_os.AdReadError, ad_os.read_computers, bad)
            run.assert_not_called()


class MatchTests(unittest.TestCase):
    def _how(self, *args, **kw):
        kw.setdefault("resolve", lambda n: set())
        return {k: (c["name"], how) for k, (c, how) in ad_os.match(*args, today=TODAY, **kw).items()}

    def test_hostname_then_serial(self):
        pcs = [("5CD1234XYZ", ""), ("ABC1234567", ""), ("SN7654321", ""), ("SHORT1", "")]
        got = self._how([comp("PC1"), comp("SHB5CD1234XYZ"), comp("PC3", description="s/n: ABC1234567"),
                         comp("PC4", serial="sn-7654321"), comp("PC5", description="SHORT1")],
                        {"PC1": "HOSTKEY1"}, pcs)
        self.assertEqual(got, {"HOSTKEY1": ("PC1", "hostname"), "5CD1234XYZ": ("SHB5CD1234XYZ", "serial"),
                               "ABC1234567": ("PC3", "serial"), "SN7654321": ("PC4", "serial")})

    def test_ambiguous_and_duplicates(self):
        pcs = [("ABC1234567", ""), ("XYZ1234567", "")]
        self.assertEqual(self._how([comp("P", description="ABC1234567 XYZ1234567")], {}, pcs), {})
        got = self._how([comp("OLD", logon="2025-01-01 00:00:00", description="ABC1234567"),
                         comp("NEW", description="ABC1234567")], {}, pcs)
        self.assertEqual(got, {"ABC1234567": ("NEW", "serial")})

    def test_by_ip(self):
        dns = {"a.corp": {"10.0.0.1"}, "b.corp": {"10.0.0.2"}, "c.corp": {"10.0.0.2"}, "d.corp": {"10.0.0.3"},
               "old.corp": {"10.0.0.4"}, "e.corp": {"10.0.0.5"}}
        pcs = [("KEY1", "10.0.0.1"), ("KEY2", "10.0.0.2"), ("KEY3", "10.0.0.3"), ("KEY3B", "10.0.0.3"),
               ("KEY4", "10.0.0.4"), ("", "10.0.0.5"), ("KEY6", "not an ip")]
        comps = [comp(n) for n in "ABCDE"] + [comp("OLD", logon="2026-01-01 00:00:00")]
        resolve = lambda n: dns.get(n, set())  # noqa: E731
        self.assertEqual(self._how(comps, {}, pcs, by_ip=True, resolve=resolve), {"KEY1": ("A", "ip")})
        self.assertEqual(self._how(comps, {}, pcs, by_ip=False, resolve=resolve), {})
        # a computer known by name standing at that address: not free for another
        self.assertEqual(self._how([comp("B"), comp("C")], {"B": "HKEY"}, pcs, by_ip=True, resolve=resolve),
                         {"HKEY": ("B", "hostname")})

    def test_ip_from_ad_and_order(self):
        pcs = [("ABC1234567", "10.0.0.1"), ("XYZ1234567", "10.0.0.2")]
        dns = lambda n: {"10.0.0.2"}  # noqa: E731
        # AD's own address beats DNS; IP is tried before a serial in the text
        self.assertEqual(self._how([comp("P", ip="10.0.0.1")], {}, pcs, resolve=dns), {"ABC1234567": ("P", "ip")})
        self.assertEqual(self._how([comp("P", description="Tang 2 - 10.0.0.1.")], {}, pcs, resolve=dns),
                         {"ABC1234567": ("P", "ip")})
        # two addresses, two PCs: no telling which
        self.assertEqual(self._how([comp("P")], {}, pcs, resolve=lambda n: {"10.0.0.1", "10.0.0.2"}), {})
        self.assertEqual(self._how([comp("P", description="XYZ1234567", ip="10.0.0.1")], {}, pcs),
                         {"ABC1234567": ("P", "ip")})
        self.assertEqual(self._how([comp("P", ip="10.0.0.1")], {"P": "XYZ1234567"}, pcs),
                         {"XYZ1234567": ("P", "hostname")})
        self.assertEqual(ad_os._ips_in("a 10.0.0.1, 999.1.1.1 v1.2.3.4.5"), {"10.0.0.1"})


class PageTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_ados_")
        self.app = create_app(csrf=False)
        self.app.config["AD_READ_BACKGROUND"] = False
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            conn.execute("INSERT INTO branches (branch_no, local_name, eng_name, updated_at)"
                         " VALUES ('001', 'ZETA BRANCH', 'ZETA BRANCH', datetime('now'))")
            roles = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM roles")}
            conn.commit()
        finally:
            conn.close()
        create_account("ad", "adpass1234", roles["Admin"])
        create_account("view", "viewpass12", roles["Viewer"])
        self.client.post("/login", data={"username": "ad", "password": "adpass1234"})
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([None] * 7 + ["Branch/TO/Center Name:", "Zeta Branch"])
        ws.append(["NO", "BRANCH / DEPT", "DEVICE NAME", "USER ID", "FULL NAME", "IP", "MODEL DEVICE",
                   "SERIAL/ SERVICE TAG", "STATUS", "REMARK"])
        for i, serial in enumerate(("ABC1234567", "XYZ1234567"), start=1):
            ws.append([i, "ZETA BRANCH", "PC", "111", "A", f"10.0.0.{i}", "DELL", serial, "USING LOCAL", ""])
        path = os.path.join(tempfile.mkdtemp(prefix="am_ados_f_"), "Zeta Branch 2026-03.xlsx")
        wb.save(path)
        import_asset_report(path, source_label=os.path.basename(path), period="2026-03")

    def test_not_in_use_pcs_match_neither_name_nor_ip(self):
        self._scan("ABC1234567", "P1", "2026-11-10 08:00:00")
        conn = get_connection()
        try:
            conn.execute("UPDATE asset_items SET status = 'WAREHOUSE'")
            conn.commit()
            self.assertEqual(ad_os.scanned_hosts(conn), {})
            self.assertEqual({ip for _k, ip in ad_os.current_pcs(conn)}, {""})
        finally:
            conn.close()
        self.assertIn("none could be matched", self._read([comp("P1", "Windows 11 Pro", ip="10.0.0.1")]))
        self.assertEqual(self._shown()["ABC1234567"], ("Windows 10 Pro", "Scan"))

    def test_serial_text_never_replaces_a_scan_and_long_names(self):
        self._scan("ABC1234567", "VERYLONGHOSTNAM", "2026-11-10 08:00:00")
        self._read([comp("X", "Windows 11 Pro", description="ABC1234567")])
        self.assertEqual(self._shown()["ABC1234567"], ("Windows 10 Pro", "Scan"))
        self._read([comp("VERYLONGHOSTNAME01", "Windows 11 Pro")])
        self.assertEqual(self._shown()["ABC1234567"], ("Windows 11 Pro", "AD (by hostname)"))

    def test_nothing_matched_keeps_the_last(self):
        self._read([comp("P1", description="ABC1234567")])
        self.assertIn("The last read was kept", self._read([comp("ZZ")]))
        self.assertEqual(self._shown()["ABC1234567"][0], "Windows 10 Pro")

    def _read(self, computers, **form):
        form.setdefault("by_ip", "1")
        kw = {"side_effect": computers} if isinstance(computers, Exception) else {"return_value": computers}
        with mock.patch.object(ad_os, "read_computers", **kw), \
                mock.patch.object(ad_os.socket, "getaddrinfo", side_effect=OSError):
            return self.client.post("/network-check/ad-os/read", data=form, follow_redirects=True).get_data(as_text=True)

    def _shown(self):
        conn = get_connection()
        try:
            rows, _ = search_assets(conn, {}, sort="os")
            return {r["serial_tag"]: (r["os_name"], r["os_source"]) for r in rows}
        finally:
            conn.close()

    def _scan(self, serial, hostname, scanned_at):
        conn = get_connection()
        try:
            save_scan(conn, serial, "10.0.0.1", "dns-says-otherwise",
                      {"name": "Windows 10 Pro", "version": "10.0.19045", "hostname": hostname})
            conn.execute("UPDATE asset_os SET scanned_at = ?", (scanned_at,))
            conn.commit()
        finally:
            conn.close()

    def test_fills_gap_and_replaces(self):
        page = self._read([comp("P1", description="ABC1234567")])
        self.assertIn("matched 1 PCs", page)
        self.assertEqual(self._shown(), {"ABC1234567": ("Windows 10 Pro", "AD (by serial)"),
                                         "XYZ1234567": (None, None)})
        conn = get_connection()
        try:
            self.assertEqual(dict(os_coverage(conn)), {"Scan": 0, "AD (by hostname)": 0, "AD (by serial)": 1, "AD (by IP)": 0,
                                                       NO_OS: 1})
        finally:
            conn.close()
        html = self.client.get("/assets/", query_string={"os": "Windows 10 Pro"}).get_data(as_text=True)
        self.assertIn("AD (by serial)", html)
        self.assertNotIn('data-value="XYZ1234567"', html)
        self._read([comp("P2", description="XYZ1234567")])
        self.assertEqual(self._shown()["ABC1234567"], (None, None))

    def test_by_ip_never_replaces_a_scan(self):
        self._scan("ABC1234567", "OTHER", "2026-11-10 08:00:00")
        self._read([comp("P1", "Windows 11 Pro", ip="10.0.0.1"), comp("P2", "Windows 11 Pro", ip="10.0.0.2")])
        self.assertEqual(self._shown(), {"ABC1234567": ("Windows 10 Pro", "Scan"),
                                         "XYZ1234567": ("Windows 11 Pro", "AD (by IP)")})

    def test_failed_or_empty_read_keeps_the_last(self):
        self._read([comp("P1", description="ABC1234567")])
        self.assertIn("no computers", self._read([]))
        self.assertIn("Could not read AD", self._read(ad_os.AdReadError("down")))
        self.assertEqual(self._shown()["ABC1234567"][0], "Windows 10 Pro")

    def test_scan_wins_unless_ad_is_newer(self):
        self._scan("ABC1234567", "P1", "2026-11-10 08:00:00")
        new = comp("P1", "Windows 11 Pro", logon="2026-11-20 08:00:00")
        self._read([comp("P1", "Windows 11 Pro", logon="2026-11-01 08:00:00")])
        self.assertEqual(self._shown()["ABC1234567"], ("Windows 10 Pro", "Scan"))
        self._read([new])
        self.assertEqual(self._shown()["ABC1234567"], ("Windows 11 Pro", "AD (by hostname)"))

    def test_permission_and_owner(self):
        self._read([comp("P1")])
        job_id = jobs.ad_runner.latest_done("ad").id
        viewer = self.app.test_client()
        viewer.post("/login", data={"username": "view", "password": "viewpass12"})
        with mock.patch.object(ad_os, "read_computers") as read:
            viewer.post("/network-check/ad-os/read")
            read.assert_not_called()
        self.assertNotEqual(viewer.get(f"/network-check/ad-os/jobs/{job_id}").status_code, 200)
        self.assertEqual(viewer.get("/network-check/ad-os").status_code, 200)
        self.assertIn("OS from AD", self.client.get("/network-check/").get_data(as_text=True))

    def test_background(self):
        self.app.config["AD_READ_BACKGROUND"] = True
        with mock.patch.object(ad_os, "read_computers", return_value=[comp("P1", description="ABC1234567")]):
            resp = self.client.post("/network-check/ad-os/read")
            job_id = int(resp.headers["Location"].rsplit("/", 1)[1])
            end = dt.datetime.now() + dt.timedelta(seconds=10)
            while not jobs.ad_runner.get(job_id).done and dt.datetime.now() < end:
                self.client.get(f"/network-check/ad-os/jobs/{job_id}/status")
        self.assertIn("matched 1 PCs", self.client.get(resp.headers["Location"],
                                                       follow_redirects=True).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
