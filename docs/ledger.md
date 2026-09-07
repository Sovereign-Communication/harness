# Ledger semantics

The autonomy ledger is a local JSONL evidence log. Each entry contains a
sequence number, previous hash, canonicalized body, and SHA-256 hash.

## What it proves

`ledger verify` proves that the entries currently loaded from the active file
form an internally consistent hash chain. It does not prove that the recorded
event happened, that the model's claim was true, or that an attacker with
filesystem access did not replace the complete file.

Malformed or torn lines are quarantined on load and reported. A repair
operation truncates the file to its longest valid prefix. Repair is destructive
to the invalid tail and should be treated as an operator action.

The active ledger rotates after the configured size limit and retains a bounded
number of old files. Rotated files are retention artifacts; users requiring a
complete audit archive should preserve and externally protect all segments.

## Operational guidance

- Keep the ledger on a durable local filesystem.
- Back it up before repair.
- Restrict permissions on the ledger directory.
- Preserve rotated files if evidence must remain complete.
- Treat `quarantined`, failed verification, and repair output as audit events
  requiring review.
