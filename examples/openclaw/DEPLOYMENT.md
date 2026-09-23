# OpenClaw deployment record — 2026-09-23 UTC

This records one deployment, not product STATUS or a completed 24-hour soak.

## Proven state

- The Pixel-only durable bridge from SCMessenger PR #363 is deployed. The deployment pass observed an active service, seven passing Linux tests, 26 imported historical IDs, and zero queued replays.
- Deployed bridge SHA256: `339a9b339a47b72db70b49104c82d060d7571fccbbb9156182e8b8c4610eeead`.
- A fresh encrypted off-host backup passed byte-identical decryption and five restored SQLite integrity checks. Snapshot and termination-protection changes were denied by AWS IAM.
- The Harness worker is **not deployed** and has **no timer**. No successful worker edit, native Jev completion judgment, Pixel completion delivery, or unattended soak is established.
- Needle 3 executed one safe in-memory classification tool under Python 3.12. It is not connected to production routing.

## Reproduce the failed worker attempt

Use the branch for Harness PR #73, initially `500c4847aaf96e083db4ad994d1a0188ff0385ef`. Load credentials from private environment files; never put values in arguments, logs, or Git. The relevant source transfer to OpenRouter and native Jev was explicitly authorized.

The deployment pass invoked:

```sh
HARNESS_JEV_DISABLE=1 python -m harness.cli apply \
  --file examples/openclaw/runtime/dogfood_worker.py \
  --instruction "<scoped implementation request>" \
  --model deepseek/deepseek-v4.1-flash --backend diff \
  --max-lines 500 --max-rounds 1 --max-rotations 0 --max-tokens 5000 \
  --task-max-cost 0.01 --no-consent \
  --verify 'python -m unittest discover -s examples/openclaw/tests -p test_dogfood_worker.py -q' \
  --out runtime/worker-apply-2.json --quiet
```

Do not rerun this paid command until its budget behavior is reviewed. It returned `verify_failed`, persisted no source edit, observed DeepSeek and GLM, and reported OpenRouter cost `$0.01297081764` despite the requested `$0.01` ceiling. Two prior Ling attempts deferred; total reported OpenRouter cost was `$0.01415811573`. This is structured run evidence, not an independently reconciled provider invoice. Native Jev was not run for this worker attempt. Disabling Jev here separated paid accounting for the bounded attempt; native Jev must still pass before any production worker deployment.

## Needle reproduction

The existing `cactus-needle` package is version `3.0.4`. Its Python 3.9 environment fails to import; the observed workaround uses `/usr/bin/python3.12` with `PYTHONPATH=/home/ec2-user/needle-venv/lib/python3.9/site-packages`.

The smoke used `needle.Needle(tools=[classify_safe], weights='/home/ec2-user/needle3.cact')`, where the decorated tool only appends to an in-memory list and returns `safe`; `run` used `max_steps=1, max_new_tokens=64`. Needle executes registered tools, so do not register production actions for this smoke.

| Artifact | SHA256 |
| --- | --- |
| Model | `c9d915eca282ed42d1a09b143b592adb4cc6744ffe2d294adf5cfc5548170c38` |
| Engine | `5eb163c5ed33bd914c103ef8eba2134c7bb2d97bafafdd69f410a4a3100e8c37` |
| Library | `2581e7d46acd4f66c5839bcfb06b0af11c157c8775636875beb0af5ca35ded54` |

## Recovery and remaining gates

Resolve the running instance from AWS Name tag `openclaw-node` in `us-east-1`; do not reuse a historical IP or touch the separate SCM cloud node. Preserve host keys, private identities and credentials outside Git. The private operations receipt records the DPAPI backup location, archive hash, and restore proof. DPAPI recovery requires the original Windows user context; portable recovery has not been demonstrated.

Before changes, preserve application state and use SQLite's backup API, then decrypt into a scratch location and check database integrity. Restore only after stopping affected services and retaining the current files. Do not overwrite an existing bridge database with an older snapshot without reconciling later work.

The prior bridge script and allowlist are retained on-host under `~/oc-candidate/`. The prior bridge has unsafe intake behavior: for incident containment stop `scm-bridge.service`; do not automatically restart the old script. For reinstall, retrieve SCMessenger PR #363 commit `43b0d371884d406182d07ebd6c278ae55a92ce9d`, verify the deployed file hash, run its seven bridge tests on Linux, provide the private Pixel-only allowlist, and reconcile historical IDs before starting `scm-bridge.service`. Never guess peer keys or replay uncertain sends.

Keep the worker disabled until a real scoped edit passes prescribed tests, native Jev, independent verification, budget enforcement and interruption recovery. Then prove a Git artifact, an SCM delivery receipt and a bounded soak separately. This record is not a complete unattended rebuild installer.
