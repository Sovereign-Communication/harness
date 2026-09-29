# Harness API contract finding

**Date:** 2026-09-24
**Owner:** Harness
**Change boundary:** Documentation only; this record update changes no Harness product source, tests, hooks, CI, installation, deployment, runtime, or device state. It changes no consumer source or test WIP.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

## Finding

The consuming operations adapter previously referenced `AutonomousAgent.run_hourglass_request(...)`, which is absent from the inspected Harness agent. The supported public entry point is `AutonomousAgent.run_prompt(...)` at `harness/agent.py:144`.

The current consumer WIP calls `run_prompt(text, session_id="fastdriver")` at `runtime/fastdriver.py:154-166`. A direct local CLI smoke returned exit 0 and a structured `harness_hourglass` response. This is evidence of API compatibility with the inspected local Harness checkout only; it is not evidence of a completed Harness mission or an authorized Harness implementation.

## Verified boundary behavior

The current local evidence recorded the following results without retaining provider responses, credentials, or message bodies:

- `python -m unittest tests.test_fastdriver -v` — **23/23 passed**.
- `python -m unittest discover -s tests -p "test_*.py" -q` — **52/52 passed**.
- A real ephemeral `ThreadingHTTPServer` probe returned 400 for missing, whitespace, array, malformed, and negative-`Content-Length` inputs. A declared length of `1,000,000,000` returned 413 in under one second without calling the engine; 30,000-level nested JSON returned 400 without calling the engine.
- Independent HTTP requests overlapped (`max_active=2`), and shutdown stopped the serve thread and closed the listening socket.
- The real `python runtime/fastdriver.py --serve` entry point returned a structured 400 for an empty POST. Empty CLI argument and empty stdin returned `empty_input` with exit 1.
- The real greeting command `python runtime/fastdriver.py hey` remains unavailable: exit 1 with `error=internal` and `detail=URLError` because the local Ollama endpoint refused the connection.

## Redacted provenance and WIP boundary

The following identifies current uncommitted consumer artifacts only. Hashes are redacted provenance; no raw patch, source bytes, credentials, provider configuration, or response body is transferred.

| Artifact | Current location | SHA-256 |
|---|---|---|
| Consumer source WIP | `runtime/fastdriver.py:115-166, 204-355` | `81cdc9a5e85fe25b13acb059e1ccdb98c8717ffda6b5c4e78e6de0d42bd4b7fc` |
| Consumer test WIP | `tests/test_fastdriver.py:275-422` | `520fa3333f71277baa8d11f7d37a40ffd0f30518e13613f835586df76eb7fe52` |
| Combined source/test diff (raw diff withheld) | `git diff -- runtime/fastdriver.py tests/test_fastdriver.py` | `a1089604c3903affc751527b8b2864862aa1e8f75cb2c99597582745901dff07` |

These source and test edits are **unowned WIP in the consumer operations checkout**. They are not Harness-authorized implementation, are not transferable, and must not be staged, committed, pushed, deployed, or treated as Harness product or completion evidence. This documentation update does not alter them.

## Owner-local remediation boundary

Harness continues to own the `run_prompt` entry point and its compatibility policy. No Harness source change, compatibility shim, second agent lane, deployment, or mass legacy-handoff migration is authorized by this record. Any adoption of the consumer WIP requires owner review through a separate authorized change.

## Evidence limits

- The local Harness checkout is behind origin; the single substantive CLI smoke proves only the inspected API contract.
- No deployed fastdriver or fully working OC instance was proven.
- No native Harness mission, native Jev judgment, edit verification, or production availability was proven.
- The local Ollama greeting failure remains unresolved; this record does not authorize starting, installing, or reconfiguring a provider.
- No outward contact or legacy handoff migration was performed.
