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

    def test_loopback_is_ip_literals_only_not_names_that_look_like_it(self):
        ok = ("127.0.0.1", "127.9.9.9", "::1", "[::1]", "localhost",
              "app.localhost", "::ffff:127.0.0.1", "0.0.0.0", "")
        bad = ("127.evil.example", "127.0.0.1.evil.example", "127.",
               "8.8.8.8", "203.0.113.1", "example.com")
        for h in ok:
            self.assertTrue(tests._is_loopback_host(h), h)
        for h in bad:
            self.assertFalse(tests._is_loopback_host(h), h)

    @unittest.skipIf(os.environ.get("HARNESS_TEST_ALLOW_NETWORK") == "1",
                     "api key: network guard disabled for a live probe")
    def test_the_guard_also_covers_gethostbyname_and_gethostbyaddr(self):
        before = len(tests.NETWORK_VIOLATIONS)
        try:
            with self.assertRaises(OSError):
                socket.gethostbyname("127.evil.example")
            with self.assertRaises(OSError):
                socket.gethostbyname_ex("not-a-real-host.example")
            with self.assertRaises(OSError):
                socket.gethostbyaddr("203.0.113.1")
            self.assertEqual(len(tests.NETWORK_VIOLATIONS), before + 3)
            self.assertEqual(socket.gethostbyname("localhost").split(".")[0], "127")
        finally:
            del tests.NETWORK_VIOLATIONS[before:]

    def test_violations_are_reported_at_exit(self):
        import io
        from contextlib import redirect_stderr
        before = len(tests.NETWORK_VIOLATIONS)
        tests.NETWORK_VIOLATIONS.append("connect ('x', 1)")
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                tests._report_violations_at_exit()
            self.assertIn("NETWORK_VIOLATIONS", buf.getvalue())
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
