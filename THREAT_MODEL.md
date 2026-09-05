# Threat model

Harness executes model-proposed edits and model-supplied verification
commands. This document names the trust boundaries, what a malicious or
merely-confused model could do, and the controls in place. Read it before
extending any surface that touches the filesystem or spawns processes.

## Trust boundaries

```
OpenRouter models  ── untrusted ──▶  Harness engine  ── trusted host ──▶  your files & shell
      (text in, text out)            (policy enforced here)            (the asset)
```

Everything a model returns is **untrusted input**, including JSON that looks
like a verdict, file content that looks like an edit, and prose that claims a
verification passed. The user's machine is the asset. The MCP client (e.g.
an IDE agent) sits between the human and the engine and inherits the human's
authority, so MCP write/exec surfaces are gated one step stricter than CLI.

## Controls

| Risk | Control | Where |
|---|---|---|
| Model-invented verification command | `verify_cmd` is parsed with `shlex`, executed `shell=False`, hard timeout, gate id bound into continuation state; MCP requires explicit `allow_verify` per call | `harness/apply.py`, `harness/mcp.py` |
| Path escape via edit targets | Targets must resolve inside the sandbox root; absolute escapes, `..` traversal, and symlink swaps are refused; original file mode is preserved | `harness/apply.py` (`_AtomicWrite`, bench sandbox) |
| Arbitrary overwrite via MCP apply | Root containment check + backup written outside the tree; write refused (never best-effort) when containment fails | `harness/apply.py`, `harness/mcp.py` |
| Prompt marker smuggling | `HARNESS_READY`/`HARNESS_DEFER` protocol markers are stripped from file content anywhere in the body, so a model cannot smuggle protocol text into user files | `harness/apply.py` |
| Spend runaway (model or bug) | Worst-case preflight against the ceiling before every network call; per-actual enforcement; consent, rotation, and probe calls all preflighted; per-invocation `--max-cost` on verify/bench/capabilities | `harness/core.py` (SpendGovernor) |
| BYOK key leakage | Org-prefix denylist refuses disallowed models outright; learned prefixes persisted and re-checked | `harness/core.py` |
| Credential hygiene | Key files warn loudly when group/world readable (POSIX); `expect_key_label` supports exact match; labels never echoed in errors | `harness/config.py` |
| Ledger tampering | Hash-chained JSONL entries; `verify` recomputes the chain; corrupt/torn lines are quarantined with a stderr note instead of crashing; cross-process advisory lock; 10 MB rotation | `harness/ledger.py` |
| MCP tool abuse | `allow_verify` confirmation gate on verification commands; structured error codes; `notifications/cancelled` aborts in-flight work via cancellation events | `harness/mcp.py` |

## Accepted residual risks

- **Verification commands still run on your host.** The gate executes with
  `shell=False` under a timeout, but whatever command string is approved
  runs with the harness's privileges. Review `verify_cmd` on any apply you
  did not write yourself. MCP callers must pass `allow_verify` per call.
- **Model output drives file content.** The verification gate is the real
  defense; the consent probe and panel votes are advisory signals, not
  sandboxes.
- **The ledger proves what was recorded, not what is true.** It is
  append-only and hash-chained, but an attacker with filesystem write
  access can rewrite it wholesale.

## Reporting

Open a private security advisory via GitHub rather than a public issue if
the report includes a working exploit.
