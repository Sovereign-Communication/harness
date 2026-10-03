"""Command-line entry point.

Small on purpose. The interesting behaviour is in the library, and a CLI
that grew its own opinions would become a second implementation of the same
policy -- which is the single thing this project is structured to prevent.

Every subcommand that can spend money or touch a machine reports what it did
and refuses honestly when it could not. ``--json`` is available on all of
them so a script can branch on ``ok`` and ``reason`` rather than parsing
prose, and the exit code reflects whether the step actually executed.

One consequence of the consent law is visible here. There is no blanket
``--grant-write``: a consent is a capability for one exact ``(action, params)``
pair, so ``step`` takes ``--grant ACTION`` plus the parameters that pair is
bound to, and **echoes the resolved form it is about to use** before acting.
That echo is not decoration. It is the only way a person can consent to what
will actually run rather than to what they typed -- ``--grant delete_file
--grant-path '~/notes'`` is shown as the absolute path, because that is the
path that would be deleted.
"""
import argparse
import json
import sys

from .actions import DEFAULT_VOCABULARY
from .config import load_settings
from .driver import driver_from_settings
from .executor import Consent, normalise_params
from .perception import Target
from .server import Service, serve
from .states import WIRE_TARGETS, resolve_wire_target

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2


def _print(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def cmd_health(args, driver):
    payload = Service(driver).health()
    if not args.json:
        settings = driver.settings
        print(f"driver-core: ok (keyed={settings.keyed})")
        print(f"  vocabulary : {driver.vocabulary.identity()} "
              f"({len(driver.vocabulary)} actions)")
        print(f"  sources    : {', '.join(payload['sources']) or 'none declared'}")
        print(f"  writes     : {'allowed' if settings.allow_write else 'off'} "
              f"(DRIVER_ALLOW_WRITE)")
        print(f"  budget     : ${driver.budget.remaining:.6f} remaining of "
              f"${driver.budget.ceiling:.6f}")
        print(f"  audit      : {driver.audit.count} records")
    _print(payload, args.json)
    return EXIT_OK


def cmd_vocabulary(args, driver):
    payload = Service(driver).vocabulary()
    if not args.json:
        for action in driver.vocabulary.to_dict()["actions"]:
            print(f"  {action['name']:<26} {action['class']:<12} "
                  f"target={action['target']}")
    _print(payload, args.json)
    return EXIT_OK


def cmd_schema(args, driver):
    payload = Service(driver).schemas()
    if not args.json:
        for schema in payload["schemas"]:
            print(f"{schema['id']}@{schema['version']}")
            for field in schema["fields"]:
                flag = "" if field["presence"] == "required" else " (optional)"
                print(f"  {field['name']:<26} {field['type']}{flag}")
    _print(payload, args.json)
    return EXIT_OK


def _grant_for(args):
    """The exact ``(action, params)`` pair asked for, or ``None``.

    Raises on a bad grant. The parameters are validated and normalised
    through the *same* path the executor uses, so what the operator is shown
    is byte-identical to what will be compared and what will run. Validating
    here also means a mistyped grant fails before a step starts, rather than
    after a capture and a decision have already been paid for.

    One declaration, two uses: the same pair becomes the consent *and* the
    action's parameters. There is nowhere to put a second, slightly different
    set of parameters, which is the point -- a CLI that could consent for one
    thing and execute another would be asking to be trusted twice.
    """
    if not args.grant:
        return None
    action = DEFAULT_VOCABULARY.resolve(args.grant)
    if args.grant_params:
        params = json.loads(args.grant_params)
        if not isinstance(params, dict):
            raise ValueError("--grant-params must be a JSON object")
    elif args.grant_path:
        params = {"path": args.grant_path}
    else:
        params = {}
    resolved = normalise_params(action, action.check_params(params))
    return action.name, resolved


def cmd_step(args, driver):
    granted = _grant_for(args)
    consent = None
    action_params = {}
    if granted is not None:
        name, action_params = granted
        consent = Consent(True, name, params=action_params, by="cli")
        # stderr, not stdout: on a --json run stdout must stay one clean
        # document that a script can parse without stripping a preamble.
        print(f"[consent] {json.dumps(consent.to_dict(), sort_keys=True)}",
              file=sys.stderr)
    # Resolved through the one rule the service uses, so the two surfaces
    # cannot drift into disagreeing about what a class is. The service
    # refuses an absent ``schema`` and this flag defaults to ``gui``; that
    # asymmetry is deliberate and lives here rather than in the rule, which
    # is why a person at a terminal is not forced to type a flag to look at
    # something while a host integrating over HTTP is made to declare what it
    # is looking at.
    target_class, schema = resolve_wire_target(args.schema)
    result = driver.step(Target(args.target, target_class), schema=schema,
                         consent=consent,
                         prefer=tuple(args.prefer or ()),
                         require_stable=not args.allow_unstable,
                         params=action_params)
    payload = result.to_dict()
    if not args.json:
        if payload["ok"]:
            execution = payload.get("execution") or {}
            print(f"[executed] {execution.get('action')} "
                  f"(confidence {payload['decision']['confidence']})")
        else:
            print(f"[refused] {payload['reason']}: {payload['detail']}")
    _print(payload, args.json)
    return EXIT_OK if payload["ok"] else EXIT_REFUSED


def cmd_verify(args, driver):
    payload = Service(driver).verify()
    if not args.json:
        audit = payload["audit"]
        print(f"audit: {'VERIFIED' if audit['ok'] else 'BROKEN'} "
              f"({audit['records']} records) -- {audit['detail']}")
        budget = payload["budget"]
        print(f"spend: ${budget['spent_usd']:.6f} of "
              f"${budget['ceiling_usd']:.6f} "
              f"(${budget['remaining_usd']:.6f} remaining)")
    _print(payload, args.json)
    return EXIT_OK if payload["audit"]["ok"] else EXIT_REFUSED


def cmd_serve(args, driver):
    if args.print_token:
        # A pure query, binding nothing. It used to bind the port, print, and
        # exit -- which produced a token no process could ever present,
        # because the process holding it was gone and the socket closed. A
        # host that cannot scrape stdout from a process it just started needs
        # this to be answerable *before* anything is listening.
        declared = driver.settings.token
        if not declared:
            print("serve --print-token needs a declared DRIVER_TOKEN: a "
                  "generated token dies with the process that made it, so "
                  "there is nothing to hand a caller.", file=sys.stderr)
            return EXIT_ERROR
        print(declared)
        return EXIT_OK
    serve(args.host, args.port, service=Service(driver), block=True,
          announce=lambda addr: print(
              f"listening on http://{addr[0]}:{addr[1]}"))
    return EXIT_OK


COMMANDS = {
    "health": cmd_health,
    "vocabulary": cmd_vocabulary,
    "schema": cmd_schema,
    "step": cmd_step,
    "verify": cmd_verify,
    "serve": cmd_serve,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="driver-core",
        description="Verified extraction -> Jev decision -> deterministic action")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON only")
    parser.add_argument("--dry-run", action="store_true",
                        help="perform no side effects; report what would happen")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="show settings, budget and audit state")
    sub.add_parser("vocabulary", help="list the declared action vocabulary")
    sub.add_parser("schema", help="list the declared extraction schemas")
    sub.add_parser("verify", help="verify the audit chain and report spend")

    step = sub.add_parser("step", help="run one full pipeline step")
    step.add_argument("target", help="what to observe")
    step.add_argument(
        "--schema", default="gui", choices=sorted(WIRE_TARGETS),
        help="the target class to observe; required at the HTTP boundary and "
             "explicit here because a default would silently observe a "
             "different machine than the one you named")
    step.add_argument("--prefer", action="append",
                      help="prefer a structured source (cli, mcp, dom)")
    step.add_argument(
        "--grant",
        help="consent for exactly this action name; there is no wildcard, "
             "because a grant nobody was shown the parameters for is not a "
             "grant. Must be one of: "
             + ", ".join(DEFAULT_VOCABULARY.names()))
    granted = step.add_mutually_exclusive_group()
    granted.add_argument(
        "--grant-path", default="",
        help="bind a --grant to this path; shorthand for --grant-params "
             "'{\"path\": \"...\"}'")
    granted.add_argument(
        "--grant-params", default="",
        help="bind a --grant to this exact JSON parameter object; required "
             "for any action whose declared params are not just 'path'")
    step.add_argument("--allow-unstable", action="store_true",
                      help="do not require the stability guard")

    srv = sub.add_parser("serve", help="run the loopback REST service")
    srv.add_argument("--host", default=None)
    srv.add_argument("--port", type=int, default=None)
    srv.add_argument("--print-token", action="store_true",
                     help="print the declared DRIVER_TOKEN and exit, without "
                          "binding; errors if no token is declared, because a "
                          "generated one dies with this process")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        # Both calls are inside the try, and that includes the settings load:
        # a declared source that cannot even be tokenised, or a token that is
        # blank or too short, is a configuration fault, and it has to be
        # reported like every other one rather than as a traceback out of a
        # constructor. Only override when the flag is actually set -- passing
        # False would clobber DRIVER_DRY_RUN from the environment, which is a
        # quieter bug than it looks, since the flag would appear to do nothing.
        settings = load_settings(**({"dry_run": True} if args.dry_run else {}))
        driver = driver_from_settings(settings)
        return COMMANDS[args.command](args, driver)
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
