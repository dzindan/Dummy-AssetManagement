# -*- coding: utf-8 -*-
"""Network Check targets: only plain IPs reach the PowerShell the scans
build. ipaddress takes an IPv6 scope id ("fe80::1%<anything>"), and the
uptime / hardware scripts put the IP inside '...': a scope id with a quote
ran any command - on the central server, as its service account."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import scanner  # noqa: E402

ATTACK = "fe80::1%x'; Write-Output PWNED; '"


class TargetTests(unittest.TestCase):
    def test_scope_ids_and_junk_are_refused(self):
        for bad in (ATTACK, "fe80::1%eth0", "10.0.0.1-5%x", "fe80::%x/64", "10.0.0.1;calc"):
            with self.subTest(target=bad), self.assertRaises(scanner.TargetParseError):
                scanner.parse_targets(bad)

    def test_plain_targets_still_parse(self):
        self.assertEqual(scanner.parse_targets("10.0.0.1 - 10.0.0.3, 10.0.1.0/30\nfe80::1"),
                         ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.1.1", "10.0.1.2", "fe80::1"])

    def test_scans_never_build_a_script_from_a_scoped_ip(self):
        with mock.patch.object(scanner, "_run") as run:
            self.assertFalse(scanner.get_uptime(ATTACK)["ok"])
            self.assertFalse(scanner.get_hardware(ATTACK)["system"]["ok"])
        run.assert_not_called()
        self.assertIsNone(scanner._validate_ip("10.20.30.40"))


if __name__ == "__main__":
    unittest.main()
