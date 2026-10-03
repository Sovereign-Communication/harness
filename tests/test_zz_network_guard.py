"""The suite opened no real (non-loopback) network connection.

Named ``test_zz`` so discovery runs it last: it reads what the guard in
``tests/__init__.py`` recorded over the whole run. A recorded attempt means a
test reached the network instead of patching its seam -- fix that test.
"""
import os
import socket
import unittest

import tests


class NetworkGuardTests(unittest.TestCase):
    @unittest.skipIf(os.environ.get("HARNESS_TEST_ALLOW_NETWORK") == "1",
                     "api key: network guard disabled for a live probe")
    def test_the_guard_refuses_and_records_a_real_connect(self):
        before = len(tests.NETWORK_VIOLATIONS)
        try:
            with self.assertRaises(OSError):
                socket.create_connection(("203.0.113.1", 9), timeout=1)
            with self.assertRaises(OSError):
                socket.getaddrinfo("not-a-real-host.example", 443)
            self.assertEqual(len(tests.NETWORK_VIOLATIONS), before + 2)
        finally:
            del tests.NETWORK_VIOLATIONS[before:]

    @unittest.skipIf(os.environ.get("HARNESS_TEST_ALLOW_NETWORK") == "1",
                     "api key: network guard disabled for a live probe")
    def test_no_test_reached_the_real_network(self):
        self.assertEqual(
            tests.NETWORK_VIOLATIONS, [],
            "these tests opened real network connections instead of patching "
            "their seam: " + "; ".join(tests.NETWORK_VIOLATIONS))


if __name__ == "__main__":
    unittest.main()
