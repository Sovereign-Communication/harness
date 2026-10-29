"""Hermetic tests for the browser evidence egress boundary."""
import ipaddress
import socket
import socketserver
import threading
import unittest
import urllib.parse

from harness.site_evidence import _BrowserEgressProxy


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.accepted += 1
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.accepted = 0
        super().__init__(("127.0.0.1", 0), _EchoHandler)


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _gai(host, port, address):
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    sockaddr = ((str(parsed), port, 0, 0) if parsed.version == 6
                else (str(parsed), port))
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def _connect(proxy_url, authority, payload=b""):
    proxy = urllib.parse.urlsplit(proxy_url)
    with socket.create_connection((proxy.hostname, proxy.port), timeout=2) as client:
        client.settimeout(2)
        client.sendall((f"CONNECT {authority} HTTP/1.1\r\n"
                        f"Host: {authority}\r\n\r\n").encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(client.recv(4096))
        status = int(response.split(b"\r\n", 1)[0].split()[1])
        if status != 200 or not payload:
            return status, b""
        client.sendall(payload)
        echoed = bytearray()
        while len(echoed) < len(payload):
            echoed.extend(client.recv(len(payload) - len(echoed)))
        return status, bytes(echoed)


class BrowserEgressProxyTests(unittest.TestCase):
    def test_public_https_tunnel_succeeds_through_a_local_test_server(self):
        public_server = _EchoServer()
        public_thread = _serve(public_server)
        seen = []

        def resolver(host, port):
            self.assertEqual(host, "public.example")
            seen.append((host, port))
            return _gai(host, port, "93.184.216.34")

        def connector(address, port, timeout):
            self.assertEqual(address, "93.184.216.34")
            self.assertEqual(port, 443)
            return socket.create_connection(
                public_server.server_address, timeout=timeout)

        try:
            with _BrowserEgressProxy(
                    resolve_fn=resolver, connect_fn=connector) as proxy:
                status, echoed = _connect(
                    proxy.url, "public.example:443", b"hermetic https tunnel")
            self.assertEqual(status, 200)
            self.assertEqual(echoed, b"hermetic https tunnel")
            self.assertEqual(seen, [("public.example", 443)])
            self.assertEqual(public_server.accepted, 1)
        finally:
            _stop(public_server, public_thread)

    def test_private_iframe_subresource_loopback_link_local_and_metadata_refuse(self):
        private_server = _EchoServer()
        private_thread = _serve(private_server)
        resolved = {
            "private-frame.example": "10.20.30.40",
            "private-image.example": "192.168.4.5",
            "metadata.example": "169.254.169.254",
            "loopback.example": "127.0.0.1",
            "fe80::1": "fe80::1",
        }
        connect_attempts = []

        def resolver(host, port):
            return _gai(host, port, resolved[host])

        def connector(address, port, timeout):
            connect_attempts.append((address, port))
            return socket.create_connection(
                private_server.server_address, timeout=timeout)

        try:
            with _BrowserEgressProxy(
                    resolve_fn=resolver, connect_fn=connector) as proxy:
                for authority in (
                    "private-frame.example:443",
                    "private-image.example:443",
                    "metadata.example:443",
                    "loopback.example:443",
                    "[fe80::1]:443",
                ):
                    with self.subTest(authority=authority):
                        status, _echoed = _connect(proxy.url, authority)
                        self.assertEqual(status, 403)
            self.assertEqual(connect_attempts, [])
            self.assertEqual(private_server.accepted, 0)
        finally:
            _stop(private_server, private_thread)

    def test_public_redirect_to_loopback_is_denied_before_local_connect(self):
        public_server = _EchoServer()
        private_server = _EchoServer()
        public_thread = _serve(public_server)
        private_thread = _serve(private_server)
        connect_attempts = []

        def resolver(host, port):
            address = ("93.184.216.34" if host == "public.example" else host)
            return _gai(host, port, address)

        def connector(address, port, timeout):
            connect_attempts.append(address)
            target = (public_server.server_address
                      if address == "93.184.216.34"
                      else private_server.server_address)
            return socket.create_connection(target, timeout=timeout)

        try:
            with _BrowserEgressProxy(
                    resolve_fn=resolver, connect_fn=connector) as proxy:
                status, echoed = _connect(
                    proxy.url, "public.example:443", b"first page")
                self.assertEqual((status, echoed), (200, b"first page"))
                redirect_status, _echoed = _connect(
                    proxy.url, "127.0.0.1:443")
            self.assertEqual(redirect_status, 403)
            self.assertEqual(connect_attempts, ["93.184.216.34"])
            self.assertEqual(private_server.accepted, 0)
        finally:
            _stop(public_server, public_thread)
            _stop(private_server, private_thread)

    def test_dns_rebinding_is_rechecked_and_second_connection_is_refused(self):
        public_server = _EchoServer()
        private_server = _EchoServer()
        public_thread = _serve(public_server)
        private_thread = _serve(private_server)
        answers = iter(("93.184.216.34", "127.0.0.1"))
        resolved = []
        connect_attempts = []

        def resolver(host, port):
            address = next(answers)
            resolved.append((host, address))
            return _gai(host, port, address)

        def connector(address, port, timeout):
            connect_attempts.append(address)
            target = (public_server.server_address
                      if address == "93.184.216.34"
                      else private_server.server_address)
            return socket.create_connection(target, timeout=timeout)

        try:
            with _BrowserEgressProxy(
                    resolve_fn=resolver, connect_fn=connector) as proxy:
                first, echoed = _connect(
                    proxy.url, "rebind.example:443", b"pinned")
                second, _echoed = _connect(proxy.url, "rebind.example:443")
            self.assertEqual((first, echoed), (200, b"pinned"))
            self.assertEqual(second, 403)
            self.assertEqual(resolved, [
                ("rebind.example", "93.184.216.34"),
                ("rebind.example", "127.0.0.1"),
            ])
            self.assertEqual(connect_attempts, ["93.184.216.34"])
            self.assertEqual(private_server.accepted, 0)
        finally:
            _stop(public_server, public_thread)
            _stop(private_server, private_thread)


if __name__ == "__main__":
    unittest.main()
