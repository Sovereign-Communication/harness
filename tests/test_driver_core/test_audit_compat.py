"""The audit chain is a compatibility surface, and this is the test for it.

An audit log is the one artifact whose value is that a record means one thing
and cannot be quietly reinterpreted later. Two claims are checked, and they
are different claims:

* **An existing log still verifies.** ``GOLDEN_CHAIN`` below is a real chain,
  recorded before the record-kind vocabulary was given an owner. It must
  re-hash clean under the current code.
* **The same run still writes the same bytes.** The scenario pins the clock
  and every ``step_id``, so what it produces is a pure function of the code,
  and it is compared against that same chain.

The second is the stronger claim and the one that catches a renamed constant.
A log can verify perfectly while meaning something new.

The chain is inline rather than a ``.jsonl`` file on purpose: ``.gitignore``
refuses ``*.jsonl`` because an audit log is evidence about a machine and must
never be committed, and carrying it here also puts any change to it in the
diff beside the test that explains it. Regenerate with
``python -m tests.test_audit_compat --emit``.
"""
import ast
import importlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import driver_core.audit as audit_module
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import AuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeTransport, ok_response
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.jev_client import JevClient
from driver_core.perception import CLI, StructuredSource, Target
from driver_core.states import CLI_SCHEMA

#: One JSON object per line, exactly as the log stores them, wrapped to stay
#: inside the line length.
GOLDEN_CHAIN = (
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"target 'unobser"
     "vable' is declared 'cli' and no configured source serves that clas"
     "s; nothing was tried. Configured sources: ['none'].\",\"hash\":\"98c94"
     "270f43a33f50f308df7fad8290836267bb9f7b504e02aee5056d59bd982\",\"kind"
     "\":\"refusal\",\"previous\":\"driver-core/audit/v1\",\"reason\":\"no_capture"
     "\",\"seq\":0,\"step_id\":\"step000000001\",\"stopped_at\":\"capture\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"\",\"fingerprint\""
     ":\"45475af213f2b5ea\",\"hash\":\"7218b26519ca0fffe828626465a02b2d97f46e"
     "5423089fce1e6ecfb781f14da3\",\"kind\":\"capture\",\"ok\":true,\"previous\":"
     "\"98c94270f43a33f50f308df7fad8290836267bb9f7b504e02aee5056d59bd982\""
     ",\"seq\":1,\"source\":\"cli\",\"step_id\":\"step000000002\",\"target\":\"observ"
     "ed\"}"
    ),
    (
     "{\"agreed_fields\":[\"exit_code\",\"stdout\",\"stderr\"],\"answering\":2,\"as"
     "ked\":2,\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"contested_fields\":"
     "[],\"cost\":0.0,\"extraction_id\":\"step000000002\",\"hash\":\"e9ceec17fd3e"
     "853a7e465f37baec2c5812140ebd144da1c45854a06b9590de8c\",\"kind\":\"extr"
     "action\",\"outcome\":\"agreed\",\"previous\":\"7218b26519ca0fffe828626465a"
     "02b2d97f46e5423089fce1e6ecfb781f14da3\",\"schema\":\"driver-core-cli@1"
     ".0.0\",\"seq\":2,\"step_id\":\"step000000002\",\"unanswered\":[]}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"confidence\":0.1,\"cost_us"
     "d\":4.2e-06,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"st"
     "derr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\"e"
     "xtraction_id\":\"step000000002\",\"outcome\":\"agreed\",\"schema\":\"driver-"
     "core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_r"
     "equired\":0.2,\"state_is_stable\":0.95},\"hash\":\"bb5dfeca8e9548cf49abc"
     "4beb901c74e32d08c461d49f1701e2a3012ba98336d\",\"kind\":\"decision\",\"mo"
     "del\":\"fake-jev\",\"native\":true,\"previous\":\"e9ceec17fd3e853a7e465f37"
     "baec2c5812140ebd144da1c45854a06b9590de8c\",\"probabilities\":{\"call_r"
     "ead_tool\":0.06923076923076923,\"click\":0.06923076923076923,\"delete_"
     "file\":0.06923076923076923,\"focus\":0.06923076923076923,\"no_action\":"
     "0.06923076923076923,\"observe\":0.06923076923076923,\"press_key\":0.06"
     "923076923076923,\"read_dom\":0.06923076923076923,\"read_value\":0.0692"
     "3076923076923,\"run_probe\":0.06923076923076923,\"scroll\":0.069230769"
     "23076923,\"submit_irreversible\":0.06923076923076923,\"type_text\":0.0"
     "6923076923076923,\"write_file\":0.1},\"recommended_action\":\"write_fil"
     "e\",\"seq\":3,\"status\":\"native\",\"step_id\":\"step000000002\",\"usage_sour"
     "ce\":\"actual\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"f9582a4ff0b47d627eb32119328219"
     "bd56820f97d7063c64f6b420e6bf95732a\",\"kind\":\"escalation\",\"previous\""
     ":\"bb5dfeca8e9548cf49abc4beb901c74e32d08c461d49f1701e2a3012ba98336d"
     "\",\"reason\":\"confidence_below_threshold\",\"seq\":4,\"step_id\":\"step000"
     "000002\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"confidence 0.1 "
     "is below the 0.7 threshold\",\"hash\":\"620a9555c245e23c7fee91509e56c5"
     "c349f0115e9a1f4280ea5bca32830f7bde\",\"kind\":\"refusal\",\"previous\":\"f"
     "9582a4ff0b47d627eb32119328219bd56820f97d7063c64f6b420e6bf95732a\",\""
     "reason\":\"confidence_below_threshold\",\"seq\":5,\"step_id\":\"step000000"
     "002\",\"stopped_at\":\"decision\"}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"detail\":\"\",\"fingerprint\""
     ":\"45475af213f2b5ea\",\"hash\":\"fa1bd69fb776a75f0698fbcf9e9461921cd3e3"
     "4ad7938e16acc71426bb3a803d\",\"kind\":\"capture\",\"ok\":true,\"previous\":"
     "\"620a9555c245e23c7fee91509e56c5c349f0115e9a1f4280ea5bca32830f7bde\""
     ",\"seq\":6,\"source\":\"cli\",\"step_id\":\"step000000003\",\"target\":\"observ"
     "ed\"}"
    ),
    (
     "{\"agreed_fields\":[\"exit_code\",\"stdout\",\"stderr\"],\"answering\":2,\"as"
     "ked\":2,\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"contested_fields\":"
     "[],\"cost\":0.0,\"extraction_id\":\"step000000003\",\"hash\":\"3097a849e220"
     "0e106c7e502755d86a87b0a35ab1794c177022b693ccee9357bd\",\"kind\":\"extr"
     "action\",\"outcome\":\"agreed\",\"previous\":\"fa1bd69fb776a75f0698fbcf9e9"
     "461921cd3e34ad7938e16acc71426bb3a803d\",\"schema\":\"driver-core-cli@1"
     ".0.0\",\"seq\":7,\"step_id\":\"step000000003\",\"unanswered\":[]}"
    ),
    (
     "{\"at\":\"2026-01-01T00:00:00.000000+00:00\",\"confidence\":0.99,\"cost_u"
     "sd\":4.2e-06,\"extraction\":{\"agreed_fields\":[\"exit_code\",\"stdout\",\"s"
     "tderr\"],\"answering\":2,\"asked\":2,\"contested_fields\":[],\"cost\":0.0,\""
     "extraction_id\":\"step000000003\",\"outcome\":\"agreed\",\"schema\":\"driver"
     "-core-cli@1.0.0\",\"unanswered\":[]},\"guards\":{\"a_blocking_choice_is_"
     "required\":0.2,\"state_is_stable\":0.95},\"hash\":\"808ddcb1e8d7a2a2b345"
     "933ad3aa53874fa705288577f1d0200892e0e480541a\",\"kind\":\"decision\",\"m"
     "odel\":\"fake-jev\",\"native\":true,\"previous\":\"3097a849e2200e106c7e502"
     "755d86a87b0a35ab1794c177022b693ccee9357bd\",\"probabilities\":{\"call_"
     "read_tool\":0.0007692307692307699,\"click\":0.0007692307692307699,\"de"
     "lete_file\":0.0007692307692307699,\"focus\":0.0007692307692307699,\"no"
     "_action\":0.0007692307692307699,\"observe\":0.99,\"press_key\":0.000769"
     "2307692307699,\"read_dom\":0.0007692307692307699,\"read_value\":0.0007"
     "692307692307699,\"run_probe\":0.0007692307692307699,\"scroll\":0.00076"
     "92307692307699,\"submit_irreversible\":0.0007692307692307699,\"type_t"
     "ext\":0.0007692307692307699,\"write_file\":0.0007692307692307699},\"re"
     "commended_action\":\"observe\",\"seq\":8,\"status\":\"native\",\"step_id\":\"s"
     "tep000000003\",\"usage_source\":\"actual\"}"
    ),
    (
     "{\"action\":\"observe\",\"action_class\":\"read_only\",\"at\":\"2026-01-01T00"
     ":00:00.000000+00:00\",\"consent\":null,\"detail\":\"\",\"dry_run\":false,\"h"
     "ash\":\"b7790527c90585f61e7c5d24b583638836b3f97ac9826916c6c2369d9434"
     "a220\",\"kind\":\"action\",\"ok\":true,\"previous\":\"808ddcb1e8d7a2a2b34593"
     "3ad3aa53874fa705288577f1d0200892e0e480541a\",\"seq\":9,\"step_id\":\"ste"
     "p000000003\"}"
    ),
)
FIXED_NOW = "2026-01-01T00:00:00.000000+00:00"


def _capture(target):
    """A structured capture with nothing random in it."""
    return {"exit_code": 0, "stdout": f"observed {target}", "stderr": ""}


def _reader(capture, schema):
    return dict(capture.payload)


def _answers(action, confidence):
    """A response body covering every declared option, as the real one does."""
    options = DEFAULT_VOCABULARY.names()
    others = [name for name in options if name != action]
    share = (1.0 - confidence) / len(others) if others else 0.0
    return {
        "action": {"type": "choice", "choice": action,
                   "probabilities": {
                       name: (confidence if name == action else share)
                       for name in options},
                   "confidence": confidence},
        "state_is_stable": {"type": "noul", "noul": 0.95},
        "a_blocking_choice_is_required": {"type": "noul", "noul": 0.2},
    }


def _driver(workdir, responses, *, blind=False):
    """A driver whose decision tier is the real client on a fake transport.

    :class:`~driver_core.ev.FakeJev` would be shorter, but it writes no
    ``decision`` record at all -- it is not the client that ships. Using the
    real one is what puts the fifth record kind in the recorded chain.
    """
    settings = load_settings(
        env={}, jev_api_key="k",
        quorum=2, min_agreement=1.0, confidence_threshold=0.7,
        run_ceiling_usd=1.0, step_ceiling_usd=1.0, allow_write=True,
    )
    budget = Budget(1.0, step_ceiling_usd=1.0)
    audit = AuditLog(os.path.join(workdir, "audit.jsonl"))
    return Driver(
        settings=settings,
        budget=budget,
        audit=audit,
        pool=ExtractorPool([StructuredExtractor(f"cli-{i}", _reader)
                            for i in range(2)]),
        jev=JevClient(settings, budget=budget, audit=audit,
                      transport_module=FakeTransport(*responses)),
        sources=[] if blind else [StructuredSource("cli", _capture)],
    )


def run_scenario(workdir):
    """Every path that writes a record, deterministically.

    Three steps, chosen to cover all six live kinds:

    * an unobservable target -> a refusal and nothing else;
    * a decision below the threshold -> capture, extraction, decision,
      escalation, refusal;
    * a decision above it, naming a read-only action -> capture, extraction,
      decision, action.

    The third step deliberately names ``observe``, which is read-only and
    takes no parameters, rather than a mutating action. A mutating action
    would put an absolute path in its record -- and into the hash of that
    record -- so the golden would carry whichever temporary directory
    generated it and could not be compared on any other machine. A golden that
    only verifies on the machine that wrote it is not a golden.
    """
    previous = os.getcwd()
    os.chdir(workdir)
    try:
        _driver(workdir, [], blind=True).step(
            Target("unobservable", CLI), schema=CLI_SCHEMA,
            step_id="step000000001")

        _driver(workdir, [ok_response(_answers("write_file", 0.10))]
                ).step(Target("observed", CLI), schema=CLI_SCHEMA,
                       step_id="step000000002")

        _driver(workdir, [ok_response(_answers("observe", 0.99))]
                ).step(Target("observed", CLI), schema=CLI_SCHEMA,
                       step_id="step000000003")
    finally:
        os.chdir(previous)

    return AuditLog(os.path.join(workdir, "audit.jsonl"))


def golden_records():
    return [json.loads(record) for record in GOLDEN_CHAIN]


class ChainCompatibilityTests(unittest.TestCase):
    """The two compatibility claims, and the two guards on the vocabulary."""

    def _declared_kinds(self):
        return {value for name, value in vars(audit_module).items()
                if name.startswith("KIND_")}

    def _verify_recorded_chain(self):
        """Verify the recorded chain the way a host does: read it from a file.

        The chain is a literal in this module, so writing it out and reading
        it back is what makes the claim about *logs on disk* rather than about
        a list in memory.
        """
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "audit.jsonl")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                for record in GOLDEN_CHAIN:
                    handle.write(record + "\n")
            return AuditLog(path).verify()

    def _run_once(self):
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                return run_scenario(workdir).read_all()

    def test_a_log_written_before_this_change_still_verifies(self):
        verdict = self._verify_recorded_chain()
        self.assertTrue(
            verdict.ok, f"an existing log stopped verifying: {verdict}")

    def test_the_recorded_chain_covers_every_declared_kind(self):
        kinds = {record["kind"] for record in golden_records()}
        self.assertEqual(kinds, self._declared_kinds())

    def test_the_same_run_writes_the_same_bytes(self):
        """Stronger than verification: identical records, not a valid chain.

        Run twice so the comparison cannot pass by luck -- one run agreeing
        with the chain says nothing about the next one doing the same.
        """
        for attempt in range(2):
            with self.subTest(run=attempt):
                self.assertEqual(self._run_once(), golden_records())

    def test_no_record_kind_is_written_as_a_string(self):
        """A record kind is a declared name, never a literal, wherever it is
        written -- in a module nobody listed, or through an alias.

        The scan covers the whole package and keys on the *shape* of the call
        rather than the name of its receiver, so neither a new producer nor a
        local ``log = self.audit`` can smuggle a string past it.

        The rule is that a string literal may only be appended when the call
        passes nothing by keyword: a record always carries fields, and the
        three places that append bare strings -- the problem messages in
        ``jev_client`` -- never do.
        """
        offenders = []
        for path, tree in _package():
            for node in _appends(tree):
                first = node.args[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and node.keywords):
                    offenders.append(
                        f"{path.name}:{node.lineno} appends the literal "
                        f"{first.value!r}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_vocabulary_is_declared_once_and_every_name_is_written(self):
        """Both directions of the same claim, and neither needs a list here.

        A declared name nothing writes is a claim about the log that can rot
        with nothing to notice it. So is a second copy of a name, which is
        why ``audit`` is the only module allowed to declare one: everyone
        else imports it. The producers are derived from the code -- every
        module that appends a ``KIND_*`` name -- so a new one is covered
        without anyone editing a list.
        """
        produced, redeclared = set(), []
        for path, tree in _package():
            for node in ast.walk(tree):
                if (path.stem != "audit" and isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name)
                                and target.id.startswith("KIND_")
                                for target in node.targets)):
                    redeclared.append(f"{path.name}:{node.lineno}")
            names = {node.args[0].id for node in _appends(tree)
                     if isinstance(node.args[0], ast.Name)
                     and node.args[0].id.startswith("KIND_")}
            if not names:
                continue
            module = importlib.import_module(
                "driver_core" if path.stem == "__init__"
                else f"driver_core.{path.stem}")
            produced.update(getattr(module, name) for name in names)
        self.assertEqual(
            redeclared, [], f"a KIND_* declared outside audit.py: {redeclared}")
        self.assertEqual(produced, self._declared_kinds())


def _package():
    """Every module in the package, parsed once, for the two scans above."""
    root = pathlib.Path(audit_module.__file__).parent
    for path in sorted(root.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _appends(tree):
    """Every ``<anything>.append(<something>, ...)`` call in a parsed module.

    Deliberately ignorant of what is being appended to. Naming the receiver
    would miss an alias, and listing the modules would miss a new one.
    """
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args):
            yield node


def _emit(records, width=66):
    """The chain as wrapped Python literals, for pasting over GOLDEN_CHAIN.

    Wrapped because ``ruff`` selects ``E``, so a 700-character record on one
    line is a lint failure. Dumping each fragment on its own is enough: the
    escaping is recomputed per fragment, so a split never lands inside one.
    """
    for record in records:
        text = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print("    (")
        for start in range(0, len(text), width):
            print("     " + json.dumps(text[start:start + width]))
        print("    ),")


if __name__ == "__main__":
    import sys

    if "--emit" in sys.argv:
        # Run once, against the code as it stands, to record what the chain
        # looks like *now*. Never run in CI: a chain regenerated by the code
        # under test proves nothing about the code under test.
        with tempfile.TemporaryDirectory() as workdir:
            with mock.patch("driver_core.audit._now", return_value=FIXED_NOW):
                _emit(run_scenario(workdir).read_all())
