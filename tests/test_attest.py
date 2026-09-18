"""Sovereign-diff-v1 attestation contract (M4 phase 1, drafted by MR-9).

Every test encodes a binding rule: missing/malformed/extra/unverifiable/
mismatched/expired/replayed evidence is rejected, and rejection is the
only fallback -- nothing here can ever return "proceed".
"""
import json
import unittest

from harness.attest import (DIFF_ATTESTATION_VERSION,
                            canonical_attestation_payload,
                            compute_diff_sha256, parse_attestation,
                            require_attestation)
from harness.errors import HarnessError

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-ok\n+ok\n"
DIFF_SHA = compute_diff_sha256(DIFF)
BASE_SHA = "b" * 64
NONCE = "c" * 64
NOW = 1_000_000


def _attestation(**overrides):
    fields = {
        "verifier_id": "verifier-1",
        "verdict": "allow",
        "diff_sha256": DIFF_SHA,
        "base_sha256": BASE_SHA,
        "round_nonce": NONCE,
        "expires_at": NOW + 60,
        "signature": "d" * 128,
    }
    fields.update(overrides)
    fields = {k: v for k, v in fields.items() if v is not None}
    return json.dumps(fields)


def _verify(payload, sig):
    return True


class CanonicalPayloadTests(unittest.TestCase):
    def test_payload_is_version_prefixed_and_newline_joined(self):
        payload = canonical_attestation_payload(
            "verifier-1", "allow", DIFF_SHA, BASE_SHA, NONCE, NOW + 60)
        self.assertEqual(
            payload,
            f"{DIFF_ATTESTATION_VERSION}\nverifier-1\nallow\n{DIFF_SHA}"
            f"\n{BASE_SHA}\n{NONCE}\n{NOW + 60}".encode("utf-8"))

    def test_payload_has_no_trailing_newline(self):
        payload = canonical_attestation_payload(
            "v", "allow", DIFF_SHA, BASE_SHA, NONCE, 7)
        self.assertFalse(payload.endswith(b"\n"))

    def test_diff_hash_is_over_exact_bytes(self):
        self.assertEqual(compute_diff_sha256(DIFF.encode("utf-8")), DIFF_SHA)
        self.assertNotEqual(compute_diff_sha256(DIFF + " "), DIFF_SHA)


class ParseAttestationTests(unittest.TestCase):
    def test_happy_path(self):
        data = parse_attestation(
            _attestation(), diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
            round_nonce=NONCE, now=NOW, verify_signature=_verify)
        self.assertEqual(data["verifier_id"], "verifier-1")

    def test_missing_field_rejected(self):
        for field in ("verifier_id", "verdict", "diff_sha256", "base_sha256",
                      "round_nonce", "expires_at", "signature"):
            with self.assertRaises(HarnessError):
                parse_attestation(
                    _attestation(**{field: None}), diff_sha256=DIFF_SHA,
                    base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                    verify_signature=_verify)

    def test_extra_field_rejected(self):
        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(note="extra"), diff_sha256=DIFF_SHA,
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)

    def test_duplicate_json_key_rejected(self):
        raw = ('{"verdict": "allow", "verdict": "allow", "diff_sha256": "'
               + DIFF_SHA + '"}')
        with self.assertRaises(HarnessError):
            parse_attestation(
                raw, diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
                round_nonce=NONCE, now=NOW, verify_signature=_verify)

    def test_malformed_shapes_rejected(self):
        bad = [
            _attestation(verifier_id="has space"),
            _attestation(verifier_id="x" * 65),
            _attestation(verdict="approve"),       # wrong enum (consent word)
            _attestation(diff_sha256="Z" * 64),    # not lowercase hex
            _attestation(diff_sha256="abc"),
            _attestation(base_sha256="b" * 63),
            _attestation(round_nonce="c" * 65),
            _attestation(signature="d" * 127),
            _attestation(expires_at="soon"),
            _attestation(expires_at=-1),
        ]
        for raw in bad:
            with self.assertRaises(HarnessError):
                parse_attestation(
                    raw, diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
                    round_nonce=NONCE, now=NOW, verify_signature=_verify)

    def test_deny_verdict_rejected(self):
        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(verdict="deny"), diff_sha256=DIFF_SHA,
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)

    def test_hash_mismatch_rejected(self):
        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(diff_sha256="a" * 64), diff_sha256=DIFF_SHA,
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)

    def test_base_mismatch_rejected(self):
        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(base_sha256="a" * 64), diff_sha256=DIFF_SHA,
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)

    def test_nonce_mismatch_rejected(self):
        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(round_nonce="a" * 64), diff_sha256=DIFF_SHA,
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)

    def test_expired_rejected_including_boundary(self):
        for expires_at in (NOW, NOW - 1):
            with self.assertRaises(HarnessError):
                parse_attestation(
                    _attestation(expires_at=expires_at), diff_sha256=DIFF_SHA,
                    base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                    verify_signature=_verify)

    def test_no_verifier_configured_fails_closed(self):
        with self.assertRaises(HarnessError) as ctx:
            parse_attestation(
                _attestation(), diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
                round_nonce=NONCE, now=NOW, verify_signature=None)
        self.assertIn("fail-closed", str(ctx.exception))

    def test_bad_signature_rejected(self):
        def deny(payload, sig):
            return False

        with self.assertRaises(HarnessError):
            parse_attestation(
                _attestation(), diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
                round_nonce=NONCE, now=NOW, verify_signature=deny)

    def test_malformed_json_rejected(self):
        for raw in ("", "   ", "not json", "[1]", '"str"'):
            with self.assertRaises(HarnessError):
                parse_attestation(
                    raw, diff_sha256=DIFF_SHA, base_sha256=BASE_SHA,
                    round_nonce=NONCE, now=NOW, verify_signature=_verify)


class RequireAttestationTests(unittest.TestCase):
    def test_hashes_the_frozen_diff_bytes_itself(self):
        data = require_attestation(
            _attestation(), diff_bytes=DIFF, base_sha256=BASE_SHA,
            round_nonce=NONCE, now=NOW, verify_signature=_verify)
        self.assertEqual(data["verdict"], "allow")

    def test_a_summarized_diff_never_matches(self):
        # The bait-and-switch: attestation bound to the real diff, but the
        # caller writes from a summary. The gate hashes the FROZEN bytes.
        with self.assertRaises(HarnessError):
            require_attestation(
                _attestation(), diff_bytes="summary: makes x.py better",
                base_sha256=BASE_SHA, round_nonce=NONCE, now=NOW,
                verify_signature=_verify)


if __name__ == "__main__":
    unittest.main()
