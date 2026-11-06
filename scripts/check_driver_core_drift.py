"""Fail when the vendored driver_core drifts from its standalone pin.

The standalone repository publishes no tags, so the pin is the version plus
the upstream commit recorded in ``driver_core/UPSTREAM.json``.  The default
check is offline and compares the pin with ``driver_core.__version__``; with
``--remote`` it also reads the standalone ``driver_core/__init__.py`` from the
pinned raw URL and fails if that version differs.  A fetch failure is a drift
failure, never a silent pass.

No harness imports and no third-party dependencies: the check runs in the
checkout-hygiene job before the package is installed.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PIN = ROOT / "driver_core" / "UPSTREAM.json"
DEFAULT_VENDORED = ROOT / "driver_core" / "__init__.py"
MAX_REMOTE_BYTES = 64 * 1024
_VERSION_RE = re.compile(
    r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


class DriftError(Exception):
    """A pin, vendored copy, or remote response cannot be used."""


def version_from_text(text, label="source"):
    """Extract ``__version__`` from Python source, or raise DriftError."""
    match = _VERSION_RE.search(text or "")
    if not match:
        raise DriftError(f"{label} does not declare a top-level __version__")
    return match.group(1).strip()


def read_version(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DriftError(f"cannot read {path}: {exc}") from exc
    return version_from_text(text, str(path))


def load_pin(path=DEFAULT_PIN):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DriftError(f"cannot read pin {path}: {exc}") from exc
    except ValueError as exc:
        raise DriftError(f"pin {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DriftError(f"pin {path} must be a JSON object")
    for key in ("repository", "version"):
        if not str(data.get(key) or "").strip():
            raise DriftError(f"pin {path} is missing {key}")
    if not isinstance(data.get("version"), str):
        raise DriftError(f"pin {path} version must be a string")
    source = data.get("source")
    if source is not None and urllib.parse.urlsplit(str(source)).scheme != "https":
        raise DriftError(f"pin {path} source must be an https URL")
    return data


def fetch_remote(url, timeout=15.0):
    """Read a bounded upstream version file over HTTPS."""
    if urllib.parse.urlsplit(str(url)).scheme != "https":
        raise DriftError("remote source must be an https URL")
    request = urllib.request.Request(
        str(url), headers={"User-Agent": "harness-driver-core-drift/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_REMOTE_BYTES + 1)
    if len(body) > MAX_REMOTE_BYTES:
        raise DriftError("remote version file exceeds the size cap")
    return body.decode("utf-8", "replace")


def check_drift(pin_path=DEFAULT_PIN, vendored_path=DEFAULT_VENDORED,
                remote_text=None, remote_url=None, fetch=None):
    """Return a machine-readable drift result; never fetch unless asked."""
    failures = []
    pin = {}
    vendored = None
    upstream = None
    try:
        pin = load_pin(pin_path)
    except DriftError as exc:
        failures.append(str(exc))
    try:
        vendored = read_version(vendored_path)
    except DriftError as exc:
        failures.append(str(exc))
    pinned = str(pin.get("version") or "").strip() or None
    if pinned and vendored and pinned != vendored:
        failures.append(
            f"vendored driver_core is {vendored}, pin declares {pinned}")
    if remote_text is None and remote_url:
        fetcher = fetch or fetch_remote
        try:
            remote_text = fetcher(remote_url)
        except Exception as exc:
            failures.append(f"remote_unavailable: {exc}")
    if remote_text is not None:
        try:
            upstream = version_from_text(remote_text, "upstream driver_core")
        except DriftError as exc:
            failures.append(str(exc))
        if upstream and vendored and upstream != vendored:
            failures.append(
                f"upstream driver_core is {upstream}, vendored is {vendored}")
        if upstream and pinned and upstream != pinned:
            failures.append(
                f"upstream driver_core is {upstream}, pin declares {pinned}")
    return {
        "ok": not failures,
        "vendored": vendored,
        "pinned": pinned,
        "upstream": upstream,
        "remote_checked": remote_text is not None,
        "pin": str(pin_path),
        "failures": failures,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fail when vendored driver_core drifts from its pin.")
    parser.add_argument("--pin", default=str(DEFAULT_PIN))
    parser.add_argument("--vendored", default=str(DEFAULT_VENDORED))
    parser.add_argument("--remote", action="store_true",
                        help="also compare with the standalone repository")
    parser.add_argument("--url", default=None,
                        help="override the pin's upstream source URL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    remote_url = args.url
    if args.remote and not remote_url:
        try:
            remote_url = load_pin(args.pin).get("source")
        except DriftError:
            remote_url = None
    result = check_drift(
        pin_path=args.pin, vendored_path=args.vendored, remote_url=remote_url)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("driver_core vendored={vendored} pinned={pinned} upstream={upstream}".format(**result))
        for failure in result["failures"]:
            print(f"DRIFT: {failure}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
