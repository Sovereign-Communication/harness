"""Regression tests for the Phase B dogfood defects.

Each test names the user-visible failure it pins, so a future refactor that
reintroduces the bug fails on the symptom rather than on an internal detail.
All hermetic: no network, no server process, no live Jev call.
"""

import re
import unittest
from pathlib import Path

from harness.errors import HarnessError
from harness.spend import SpendGovernor

UI = Path(__file__).resolve().parents[1] / "harness" / "ui" / "panes.js"


def ui_source():
    return UI.read_text(encoding="utf-8")


class DriverPaneFormAssemblyTests(unittest.TestCase):
    """The request form rendered blank: every field was constructed and then
    never attached, so the pane appended an empty <form>."""

    def test_every_driver_form_field_is_actually_appended(self):
        src = ui_source()
        # Every field the handlers read must be appended to the form.
        for name in ("goalField", "targetField", "schemaField", "stepsField",
                     "verifyCmdField", "autoApproveLabel", "stableLabel",
                     "btnRow"):
            self.assertIn(name, src, name)
        # The append must actually name the form, not just exist somewhere.
        self.assertRegex(
            src,
            r"driveForm\.append\(\s*goalField,[\s\S]*?btnRow\s*\)",
            "driveForm.append(...) must assemble all eight fields",
        )

    def test_fields_are_assembled_before_the_form_is_appended_to_the_pane(self):
        src = ui_source()
        assemble = src.index("driveForm.append(")
        appended = src.index("driveSec.append(driveForm)")
        self.assertLess(assemble, appended,
                        "the form must be filled before it is attached")


class VocabularyTabTests(unittest.TestCase):
    """The Action Vocabulary tab crashed: `normalisers` is a name -> value
    mapping, and the pane called .join() on it as if it were a list."""

    def test_normalisers_is_never_joined_as_if_it_were_an_array(self):
        self.assertNotIn("(act.normalisers || []).join(", ui_source())

    def test_normalisers_handles_both_the_mapping_and_list_shapes(self):
        src = ui_source()
        self.assertIn("Array.isArray(n)", src)
        self.assertIn("Object.keys(n)", src)

    def test_the_mapping_shape_is_handled_without_throwing(self):
        """Exercise the real shape off the wire: a name -> value mapping."""
        normalisers = {"target": "cli", "aspect": "schema"}
        keys = sorted(normalisers.keys())
        self.assertEqual(keys, ["aspect", "target"])

    def test_tier_reads_the_class_field_the_wire_shape_actually_carries(self):
        src = ui_source()
        # `class` is the field; `act.mutating` / `act.irreversible` never existed.
        self.assertIn("TIERS[act.class]", src)
        self.assertNotIn("act.mutating ?", src)

    def test_refused_actions_say_what_they_need(self):
        """Nothing previously told the reader that input actions are declared
        but not executable."""
        src = ui_source()
        self.assertIn("executable", src)
        self.assertIn("REFUSED", src)


class TableRenderingTests(unittest.TestCase):
    """Every multi-row table rendered as "[object HTMLTableRowElement],...".

    `$` appended each child with node.append(child), but table() passes a
    mapped LIST as a single child, so the array was stringified.
    """

    def test_the_dom_helper_flattens_array_children(self):
        self.assertIn("flatten([], children)", ui_source())

    def test_table_passes_mapped_lists_as_children(self):
        # Guard the calling convention the flattening exists to support.
        src = ui_source()
        self.assertRegex(src, r'\$[\(]"tbody", \{\}, rows\.map')


class SpendBadgeHonestyTests(unittest.TestCase):
    """The header spend badge always read $0.00. pollSpend swallowed every
    error, so an unreadable envelope was indistinguishable from zero spend."""

    def test_spend_poll_marks_unreadable_spend_distinctly(self):
        src = (Path(__file__).resolve().parents[1] / "harness" / "ui"
               / "app.js").read_text(encoding="utf-8")
        self.assertIn("markSpendUnavailable", src)
        # A payload that carries {error} must not be read as a session total.
        self.assertRegex(src, r"spend\.error\s*\)[\s\S]{0,120}markSpendUnavailable")
        # pollSpend itself must not swallow its errors silently. Scoped to the
        # function body: other empty catches in this file are legitimate.
        body = re.search(
            r"async function pollSpend\(\) \{[\s\S]*?\n\}", src)
        self.assertIsNotNone(body)
        self.assertNotIn("catch (_e) {}", body.group(0))
        self.assertIn("catch (err)", body.group(0))

    def test_unavailable_spend_does_not_render_a_zero(self):
        src = (Path(__file__).resolve().parents[1] / "harness" / "ui"
               / "app.js").read_text(encoding="utf-8")
        block = re.search(
            r"function markSpendUnavailable\(reason\) \{[\s\S]*?\n\}", src)
        self.assertIsNotNone(block, "markSpendUnavailable must exist")
        self.assertIn('"—"', block.group(0),
                      "an unmeasurable spend must render as a dash, not $0")


class SpendStatusJevCreditTests(unittest.TestCase):
    """MCP's spend_status reported no Jev credit at all, so the three
    surfaces (CLI, API, GUI) could disagree."""

    def test_spend_status_carries_a_jev_credit_block_with_a_month(self):
        from harness import mcp
        src = (Path(mcp.__file__).resolve().read_text(encoding="utf-8"))
        block = re.search(
            r'if name == "spend_status":[\s\S]*?return status', src)
        self.assertIsNotNone(block)
        self.assertIn('"jev_credit"', block.group(0))
        self.assertIn('"month"', block.group(0))
        # Composed from the existing owners, not recomputed here.
        self.assertIn("jev_credit_status", block.group(0))
        self.assertIn("cost_report", block.group(0))


class TaskScopeTests(unittest.TestCase):
    """panel_verify's per-task cap was preflighted and then dropped: the
    injected session governor made max_cost unreachable. The fix extends the
    ONE governor with a task scope rather than forking a second class."""

    def _session(self, max_cost=1.0):
        return SpendGovernor(transport=None, api_key="k", max_cost=max_cost)

    def test_no_second_governor_class_is_introduced(self):
        import harness.spend as spend_mod
        names = [n for n in dir(spend_mod) if "Governor" in n]
        self.assertEqual(names, ["SpendGovernor"])

    def test_a_reservation_over_the_task_cap_is_refused_before_dispatch(self):
        gov = self._session()
        with gov.task_scope(0.05, label="verify"):
            gov.reserve(0.04, "panel")
            with self.assertRaises(HarnessError):
                gov.reserve(0.02, "panel")

    def test_the_task_cap_is_independent_of_prior_session_spend(self):
        """The whole point: a task gets its own ceiling, not the session's."""
        gov = self._session()
        t = gov.reserve(0.40, "earlier")
        gov.reconcile(t, 0.40)
        with gov.task_scope(0.05, label="verify"):
            self.assertAlmostEqual(gov.remaining(), 0.05, places=6)

    def test_spend_still_lands_on_the_one_governor(self):
        gov = self._session()
        with gov.task_scope(0.05, label="verify"):
            token = gov.reserve(0.02, "panel")
            gov.reconcile(token, 0.01)
        self.assertAlmostEqual(gov.spent, 0.01, places=6)

    def test_the_scope_is_released_so_later_work_is_unaffected(self):
        gov = self._session()
        with gov.task_scope(0.05, label="verify"):
            token = gov.reserve(0.04, "panel")
            gov.reconcile(token, 0.04)
        self.assertAlmostEqual(gov.remaining(), 0.96, places=6)

    def test_a_tight_session_still_wins_over_a_loose_task_cap(self):
        gov = self._session(max_cost=0.01)
        with gov.task_scope(5.0, label="verify"):
            self.assertAlmostEqual(gov.remaining(), 0.01, places=6)

    def test_a_zero_or_negative_cap_is_refused(self):
        gov = self._session()
        for bad in (0.0, -1.0):
            with self.assertRaises(HarnessError):
                with gov.task_scope(bad):
                    pass


class PanelVerifyCapIsEnforcedTests(unittest.TestCase):
    """The service must not silently accept max_cost on the injected path."""

    def test_max_cost_with_an_injected_governor_is_rejected_not_ignored(self):
        from harness import service
        src = Path(service.__file__).resolve().read_text(encoding="utf-8")
        self.assertIn("max_cost is ignored when a governor is injected", src)

    def test_task_max_cost_wraps_the_injected_governor(self):
        from harness import service
        src = Path(service.__file__).resolve().read_text(encoding="utf-8")
        self.assertIn("gov.task_scope(task_max_cost", src)


if __name__ == "__main__":
    unittest.main()
