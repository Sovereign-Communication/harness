"""The EV-0 price gate: no committed verdict, no answer.

OpenRouter publishes ``discount`` next to the listed rates without saying
whether the promotion is already applied, so a promoted model's rates cannot be
used until that question has been answered by measurement
(``discount_probe``) and the answer has been committed as evidence.

The gate is fail-closed on four questions, and every refusal names which one
failed: is there a readable receipt at all, is it in a format this code
understands, does it name THIS model, and does its promotion still match.  It
never defaults: the two candidate readings differ by 2x, and a default here
would be a silent 2x error in the cost model of the whole system.

That last pair is not decoration.  The receipt records the model it was
measured on precisely so that a verdict about one promotion cannot be spent on
another, and a receipt stamped with a schema this code has never read cannot be
interpreted at all -- honouring either would be the 2x error arriving by a
different door than the one the probe exists to close.

The verdict's *location* is part of that.  It is a repo-committed receipt
(:data:`~harness.config.ECONOMICS_VERDICT_PATH`) resolved from the checkout
root, not state under the operator's config dir: a verdict that authorizes
every downstream cost decision has to be reviewable evidence travelling with
the code, and two checkouts must never disagree about whether the gate is
satisfied.  It lives under ``audits/self/dogfood/`` because that is where the
DoD puts live receipts and where D11 already SHA-256-pins every tracked file,
so a hand-edited verdict fails the audit deterministically.

Consumes :class:`~harness.endpoint_pricing.ModelEndpoints` structurally --
``has_discount``/``max_discount``/``model_id`` -- so this gate holds no
dependency on the pricing module and stays the one place a verdict is decided.

Part 4 of 4 in EV-0's evidence layer: ``endpoint_pricing``,
``benchmark_ingest``, ``discount_probe``, ``discount_gate`` (this module).
Canon lives in ``docs/jev-roadmap.md`` (``EV-*``).
"""
import json
import os

from .config import (DISCOUNT_NOT_APPLICABLE, DISCOUNT_SEMANTICS_VALUES,
                     ECONOMICS_SCHEMA_VERSION, ECONOMICS_VERDICT_PATH)
from .errors import HarnessError
from .events import emit
from .osal import write_text
from .output import eprint
from .routing_table import strip_variant_suffix

#: The Harness checkout that owns the committed discount-verdict receipt.
#: Resolved from this file rather than the CWD so the gate asks the same
#: question in every checkout (an installed wheel resolves to site-packages,
#: which has no receipt -- and the gate then refuses, which is correct: a
#: wheel carries code, not evidence).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Recording + the gate
def discount_verdict_path(path=None, repo_root=None):
    """Where the verdict lives: a REPO-COMMITTED receipt.

    Explicit ``path`` wins (hermetic tests, CI, an operator inspecting a
    checkout). Otherwise the path is resolved against the checkout root, not
    ``~/.config``: the verdict authorizes every downstream cost decision, so
    it is evidence that has to travel with the code and be reviewable in a PR.

    There is no machine-local fallback, and that absence is the fix. While the
    gate read ``~/.config/harness/economics.json`` it asked the operator's
    filesystem rather than the repository, so two checkouts could disagree
    about whether the price gate was satisfied -- and a verdict nobody could
    review governed the cost model of the whole system.
    """
    if path:
        # An explicit override is used exactly as given: it may be a scratch
        # path from a test or CI, and rewriting it is not this gate's call.
        return path
    # Normalized because this string is shown to the operator in every refusal
    # and in the "wrote repo evidence" line. ECONOMICS_VERDICT_PATH is a
    # POSIX-style repo-relative constant, and joining it unnormalized rendered
    # C:\repo/audits/self/dogfood/... on Windows -- the one piece of evidence
    # text the reader is most likely to copy somewhere.
    return os.path.normpath(os.path.join(repo_root or REPO_ROOT,
                                         ECONOMICS_VERDICT_PATH))


def _read_verdict(target):
    """The receipt as written, or None when there is nothing readable.

    Deliberately raw: the gate needs to see a receipt it cannot use in order to
    NAME why, so validity is decided by :func:`_verdict_rejection` and not by
    collapsing every failure into "absent".
    """
    try:
        with open(target, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _verdict_rejection(record):
    """Why this receipt cannot be trusted as-is, or None when it can.

    ONE validator, two consumers: the recorder refuses to write a receipt that
    would fail here, and the gate refuses to read one. Format first -- a receipt
    stamped with a schema this code has never read cannot be interpreted at
    all, so guessing at its fields is how a 2x error sneaks in wearing the
    authority of a measurement.
    """
    if record.get("schema") != ECONOMICS_SCHEMA_VERSION:
        return (f"it declares schema {record.get('schema')!r} and this code "
                f"reads schema {ECONOMICS_SCHEMA_VERSION}")
    if record.get("semantics") not in DISCOUNT_SEMANTICS_VALUES:
        return (f"its verdict {record.get('semantics')!r} is not conclusive "
                f"(expected one of {', '.join(DISCOUNT_SEMANTICS_VALUES)})")
    fingerprint = record.get("fingerprint")
    # A verdict with no fingerprint cannot be re-checked against the live
    # promotion, so it can never be trusted to still apply.
    if not isinstance(fingerprint, dict) or not fingerprint:
        return "it carries no promotion fingerprint"
    return None


def record_discount_semantics(record, path=None, repo_root=None):
    """Write a probe verdict into the committed receipt. Conclusive only.

    ``unresolved``/``ambiguous`` are written nowhere on purpose: a refusal to
    decide is not a decision, and caching one would let a later run believe
    the question had been answered.

    The recorder stamps the schema it writes when the record does not carry
    one, and REFUSES a record that declares a different one -- writing a
    receipt the gate would then reject is the same defect as shipping a
    verdict nothing can read.

    The write lands in the working tree, which is the point: the verdict is
    repo evidence and is meant to be committed with the code it governs (and,
    under ``audits/self/dogfood/``, SHA-256-pinned by the corpus manifest, so
    a later hand-edit of it fails the audit).
    """
    semantics = record.get("semantics")
    if semantics not in DISCOUNT_SEMANTICS_VALUES:
        raise HarnessError(
            f"refusing to record discount semantics {semantics!r}: only a "
            f"conclusive verdict ({', '.join(DISCOUNT_SEMANTICS_VALUES)}) can "
            f"be stored. Re-run the probe on an unambiguous model.")
    if record.get("schema") is None:
        record = {**record, "schema": ECONOMICS_SCHEMA_VERSION}
    problem = _verdict_rejection(record)
    if problem:
        raise HarnessError(
            f"refusing to record a discount verdict the gate could not "
            f"honour: {problem}. Re-run the probe and record what it returns.")
    target = discount_verdict_path(path, repo_root)
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    write_text(target, json.dumps(record, indent=2, sort_keys=True) + "\n")
    emit("economics_discount_recorded", model=record.get("model"),
         semantics=semantics)
    eprint(f"[economics] wrote repo evidence {target}; commit it -- a verdict "
           f"that lives on one machine is not repo evidence and the gate "
           f"reads only the committed receipt.")
    return target


def load_discount_semantics(path=None, repo_root=None):
    """The committed verdict, or None when there is none this code can use.

    Absence is a normal state, not an error: a checkout with no committed
    verdict has nothing to trust, and downstream price computation stays
    refused until the probe runs and its receipt is committed. A receipt that
    exists but is unusable -- wrong schema, inconclusive, no fingerprint --
    reads as None here; :func:`resolve_discount_semantics` is the surface that
    names which of those it was.
    """
    record = _read_verdict(discount_verdict_path(path, repo_root))
    if record is None or _verdict_rejection(record):
        return None
    return record


def resolve_discount_semantics(endpoints, path=None, repo_root=None):
    """The semantics to use for THIS model's prices, or a refusal.

    States, in order:

    * no promotion running -> ``not_applicable``; listed rates are used
      as-is and the question never arises;
    * a conclusive verdict in the COMMITTED receipt, written in a schema this
      code reads, measured ON THIS MODEL, whose promotion still matches -> that
      verdict;
    * otherwise -> :class:`HarnessError`, naming the question that failed.

    There is no default, because the two candidates differ by 2x and guessing
    is the one failure mode this gate exists to remove. Each clause below is a
    distinct way a verdict can fail to describe this model, and each names
    itself in the refusal: "it was measured on a different model" and "it is in
    a schema this code cannot read" are not the same defect, and an operator
    fixing one would be misled by a message that named the other.
    """
    if not endpoints.has_discount:
        return DISCOUNT_NOT_APPLICABLE
    target = discount_verdict_path(path, repo_root)
    record = _read_verdict(target)
    if record is None:
        raise HarnessError(
            f"{endpoints.model_id} has a running discount "
            f"({endpoints.max_discount:g}) but no readable committed semantics "
            f"verdict at {target}. OpenRouter does "
            f"not document whether the published rate already includes the "
            f"promotion, and the two readings differ by "
            f"{1 / max(1e-9, 1 - endpoints.max_discount):.2f}x. Run "
            f"`harness economics --probe-model <id> --record` to measure it "
            f"and commit the receipt; refusing rather than guessing.")
    problem = _verdict_rejection(record)
    if problem:
        raise HarnessError(
            f"{endpoints.model_id} has a running discount "
            f"({endpoints.max_discount:g}) and the committed verdict at "
            f"{target} cannot be honoured: {problem}. Re-run "
            f"`harness economics --probe-model {endpoints.model_id} --record` "
            f"and commit what it returns; refusing rather than guessing.")
    fingerprint = record["fingerprint"]
    # Model identity is compared canonically on BOTH sides: `a/m` and
    # `a/m:free` are one model to the feed, so a variant suffix must not turn a
    # matching receipt into a refusal.
    measured_on = fingerprint.get("model")
    asked_about = strip_variant_suffix(endpoints.model_id)
    if measured_on != asked_about:
        raise HarnessError(
            f"the committed discount verdict at {target} was measured on "
            f"{measured_on!r}, not on {asked_about!r}. The question is about "
            f"how this promotion is accounted for on THIS model, so another "
            f"model's answer is not an answer here -- and the two readings "
            f"differ by "
            f"{1 / max(1e-9, 1 - endpoints.max_discount):.2f}x. Run "
            f"`harness economics --probe-model {asked_about} --record` and "
            f"commit that receipt; refusing rather than borrowing one.")
    recorded_discount = fingerprint.get("max_discount")
    if recorded_discount is None:
        raise HarnessError(
            f"committed discount verdict for {asked_about} carries no "
            f"promotion fingerprint; re-run the probe and commit the new "
            f"receipt.")
    if abs(float(recorded_discount) - endpoints.max_discount) > 1e-9:
        raise HarnessError(
            f"committed discount verdict was measured against a "
            f"{float(recorded_discount):g} promotion but "
            f"{asked_about} now publishes {endpoints.max_discount:g}. "
            f"The answer may no longer hold; re-run "
            f"`harness economics --probe-model {asked_about} --record`.")
    return record["semantics"]
