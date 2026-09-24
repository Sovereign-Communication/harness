# OC findings handoff worker

This Harness-only example accepts locally approved findings and writes exactly
`HANDOFF/OC_FINDINGS.md` in an isolated local worktree. It never chooses a
repository, target path, command, verifier, database, or endpoint from a
manifest. It leaves a local branch for review and does not merge, push, message,
or change any external instance or configuration.

## Approval and intake

1. Place an unsigned candidate at
   `.harness/oc_handoff/inbox/candidates/<task-id>.json` with only `task_id`,
   `repo_sha`, and a `findings` array. Each finding has `finding_id`, `severity`,
   `summary`, `evidence` (tracked Harness paths plus line numbers), and
   `recommendation`.
2. Set `HARNESS_OC_HANDOFF_APPROVER` and a private 32-byte hex key in
   `HARNESS_OC_HANDOFF_HMAC_KEY`. Keep the key in the local operator environment;
   never put it in the repository or an untrusted worker environment.
3. Run `python -m examples.oc_handoff.worker approve --task-id <task-id>` and
   type the exact approval phrase after reviewing the candidate. Approval signs
   the fixed schema, base commit, nonce, and 15-minute expiry using the existing
   Harness attestation payload contract.
4. Run `python -m examples.oc_handoff.worker run-once`. Harness verifies the
   signature and approval identity, rejects expiry and replay, and runs one
   native Jev check over the compact finding summary, cited evidence, and
   recommendation. Its exact request payload is capped at 800 bytes before
   dispatch; the 1,024-token reservation is bounded by a $0.05 call ceiling.
   Code validates the full signed record and append mechanics separately, then creates a local
   WorktreeIsolation branch. A fixed validator and exact changed-file audit
   permit only `HANDOFF/OC_FINDINGS.md` to be committed.

The private key is shared-secret HMAC-SHA512 material. This local example does
not distribute it, contact a remote machine, or claim remote enforcement.
The native Jev check fails closed when Jev is unavailable, falls back, or
refuses the bounded spend reservation. No manifest-controlled executable or
network destination exists.

The receipt is available through `pending`; its consumer acknowledges it with
`ack --task-id ... --receipt-sha256 ...`. The worker will not start another task
until the previous handoff branch is merged into the executing checkout. If a
crash leaves the output state ambiguous, the task becomes `uncertain` and needs
human review; it is never automatically replayed.
