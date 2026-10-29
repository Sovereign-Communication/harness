"""Deterministic contract for Lucas Gate v2 typed-track assessments.

The calling agent owns the semantic judgment against the private operator
profile. This module validates the required scorecard and applies the
published category, rounding, and bias bars without calling Jev or a model.
"""

from decimal import Decimal, InvalidOperation
import hashlib


MAX_ARTIFACT_CHARS = 100_000

CATEGORY_LABELS = {
    "1": "surface linguistics",
    "2": "register/tone",
    "3": "discourse moves",
    "4": "decision style",
    "5": "verification epistemics",
    "6": "values",
    "8": "meta-cognition",
    "9": "bias profile",
    "10": "interaction norms",
    "11": "domain fingerprints",
}

BIAS_MINIMUMS = {
    "B1": Decimal("10"),
    "B3": Decimal("9"),
    "B4": Decimal("9"),
    "B5": Decimal("9"),
    "B8": Decimal("9"),
    "B9": Decimal("8"),
    "B10": Decimal("8"),
    "B11": Decimal("9"),
}

LUCAS_GATE_V2_INPUT_FIELDS = frozenset({
    "track", "artifact_text", "category_scores", "bias_checks",
    "overall_average", "actual_risk", "investigated_reason",
})

CATEGORY_FIELDS = frozenset({
    "id", "applicable", "not_applicable_reason", "score",
    "category_average", "evidence", "precedent",
})
BIAS_FIELDS = frozenset({"id", "score", "rationale"})


def _text(value, field, max_length):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field))
    if len(value) > max_length:
        raise ValueError("{} exceeds its {} character limit".format(
            field, max_length))
    return value.strip()


def _score(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("{} must be a number from 0 to 10".format(field))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("{} must be a number from 0 to 10".format(field)) from exc
    if not result.is_finite() or result < 0 or result > 10:
        raise ValueError("{} must be a finite number from 0 to 10".format(field))
    return result


def _rows_by_id(rows, field, expected_ids, allowed_fields):
    if not isinstance(rows, list):
        raise ValueError("{} must be a list".format(field))
    values = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each {} entry must be an object".format(field))
        if not set(row).issubset(allowed_fields):
            raise ValueError("{} contains unsupported fields".format(field))
        item_id = row.get("id")
        if not isinstance(item_id, str) or item_id not in expected_ids:
            raise ValueError("{} contains an unknown id".format(field))
        if item_id in values:
            raise ValueError("{} contains a duplicate id".format(field))
        values[item_id] = row
    if set(values) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(values))
        raise ValueError("{} is missing required ids: {}".format(
            field, ", ".join(missing)))
    return values


def _category_clears_bar(score, category_average, overall_average):
    if score >= Decimal("9"):
        return True, False
    if score >= Decimal("8.95"):
        return category_average >= Decimal("9.25"), True
    if score >= Decimal("8.9"):
        return category_average >= Decimal("9.5"), True
    if score >= Decimal("8.8"):
        return overall_average >= Decimal("9.9"), True
    return False, False


def run_typed_track(*, track, artifact_text, category_scores, bias_checks,
                    actual_risk, overall_average=None, investigated_reason=None):
    """Validate a caller-produced typed scorecard and return its gate result.

    Applicable categories require evidence and precedent. An N/A category
    requires a reason and is neither scored nor echoed by this function.
    The supplied overall average must cover applicable categories only.
    """
    if track != "typed":
        raise ValueError("Lucas Gate v2 runner supports only the typed track")
    _text(artifact_text, "artifact_text", MAX_ARTIFACT_CHARS)
    if not isinstance(actual_risk, bool):
        raise ValueError("actual_risk must be a boolean")
    if investigated_reason is not None:
        investigated_reason = _text(
            investigated_reason, "investigated_reason", 4_000)
    category_rows = _rows_by_id(
        category_scores, "category_scores", CATEGORY_LABELS, CATEGORY_FIELDS)
    applicable_categories = []
    not_applicable_categories = []
    for category_id in CATEGORY_LABELS:
        row = category_rows[category_id]
        applicable = row.get("applicable", True)
        if not isinstance(applicable, bool):
            raise ValueError("category {} applicable must be a boolean".format(
                category_id))
        if applicable:
            if "not_applicable_reason" in row:
                raise ValueError(
                    "applicable category {} cannot have a not-applicable reason".
                    format(category_id))
            applicable_categories.append(category_id)
        else:
            extra = set(row) - {"id", "applicable", "not_applicable_reason"}
            if extra:
                raise ValueError(
                    "not-applicable category {} cannot include scores or evidence".
                    format(category_id))
            _text(row.get("not_applicable_reason"),
                  "category {} not_applicable_reason".format(category_id), 4_000)
            not_applicable_categories.append(category_id)

    if applicable_categories:
        if overall_average is None:
            raise ValueError(
                "overall_average is required when a category is applicable")
        overall = _score(overall_average, "overall_average")
    elif overall_average is None:
        overall = None
    else:
        overall = _score(overall_average, "overall_average")

    below_bar = []
    rounded = []
    for category_id in applicable_categories:
        row = category_rows[category_id]
        score = _score(row.get("score"), "category {} score".format(category_id))
        average = _score(
            row.get("category_average"),
            "category {} average".format(category_id))
        _text(row.get("evidence"), "category {} evidence".format(category_id), 4_000)
        _text(row.get("precedent"), "category {} precedent".format(category_id), 4_000)
        clears, used_rounding = _category_clears_bar(score, average, overall)
        if not clears:
            below_bar.append((category_id, score))
        elif used_rounding:
            rounded.append(category_id)

    bias_rows = _rows_by_id(
        bias_checks, "bias_checks", BIAS_MINIMUMS, BIAS_FIELDS)
    bias_failures = []
    for bias_id, minimum in BIAS_MINIMUMS.items():
        row = bias_rows[bias_id]
        score = _score(row.get("score"), "{} score".format(bias_id))
        _text(row.get("rationale"), "{} rationale".format(bias_id), 4_000)
        if score < minimum:
            bias_failures.append((bias_id, score, minimum))

    low_note = False
    note_categories = [
        item_id for item_id, score in below_bar
        if Decimal("8.5") <= score <= Decimal("8.9")
    ]
    if (not actual_risk and len(below_bar) == 1 and len(note_categories) == 1
            and investigated_reason):
        below_bar = []
        low_note = True

    if actual_risk:
        status = "blocked"
        reasons = ["assessment reports actual risk"]
    elif below_bar or bias_failures:
        status = "revise"
        reasons = []
        if below_bar:
            reasons.append("one or more typed categories remain below bar")
        if bias_failures:
            reasons.append("one or more required bias checks are below their floors")
    elif not applicable_categories:
        status = "not_applicable"
        reasons = ["no typed category applies; use the semantic gate form"]
    elif low_note:
        status = "pass_with_note"
        reasons = ["one category passed the investigated 8.5–8.9 exception"]
    else:
        status = "pass"
        reasons = []

    return {
        "status": status,
        "track": "typed",
        "judgment_owner": "calling agent",
        "validation": "deterministic_scorecard_contract",
        "artifact_sha256": hashlib.sha256(
            artifact_text.encode("utf-8")).hexdigest(),
        "category_ids": list(CATEGORY_LABELS),
        "applicable_category_ids": applicable_categories,
        "not_applicable_categories": not_applicable_categories,
        "bias_ids": list(BIAS_MINIMUMS),
        "bias_minimums": {key: float(value)
                          for key, value in BIAS_MINIMUMS.items()},
        "failing_categories": [
            {"id": category_id, "score": float(score)}
            for category_id, score in below_bar
        ],
        "failing_bias_checks": [
            {"id": bias_id, "score": float(score),
             "minimum": float(minimum)}
            for bias_id, score, minimum in bias_failures
        ],
        "rounded_categories": rounded,
        "investigated_exception": low_note,
        "investigated_category": note_categories[0] if low_note else None,
        "actual_risk": actual_risk,
        "product_jev_run": False,
        "model_called": False,
        "reasons": reasons,
    }
