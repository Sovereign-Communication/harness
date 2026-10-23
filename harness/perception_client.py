"""harness.perception_client — adapter to the driver-core service.

Thin, zero-dependency client (stdlib only), in the same shape as
:mod:`harness.media_client`: harness never holds the driver's credentials,
never names a provider, and never re-implements its policy. Verified
extraction, the Jev decision, and the action itself all live service-side;
harness receives one step envelope and branches on it.

The ordering is the whole point, and it is why this is an adapter and not a
feature. driver-core cross-verifies an extraction *before* the Jev decision,
never after, because a calibrated confidence is calibrated only for the
question that was asked — hand a model a hallucinated screen state and it
returns a confidently wrong answer that no amount of downstream checking can
detect. So nothing in harness is invited to reach past the decision and act
on an unverified read of the machine.

Honest-failure contract, and the one distinction this module exists to keep:

* **A refusal is a successful call.** The service answers HTTP 200 with
  ``ok: false`` and a ``reason`` from a closed set when the driver declined
  to act. That is data. :meth:`PerceptionAdapter.step` returns it.
* **Only transport failures raise** :class:`PerceptionUnavailable`.

Collapsing those two is the interesting bug this adapter is shaped to avoid.
Raising on a refusal would make a caller retry a *decision*; returning a
transport error as a refusal would make a caller retry an outage as though
the driver had declined. Both are recoverable-looking and neither is correct,
so the boundary is asserted rather than documented.

Non-interference: driver-core is configured entirely under ``DRIVER_*`` and
reads nothing from this project's environment. This adapter reads only
``DRIVER_BASE_URL`` / ``DRIVER_TOKEN`` / ``DRIVER_CONFIG_PATH`` and never
writes a Harness state file on the driver's behalf.

Usage (Python):
    from harness.perception_client import PerceptionAdapter, PerceptionUnavailable
    driver = PerceptionAdapter()
    env = driver.step("file-manager", schema="screen",
                      consent={"granted": True, "action": "open_window",
                               "params": {"path": "~/notes"}, "by": "operator"})
    env["ok"]        # True only if the action actually executed
    env["stopped_at"]  # which tier stopped: capture / agree / decide / execute
    env["reason"]    # a member of STOP_REASONS, or None
    env["cost_usd"]  # what the step spent on the decision

Usage (CLI, wired into harness.cli as `harness driver ...`):
    harness driver health
    harness driver step "file-manager" --action open_window --by operator
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# No provider or service brand strings are hardcoded here. The endpoint is
# always resolved from config or environment, in this order: explicit
# constructor args > config file > env vars > loopback default. The default
# is a *local* fallback, matching the `driver-core serve` quickstart.
#
# Resolved lazily rather than at import. A module-level
# ``os.environ.get`` freezes the endpoint at import time, which means a
# caller that configures the environment after importing this module -- or a
# test that patches it -- silently gets the loopback default and no error
# anywhere. Reading it per construction costs nothing and keeps "where did
# this endpoint come from" answerable at the moment it is asked.
DEFAULT_BASE_URL = "http://127.0.0.1:8791"


def _default_base():
    return os.environ.get("DRIVER_BASE_URL", DEFAULT_BASE_URL)


def _config_path():
    return os.environ.get(
        "DRIVER_CONFIG_PATH",
        os.path.join(os.path.expanduser("~"), ".config", "harness", "driver.json"),
    )

#: The closed set of reasons the driver may report for a blocked step,
#: mirrored from the service so a caller can branch without prose parsing.
#: The adapter never invents a reason and never coerces an unknown one --
#: see :meth:`PerceptionAdapter.step`, which treats a refusal with no reason
#: as a contract violation rather than passing it through.
STOP_REASONS = (
    "no_capture", "insufficient_agreement", "extraction_disagreement",
    "confidence_below_threshold", "state_not_stable", "decision_not_usable",
    "no_action_recommended", "undeclared_action", "execution_refused",
)

#: The tiers a step can stop at, in pipeline order. ``stopped_at`` is always
#: one of these on a refusal, so a caller can tell "we never read the screen"
#: apart from "we read it, agreed on it, and the model declined".
STOP_TIERS = ("capture", "agree", "decide", "execute")


def _load_endpoint():
    env_token = os.environ.get("DRIVER_TOKEN")
    default = _default_base()
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("base_url", default), cfg.get("token") or env_token
        except (OSError, ValueError):
            # A config file this project cannot read is not a reason to
            # refuse to start; it is a reason to fall through to the
            # environment and say so later if the endpoint is wrong.
            pass
    return default, env_token


class PerceptionUnavailable(Exception):
    """The driver service could not be reached, or broke its own contract.

    Raised only for transport failures and for malformed responses. It is
    never raised because the driver declined to act — see the module
    docstring; that case is a returned envelope with ``ok: False``.
    """


class PerceptionAdapter:
    def __init__(self, base_url=None, token=None, timeout=120, opener=None):
        base, env_token = _load_endpoint()
        self.base = (base_url or base).rstrip("/")
        self.token = token or env_token
        self.timeout = timeout
        # Injectable transport seam for hermetic tests: defaults to the real
        # urllib opener, but tests pass a fake so nothing touches the network.
        self._opener = opener or urllib.request.urlopen

    # ---- transport ----

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # A 4xx/5xx here is the service failing, not the driver
            # deciding: the driver answers 200 for every refusal, so an HTTP
            # error means the request never became a decision.
            try:
                try:
                    err = json.loads(e.read().decode("utf-8"))
                except Exception:
                    err = {"error": "HTTP {}".format(e.code)}
            finally:
                e.close()
            raise PerceptionUnavailable(
                "driver service returned HTTP {}: {}".format(
                    e.code, err.get("error") or err)
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise PerceptionUnavailable(
                "driver service unreachable at {} ({}). Start it with: "
                "driver-core serve".format(self.base, e)
            ) from e
        except ValueError as e:
            # Reached the service, got bytes back that are not a step.
            # Surfacing this as unavailability rather than a refusal keeps
            # "the driver said no" reserved for the driver's own answer.
            raise PerceptionUnavailable(
                "driver service returned a non-JSON response ({})".format(e)
            ) from e

    # ---- core API ----

    def health(self):
        """Liveness plus the driver's own settings, redacted service-side."""
        return self._request("GET", "/health")

    def schemas(self):
        return self._request("GET", "/schemas")

    def vocabulary(self):
        """The closed action vocabulary this driver will accept."""
        return self._request("GET", "/vocabulary")

    def verify(self):
        """Audit-chain verdict and spend snapshot."""
        return self._request("GET", "/verify")

    def step(self, target, *, schema=None, consent=None, prefer=(),
             require_stable=True, step_id=None):
        """Run one step. Returns the driver's envelope; never raises for a
        refusal.

        ``consent`` is passed through verbatim and this adapter never
        synthesises one. Omitting it is the safe default: the driver then
        refuses every mutating action rather than assuming permission the
        caller never gave.
        """
        body = {
            "target": target,
            "schema": schema,
            "consent": consent,
            "prefer": list(prefer) or None,
            "require_stable": bool(require_stable),
            "step_id": step_id,
        }
        # Drop the unset keys so the service sees an absent field rather
        # than an explicit null, which it would otherwise have to
        # distinguish from "the caller asked for null".
        body = {k: v for k, v in body.items() if v is not None}
        envelope = self._request("POST", "/step", body)
        self._check_contract(envelope)
        return envelope

    @staticmethod
    def _check_contract(envelope):
        """Refuse to relay a response that contradicts the service's own
        documented shape.

        A refusal with no reason, or a success with no verdict fields, is not
        something a caller can act on safely: the first invites a blind
        retry, the second invites the caller to assume a decision it never
        received. Catching it here keeps the guarantee at the boundary
        rather than in every future caller.
        """
        if not isinstance(envelope, dict):
            raise PerceptionUnavailable(
                "driver step returned {} where a JSON object was "
                "declared".format(type(envelope).__name__))
        if "ok" not in envelope:
            raise PerceptionUnavailable(
                "driver step response carries no 'ok'; the service's "
                "contract was not honoured")
        if envelope["ok"]:
            return envelope
        reason = envelope.get("reason")
        if not reason:
            raise PerceptionUnavailable(
                "driver reported a refusal with no reason; a caller cannot "
                "distinguish a decision from a defect without one")
        if reason not in STOP_REASONS:
            # Preserved and loud rather than coerced: a new reason is a real
            # signal that the service moved ahead of this adapter, and
            # silently folding it into a known bucket would hide that.
            raise PerceptionUnavailable(
                "driver reported undeclared stop reason {!r}; this adapter "
                "knows {}".format(reason, ", ".join(STOP_REASONS)))
        return envelope

    @staticmethod
    def summarize(envelope):
        """One line a person can read in a log."""
        if not envelope.get("ok"):
            return "stopped at {}: {}".format(
                envelope.get("stopped_at") or "?", envelope.get("reason") or "?")
        decision = envelope.get("decision") or {}
        action = decision.get("action") if isinstance(decision, dict) else None
        execution = envelope.get("execution") or {}
        outcome = execution.get("ok") if isinstance(execution, dict) else None
        return "executed {} ({}) - ${:.6f}".format(
            action or "?", outcome, float(envelope.get("cost_usd") or 0.0))


# ---- CLI handler (wired as `harness driver ...`) ----

def run_cli(args, settings=None):
    """harness driver {health|step|vocabulary|schemas|verify} — exit code.

    Mirrors the media adapter's exit-code shape: 0 on success, 1 on a
    refusal, 4 on a defer-style unavailability, so `harness driver step`
    composes in a shell the same way `harness media` does.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="harness driver")
    sub = ap.add_subparsers(dest="driver_cmd", required=True)

    p = sub.add_parser("health", help="driver liveness and redacted settings")
    sub.add_parser("schemas", help="declared extraction schemas")
    sub.add_parser("vocabulary", help="the closed action vocabulary")
    sub.add_parser("verify", help="audit chain verdict and spend snapshot")

    p = sub.add_parser("step", help="run one verified-extraction step")
    p.add_argument("target", help="what to read: a window name, app, or source")
    p.add_argument("--schema", default=None, choices=[None, "screen", "gui", "dom", "cli"])
    p.add_argument("--action", default=None,
                   help="action to consent to; omit for read-only observation")
    p.add_argument("--params", default=None,
                   help="JSON object of action parameters")
    p.add_argument("--by", default="operator",
                   help="who is granting consent (recorded in the audit log)")
    p.add_argument("--prefer", default=None,
                   help="comma-separated capture source preference order")
    p.add_argument("--allow-unstable", action="store_true",
                   help="do not require the screen to be stable before acting")
    p.add_argument("--raw", action="store_true", help="print the full envelope")

    opts = ap.parse_args(args)
    adapter = PerceptionAdapter()

    try:
        if opts.driver_cmd == "health":
            print(json.dumps(adapter.health(), indent=2, sort_keys=True))
            return 0
        if opts.driver_cmd == "schemas":
            print(json.dumps(adapter.schemas(), indent=2, sort_keys=True))
            return 0
        if opts.driver_cmd == "vocabulary":
            print(json.dumps(adapter.vocabulary(), indent=2, sort_keys=True))
            return 0
        if opts.driver_cmd == "verify":
            report = adapter.verify()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if (report.get("audit") or {}).get("ok") else 1

        # step -- consent is built here or not at all. The adapter has no
        # code path that infers one, so a --step without --action is a
        # read-only observation and the driver will refuse anything
        # mutating on its own.
        consent = None
        if opts.action:
            try:
                params = json.loads(opts.params) if opts.params else {}
            except ValueError as e:
                print("[defer] driver: --params is not JSON ({})".format(e),
                      file=sys.stderr)
                return 2
            if not isinstance(params, dict):
                print("[defer] driver: --params must be a JSON object",
                      file=sys.stderr)
                return 2
            consent = {"granted": True, "action": opts.action,
                       "params": params, "by": opts.by}
        prefer = tuple(s.strip() for s in opts.prefer.split(",") if s.strip()) \
            if opts.prefer else ()
        envelope = adapter.step(
            opts.target, schema=opts.schema, consent=consent, prefer=prefer,
            require_stable=not opts.allow_unstable)
        if opts.raw:
            print(json.dumps(envelope, indent=2, sort_keys=True))
        else:
            print(adapter.summarize(envelope))
            if not envelope.get("ok") and envelope.get("detail"):
                print("  detail: {}".format(envelope["detail"]))
        return 0 if envelope.get("ok") else 1
    except PerceptionUnavailable as e:
        # defer-style honest failure: state the reason, act on nothing
        print("[defer] driver: {}".format(e), file=sys.stderr)
        return 4


__all__ = [
    "PerceptionAdapter",
    "PerceptionUnavailable",
    "STOP_REASONS",
    "STOP_TIERS",
    "run_cli",
]
