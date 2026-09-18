"""Diff-bound independent authorization before write (M4 phase 1).

The contract (drafted by MR-9, run through Harness itself): a verifier
OUTSIDE the brief pipeline and the apply model authorizes the EXACT
proposed diff -- fail-closed, attestation bound to the diff hash. Consent
today accepts INTENT (path, instruction, content-so-far); the bytes
actually written are whatever the apply model produces. This module owns
the attestation schema, the canonical signing payload, and the binding
checks; the enforcement seam (consulting it before every write) is M4
phase 2.

Binding rules (MR-9, binding on every check):
1. Only an operationally independent verifier may attest -- its identity
   and key are pinned outside the brief pipeline; a brief, plan approval,
   or intent probe is not evidence of authorization.
2. Before any write in every round: recompute the diff and base hashes,
   require the matching signed values, the outstanding nonce, an
   unexpired ``allow`` attestation, and a valid signature; any
   regeneration requires new authorization.
3. Missing, malformed, extra, unverifiable, mismatched, expired, or
   replayed evidence rejects -- and rejection performs no write and
   permits no fallback to intent approval.
"""
import hashlib
import json
import re
from typing import Callable

from .errors import HarnessError

DIFF_ATTESTATION_VERSION = "sovereign-diff-v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")
_VERIFIER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REQUIRED_FIELDS = ("verifier_id", "verdict", "diff_sha256", "base_sha256",
                    "round_nonce", "expires_at", "signature")

VerifySignature = Callable[[bytes, str], bool]


def compute_diff_sha256(diff_bytes):
    """SHA-256 hex of the complete immutable proposed diff bytes -- never a
    summary or intent."""
    if isinstance(diff_bytes, str):
        diff_bytes = diff_bytes.encode("utf-8")
    return hashlib.sha256(diff_bytes).hexdigest()


def canonical_attestation_payload(verifier_id, verdict, diff_sha256,
                                  base_sha256, round_nonce, expires_at):
    """The exact bytes the verifier signs: UTF-8 newline-joined values,
    WITHOUT a trailing newline. Any change to serialization would break
    every existing signature -- treat as frozen."""
    joined = "\n".join([
        DIFF_ATTESTATION_VERSION, verifier_id, verdict, diff_sha256,
        base_sha256, round_nonce, str(int(expires_at))])
    return joined.encode("utf-8")


def _strict_json_object(raw, what):
    """Parse ONE JSON object, rejecting duplicate keys and non-object
    roots (stricter than dag._parse_json_object: the attestation is
    machine-to-machine, so duplicate keys are hostile, not sloppy)."""

    def _no_dupes(pairs_):
        seen = {}
        for k, v in pairs_:
            if k in seen:
                raise HarnessError(
                    f"{what} has duplicate JSON key {k!r}")
            seen[k] = v
        return seen

    if not raw or not str(raw).strip():
        raise HarnessError(f"empty {what}")
    try:
        data = json.loads(str(raw), object_pairs_hook=_no_dupes)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"failed to parse {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError(f"{what} must be a JSON object")
    return data


def parse_attestation(raw, *, diff_sha256, base_sha256, round_nonce, now,
                      verify_signature=None):
    """Validate ONE attestation against THIS round's frozen proposal.

    Returns the parsed dict on success; raises HarnessError on every
    other path (fail-closed: the caller must treat any exception as
    "no authorization", never as "proceed with the previous one").
    ``verify_signature(payload_bytes, signature_hex) -> bool`` is the
    pinned-verifier seam; without it nothing can authorize a write.
    """
    data = _strict_json_object(raw, "diff attestation")
    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        raise HarnessError(f"diff attestation missing fields: {missing}")
    extra = [f for f in data if f not in _REQUIRED_FIELDS]
    if extra:
        raise HarnessError(f"diff attestation has unknown fields: {extra}")

    verifier_id = data["verifier_id"]
    if not isinstance(verifier_id, str) or not _VERIFIER_ID.match(verifier_id):
        raise HarnessError("diff attestation verifier_id malformed")
    verdict = data["verdict"]
    if verdict not in ("allow", "deny"):
        raise HarnessError(f"diff attestation verdict {verdict!r} invalid")
    for field in ("diff_sha256", "base_sha256", "round_nonce"):
        value = data[field]
        if not isinstance(value, str) or not _HEX64.match(value):
            raise HarnessError(f"diff attestation {field} malformed")
    expires_at = data["expires_at"]
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) \
            or expires_at < 0:
        raise HarnessError("diff attestation expires_at must be a unix-seconds int")
    signature = data["signature"]
    if not isinstance(signature, str) or not _HEX128.match(signature):
        raise HarnessError("diff attestation signature malformed")

    if verdict != "allow":
        raise HarnessError(f"diff attestation is a {verdict!r}, not an allow")
    if data["diff_sha256"] != diff_sha256:
        raise HarnessError(
            "diff attestation is bound to a different diff (hash mismatch)")
    if data["base_sha256"] != base_sha256:
        raise HarnessError(
            "diff attestation is bound to a different base snapshot")
    if data["round_nonce"] != round_nonce:
        raise HarnessError("diff attestation nonce does not match this round")
    if expires_at <= now:
        raise HarnessError("diff attestation expired before the write")
    if verify_signature is None:
        raise HarnessError(
            "no attestation verifier configured; refusing fail-closed "
            "(intent approval is never a fallback)")
    payload = canonical_attestation_payload(
        verifier_id, verdict, data["diff_sha256"], data["base_sha256"],
        data["round_nonce"], expires_at)
    if not verify_signature(payload, signature):
        raise HarnessError("diff attestation signature failed verification")
    return data


def require_attestation(raw, *, diff_bytes, base_sha256, round_nonce, now,
                        verify_signature=None):
    """One-call gate for the phase-2 enforcement seam: hash the frozen
    diff bytes and validate the attestation against them."""
    return parse_attestation(
        raw, diff_sha256=compute_diff_sha256(diff_bytes),
        base_sha256=base_sha256, round_nonce=round_nonce, now=now,
        verify_signature=verify_signature)
