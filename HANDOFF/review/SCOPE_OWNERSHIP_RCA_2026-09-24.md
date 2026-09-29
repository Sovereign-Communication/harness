# Harness scope-ownership RCA and corrective rule

**Date:** 2026-09-24
**RCA ID:** `SCOPE-MISTAKE-001`
**Change boundary:** Documentation only. No source, test, hook, CI, identity, contact, runtime, installation, deployment, commit, or PR action was performed.

<!-- HANDOFF-SCOPE-BEGIN -->
scope: Harness
owner: Sovereign-Communication/Harness
purpose: Harness-only findings and remediation handoff
foreign_material: NONE
boundary: No foreign-repository findings, evidence, status, or remediation are included.
<!-- HANDOFF-SCOPE-END -->

## Concrete scope mistake

The previous cross-lane handoff process treated a dossier produced for a different owner as if it were a Harness-owned reviewer packet. That was a scope error. The dossier is quarantined in the other owner’s tree and is not imported, summarized, or used as Harness evidence here.

This record acknowledges the ownership failure only. It does not adopt the other owner’s findings, status, evidence, or remediation.

## Corrective rule

1. Every Harness handoff has exactly one owner: Harness.
2. A document is `owner-valid` only when it has the exact Harness metadata, identifies Harness in the body, passes the repository-local gate on the actual bytes, and contains assignable Harness evidence.
3. A document with a foreign-product alias in its path or bytes is `quarantined`; it is not edited, copied, committed, published, or used as evidence.
4. A document without a foreign alias but failing the metadata/owner gate is `blocked-by-metadata`; it remains untouched until the owner supplies valid metadata and an owner-approved manifest.
5. No legacy file is mass-migrated. Migration requires an owner-approved manifest and a deterministic rule, followed by fresh owner-local gate evidence.
6. Cross-lane observations must be split into separate owner records before handoff. This record contains no other owner’s product conclusion.

## Current disposition

The owner-local inventory is `SCOPE_INVENTORY_2026-09-24.md`. The operations index records exact paths and classifications without making this handoff a mixed document. PR contact is intentionally not attempted because no PR identity is established; the local branch, status, and evidence are the reportable record.
