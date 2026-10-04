"""The EV-0 price gate: no committed verdict, no answer.

OpenRouter publishes ``discount`` next to the listed rates without saying
whether the promotion is already applied, so a promoted model's rates cannot be
used until that question has been answered by measurement
(``discount_probe``) and the answer has been committed as evidence.

The gate is fail-closed in exactly three cases -- no receipt, an unreadable
one, or one measured against a different promotion -- and there is **no
default** anywhere: the two candidates differ by 2x, and a default here would
be a silent 2x error in the cost model of the whole system.

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
                     ECONOMICS_VERDICT_PATH)
from .errors import HarnessError
from .events import emit
from .osal import write_text
from .output import eprint

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
        return path
    return os.path.join(repo_root or REPO_ROOT, ECONOMICS_VERDICT_PATH)


def record_discount_semantics(record, path=None, repo_root=None):
    """Write a probe verdict into the committed receipt. Conclusive only.

    ``unresolved``/``ambiguous`` are written nowhere on purpose: a refusal to
    decide is not a decision, and caching one would let a later run believe
    the question had been answered.

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
    """The committed verdict, or None when there is none.

    Absence is a normal state, not an error: a checkout with no committed
    verdict has nothing to trust, and downstream price computation stays
    refused until the probe runs and its receipt is committed.
    """
    target = discount_verdict_path(path, repo_root)
    try:
        with open(target, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("semantics") not in DISCOUNT_SEMANTICS_VALUES:
        return None
    if not record.get("fingerprint"):
        # A verdict with no fingerprint cannot be re-checked against the
        # live promotion, so it can never be trusted to still apply.
        return None
    return record


def resolve_discount_semantics(endpoints, path=None, repo_root=None):
    """The semantics to use for THIS model's prices, or a refusal.

    Three states, in order:

    * no promotion running -> ``not_applicable``; listed rates are used
      as-is and the question never arises;
    * a conclusive verdict in the COMMITTED receipt whose fingerprint still
      matches the live promotion -> that verdict;
    * otherwise -> :class:`HarnessError`. There is no default, because the
      two candidates differ by 2x and guessing is the one failure mode this
      gate exists to remove.

    Fail-closed is unchanged by where the verdict lives: a checkout with no
    committed receipt, an unreadable one, or one measured against a different
    promotion all refuse, and each refusal names the receipt path so the
    operator knows exactly what is missing.
    """
    if not endpoints.has_discount:
        return DISCOUNT_NOT_APPLICABLE
    record = load_discount_semantics(path, repo_root)
    if record is None:
        raise HarnessError(
            f"{endpoints.model_id} has a running discount "
            f"({endpoints.max_discount:g}) but no committed semantics verdict "
            f"at {discount_verdict_path(path, repo_root)}. OpenRouter does "
            f"not document whether the published rate already includes the "
            f"promotion, and the two readings differ by "
            f"{1 / max(1e-9, 1 - endpoints.max_discount):.2f}x. Run "
            f"`harness economics --probe-model <id> --record` to measure it "
            f"and commit the receipt; refusing rather than guessing.")
    fingerprint = record.get("fingerprint") or {}
    recorded_discount = fingerprint.get("max_discount")
    if recorded_discount is None:
        raise HarnessError(
            f"committed discount verdict for {endpoints.model_id} carries no "
            f"promotion fingerprint; re-run the probe and commit the new "
            f"receipt.")
    if abs(float(recorded_discount) - endpoints.max_discount) > 1e-9:
        raise HarnessError(
            f"committed discount verdict was measured against a "
            f"{float(recorded_discount):g} promotion but "
            f"{endpoints.model_id} now publishes {endpoints.max_discount:g}. "
            f"The answer may no longer hold; re-run "
            f"`harness economics --probe-model <id> --record`.")
    return record["semantics"]
