"""Read-only environment preflight for ``harness doctor``.

The report contains check outcomes and safe metadata only. Provider, Jev,
driver, and MCP credentials are used only in request headers or child stdin;
they are never included in details, fixes, exceptions, or JSON output.
"""
import ipaddress
import json
import math
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

from . import config, osal, tools
from .errors import HarnessError


OPENROUTER_KEY_URL = config.OPENROUTER_KEY_URL
OPENROUTER_HOST = urllib.parse.urlsplit(OPENROUTER_KEY_URL).hostname
DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def _check(name, status, detail, fix=None, **metadata):
    out = {"name": name, "status": status, "detail": detail}
    if fix:
        out["fix"] = fix
    out.update(metadata)
    return out


def check_python():
    try:
        executable = tools.resolve_python()
    except HarnessError:
        return _check(
            "python", "MISSING", "no usable Python interpreter was resolved",
            "Install Python, put python/python3/py on PATH, or set HARNESS_PYTHON.")
    return _check("python", "OK", f"resolved {executable}")


def check_ruff():
    try:
        argv = tools.ruff_argv(strict=True)
    except HarnessError:
        return _check(
            "ruff", "MISSING", "ruff is not available",
            "Install the dev extra (pip install -e '.[dev]'), put ruff on PATH, "
            "or set HARNESS_RUFF.")
    return _check("ruff", "OK", "resolved " + " ".join(argv))


def _safe_host(endpoint):
    try:
        parsed = urllib.parse.urlsplit(str(endpoint))
        return parsed.hostname
    except (TypeError, ValueError):
        return None


def _dns_class(host):
    """Resolve a host and report only its address class, never raw addresses."""
    if not host:
        return {"class": "unresolved", "count": 0}
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
        addresses = [literal]
    except ValueError:
        try:
            rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            addresses = []
            for row in rows:
                try:
                    addresses.append(ipaddress.ip_address(
                        row[4][0].split("%", 1)[0]))
                except (ValueError, IndexError, TypeError):
                    continue
        except (OSError, socket.gaierror):
            return {"class": "unresolved", "count": 0}
    public = any(address.is_global for address in addresses)
    non_public = any(not address.is_global for address in addresses)
    if public and non_public:
        classification = "mixed"
    elif public:
        classification = "public"
    elif addresses:
        classification = "non-public"
    else:
        classification = "unresolved"
    return {"class": classification, "count": len(addresses)}


def check_dns(settings):
    endpoint = getattr(settings, "jev_endpoint", DEFAULT_JEV_ENDPOINT)
    targets = {
        "openrouter.ai": _dns_class(OPENROUTER_HOST),
        (_safe_host(endpoint) or "TypeSafe endpoint"): _dns_class(_safe_host(endpoint)),
    }
    degraded = any(item["class"] != "public" for item in targets.values())
    detail = "; ".join(
        f"{host}={item['class']} ({item['count']} address(es))"
        for host, item in targets.items())
    fix = None
    if degraded:
        fix = ("Remove fake-IP DNS overrides or change VPN/DNS mode, then retry; "
               "both external probe hosts should resolve to public addresses.")
    return _check("dns", "DEGRADED" if degraded else "OK", detail, fix,
                  hosts=targets)


def _dns_host_is_public(dns_check, host):
    item = (dns_check or {}).get("hosts", {}).get(host, {})
    return item.get("class") == "public"


def _openrouter_key_info(api_key, *, timeout=5):
    """Make one bounded, read-only /key request without following redirects."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(
        OPENROUTER_KEY_URL,
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
        method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(65536)
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return status, None


def _number(value, *, allow_negative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (result < 0 and not allow_negative):
        return None
    return result


def check_openrouter_key(dns_check=None):
    try:
        api_key, source = config.resolve_api_key_with_source()
    except Exception:
        return _check(
            "openrouter_key", "MISSING", "key source could not be read",
            "Check OPENROUTER_API_KEY and ~/.config/scmorc/openrouter*.env or "
            "$HARNESS_CONFIG_DIR/openrouter.env.")
    if not api_key:
        return _check(
            "openrouter_key", "MISSING", "no OpenRouter key is configured",
            "Set OPENROUTER_API_KEY or add OPENROUTER_API_KEY to "
            "~/.config/scmorc/openrouter*.env or "
            "$HARNESS_CONFIG_DIR/openrouter.env (default: "
            "~/.config/harness/openrouter.env).")
    safe_source = dict(source or {})
    if not _dns_host_is_public(dns_check, OPENROUTER_HOST):
        return _check(
            "openrouter_key", "DEGRADED",
            "key is configured; /key probe skipped because DNS was not public",
            "Fix the DNS/VPN resolution shown by the dns check, then retry.",
            source=safe_source)
    try:
        status, payload = _openrouter_key_info(api_key)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return _check(
            "openrouter_key", "DEGRADED",
            "key source is configured; provider /key endpoint was unreachable",
            "Check network/TLS access to openrouter.ai, then rerun harness doctor.",
            source=safe_source)
    if status in (401, 403):
        return _check(
            "openrouter_key", "MISSING", "provider rejected the configured key",
            "Replace the key in its reported source and confirm it is enabled.",
            source=safe_source)
    if status != 200 or not isinstance(payload, dict):
        return _check(
            "openrouter_key", "DEGRADED",
            f"provider /key endpoint returned HTTP {status}",
            "Check provider availability and rerun harness doctor.",
            source=safe_source)
    data = payload.get("data")
    if not isinstance(data, dict):
        return _check(
            "openrouter_key", "DEGRADED", "provider /key response was incomplete",
            "Check provider availability and rerun harness doctor.",
            source=safe_source)
    limit = _number(data.get("limit"))
    remaining = _number(data.get("limit_remaining"), allow_negative=True)
    if limit is None:
        return _check(
            "openrouter_key", "MISSING", "provider key has no valid spend limit",
            "Set a spend limit for this provider key.", source=safe_source)
    if remaining is not None and remaining <= 0:
        return _check(
            "openrouter_key", "MISSING", "provider key has no remaining allowance",
            "Restore provider allowance or configure a key with available spend.",
            source=safe_source, limit=limit, remaining=remaining)
    return _check("openrouter_key", "OK", "provider key accepted",
                  source=safe_source, limit=limit, remaining=remaining)


def check_jev_key(settings=None):
    if getattr(settings, "jev_disabled", False):
        return _check(
            "jev_key", "DEGRADED", "Jev is explicitly disabled",
            "Unset HARNESS_JEV_DISABLE=1 if this environment should use Jev.")
    try:
        key = (getattr(settings, "jev_api_key", None)
               or config.resolve_jev_key())
    except Exception:
        key = None
    if not key:
        return _check(
            "jev_key", "MISSING", "no Jev key is configured",
            "Set HARNESS_JEV_KEY, TYPESAFE_API_KEY, or JEV_API_KEY, or add a key "
            "to ~/.config/scmorc/jev.env or $HARNESS_CONFIG_DIR/jev.env.")
    return _check("jev_key", "OK", "Jev key is configured")


def _typesafe_probe(endpoint, *, timeout=4):
    request = urllib.request.Request(str(endpoint), method="HEAD",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return status


def check_typesafe(settings, dns_check=None):
    endpoint = getattr(settings, "jev_endpoint", DEFAULT_JEV_ENDPOINT)
    host = _safe_host(endpoint)
    if not _dns_host_is_public(dns_check, host):
        return _check(
            "typesafe", "DEGRADED",
            "reachability probe skipped because endpoint DNS was not public",
            "Fix the DNS/VPN resolution shown by the dns check, then retry.")
    try:
        status = _typesafe_probe(endpoint)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return _check(
            "typesafe", "DEGRADED", "TypeSafe endpoint could not be reached",
            "Check network/TLS access and HARNESS_JEV_ENDPOINT, then rerun harness doctor.")
    if 200 <= status < 500:
        return _check("typesafe", "OK", f"endpoint responded HTTP {status}")
    return _check(
        "typesafe", "DEGRADED", f"endpoint responded HTTP {status}",
        "Check TypeSafe service availability, then rerun harness doctor.")


def check_driver():
    from .perception_client import PerceptionAdapter, PerceptionUnavailable

    try:
        payload = PerceptionAdapter(timeout=2).health()
    except (PerceptionUnavailable, OSError, TimeoutError):
        return _check(
            "driver", "MISSING", "driver-core health endpoint is unavailable",
            "Start it with `driver-core serve`; configure DRIVER_BASE_URL and "
            "DRIVER_TOKEN when it uses a non-default endpoint or token.")
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "up":
        version = payload.get("version")
        detail = "driver-core is up" + (f" (version {version})" if version else "")
        return _check("driver", "OK", detail)
    return _check(
        "driver", "DEGRADED", "driver-core responded with an unexpected health status",
        "Check `driver-core serve` logs and the configured DRIVER_BASE_URL.")


def _connection_refused(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError) or (
        isinstance(reason, OSError) and getattr(reason, "winerror", None) == 10061)


def check_gui_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return _check(
            "gui_port", "MISSING", "GUI port must be between 1 and 65535",
            "Pass --gui-port with a valid loopback port.")
    url = f"http://127.0.0.1:{port}/api/status"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            status = response.status
            body = response.read(1024).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(1024).decode("utf-8", errors="ignore")
        exc.close()
    except urllib.error.URLError as exc:
        if _connection_refused(exc):
            return _check("gui_port", "OK", f"127.0.0.1:{port} is free")
        return _check(
            "gui_port", "DEGRADED", f"127.0.0.1:{port} could not be inspected",
            "Check local firewall or listener state, or choose another --gui-port.")
    except (OSError, TimeoutError):
        return _check(
            "gui_port", "DEGRADED", f"127.0.0.1:{port} could not be inspected",
            "Check local firewall or listener state, or choose another --gui-port.")
    if status == 200:
        return _check("gui_port", "OK", f"Harness GUI responds on 127.0.0.1:{port}")
    if status == 401 and "X-Harness-Auth" in body:
        return _check(
            "gui_port", "OK", f"Harness GUI is running on 127.0.0.1:{port}; auth is required")
    return _check(
        "gui_port", "DEGRADED", f"127.0.0.1:{port} is occupied by an unverified service",
        "Choose another --gui-port or stop the service occupying this port.")


def check_ledger(path):
    if not path:
        return _check(
            "ledger", "MISSING", "no ledger path is configured",
            "Set HARNESS_LEDGER to a writable JSONL path.")
    path = os.path.abspath(os.path.expanduser(str(path)))
    parent = os.path.dirname(path)
    if os.path.exists(path):
        if not os.path.isfile(path):
            return _check(
                "ledger", "MISSING", "configured ledger path is not a file",
                "Set HARNESS_LEDGER to a writable JSONL file path.")
        try:
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError:
            return _check(
                "ledger", "MISSING", "configured ledger file is not writable",
                "Grant write access to the ledger file or set HARNESS_LEDGER.")
        return _check("ledger", "OK", "configured ledger file is writable")
    if not os.path.isdir(parent):
        return _check(
            "ledger", "MISSING", "ledger parent directory does not exist",
            "Create the directory or set HARNESS_LEDGER to an existing writable directory.")
    probe = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".harness-doctor-", dir=parent,
                                         delete=False) as handle:
            probe = handle.name
    except OSError:
        return _check(
            "ledger", "MISSING", "ledger parent directory is not writable",
            "Grant write access to the ledger directory or set HARNESS_LEDGER.")
    finally:
        if probe:
            try:
                os.unlink(probe)
            except OSError:
                pass
    return _check("ledger", "OK", "ledger parent is writable; ledger is created on first write")


def check_mcp_stdio(auth_token=None, *, timeout=20):
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "harness-doctor", "version": "1"},
        },
    }
    tool_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    call_params = {"name": "ledger_status", "arguments": {"limit": 1}}
    if auth_token:
        call_params["_meta"] = {"harness_token": auth_token}
    ledger_call = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": call_params,
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    payload = "\n".join(json.dumps(message) for message in
                         (initialize, initialized, tool_list, ledger_call)) + "\n"
    try:
        runtime = tools.resolve_python()
    except HarnessError:
        return _check(
            "mcp_stdio", "MISSING", "MCP runtime could not be resolved",
            "Install Python, put python/python3/py on PATH, or set HARNESS_PYTHON.")
    completed = osal.run([runtime, "-m", "harness.mcp"], input_text=payload,
                         timeout=timeout)
    try:
        frames = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    except ValueError:
        frames = []
    by_id = {frame.get("id"): frame for frame in frames if isinstance(frame, dict)}
    init = by_id.get(1, {})
    listing = by_id.get(2, {}).get("result", {})
    call = by_id.get(3, {})
    tools_list = listing.get("tools", []) if isinstance(listing, dict) else []
    names = {item.get("name") for item in tools_list if isinstance(item, dict)}
    result = call.get("result", {}) if isinstance(call, dict) else {}
    structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
    verified = structured.get("verified", {}) if isinstance(structured, dict) else {}
    if (completed.returncode != 0 or "error" in init or "error" in by_id.get(2, {})
            or "error" in call or "ledger_status" not in names
            or not isinstance(verified, dict) or verified.get("ok") is not True
            or result.get("isError") is True):
        return _check(
            "mcp_stdio", "MISSING", "MCP initialize, tools/list, or ledger_status failed",
            "Check MCP configuration, provider key access, ledger integrity, and "
            "`harness-mcp --help`.")
    return _check(
        "mcp_stdio", "OK",
        f"stdio initialize and ledger_status passed; {len(tools_list)} tools listed")


def _minimal_settings():
    """Safe defaults used only when the normal config cannot be loaded."""
    cfg = {}
    try:
        with open(os.path.join(config.CONFIG_DIR, "config.json"), encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            cfg = value
    except (OSError, ValueError):
        pass
    return SimpleNamespace(
        jev_endpoint=(os.environ.get("HARNESS_JEV_ENDPOINT")
                      or cfg.get("jev_endpoint") or DEFAULT_JEV_ENDPOINT),
        ledger_path=(os.environ.get("HARNESS_LEDGER")
                     or cfg.get("ledger_path")
                     or os.path.join(config.CONFIG_DIR, "ledger.jsonl")),
        mcp_auth_token=(os.environ.get("HARNESS_MCP_AUTH_TOKEN")
                        or cfg.get("mcp_auth_token")),
        jev_disabled=os.environ.get("HARNESS_JEV_DISABLE") == "1")


def run_doctor(settings, *, gui_port=8765, config_error=False):
    dns = check_dns(settings)
    checks = [
        check_python(),
        check_ruff(),
        check_openrouter_key(dns),
        check_jev_key(settings),
        check_typesafe(settings, dns),
        dns,
        check_driver(),
        check_gui_port(gui_port),
        check_ledger(getattr(settings, "ledger_path", None)),
        check_mcp_stdio(getattr(settings, "mcp_auth_token", None)),
    ]
    if config_error:
        checks.insert(0, _check(
            "config", "MISSING", "Harness settings did not load cleanly",
            "Repair config.json and invalid HARNESS_* settings, then rerun harness doctor."))
    missing = any(item["status"] == "MISSING" for item in checks)
    degraded = any(item["status"] == "DEGRADED" for item in checks)
    status = "MISSING" if missing else "DEGRADED" if degraded else "OK"
    return {"status": status, "checks": checks, "exit_code": 1 if missing else 0}


def print_report(report, *, json_output=False):
    if json_output:
        print(json.dumps(report, sort_keys=True))
        return
    print("Harness environment preflight")
    for item in report["checks"]:
        print(f"[{item['status']}] {item['name']}: {item['detail']}")
        if item.get("fix"):
            print(f"       Fix: {item['fix']}")
    print(f"Result: {report['status']} (exit {report['exit_code']})")
