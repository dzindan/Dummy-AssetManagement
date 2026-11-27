"""Nothing leaves for the internet (app/netguard.py, 2026-11-27) - the same test in AM, MM and AAL."""

import importlib.util
import io
import os
import socket
import sys
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import netguard  # noqa: E402

PUBLIC = "93.184.216.34"


def resolves_to(*addresses):
    return mock.patch("app.netguard._real_getaddrinfo",
                      return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses])


class NetGuardTests(unittest.TestCase):
    def setUp(self):
        netguard._cache.clear()

    def test_host_of(self):
        self.assertEqual(netguard.host_of("https://cucm01:8443/realtimeservice2?wsdl"), "cucm01")
        self.assertEqual(netguard.host_of("mail.bank.local:25"), "mail.bank.local")
        self.assertEqual(netguard.host_of(r"tcp:DBSRV01\SQL2019,1433"), "DBSRV01")
        self.assertEqual(netguard.host_of("https://user:pw@10.1.1.1/x"), "10.1.1.1")

    def test_servers(self):
        i = netguard.is_internal
        self.assertTrue(i("10.1.2.3") and i("192.168.1.5") and i("172.20.0.1") and i("localhost") and i(""))
        self.assertFalse(i(PUBLIC) or i("8.8.8.8") or i(f"https://{PUBLIC}/x.wsdl"))
        with resolves_to("10.9.1.10"):
            self.assertTrue(i("MAIL01"))
        netguard._cache.clear()
        with resolves_to("10.9.1.10", PUBLIC):
            self.assertFalse(i("smtp.example.com"))
        netguard._cache.clear()
        with resolves_to(PUBLIC), mock.patch.dict(os.environ, {"USERDNSDOMAIN": "BANK.CORP"}):
            self.assertTrue(i("mail.bank.corp"))
            self.assertFalse(i("mail.bank.corp.evil.example"))
        with self.assertRaises(netguard.NotInternal):
            netguard.require_internal("8.8.8.8", "SMTP server")

    def test_sockets(self):
        netguard.block_outbound_sockets()
        for address in ((PUBLIC, 80), ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)):
            family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as s:
                with self.assertRaises(ConnectionRefusedError):
                    s.connect(address)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            with self.assertRaises(ConnectionRefusedError):
                u.sendto(b"x", ("8.8.8.8", 53))
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((PUBLIC, 80), timeout=1)
        with resolves_to(PUBLIC):
            netguard._cache.clear()
            with socket.socket() as s, self.assertRaises(ConnectionRefusedError):
                s.connect(("phone-home.example.com", 443))   # a name: resolved, refused
        # inside the bank's network / this PC: still allowed (nothing listens: an ordinary failure, not a refusal)
        with socket.socket() as s:
            s.settimeout(0.5)
            self.assertIsInstance(s.connect_ex(("127.0.0.1", 9)), int)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.connect(("10.255.255.255", 1))   # how the app finds its own LAN address


@unittest.skipUnless(importlib.util.find_spec("app.safezip"), "this app opens no uploaded workbook")
class ZipBombTests(unittest.TestCase):
    def test_refused_before_openpyxl_reads_it(self):
        import openpyxl

        from app import safezip
        safezip.install()
        buf = io.BytesIO()
        wb = openpyxl.Workbook()
        wb.active["A1"] = "x"
        wb.save(buf)
        with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/padding.xml", b"x" * (safezip.MAX_UNPACKED + 1))
        data = buf.getvalue()
        self.assertLess(len(data), 2 * 1024 * 1024)
        with self.assertRaises(safezip.TooLarge):
            openpyxl.load_workbook(io.BytesIO(data))
        from openpyxl import load_workbook   # the name other modules import
        with self.assertRaises(safezip.TooLarge):
            load_workbook(io.BytesIO(data), read_only=True)
        ok = io.BytesIO()
        openpyxl.Workbook().save(ok)
        ok.seek(0)
        self.assertIsNotNone(openpyxl.load_workbook(ok))   # an ordinary workbook still opens




class CucmSettingsTests(unittest.TestCase):
    def test_cucm_and_its_wsdl_stay_inside(self):
        from app.cucm import check_cucm_settings
        check_cucm_settings("10.1.1.5", "")
        check_cucm_settings("10.1.1.5", "https://10.1.1.5:8443/realtimeservice2/services/RISService70?wsdl")
        check_cucm_settings("10.1.1.5", r"C:\cucm\RISService70.wsdl")
        check_cucm_settings("10.1.1.5", "file:///C:/cucm/RISService70.wsdl")
        for ip, wsdl in ((PUBLIC, ""), ("10.1.1.5", f"https://{PUBLIC}/ris.wsdl"),
                         ("10.1.1.5", "http://10.1.1.5/ris.wsdl"), ("10.1.1.5", "https://10.1.1.6/ris.wsdl"),
                         ("10.1.1.5", "//evil/ris.wsdl"), ("10.1.1.5;x", "")):
            with self.assertRaises(ValueError, msg=(ip, wsdl)):
                check_cucm_settings(ip, wsdl)


if __name__ == "__main__":
    unittest.main()
