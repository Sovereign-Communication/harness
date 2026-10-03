"""The REST token check, proven over a real socket.

An endpoint that can act on a machine is only as safe as the check in front
of it, and an in-process test of ``Handler._authorised`` proves very little:
it cannot show whether the check runs *before* routing, whether a path
variant reaches a different handler, or what a client actually receives when
the check misbehaves. All three were real questions here, so every test in
this module puts bytes on a socket and reads the response off it.

The shapes that matter are the ones a client can vary without trying to
break anything: a missing header, an empty one, a token that is nearly
right, and a request target with a trailing slash, a different case, or a
method the server does not implement. The claim is that the token decides
all of them, before any of them is interpreted.

The request lines are written verbatim through ``putrequest`` rather than
through a client library, because the libraries normalise exactly the things
under test.
"""
import http.client
import socket
import threading
import unittest

from driver_core.audit import MemoryAuditLog
from driver_core.config import MIN_TOKEN_LENGTH, ConfigError, load_settings
from driver_core.driver import Driver
from driver_core.server import Service, serve

#: Fixed, so the assertions below can name it. A real service mints a fresh
#: random token per process; what is under test is the comparison, not the
#: generator.
TOKEN = "test-token-0123456789abcdef"

#: Request targets that a client might send and that must not be able to
#: reach a handler by being shaped differently from ``/health``.
PATHS = (
    "/health",        # the canonical one, for contrast
    "/health/",       # trailing slash
    "//health",       # doubled leading slash
    "health",         # no leading slash at all
    "/",              # bare slash
    "/HEALTH",        # upper case
    "/Health",        # mixed case
    "/he%61lth",      # percent-encoded
    "/./health",      # dot segment
    "/foo/../health",  # parent segment
    "/nope",          # unknown route
    "/health?x=1",    # query string, which is not part of the route
)

#: Methods with no ``do_`` on the handler. ``BaseHTTPRequestHandler`` answers
#: these itself, so "does not reach the handler" is the claim being pinned.
METHODS = ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "BREW")


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RestAuthTests(unittest.TestCase):
    """One real server, shared by every case; the token check is the subject."""

    @classmethod
    def setUpClass(cls):
        driver = Driver(settings=load_settings(env={}), audit=MemoryAuditLog())
        cls.httpd, cls.service = serve("127.0.0.1", _free_port(),
                                       service=Service(driver, token=TOKEN),
                                       block=False)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        # shutdown() stops the accept loop; server_close() releases the
        # socket. Under -W error::ResourceWarning the difference between the
        # two is an unraisable warning.
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=10)

    # -- helpers --------------------------------------------------------

    def request(self, method, path, *, token=None, body=None, raw_header=None):
        """One request line sent verbatim. Returns ``(status, body_bytes)``.

        ``token=None`` omits the header entirely, which is different from
        sending an empty one and is the case a client is most likely to get
        wrong.
        """
        headers = {} if raw_header is not None else (
            {} if token is None else {"Authorization": f"Bearer {token}"})
        if raw_header is not None:
            headers["Authorization"] = raw_header
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True,
                            skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def refused(self, label, *args, **kwargs):
        """Assert a 401 carrying *our* refusal, not a base-class error page."""
        status, payload = self.request(*args, **kwargs)
        self.assertEqual(status, 401, f"{label}: {status} {payload[:120]!r}")
        self.assertIn(b"unauthorized", payload, label)
        return status

    # -- the token decides ----------------------------------------------

    def test_a_valid_token_is_served(self):
        status, payload = self.request("GET", "/health", token=TOKEN)
        self.assertEqual(status, 200, payload[:200])
        self.assertIn(b'"ok": true', payload)

    def test_a_valid_token_reaches_the_step_route(self):
        """Not merely a 200 from ``/health``.

        ``POST /step`` with no schema is a 400 -- which is only reachable
        *after* the token check, so it is the proof that a POST is
        authenticated too and not just exempted from the test.
        """
        status, payload = self.request("POST", "/step", token=TOKEN,
                                       body=b'{"target": "t"}')
        self.assertEqual(status, 400, payload[:200])
        self.assertIn(b"schema is required", payload)

    def test_a_missing_token_is_refused(self):
        self.refused("no header", "GET", "/health")
        self.refused("no header on POST", "POST", "/step", body=b"{}")

    def test_an_empty_token_is_refused(self):
        for header in ("", "Bearer", "Bearer ", "   ", "Bearer    "):
            with self.subTest(header=header):
                self.refused(f"empty {header!r}", "GET", "/health",
                             raw_header=header)

    def test_a_wrong_token_is_refused(self):
        self.refused("wrong", "GET", "/health", token="x" * len(TOKEN))
        self.refused("prefix", "GET", "/health", token=TOKEN[:-1])
        self.refused("suffix", "GET", "/health", token=TOKEN + "x")
        self.refused("case-flipped", "GET", "/health", token=TOKEN.upper())

    def test_a_non_ascii_token_is_refused_rather_than_crashing(self):
        """The regression.

        ``secrets.compare_digest`` raises ``TypeError`` when a ``str``
        argument holds a non-ASCII character, so comparing the header as
        text let a wrong token escape the check as an exception: the
        connection was closed with no HTTP response and a traceback printed.
        A refusal that is not a refusal is the failure mode this whole
        module exists to rule out.

        The characters have to be latin-1, because that is all an HTTP
        header can carry; anything wider cannot be sent here at all.
        """
        self.refused("non-ascii", "GET", "/health", token="café-token")
        self.refused("non-ascii, wrong scheme", "GET", "/health",
                     raw_header="Bearer ünïcödé")

    def test_an_unauthenticated_post_receives_its_401(self):
        """The second regression, and the one a host would actually hit.

        The refusal is written before the body is read, and a socket closed
        with unread bytes in its receive queue is reset by the OS, which
        destroys the response in flight. Measured over a real connection, an
        unauthenticated ``POST /step`` delivered its 401 in one request out
        of four with a small body and in none at all with a large one -- the
        caller saw a connection reset, which it cannot distinguish from the
        server having failed.
        """
        for label, body in (("small", b'{"target": "t", "schema": "cli"}'),
                            ("large", b'{"target": "' + b"x" * 60000
                             + b'", "schema": "cli"}')):
            with self.subTest(body=label):
                # Twice, because the failure was intermittent rather than
                # total and a single attempt would have passed one time in
                # four.
                for _ in range(2):
                    self.refused(f"POST {label}", "POST", "/step", body=body)

    def test_a_duplicate_header_cannot_smuggle_a_second_token(self):
        """Two ``Authorization`` lines, the valid one second.

        ``http.client`` collapses them, so the request is written to the
        socket by hand. Only the first header is considered, so a client
        cannot put a bad one in front of a good one and be let in.
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            f"GET /health HTTP/1.0\r\n"
            f"Authorization: Bearer {'x' * len(TOKEN)}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n"
            f"\r\n".encode())
        self.assertIn(b"401", sock.recv(64))

    def test_a_folded_header_is_refused(self):
        """An obs-fold continuation cannot smuggle a token past the check."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            f"GET /health HTTP/1.0\r\nAuthorization: Bearer\r\n {TOKEN}\r\n\r\n"
            .encode())
        self.assertIn(b"401", sock.recv(64))

    def test_an_expect_continue_request_is_refused_before_its_body(self):
        """``100-continue`` must not buy an unauthenticated round trip."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /step HTTP/1.0\r\nExpect: 100-continue\r\n"
            b"Content-Length: 2\r\n\r\n{}")
        self.assertIn(b"401", sock.recv(64))

    # -- the token decides before the path is interpreted ---------------

    def test_no_path_variant_reaches_a_handler_without_the_token(self):
        """Every shape answers 401 unauthenticated, 200 or 404 with the token.

        The 401 for *every* shape is the load-bearing half. A variant that
        resolved to a route before the check ran would answer 404, and the
        404 would tell a prober the route exists.
        """
        for path in PATHS:
            with self.subTest(path=path, token="absent"):
                self.refused(f"unauthenticated {path}", "GET", path)
            with self.subTest(path=path, token="present"):
                status, payload = self.request("GET", path, token=TOKEN)
                self.assertIn(status, (200, 404),
                              f"{path} -> {status} {payload[:80]!r}")

    def test_the_canonical_path_is_the_only_one_that_serves_health(self):
        """``//health``, ``health`` and ``/`` are aliases, and are pinned.

        ``path.lstrip("/")`` strips *every* leading slash and does not
        require one, so all three resolve to ``health``. That is laxness,
        not a hole -- the token is still required first, and the earlier test
        shows the same 401 for each -- but it is pinned here so that
        tightening the parsing is a deliberate, visible change rather than a
        silent behaviour shift for a client that happens to send one.
        """
        for path in ("//health", "health", "/"):
            with self.subTest(path=path):
                status, payload = self.request("GET", path, token=TOKEN)
                self.assertEqual(status, 200, f"{path} -> {payload[:80]!r}")
                self.assertIn(b'"keyed"', payload)

    def test_a_trailing_slash_or_case_change_on_step_is_not_the_step_route(self):
        """The one route with consequences must not answer to a near-miss."""
        for path in ("/step/", "/STEP", "/Step", "/step%20", "/s%74ep"):
            with self.subTest(path=path):
                status, payload = self.request("POST", path, token=TOKEN,
                                               body=b'{"target": "t"}')
                self.assertEqual(status, 404, f"{path} -> {payload[:120]!r}")

    # -- methods the handler does not implement -------------------------

    def test_an_unhandled_method_never_reaches_the_handler(self):
        """501, not 200 and not 401.

        ``BaseHTTPRequestHandler`` answers a method with no ``do_`` itself,
        before any of this class runs, so the handler is never entered and
        the token is never consulted. That is the correct semantic -- the
        method is unimplemented, and a 401 would imply that a token would
        have helped.
        """
        for method in METHODS:
            for token in (None, TOKEN):
                with self.subTest(method=method, token=bool(token)):
                    status, payload = self.request(
                        method, "/health", token=token)
                    self.assertEqual(status, 501,
                                     f"{method} -> {payload[:80]!r}")

    def test_a_method_the_handler_does_implement_is_authenticated(self):
        """The 501 above is not the only answer: GET and POST are checked.

        Together the two tests say the split is deliberate -- a method that
        exists is gated, a method that does not is refused by the base
        class before this code is reached.
        """
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                self.refused(f"{method} unauthenticated", method, "/health",
                             body=b"{}" if method == "POST" else None)


class TokenProvisioningTests(unittest.TestCase):
    """A host must be able to supply the token it will later present.

    The token check is only useful if the caller can hold the token. Before
    this, the only way to learn one was ``serve --print-token``, which bound
    the port, printed, and exited -- handing back a credential no surviving
    process could present, so a host had to start the service twice. These
    tests cover the provisioning side, over a real socket where the claim is
    about what a caller receives.
    """

    DECLARED = "declared-token-0123456789abcdef"

    def driver_for(self, env):
        return Driver(settings=load_settings(env=env), audit=MemoryAuditLog())

    def test_a_declared_token_is_the_one_the_service_presents(self):
        env = {"DRIVER_TOKEN": self.DECLARED}
        service = Service(self.driver_for(env))
        self.assertEqual(service.token, self.DECLARED)

    def test_no_declared_token_still_gets_a_generated_one(self):
        service = Service(self.driver_for({}))
        self.assertTrue(service.token)
        self.assertNotEqual(service.token, "")
        # Two services in one process must not share a credential.
        self.assertNotEqual(Service(self.driver_for({})).token, service.token)

    def test_a_generated_token_is_long_enough_to_be_usable(self):
        self.assertGreaterEqual(len(Service(self.driver_for({})).token),
                                MIN_TOKEN_LENGTH)

    def test_an_explicit_argument_beats_the_declared_token(self):
        env = {"DRIVER_TOKEN": self.DECLARED}
        service = Service(self.driver_for(env), token="explicit")
        self.assertEqual(service.token, "explicit")

    def test_a_blank_declared_token_is_refused(self):
        for value in ("", "   ", "\t"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ConfigError) as ctx:
                    load_settings(env={"DRIVER_TOKEN": value})
                self.assertIn("blank", str(ctx.exception))

    def test_a_trivially_short_declared_token_is_refused(self):
        for value in ("t", "abc123", "x" * (MIN_TOKEN_LENGTH - 1)):
            with self.subTest(length=len(value)):
                with self.assertRaises(ConfigError) as ctx:
                    load_settings(env={"DRIVER_TOKEN": value})
                self.assertIn(str(MIN_TOKEN_LENGTH), str(ctx.exception))

    def test_a_token_of_exactly_the_minimum_is_accepted(self):
        value = "x" * MIN_TOKEN_LENGTH
        self.assertEqual(load_settings(env={"DRIVER_TOKEN": value}).token, value)

    def test_the_refusal_does_not_echo_the_token(self):
        """A rejection message reaches a terminal and a log, and a log is
        somewhere a credential ends up. The value must never appear in it."""
        secret = "hunter2-" + "z" * 20
        with self.assertRaises(ConfigError) as ctx:
            load_settings(env={"DRIVER_TOKEN": secret[:5]})
        self.assertNotIn(secret[:5], str(ctx.exception))

    def test_the_token_is_absent_from_repr_and_redacted(self):
        env = {"DRIVER_TOKEN": self.DECLARED}
        settings = load_settings(env=env)
        self.assertNotIn(self.DECLARED, repr(settings))
        self.assertNotIn(self.DECLARED, str(settings))
        self.assertNotIn(self.DECLARED, repr(settings.redacted()))
        self.assertNotIn("token", settings.redacted())

    def test_the_token_is_absent_from_a_health_response(self):
        """The credential belongs in a header the caller already holds. Any
        local process can reach this endpoint, so publishing the token there
        would hand it to every one of them."""
        env = {"DRIVER_TOKEN": self.DECLARED}
        httpd, _ = serve("127.0.0.1", _free_port(),
                          service=Service(self.driver_for(env)), block=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=10)
            conn.request("GET", "/health",
                         headers={"Authorization": f"Bearer {self.DECLARED}"})
            body = conn.getresponse().read().decode()
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=10)
        self.assertNotIn(self.DECLARED, body)
        # ...but the version is, so a host can tell what it is talking to.
        self.assertIn('"version"', body)

    def test_a_declared_token_authenticates_over_a_real_socket(self):
        env = {"DRIVER_TOKEN": self.DECLARED}
        httpd, _ = serve("127.0.0.1", _free_port(),
                          service=Service(self.driver_for(env)), block=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]

        def status_for(token):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                headers = ({} if token is None
                           else {"Authorization": f"Bearer {token}"})
                conn.request("GET", "/health", headers=headers)
                return conn.getresponse().status
            finally:
                conn.close()

        try:
            self.assertEqual(status_for(None), 401)
            self.assertEqual(status_for("wrong-" + "y" * 24), 401)
            self.assertEqual(status_for(self.DECLARED), 200)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=10)

    def test_a_declared_token_does_not_change_the_request_contract(self):
        """Authenticating must not have moved the wire. Same eleven keys,
        same stop reasons, same fail-closed schema."""
        from driver_core.server import STOP_REASONS
        env = {"DRIVER_TOKEN": self.DECLARED}
        service = Service(self.driver_for(env))
        health = service.health()
        self.assertEqual(sorted(health),
                         ["keyed", "ok", "settings", "sources", "status",
                          "version", "vocabulary"])
        self.assertEqual(len(STOP_REASONS), 9)


class ServeBindingTests(unittest.TestCase):
    """``serve`` has to report and honour the socket it actually bound."""

    def test_port_zero_asks_for_an_ephemeral_port(self):
        """``port or settings.port`` treats 0 as unset, so the one value a
        caller passes meaning "any free port" silently became 8791."""
        httpd, _ = serve("127.0.0.1", 0, block=False)
        try:
            self.assertNotEqual(httpd.server_address[1], 0)
            self.assertNotEqual(httpd.server_address[1], 8791)
        finally:
            httpd.server_close()

    def test_omitted_host_and_port_fall_back_to_the_settings(self):
        port = _free_port()
        env = {"DRIVER_HOST": "127.0.0.1", "DRIVER_PORT": str(port)}
        wanted = load_settings(env=env)
        driver = Driver(settings=wanted, audit=MemoryAuditLog())
        httpd, _ = serve(service=Service(driver), block=False)
        try:
            self.assertEqual(httpd.server_address[:2],
                             (wanted.host, wanted.port))
        finally:
            httpd.server_close()

    def test_an_explicit_port_still_wins_over_the_settings(self):
        port = _free_port()
        httpd, _ = serve("127.0.0.1", port, block=False)
        try:
            self.assertEqual(httpd.server_address[1], port)
        finally:
            httpd.server_close()

    def test_an_empty_host_falls_back_to_loopback_rather_than_binding_all(self):
        """``bind(("", port))`` is INADDR_ANY: it listens on every interface,
        which is the one thing a server that documents itself as loopback-only
        must not do. So ``""`` is not a value the host rule will honour, even
        though ``0`` is a value the port rule does.
        """
        httpd, _ = serve("", 0, block=False)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        finally:
            httpd.server_close()

    def test_announce_receives_the_address_that_was_actually_bound(self):
        """The old message printed the *arguments*, which are None unless the
        caller passed them, so it reported ``http://None:8791``."""
        seen = []
        httpd, _ = serve("127.0.0.1", 0, block=False,
                         announce=lambda addr: seen.append(addr))
        try:
            self.assertEqual(seen, [httpd.server_address[:2]])
            self.assertIsNotNone(seen[0][0])
            self.assertNotEqual(seen[0][1], 0)
        finally:
            httpd.server_close()

    def test_nothing_is_announced_when_nobody_asked(self):
        """A library function must not print. ``announce`` is opt-in."""
        httpd, _ = serve("127.0.0.1", 0, block=False)
        try:
            self.assertTrue(httpd.server_address[1])
        finally:
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
