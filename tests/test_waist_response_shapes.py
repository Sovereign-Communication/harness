"""The response shapes models actually emit, and the ones that must not pass.

D3b/D11 from the live dogfood: a waist verdict and a task decomposition both
come back through one owner, ``_parse_json_object``. The dogfood saw
``plan_verdict unparseable for several models`` and a reasoning-only
decomposition whose trace text was fed straight into that parser.

Two properties are pinned here and they pull against each other:

* tolerate the wrapper the model chose (fence, prose, trailing commentary,
  an envelope, casing) and still read the object;
* refuse everything else. A parser that cannot find a verdict must fail
  closed, because the caller treats a parse failure as "unconfirmed" while a
  lenient parse would let prose turn into an approval.
"""
import unittest

from harness.errors import HarnessError
from harness.waist import _parse_json_object, parse_waist_verdict

NL = chr(10)


class JsonObjectExtractionTests(unittest.TestCase):
    def _ok(self, text, expected):
        self.assertEqual(_parse_json_object(text, "thing"), expected)

    def test_a_bare_object(self):
        self._ok('{"verdict": "approve"}', {"verdict": "approve"})

    def test_a_fenced_block_with_a_json_language_tag(self):
        self._ok('prose\n```json\n{"verdict": "approve"}\n```\nmore prose',
                 {"verdict": "approve"})

    def test_a_fenced_block_without_a_language_tag(self):
        self._ok('```\n{"verdict": "refuse"}\n```', {"verdict": "refuse"})

    def test_a_fenced_block_that_is_not_json_is_skipped_not_trusted(self):
        # A ```bash fence is not the payload; the real object follows it.
        self._ok('```bash\nls -la\n```\n{"verdict": "approve"}',
                 {"verdict": "approve"})

    def test_prose_before_the_object(self):
        self._ok('Let me think about this first.\n{"verdict": "approve"}',
                 {"verdict": "approve"})

    def test_trailing_text_after_the_object(self):
        self._ok('{"verdict": "approve"}\nHope that helps!',
                 {"verdict": "approve"})

    def test_a_brace_inside_a_json_string_does_not_end_the_span(self):
        self._ok('{"verdict": "approve", "why": "use {x} and } here"}',
                 {"verdict": "approve", "why": "use {x} and } here"})

    def test_an_escaped_quote_inside_a_string(self):
        self._ok(r'{"verdict": "approve", "why": "he said \"{no}\""}',
                 {"verdict": "approve", "why": 'he said "{no}"'})

    def test_an_unbalanced_brace_in_a_reasoning_trace_is_skipped(self):
        # The trace opens a brace it never closes; the answer follows it.
        self._ok('{thinking about the { thing\n\n{"verdict": "approve"}',
                 {"verdict": "approve"})

    def test_nested_objects_survive(self):
        self._ok('{"verdict": "amend", "nodes": [{"id": "a"}]}',
                 {"verdict": "amend", "nodes": [{"id": "a"}]})

    def test_the_first_of_several_objects_wins(self):
        self._ok('{"a": 1}\n{"verdict": "approve"}', {"a": 1})

    # ---- fail closed -----------------------------------------------------

    def test_empty_text_raises(self):
        for empty in ("", "   ", "\n\t "):
            with self.assertRaises(HarnessError):
                _parse_json_object(empty, "thing")

    def test_a_reasoning_only_response_with_no_object_raises(self):
        # The dogfood's exact failure: no content, only a trace. Raising is
        # what lets the caller treat it as a retryable rung failure rather
        # than a plan.
        with self.assertRaises(HarnessError):
            _parse_json_object(
                "[NOTE] model returned no content; showing reasoning trace "
                "instead... I weighed the options at length.", "decomposition")

    def test_an_object_wrapped_in_an_array_is_still_found(self):
        # An array is not the requested shape, but it holds a real object and
        # models do emit it. Finding the object is more useful than refusing a
        # response that plainly contains the answer.
        self._ok('[{"verdict": "approve"}]', {"verdict": "approve"})

    def test_an_array_of_non_objects_raises(self):
        with self.assertRaises(HarnessError):
            _parse_json_object('["approve", "refuse"]', "thing")

    def test_prose_only_raises(self):
        with self.assertRaises(HarnessError):
            _parse_json_object("I would rather not answer.", "thing")


class WaistVerdictShapeTests(unittest.TestCase):
    """D11: the verdict word models actually spell.

    Each kind keeps its own payload contract -- ``refuse`` still owes a reason
    and evidence, ``amend``/``split`` still owe nodes. Only the spelling and
    envelope are being made tolerant here.
    """

    APPROVE = '{"verdict": "approve"}'
    REFUSE = ('{"verdict": "refuse", "reason": "out of scope", '
              '"evidence": "brief section 2"}')
    AMEND = ('{"verdict": "amend", "nodes": '
             '[{"node_id": "a", "instruction": "do a"}]}')
    WINDOWS = ('{"verdict": "request_windows", "file_window_requests": '
               '[{"path": "harness/jev.py", "start_line": 1, "end_line": 40}]}')

    def test_every_canonical_kind_is_read(self):
        for text, kind in ((self.APPROVE, "approve"), (self.REFUSE, "refuse"),
                           (self.AMEND, "amend")):
            self.assertEqual(parse_waist_verdict(text)["verdict"], kind, text)
        self.assertEqual(parse_waist_verdict(self.WINDOWS)["verdict"],
                         "request_windows")

    def test_case_and_trailing_punctuation_are_tolerated(self):
        for text in ('{"verdict":"APPROVED"}', '{"verdict":"Approved."}',
                     '{"verdict":"  approve  "}', '{"verdict":"Approved!"}'):
            self.assertEqual(parse_waist_verdict(text)["verdict"], "approve",
                             text)

    def test_a_synonym_is_tolerated_but_the_set_stays_closed(self):
        self.assertEqual(
            parse_waist_verdict('{"verdict":"reject","reason":"r",'
                                '"evidence":"e"}')["verdict"], "refuse")
        self.assertEqual(parse_waist_verdict('{"verdict":"ok"}')["verdict"],
                         "approve")
        self.assertEqual(
            parse_waist_verdict('{"verdict":"Need Windows","file_window_'
                                'requests":[{"path":"a.py"}]}')["verdict"],
            "request_windows")
        # A near-miss is NOT a synonym, and must not become an approval.
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict":"approved_everything"}')

    def test_a_one_level_envelope_is_unwrapped(self):
        self.assertEqual(
            parse_waist_verdict('{"result": {"verdict": "approve"}}')["verdict"],
            "approve")
        self.assertEqual(
            parse_waist_verdict('{"plan": {"verdict": "approve"}}')["verdict"],
            "approve")

    def test_a_verdict_handed_back_as_an_object(self):
        self.assertEqual(
            parse_waist_verdict('{"verdict": {"verdict": "approve"}}')["verdict"],
            "approve")

    def test_prose_around_the_verdict_is_tolerated(self):
        text = NL.join([
            "I have read the brief.",
            "```json",
            '{"verdict":"approve"}',
            "```",
            "Done.",
        ])
        self.assertEqual(parse_waist_verdict(text), {"verdict": "approve"})

    def test_refuse_still_demands_reason_and_evidence(self):
        # Tolerance must not weaken the refusal contract.
        for text in ('{"verdict": "refuse"}',
                     '{"verdict": "REJECTED"}',
                     '{"verdict": "refuse", "reason": "", "evidence": "e"}'):
            with self.assertRaises(HarnessError):
                parse_waist_verdict(text)
        out = parse_waist_verdict(
            '{"verdict": "REJECTED", "reason": "scope", "evidence": "brief 2"}')
        self.assertEqual(out["verdict"], "refuse")
        self.assertEqual(out["reason"], "scope")

    def test_amend_still_demands_its_nodes(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "amend"}')
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "amend", "nodes": []}')

    def test_request_windows_still_demands_its_payload(self):
        with self.assertRaises(HarnessError):
            parse_waist_verdict('{"verdict": "Request Windows"}')
        out = parse_waist_verdict(self.WINDOWS)
        self.assertEqual(out["verdict"], "request_windows")
        self.assertEqual(len(out["file_window_requests"]), 1)

    def test_a_missing_verdict_fails_closed(self):
        for text in ('{"foo": 1}', '{}', '{"nodes": []}'):
            with self.assertRaises(HarnessError):
                parse_waist_verdict(text)


if __name__ == "__main__":
    unittest.main()
