"""JEV-P6 pack gate tests (tests/test_jev_repo_pack.py).

Operator pack validation (declared axes/score/nouls/keywords only), the
typed question pack (criteria == declared keys, nothing invented), and the
code-owned keyword fallback.
"""
import unittest

from harness.jev_packs import (
    heuristic_repo_axes,
    repo_summary_question_pack,
    validate_repo_summary_pack,
)


def repo_pack():
    return {
        "id": "fixture-repo-v1",
        "axes": {
            "stage": {
                "instructions": "Choose the hourglass stage.",
                "criteria": {
                    "prep": "inventory and planning inputs",
                    "waist": "frontier confirmation",
                    "adjudicate": "verification and evidence",
                },
            },
            "handling": {
                "instructions": "Choose the least capable handling tier.",
                "criteria": {
                    "code_owned": "no model needed",
                    "scout": "bounded mechanical work",
                    "frontier": "architectural attention",
                },
            },
        },
        "score": {
            "id": "attention",
            "instructions": "Rate centrality to the hourglass.",
            "levels": ["background", "notable", "central"],
        },
        "nouls": {
            "waist_relevant": {
                "instructions": "Must the waist brief cite this element?",
                "true": "Citation needed.",
                "false": "Not needed.",
            },
        },
        "keywords": {
            "stage": {
                "adjudicate": ["verify", "ledger", "test"],
                "waist": ["waist", "verdict"],
            },
            "handling": {"frontier": ["architect"]},
        },
    }


class ValidatePackTests(unittest.TestCase):
    def test_valid_pack_cleans_and_preserves(self):
        doc = validate_repo_summary_pack(repo_pack())
        self.assertEqual(doc["id"], "fixture-repo-v1")
        self.assertEqual(set(doc["axes"]), {"stage", "handling"})
        self.assertEqual(doc["score"]["levels"], ["background", "notable", "central"])
        self.assertIn("waist_relevant", doc["nouls"])
        self.assertEqual(doc["keywords"]["stage"]["adjudicate"], ["verify", "ledger", "test"])

    def _mutate(self, fn):
        pack = repo_pack()
        fn(pack)
        with self.assertRaises(ValueError):
            validate_repo_summary_pack(pack)

    def test_missing_id_refuses(self):
        self._mutate(lambda p: p.pop("id"))

    def test_missing_axes_refuses(self):
        self._mutate(lambda p: p.pop("axes"))

    def test_empty_criteria_refuses(self):
        self._mutate(lambda p: p["axes"]["stage"].update(criteria={}))

    def test_short_levels_refuse(self):
        self._mutate(lambda p: p["score"].update(levels=["only"]))

    def test_duplicate_levels_refuse(self):
        self._mutate(lambda p: p["score"].update(levels=["a", "a"]))

    def test_unknown_keyword_axis_refuses(self):
        self._mutate(lambda p: p["keywords"].update(bogus={"x": ["y"]}))

    def test_unknown_keyword_criterion_refuses(self):
        self._mutate(lambda p: p["keywords"]["stage"].update(nope=["y"]))

    def test_duplicate_question_ids_refuse(self):
        def collide(p):
            p["nouls"]["stage"] = p["nouls"]["waist_relevant"]
        self._mutate(collide)

    def test_non_object_refuses(self):
        with self.assertRaises(ValueError):
            validate_repo_summary_pack(["not", "a", "pack"])


class QuestionPackTests(unittest.TestCase):
    def test_types_and_declared_keys_only(self):
        questions = repo_summary_question_pack(repo_pack())
        self.assertEqual(set(questions),
                         {"stage", "handling", "attention", "waist_relevant"})
        self.assertEqual(questions["stage"]["type"], "choice")
        self.assertEqual(set(questions["stage"]["criteria"]),
                         {"prep", "waist", "adjudicate"})
        self.assertEqual(questions["handling"]["type"], "choice")
        self.assertEqual(questions["attention"]["type"], "score")
        self.assertEqual(questions["attention"]["criteria"],
                         ["background", "notable", "central"])
        self.assertEqual(questions["waist_relevant"]["type"], "noul")
        self.assertEqual(set(questions["waist_relevant"]["criteria"]),
                         {"true", "false"})

    def test_accepts_already_validated_pack(self):
        doc = validate_repo_summary_pack(repo_pack())
        self.assertEqual(set(repo_summary_question_pack(doc)), set(
            repo_summary_question_pack(repo_pack())))


class HeuristicFallbackTests(unittest.TestCase):
    def test_keyword_hits_pick_declared_criteria(self):
        axes = heuristic_repo_axes(
            "harness/ledger.py verifies the chain with tests", repo_pack())
        self.assertEqual(axes["stage"], "adjudicate")
        self.assertEqual(axes["handling"], None)

    def test_no_hit_yields_none_never_a_guess(self):
        axes = heuristic_repo_axes("totally unrelated text", repo_pack())
        self.assertIsNone(axes["stage"])
        self.assertIsNone(axes["handling"])

    def test_tie_keeps_lexicographically_first_criterion(self):
        axes = heuristic_repo_axes("waist verify", repo_pack())
        # both stage criteria hit once -> sorted-first id wins
        self.assertEqual(axes["stage"], "adjudicate")

    def test_non_string_text_yields_no_guess(self):
        axes = heuristic_repo_axes(None, repo_pack())
        self.assertIsNone(axes["stage"])


class ValidatorEdgeTests(unittest.TestCase):
    """Each refused shape: the exact raise that keeps a pack honest."""

    def _refuse(self, mutate, fragment):
        pack = repo_pack()
        mutate(pack)
        with self.assertRaises(ValueError) as ctx:
            validate_repo_summary_pack(pack)
        self.assertIn(fragment, str(ctx.exception))

    def test_axis_id_must_be_non_empty_string(self):
        self._refuse(lambda p: p["axes"].update(**{"": p["axes"].pop("stage")}),
                     "axis ids")

    def test_axis_spec_must_be_object(self):
        self._refuse(lambda p: p["axes"].update(stage="nope"),
                     "must be an object")

    def test_axis_instructions_required(self):
        self._refuse(lambda p: p["axes"]["stage"].update(instructions=""),
                     "non-empty instructions")

    def test_criterion_id_must_be_non_empty_string(self):
        self._refuse(
            lambda p: p["axes"]["stage"]["criteria"].update(
                {"": "label", "prep": p["axes"]["stage"]["criteria"]["prep"]}),
            "criterion ids")

    def test_criterion_label_required(self):
        self._refuse(
            lambda p: p["axes"]["stage"]["criteria"].update(prep=""),
            "requires a label")

    def test_score_must_be_object(self):
        self._refuse(lambda p: p.update(score=[]), "score block object")

    def test_score_id_required(self):
        self._refuse(lambda p: p["score"].update(id=""),
                     "score requires a non-empty string id")

    def test_score_instructions_required(self):
        self._refuse(lambda p: p["score"].update(instructions=""),
                     "score requires non-empty instructions")

    def test_nouls_must_be_object(self):
        self._refuse(lambda p: p.update(nouls=[]), "nouls must be an object")

    def test_noul_id_must_be_non_empty(self):
        self._refuse(lambda p: p.update(nouls={"": p["nouls"]["waist_relevant"]}),
                     "noul ids")

    def test_noul_spec_must_be_object(self):
        self._refuse(lambda p: p["nouls"].update(extra="nope"),
                     "must be an object")

    def test_noul_fields_required(self):
        self._refuse(
            lambda p: p["nouls"]["waist_relevant"].update(instructions=""),
            "requires non-empty")

    def test_keywords_must_be_object(self):
        self._refuse(lambda p: p.update(keywords=[]),
                     "keywords must be an object")

    def test_keyword_axis_value_must_be_object(self):
        self._refuse(lambda p: p["keywords"].update(stage="not-a-map"),
                     "must be an object")

    def test_keyword_words_must_be_non_empty_strings(self):
        self._refuse(lambda p: p["keywords"]["stage"].update(adjudicate=[""]),
                     "must be non-empty strings")

    def test_keyword_words_must_be_a_list(self):
        self._refuse(
            lambda p: p["keywords"]["stage"].update(adjudicate="ledger"),
            "must be non-empty strings")


if __name__ == "__main__":
    unittest.main()
