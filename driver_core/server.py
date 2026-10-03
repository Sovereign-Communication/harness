"""The REST surface, and the contract a host project integrates against.

This is the seam promised in the design. A host application -- Harness, or
anything else -- talks to driver-core over these endpoints and receives plain
JSON it can serialise, branch on, and log. Nothing about the host's process,
filesystem layout, or configuration has to match anything in here, which is
what makes the integration a port rather than a merge.

Three conventions carry the safety, and all three are visible in the response
shape rather than in a side channel:

* **Every response carries ``ok`` and, when false, a named ``reason`` from a
  closed set.** A caller never has to parse prose to find out what happened,
  and it never has to guess whether an absent field means "false" or "we
  never found out".
* **A refusal is HTTP 200 with ``ok: false``** for anything the driver
  decided against doing. Only malformed requests and unknown routes are 4xx
  and 5xx. A caller retrying on a 5xx must never be retrying a *decision*,
  and conflating the two is how a refusal turns into a loop.
* **Nothing is echoed that came off a screen.** Captures are summarised by
  fingerprint, decisions by metadata. The response is safe to log.

The server binds loopback only and requires a token, because an endpoint
that can act on a machine should never be reachable by accident from another
host. That is a floor, not a security claim -- see the threat notes in the
README.
"""
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import load_settings
from .driver import driver_from_settings
from .errors import DriverError, PerceptionUnavailable
from .perception import Target

#: The closed set of stop reasons. Callers may branch on these; nothing else
#: about a blocked step is a stable contract.
STOP_REASONS = (
    "no_capture", "insufficient_agreement", "extraction_disagreement",
    "confidence_below_threshold", "state_not_stable", "decision_not_usable",
    "no_action_recommended", "undeclared_action", "execution_refused",
)

#: Actions a caller may never reach through this API without first
#: registering an executor for it. The server refuses to expose a route that
#: would execute an action the driver has no handler for.
def _ok(payload):
    return {"ok": True, **payload}


#: Marks an internal response envelope. A named marker rather than a
#: "does it have a status key" test, because ``health`` legitimately
#: reports ``status: "up"`` in its payload and would otherwise be mistaken
#: for an already-wrapped response.
_ENVELOPE = "_envelope"


def _wrapped(status, payload):
    return {_ENVELOPE: True, "status": status, "body": payload}


def _refused(reason, detail="", **extra):
    if reason not in STOP_REASONS:
        raise DriverError(f"{reason!r} is not a declared stop reason")
    return {"ok": False, "reason": reason, "detail": detail, **extra}


class Service:
    """The endpoint logic, free of HTTP.

    Kept separate from the handler so the same behaviour can be exercised
    directly by a test or an embedding process, and so the HTTP layer holds
    no policy of its own.
    """

    def __init__(self, driver=None, *, token=None):
        self.driver = driver or driver_from_settings()
        # Three ways to end up with a token, in descending precedence: an
        # explicit argument (a library caller holding its own), the operator's
        # DRIVER_TOKEN, and a generated one. The last is the default because a
        # per-process token is right for an in-process driver and wrong for a
        # host that has to present it -- which is why the middle one exists.
        #
        # Only the *configured* token is policed, and it was policed where it
        # is read (:func:`driver_core.config.validated_token`). An explicit
        # argument is a different trust domain: that caller wrote the token in
        # the same breath as the code that presents it and can already reach
        # every executor, so a length check there buys nothing and would only
        # make the library awkward to drive from a test.
        if token is None:
            token = self.driver.settings.token
        self.token = token or secrets.token_urlsafe(24)

    # -- routes ---------------------------------------------------------

    def health(self, body=None):
        # The version is here so a host can tell what it is talking to before
        # it posts a request that means something different in another
        # release. The token is deliberately not: it is a credential, it
        # belongs in a header the caller already holds, and this endpoint is
        # reachable by anything that can open a loopback socket.
        return _ok({
            "status": "up",
            "version": __version__,
            "keyed": self.driver.settings.keyed,
            "settings": self.driver.settings.redacted(),
            "vocabulary": self.driver.vocabulary.to_dict(),
            "sources": [s.name for s in self.driver.sources],
        })

    def schemas(self, body=None):
        from .states import SCREEN_SCHEMA, CLI_SCHEMA, DOM_SCHEMA
        return _ok({"schemas": [SCREEN_SCHEMA.to_dict(), CLI_SCHEMA.to_dict(),
                                DOM_SCHEMA.to_dict()]})

    def vocabulary(self, body=None):
        return _ok({"vocabulary": self.driver.vocabulary.to_dict()})

    def step(self, body):
        """Run one step. This is the integration's single call."""
        target = body.get("target")
        if not target:
            return _wrapped(400, {"ok": False, "error": "target is required"})
        resolved = self._resolve_target(body.get("schema"))
        if isinstance(resolved, dict):
            return _wrapped(400, {"ok": False, **resolved})
        target_class, schema = resolved
        consent = self._consent_for(body.get("consent"))
        try:
            result = self.driver.step(
                Target(target, target_class), schema=schema, consent=consent,
                # The host's id, carried rather than replaced. It is not a
                # new wire field -- the frozen adapter has always sent one --
                # but it was being dropped here and the driver minted its
                # own, which left the audit chain unjoinable to the caller's
                # own log after the fact.
                step_id=body.get("step_id"),
                prefer=tuple(body.get("prefer") or ()),
                require_stable=bool(body.get("require_stable", True)),
                # No new wire field: `params` already means "the parameter
                # set" here, and consent is *defined* as bound to one exact
                # (action, params) pair. The action's parameters are
                # therefore the consent's parameters -- if the decision names
                # a different action, or the declaration wants a different
                # shape, `Consent.covers` refuses below. Adding a second
                # field would have meant two spellings of the same pair that
                # could drift apart.
                params=consent.params if consent else {})
        except DriverError as exc:
            return _wrapped(400, {"ok": False, "error": str(exc)})
        # A decision the driver declined to act on is a successful API call
        # reporting a refusal, not a transport error. See the module
        # docstring: a caller retrying on 5xx must never be retrying a
        # decision.
        return _wrapped(200, result.to_dict())

    def _resolve_target(self, name):
        """Bind a wire ``schema`` name, or refuse.

        **An absent or unknown name is refused.** It is not defaulted: the
        earlier version resolved ``None`` to the screen schema while leaving
        the class undeclared, and an undeclared class permits any source to
        answer with pixels last -- so the one request a caller made without
        thinking was the one that could reach the vision tier at all. There is
        no default that fixes this, so the service asks.

        The rule is :func:`driver_core.states.resolve_wire_target`; the only
        thing decided here is that the caller's mistake is a 400 rather than
        a transport error.
        """
        from .states import resolve_wire_target
        try:
            return resolve_wire_target(name)
        except PerceptionUnavailable as exc:
            return {"error": str(exc)}

    def _consent_for(self, body):
        """The consent for this request, bound to one ``(action, params)``.

        ``action`` defaults to ``"*"``, which under the consent law
        authorises nothing with consequences. That default is deliberate and
        should stay: a request that forgets to say what it is agreeing to
        gets a refusal rather than a blanket grant it never explicitly asked
        for.
        """
        from .executor import Consent
        if not body:
            return None
        return Consent(granted=bool(body.get("granted", False)),
                       action=body.get("action", "*"),
                       params=body.get("params") or {},
                       by=body.get("by", "api"))

    def verify(self, body=None):
        verdict = self.driver.audit.verify()
        return _ok({"audit": verdict.to_dict(),
                    "budget": self.driver.budget.snapshot()})

    # -- dispatch -------------------------------------------------------

    def handle(self, route, body):
        """Every route returns ``{"status": int, "body": dict}``.

        The envelope is uniform on purpose. When some routes return a bare
        payload and others a wrapped one, the HTTP layer has to know which is
        which, and the first route added that forgets the convention returns
        a KeyError deep in a request instead of a clean 404.
        """
        routes = {
            "health": self.health,
            "schemas": self.schemas,
            "vocabulary": self.vocabulary,
            "step": self.step,
            "verify": self.verify,
        }
        handler = routes.get(route)
        if handler is None:
            return _wrapped(404, {"ok": False,
                                  "error": f"unknown route {route!r}"})
        outcome = handler(body or {})
        if outcome.get(_ENVELOPE):
            return outcome
        return _wrapped(200, outcome)


class Handler(BaseHTTPRequestHandler):
    server_version = "driver-core"

    #: How much of an unauthenticated request body is read and thrown away so
    #: that the refusal reaches the client. See :meth:`_refuse`.
    max_drain_bytes = 1 << 20

    def log_message(self, fmt, *args):  # keep the console usable
        pass

    def _authorised(self):
        """Whether this request carries the service token.

        Compared as **bytes**, not as ``str``. ``secrets.compare_digest``
        raises ``TypeError`` on a ``str`` argument containing a non-ASCII
        character, so comparing strings meant that a request sending
        ``Authorization: Bearer café`` escaped the check as an exception
        rather than a refusal: the connection was dropped with no HTTP
        response at all and a traceback was printed. A wrong token has to
        produce a 401 like every other wrong token, and comparing the
        encoded forms is the form that comparison is specified for.
        """
        supplied = (self.headers.get("Authorization") or "").removeprefix(
            "Bearer ").strip()
        expected = self.server.service.token
        return secrets.compare_digest(supplied.encode("utf-8"),
                                      expected.encode("utf-8"))

    def _refuse(self):
        """401, having first drained whatever the caller sent.

        The drain is not tidiness. The refusal is written before the body is
        read, and a socket closed with unread bytes still in its receive
        queue is reset by the operating system -- which destroys the 401 on
        its way to the client. Measured over a real connection, an
        unauthenticated ``POST /step`` delivered its 401 in one request out
        of four with a small body, and in none at all with a 60 kB one: the
        caller saw a connection reset rather than a refusal, and a client
        that cannot tell "you are not authorised" from "the server broke"
        cannot act on either.

        The bound is what keeps this from becoming the other half of the
        trade: an unauthenticated caller may make the server read a
        megabyte and no more, in 64 kB chunks, and a body larger than that
        is deliberately left undrained. ``Transfer-Encoding: chunked`` has
        no ``Content-Length`` to read against and is not supported by this
        server in any path.
        """
        # A malformed Content-Length counts as nothing: the answer is already
        # decided, and the only question left is how much has to be read
        # before the socket can be closed cleanly.
        try:
            remaining = min(max(0, int(self.headers.get("Content-Length") or 0)),
                            self.max_drain_bytes)
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        self._respond(401, {"ok": False, "error": "unauthorized"})

    def _respond(self, status, payload):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorised():
            self._refuse()
            return
        route = self.path.lstrip("/").split("?")[0] or "health"
        outcome = self.server.service.handle(route, {})
        self._respond(outcome["status"], outcome.get("body", outcome))

    def do_POST(self):
        if not self._authorised():
            self._refuse()
            return
        route = self.path.lstrip("/").split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"ok": False, "error": "body is not JSON"})
            return
        if not isinstance(body, dict):
            self._respond(400, {"ok": False, "error": "body must be an object"})
            return
        outcome = self.server.service.handle(route, body)
        self._respond(outcome["status"], outcome.get("body", outcome))


def serve(host=None, port=None, *, service=None, block=True, announce=None):
    """Bind loopback and serve. Returns ``(server, service)``.

    ``port=0`` is honoured as the operating system intends it and asks for an
    ephemeral port. The obvious ``port or settings.port`` would treat 0 as
    unset and hand back the default instead, which silently costs a caller
    the thing it asked for -- two services that meant to coexist would
    collide, and the second would fail with a bind error naming a port nobody
    chose. ``None`` means "not specified"; 0 is a value.

    ``host`` keeps the opposite rule, deliberately: an empty host is not a
    value a caller can mean, and binding it would listen on every interface,
    which is the one thing this server promises not to do. So ``""`` falls
    back to the configured loopback address. The asymmetry is the point --
    0 is a real port, ``""`` is not a real host.

    ``announce`` is called with the bound ``(host, port)`` once the socket is
    listening, which is the only moment the answer is knowable: with an
    ephemeral port the caller cannot know it beforehand, and echoing the
    arguments it passed is how this used to report ``http://None:8791``.
    """
    settings = service.driver.settings if service else load_settings()
    host = host or settings.host
    if port is None:
        port = settings.port
    service = service or Service(driver_from_settings(settings))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service
    if announce is not None:
        announce(httpd.server_address[:2])
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    return httpd, service
