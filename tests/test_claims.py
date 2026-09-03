"""Hermetic tests for P0 self-grounding: source_refs lint + verbatim auto-expansion.

Regression target: the 04b failure from the SCMessenger audit, where a claim
asserted "no upper bound / no size cap" while the cap (MAX_SKIP_KEYS = 256,
bail at gap > 256, eviction at cache > 256) lived in get_message_key -- which
the panel never saw. 5/5 unanimity on that premise was worthless. These tests
prove the lint flags the ungrounded claim and, once the callee is resolvable,
auto-expands it and rejects the claim as contradicted by its own source.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from harness.claims import (  # noqa: E402
    Claim, build_claims_prompt, is_absence_styled, is_load_bearing,
    lint_claims, load_claims_manifest, normalize_definitions, parse_claims,
)
from harness.cli import main as cli_main  # noqa: E402

# A small, realistic quoted window (what the panel would have seen in 04b).
WINDOW = (
    "pub fn decrypt(&mut self, message_number: u32) -> Result<Vec<u8>> {\n"
    "    if let Some(key) = self.skipped_keys.get(&message_number) {\n"
    "        return Ok(key);\n"
    "    }\n"
    "    let mut cloned = (*self).clone();\n"
    "    let message_key = cloned.get_message_key(message_number)?;\n"
    "    *self = cloned;\n"
    "    Ok(message_key)\n"
    "}\n"
)

# The cap lives OUTSIDE the window, in get_message_key / MAX_SKIP_KEYS.
DEFS = {
    "get_message_key": (
        "fn get_message_key(&mut self, target: u32) -> Result<RatchetKey> {\n"
        "    if target < self.chain.index { bail!(\"behind current position\"); }\n"
        "    let skip = target - self.chain.index;\n"
        "    if skip as usize > MAX_SKIP_KEYS { bail!(\"Too many skipped messages\"); }\n"
        "    Ok(self.chain.next_message_key())\n"
        "}"
    ),
    "MAX_SKIP_KEYS": "const MAX_SKIP_KEYS: usize = 256; // gap and cache bound",
}

CTX = ("`get_message_key` advances the receiving chain and stores skipped keys "
       "for the gap.")

CLAIM_1_UNBOUNDED = (
    "UNBOUNDED GAP: there is no upper bound on the message_number gap here, so a "
    "hostile peer could force storing a very large number of skipped keys (memory DoS)."
)
CLAIM_2_NO_CAP = (
    "SKIPPED-CACHE GROWTH: skipped_keys has no size cap; sustained out-of-order "
    "messages can grow it without bound (memory exhaustion)."
)


def codes(report):
    return [i["code"] for i in report["issues"]]


class WordDetectionTests(unittest.TestCase):
    def test_load_bearing_words(self):
        self.assertTrue(is_load_bearing("there is no cap on X"))
        self.assertTrue(is_load_bearing("grows unbounded"))
        self.assertTrue(is_load_bearing("no upper bound on the gap"))
        self.assertTrue(is_load_bearing("this never happens"))
        self.assertTrue(is_load_bearing("runs only while confirmed"))
        self.assertFalse(is_load_bearing("the gap is capped at 256"))
        self.assertFalse(is_load_bearing("the clone-and-commit pattern is correct"))

    def test_absence_style(self):
        self.assertTrue(is_absence_styled("no size cap; grows unbounded"))
        self.assertTrue(is_absence_styled("there is no upper bound"))
        # universal words are load-bearing for R1 but are NOT absence-styled,
        # so R3 (contradicted-by-source) does not fire on them.
        self.assertFalse(is_absence_styled("runs only while confirmed"))


class ClaimParsingTests(unittest.TestCase):
    def test_parse_manifest_with_context_and_kinds(self):
        data = {
            "context": "see the helper below",
            "claims": [
                {"id": "c1", "text": "A has no cap on B", "kind": "defect",
                 "source_refs": [2, 3]},
                {"id": "c2", "text": "C is order-independent", "kind": "reassurance"},
            ],
        }
        ctx, claims = parse_claims(data)
        self.assertEqual(ctx, "see the helper below")
        self.assertEqual([c.claim_id for c in claims], ["c1", "c2"])
        self.assertEqual(claims[0].kind, "defect")
        self.assertEqual(claims[0].source_refs, [2, 3])
        self.assertEqual(claims[1].kind, "reassurance")
        self.assertEqual(claims[1].source_refs, [])

    def test_manifest_loader_and_definitions_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            mf = os.path.join(td, "claims.json")
            with open(mf, "w", encoding="utf-8") as f:
                json.dump({"claims": [{"id": "c1", "text": "boom",
                                       "source_refs": [1]}]}, f)
            ctx, claims = load_claims_manifest(mf)
            self.assertIsNone(ctx)
            self.assertEqual(len(claims), 1)
            d1 = normalize_definitions({"A": {"snippet": "x = 1"}, "B": "y = 2"})
            self.assertEqual(d1, {"A": "x = 1", "B": "y = 2"})
            d2 = normalize_definitions(
                [{"name": "C", "definition": "z = 3"},
                 {"id": "D", "source": "w = 4"}])
            self.assertEqual(d2, {"C": "z = 3", "D": "w = 4"})

    def test_parse_rejects_bad_kind_and_bad_refs(self):
        with self.assertRaises(ValueError):
            parse_claims({"claims": [{"id": "c1", "text": "x", "kind": "opinion"}]})
        with self.assertRaises(ValueError):
            parse_claims({"claims": [{"id": "c1", "text": "x",
                                      "source_refs": ["abc"]}]})
        with self.assertRaises(ValueError):
            parse_claims({"claims": "not a list"})

class LintTests(unittest.TestCase):
    def test_04b_regression_ungrounded_no_cap_is_flagged(self):
        # The exact 04b failure shape: absence-styled claims, NO source_refs.
        claims = [Claim("claim_1", CLAIM_1_UNBOUNDED),
                  Claim("claim_2", CLAIM_2_NO_CAP)]
        report = lint_claims(claims, WINDOW, source_index=DEFS, context=CTX)
        self.assertFalse(report["ok"])
        errs = codes(report)
        self.assertIn("ungrounded-assertion", errs)  # flagged: must be rejected
        self.assertEqual(errs.count("ungrounded-assertion"), 2)

    def test_04b_regression_refs_but_cap_in_callee_auto_expands_and_contradicts(self):
        # Author adds refs into the window (the decrypt body), but the real cap
        # is in get_message_key (named by the context) -- auto-expansion must
        # pull it in and the absence claim must be rejected as contradicted.
        claims = [Claim("claim_1", CLAIM_1_UNBOUNDED, source_refs=[6]),
                  Claim("claim_2", CLAIM_2_NO_CAP, source_refs=[2])]
        report = lint_claims(claims, WINDOW, source_index=DEFS, context=CTX)
        expanded = report["expanded_source"]
        # auto-expansion appended the callee AND its constant, verbatim
        self.assertIn("fn get_message_key", expanded)
        self.assertIn("MAX_SKIP_KEYS: usize = 256", expanded)
        self.assertIn("Too many skipped messages", expanded)
        self.assertEqual([e["identifier"] for e in report["expansions"]],
                         ["MAX_SKIP_KEYS", "get_message_key"])
        self.assertFalse(report["ok"])
        errs = codes(report)
        self.assertNotIn("ungrounded-assertion", errs)  # refs present now
        self.assertIn("contradicted-by-source", errs)   # ...but contradicted
        contrad = [i for i in report["issues"]
                   if i["code"] == "contradicted-by-source"]
        self.assertTrue(all("MAX_SKIP_KEYS" in i["message"] for i in contrad))

    def test_legitimate_absence_claim_with_in_window_refs_passes(self):
        # A claim that genuinely has no bound IN THE WINDOW and no resolvable
        # out-of-window identifier must pass when it cites its justifying lines.
        loop_window = (
            "while not eof {\n"
            "    let chunk = reader.read_chunk();\n"
            "    buf.extend_from_slice(chunk);\n"
            "}\n"
        )
        claims = [Claim("c1", "the loop below reads until EOF and has no length cap",
                        source_refs=[1])]
        report = lint_claims(claims, loop_window)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["expansions"], [])

    def test_plain_defect_and_reassurance_claims_need_no_refs(self):
        claims = [Claim("c1", "index starts at zero then increments before the subtract"),
                  Claim("c2", "the clone-and-commit pattern keeps the state",
                        kind="reassurance")]
        report = lint_claims(claims, WINDOW)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual([i["code"] for i in report["issues"]], [])

    def test_out_of_window_ref_rejected(self):
        claims = [Claim("c1", "no cap anywhere in this function", source_refs=[999])]
        report = lint_claims(claims, WINDOW)
        self.assertFalse(report["ok"])
        self.assertIn("out-of-window-ref", codes(report))

    def test_unresolved_identifier_is_warning_not_error(self):
        claims = [Claim("c1", "no cap is applied here", source_refs=[1])]
        report = lint_claims(
            claims, WINDOW,
            context="the helper `bogus_helper` does the accounting")
        self.assertTrue(report["ok"])  # warnings do not reject
        self.assertIn("unresolved-identifier", codes(report))

    def test_expansion_skips_identifiers_already_in_window_and_dedupes(self):
        window = "pub fn visible_helper() {}\n" + WINDOW
        defs = dict(DEFS)
        defs["visible_helper"] = "fn visible_helper() {}"
        claims = [Claim("c1", "no cap", source_refs=[1]),
                  Claim("c2", "no cap either", source_refs=[1])]
        report = lint_claims(claims, window, source_index=defs, context=CTX)
        # visible_helper is in the window => never appended; get_message_key and
        # MAX_SKIP_KEYS appended exactly once despite two claims + context.
        self.assertEqual([e["identifier"] for e in report["expansions"]],
                         ["MAX_SKIP_KEYS", "get_message_key"])
        self.assertEqual(report["expanded_source"].count("fn get_message_key"), 1)

class PromptTests(unittest.TestCase):
    def test_build_claims_prompt_embeds_numbered_source_expansions_and_refs(self):
        claims = [Claim("claim_1", CLAIM_1_UNBOUNDED, source_refs=[6])]
        prompt, report = build_claims_prompt(claims, WINDOW, source_index=DEFS,
                                             context=CTX)
        self.assertFalse(report["ok"])
        self.assertIn("Source under review", prompt)
        self.assertIn("fn get_message_key", prompt)      # expansion visible
        self.assertIn("MAX_SKIP_KEYS: usize = 256", prompt)
        self.assertIn("[refs: lines 6]", prompt)         # claim cites its lines
        self.assertIn("claim_1", prompt)
        self.assertIn("defect proposition", prompt.lower())
        self.assertIn("   1 | pub fn decrypt", prompt)   # numbered fence

    def test_reassurance_kind_is_marked_in_prompt(self):
        claims = [Claim("c3", "the clone-and-commit pattern is correct",
                        kind="reassurance")]
        prompt, report = build_claims_prompt(claims, WINDOW)
        self.assertTrue(report["ok"])
        self.assertIn("REASSURANCE", prompt)


class CliTests(unittest.TestCase):
    def _write(self, td, name, text):
        p = os.path.join(td, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    cli_main(argv)
                    code = 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        return code, buf.getvalue()

    def test_cli_lint_claims_rejects_ungrounded_manifest_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            cf = self._write(td, "claims.json", json.dumps({
                "context": CTX,
                "claims": [{"id": "claim_1", "text": CLAIM_1_UNBOUNDED}]}))
            sf = self._write(td, "window.txt", WINDOW)
            df = self._write(td, "defs.json", json.dumps(DEFS))
            code, out = self._run(["lint-claims", "--claims-file", cf,
                                   "--source-file", sf, "--definitions-file", df])
            self.assertEqual(code, 2)
            data = json.loads(out)
            self.assertFalse(data["ok"])
            self.assertIn("ungrounded-assertion",
                          [i["code"] for i in data["issues"]])

    def test_cli_lint_claims_clean_passes_and_show_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            cf = self._write(td, "claims.json", json.dumps({
                "claims": [{"id": "c1", "text": "capped at 256 with eviction",
                            "source_refs": [6]}]}))
            sf = self._write(td, "window.txt", WINDOW)
            code, out = self._run(["lint-claims", "--claims-file", cf,
                                   "--source-file", sf, "--show-prompt"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertTrue(data["ok"])
            self.assertIn("prompt", data)

    def test_verify_claims_mode_rejects_before_any_network(self):
        # The whole point: an ungrounded claim never reaches _governor (which
        # would need a live key). Exit 2 with a rejected payload proves the
        # lint ran first and no network was touched.
        with tempfile.TemporaryDirectory() as td:
            cf = self._write(td, "claims.json", json.dumps({
                "claims": [{"id": "claim_2", "text": CLAIM_2_NO_CAP}]}))
            sf = self._write(td, "window.txt", WINDOW)
            code, out = self._run(["verify", "--claims-file", cf,
                                   "--source-file", sf])
            self.assertEqual(code, 2)
            data = json.loads(out)
            self.assertEqual(data["status"], "rejected")
            self.assertFalse(data["lint"]["ok"])


if __name__ == "__main__":
    unittest.main()
