"""Post-action vision verification: the screen says the action landed.

The driver already verifies *what it sees before it decides*. This module
answers the next question: after a mutating action has been performed, did
the action actually produce the visible result it claimed? It exists for the
two actions where "the executor returned success" is not enough -- launching
an application and typing text into a field.

The rules here are deliberately narrow and code-owned:

* a screenshot is taken through :class:`~driver_core.adapters.ScreenSource`;
* one question goes to the same Jev endpoint, key, budget and price list as
  every other call -- there is no second client;
* the model returns probabilities and scores only. The threshold, the retry
  schedule, the score-to-percent conversion and the pass decision are code;
* a failed check is a refusal, not a note on a successful action;
* the whole feature is off unless both ``DRIVER_SCREEN`` and
  ``DRIVER_VISION_VERIFY`` are enabled, so a vision-free driver is unchanged.

The second half of the module is the versioned ``launch-verify`` pack. Each
field is asked twice over the same screenshot: a Choice naming the visibly
selected value, and a Score from 0 to 3 for how clearly that value can be
read and matches the manifest. A field passes only when the extraction agrees
with the manifest *and* ``score / 3 * 100`` reaches the 95 floor. That is
deliberately stronger than the launch/type Noul check: a chosen value that
is merely plausible cannot pass on the strength of a high score.

This module does not contain a live calibration corpus or claim a freeze.
Those require screenshots from the installed FreeBuff and a live key; the
absence is recorded as the named follow-up in the #272 evidence rather than
being papered over with synthetic results.
"""
import base64
import time
import uuid

from . import osal, transport
from .adapters import ScreenSource
from .audit import KIND_ESCALATION, KIND_VERIFICATION
from .budget import estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION, image_model
from .observation import GUI, Target

#: The original #272 visibility bar. The model answers a Noul; code compares
#: its probability with this number.
APP_VISIBILITY_THRESHOLD = 0.85
TEXT_VISIBILITY_THRESHOLD = 0.85

#: The action has just handed off, so the window may not have painted yet.
#: The two polls are measured from action completion, not from each other.
LAUNCH_POLLS = (2.0, 5.0)

#: A poll may spend at most these long in either leg. The capture is local;
#: the verdict is the remote call. Exceeding either marks the attempt as a
#: timeout, and the driver escalates rather than waiting indefinitely.
CAPTURE_BUDGET_SECONDS = 2.0
VERDICT_BUDGET_SECONDS = 5.0

#: The image request's worst-case context bound, the same one the vision
#: extractor reserves. Kept local so this module does not import the whole
#: extraction package just to name a number.
IMAGE_REQUEST_TOKEN_BOUND = 65_536

#: Models whose image-token bound has been measured. The same list the
#: vision extractor uses; a different configured model has no sourced bound
#: and is refused locally instead of being billed against a guess.
_IMAGE_MODELS_WITH_VERIFIED_BOUND = ("jev-latest", "jev-1.13.0")


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class Verification:
    """The result of one post-action visibility check.

    ``evidence`` is metadata only: attempt number, poll delay, probability,
    cost and reason. The screenshot never enters it.
    """

    __slots__ = ("kind", "passed", "attempts", "threshold", "timed_out",
                 "reason", "cost", "evidence")

    def __init__(self, kind, *, passed, attempts, threshold, timed_out,
                 reason="", cost=0.0, evidence=None):
        self.kind = kind
        self.passed = bool(passed)
        self.attempts = int(attempts)
        self.threshold = float(threshold)
        self.timed_out = bool(timed_out)
        self.reason = reason
        self.cost = float(cost or 0.0)
        self.evidence = list(evidence or [])

    def to_dict(self):
        return {
            "kind": self.kind,
            "passed": self.passed,
            "attempts": self.attempts,
            "threshold": self.threshold,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "cost_usd": round(self.cost, 9),
            "evidence": list(self.evidence),
        }

    def __repr__(self):
        return (f"Verification({self.kind!r}, passed={self.passed}, "
                f"attempts={self.attempts})")


#: The eight fields in the S3 launch-verify pack, in declaration order.
PACK_FIELDS = (
    "app_identity", "backend", "model", "thinking", "repo", "build_mode",
    "mission_queued", "start_state",
)

#: The Score levels are shared by every field. The wording is part of the
#: contract: a pack revision that changes it must bump ``PACK_VERSION`` and
#: rerun the corpus.
SCORE_LEVELS = (
    "0 = the row is absent or the value clearly differs",
    "1 = the row is visible, but the value is unreadable or ambiguous",
    "2 = the value is readable and matches, but is partially obscured or "
    "low-contrast",
    "3 = the value is clearly readable and exactly matches",
)
PACK_FLOOR = 95.0
PACK_ID = "launch-verify"
PACK_VERSION = "1.0.0"
NOT_VISIBLE = "not_visible"


class FieldResult:
    """One field's two answers, judged in code."""

    __slots__ = ("name", "expected", "observed", "score", "percent",
                 "agrees", "passed", "reason")

    def __init__(self, name, *, expected, observed=None, score=None,
                 percent=None, agrees=False, passed=False, reason=""):
        self.name = name
        self.expected = expected
        self.observed = observed
        self.score = score
        self.percent = percent
        self.agrees = bool(agrees)
        self.passed = bool(passed)
        self.reason = reason

    def to_dict(self):
        return {
            "name": self.name,
            "expected": self.expected,
            "observed": self.observed,
            "score": self.score,
            "percent": self.percent,
            "agrees": self.agrees,
            "passed": self.passed,
            "reason": self.reason,
        }


class LaunchVerifyResult:
    """The pack verdict, with every field's evidence retained."""

    __slots__ = ("pack_id", "version", "floor", "passed", "fields", "reason",
                 "cost", "timed_out")

    def __init__(self, *, passed, fields, reason="", cost=0.0,
                 timed_out=False, pack_id=PACK_ID, version=PACK_VERSION,
                 floor=PACK_FLOOR):
        self.pack_id = pack_id
        self.version = version
        self.floor = floor
        self.passed = bool(passed)
        self.fields = tuple(fields)
        self.reason = reason
        self.cost = float(cost or 0.0)
        self.timed_out = bool(timed_out)

    def to_dict(self):
        return {
            "pack": self.pack_id,
            "version": self.version,
            "floor": self.floor,
            "passed": self.passed,
            "reason": self.reason,
            "cost_usd": round(self.cost, 9),
            "timed_out": self.timed_out,
            "fields": [field.to_dict() for field in self.fields],
        }

    def __repr__(self):
        return (f"LaunchVerifyResult({self.pack_id}@{self.version}, "
                f"passed={self.passed})")


class LaunchVerifyPack:
    """The versioned eight-field gate pack.

    ``manifest`` is caller-owned: each field names the expected value and
    the complete option set the screen is allowed to show. The pack does not
    invent model or repo names, and it never puts the expected value into the
    question -- doing so would turn an observation into a leading prompt.
    """

    def __init__(self, manifest):
        if not isinstance(manifest, dict):
            raise ValueError("launch-verify manifest must be an object")
        missing = [name for name in PACK_FIELDS if name not in manifest]
        extra = sorted(set(manifest) - set(PACK_FIELDS))
        if missing or extra:
            raise ValueError(
                f"launch-verify manifest must name exactly {PACK_FIELDS}; "
                f"missing {missing}, extra {extra}")
        self.manifest = {}
        for name in PACK_FIELDS:
            entry = manifest[name]
            if not isinstance(entry, dict):
                raise ValueError(f"manifest field {name!r} must be an object")
            expected = entry.get("expected")
            options = entry.get("options")
            if not isinstance(expected, str) or not expected.strip():
                raise ValueError(
                    f"manifest field {name!r} needs a non-empty expected value")
            if (not isinstance(options, (list, tuple)) or not options
                    or any(not isinstance(option, str) or not option
                           for option in options)):
                raise ValueError(
                    f"manifest field {name!r} needs a non-empty options list")
            options = tuple(dict.fromkeys(options))
            if expected not in options:
                raise ValueError(
                    f"manifest field {name!r}: expected {expected!r} is not "
                    f"in its declared options")
            self.manifest[name] = {"expected": expected, "options": options}

    def questions(self):
        """Two questions per field over the one screenshot in ``state``."""
        questions = {}
        for name in PACK_FIELDS:
            entry = self.manifest[name]
            options = {option: option for option in entry["options"]}
            options[NOT_VISIBLE] = (
                f"The {name.replace('_', ' ')} is not visible on screen.")
            questions[f"{name}.choice"] = transport.choice(
                f"Looking only at the screenshot, which value is visibly "
                f"selected or shown for {name.replace('_', ' ')}? Choose "
                f"{NOT_VISIBLE!r} when the field is absent.",
                options)
            questions[f"{name}.score"] = transport.score(
                f"How clearly is the {name.replace('_', ' ')} value readable "
                f"and how exactly does it match the manifest? Use these "
                f"levels exactly: " + "; ".join(SCORE_LEVELS),
                SCORE_LEVELS)
        return questions

    def evaluate(self, answers):
        """Judge the pack's answers in code; fail closed on every hole."""
        expected_keys = set(self.questions())
        if not isinstance(answers, dict):
            return LaunchVerifyResult(
                passed=False,
                fields=[FieldResult(name, expected=self.manifest[name]["expected"],
                                    reason="answers were not an object")
                        for name in PACK_FIELDS],
                reason="answers were not an object")
        extra = sorted(set(answers) - expected_keys)
        if extra:
            return LaunchVerifyResult(
                passed=False,
                fields=[FieldResult(name, expected=self.manifest[name]["expected"],
                                    reason=f"undeclared answer(s): {extra}")
                        for name in PACK_FIELDS],
                reason=f"undeclared answer(s): {extra}")

        results = []
        for name in PACK_FIELDS:
            entry = self.manifest[name]
            choice = answers.get(f"{name}.choice")
            score = answers.get(f"{name}.score")
            reason = ""
            observed = None
            percent = None
            agrees = False
            if not isinstance(choice, dict) or choice.get("type") != "choice":
                reason = "choice answer missing or not a Choice"
            else:
                observed = choice.get("choice")
                if observed not in entry["options"] + (NOT_VISIBLE,):
                    reason = f"choice {observed!r} is not a declared option"
            if not reason and (not isinstance(score, dict)
                               or score.get("type") != "score"):
                reason = "score answer missing or not a Score"
            if not reason:
                try:
                    score_value = float(score.get("score"))
                except (TypeError, ValueError):
                    reason = "score was not numeric"
                else:
                    if not 0.0 <= score_value <= 3.0:
                        reason = f"score {score_value:g} is outside 0..3"
                    elif score_value not in (0.0, 1.0, 2.0, 3.0):
                        reason = f"score {score_value:g} is not a declared level"
                    else:
                        percent = score_value / 3.0 * 100.0
            if not reason:
                agrees = observed == entry["expected"]
                passed = agrees and percent >= PACK_FLOOR
                if not agrees:
                    reason = (f"extraction read {observed!r}, manifest says "
                              f"{entry['expected']!r}")
                elif percent < PACK_FLOOR:
                    reason = (f"score {score.get('score')} maps to "
                              f"{percent:.1f}%, below the {PACK_FLOOR:g} floor")
            else:
                passed = False
            results.append(FieldResult(
                name, expected=entry["expected"], observed=observed,
                score=score.get("score") if isinstance(score, dict) else None,
                percent=percent, agrees=agrees, passed=passed, reason=reason))

        failed = [field for field in results if not field.passed]
        if failed:
            reason = "below bar: " + ", ".join(field.name for field in failed)
        else:
            reason = ""
        return LaunchVerifyResult(passed=not failed, fields=results,
                                  reason=reason)

    def __repr__(self):
        return f"LaunchVerifyPack({PACK_ID}@{PACK_VERSION})"


class VisionVerifier:
    """Screenshot-and-Jev verification over one injected screen source."""

    def __init__(self, settings, *, budget, audit, source=None,
                 transport_module=None, clock=None, sleep=None,
                 image_preparer=None,
                 capture_timeout=CAPTURE_BUDGET_SECONDS,
                 verdict_timeout=VERDICT_BUDGET_SECONDS):
        self.settings = settings
        self.budget = budget
        self.audit = audit
        self._source = source
        self._transport = transport_module or transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._image_preparer = image_preparer
        self.capture_timeout = float(capture_timeout)
        self.verdict_timeout = float(verdict_timeout)
        self.url = self._endpoint()

    def _endpoint(self):
        from .jev_client import SYSTEM_ONE_URL
        return SYSTEM_ONE_URL

    def _screen(self):
        if self._source is None:
            self._source = ScreenSource(pixels=True, encoder=_read_bytes)
        return self._source

    def _precondition(self, kind, step_id):
        if not self.settings.screen_enabled:
            return self._failed(
                kind, step_id, "screen vision is not enabled",
                attempts=0, timed_out=False)
        if not self.settings.keyed:
            return self._failed(
                kind, step_id, "no key for vision verification",
                attempts=0, timed_out=False)
        if self.settings.jev_model not in _IMAGE_MODELS_WITH_VERIFIED_BOUND:
            return self._failed(
                kind, step_id,
                "no verified image budget bound for configured Jev model; "
                "use jev-latest or jev-1.13.0",
                attempts=0, timed_out=False)
        return None

    def verify_application(self, app_name, *, step_id="verification",
                           polls=LAUNCH_POLLS):
        """Confirm an application is visible and focused after ``launch_app``."""
        prompt = f"Is the application '{app_name}' visible and focused?"
        return self._verify_visibility(
            "application", prompt, APP_VISIBILITY_THRESHOLD,
            step_id=step_id, polls=polls)

    def verify_text(self, text, *, step_id="verification"):
        """Confirm typed text appears in the screenshot after ``type_text``."""
        prompt = f"Is the text '{text}' visible in the screenshot?"
        return self._verify_visibility(
            "text", prompt, TEXT_VISIBILITY_THRESHOLD, step_id=step_id,
            polls=(0.0,))

    def _verify_visibility(self, kind, prompt, threshold, *, step_id, polls):
        blocked = self._precondition(kind, step_id)
        if blocked is not None:
            return blocked
        started = self._clock()
        evidence = []
        total_cost = 0.0
        saw_timeout = False
        for attempt, delay in enumerate(polls, start=1):
            pause = float(delay) - (self._clock() - started)
            if pause > 0:
                self._sleep(pause)
            capture_started = self._clock()
            try:
                capture = self._screen().capture(
                    Target("vision-verification", GUI))
            except Exception as exc:  # a capture failure is a failed attempt
                capture = None
                capture_why = f"capture raised: {exc}"
            else:
                capture_why = capture.detail or "capture produced no payload"
            capture_elapsed = self._clock() - capture_started
            if capture is None or not capture.ok or capture_elapsed > \
                    self.capture_timeout:
                reason = (f"capture exceeded {self.capture_timeout:g}s"
                          if capture_elapsed > self.capture_timeout
                          else capture_why)
                saw_timeout = saw_timeout or capture_elapsed > \
                    self.capture_timeout
                evidence.append({
                    "attempt": attempt, "delay": float(delay),
                    "reason": reason, "cost_usd": 0.0,
                    "timed_out": capture_elapsed > self.capture_timeout,
                })
                self._audit_attempt(step_id, kind, attempt, False, None, 0.0,
                                    reason, capture_elapsed, False)
                continue

            asset, why = self._asset(capture.payload)
            if asset is None:
                evidence.append({
                    "attempt": attempt, "delay": float(delay),
                    "reason": why, "cost_usd": 0.0, "timed_out": False,
                })
                self._audit_attempt(step_id, kind, attempt, False, None, 0.0,
                                    why, capture_elapsed, False)
                continue

            asked_at = self._clock()
            probability, cost, why, verdict_timeout = self._ask(
                asset, prompt, step_id, kind, attempt)
            verdict_elapsed = self._clock() - asked_at
            total_cost += cost
            passed = probability is not None and probability >= threshold
            if not passed and not why:
                why = (f"visibility probability {probability:.3f} is below "
                       f"the {threshold:g} threshold")
            if verdict_elapsed > self.verdict_timeout:
                passed = False
                why = f"verdict exceeded {self.verdict_timeout:g}s"
                verdict_timeout = True
            saw_timeout = saw_timeout or verdict_timeout
            evidence.append({
                "attempt": attempt, "delay": float(delay),
                "probability": probability, "reason": why,
                "cost_usd": round(cost, 9), "timed_out": verdict_timeout,
            })
            self._audit_attempt(step_id, kind, attempt, passed, probability,
                                cost, why, verdict_elapsed, verdict_timeout)
            if passed:
                return Verification(
                    kind, passed=True, attempts=attempt, threshold=threshold,
                    timed_out=False, cost=total_cost, evidence=evidence)
            if verdict_timeout:
                # The verdict already exceeded its 5s budget. Waiting for the
                # next poll would add three more seconds to a call that has
                # already failed its clock, so this is the escalation point.
                break
        reason = "application not visible" if kind == "application" \
            else "typed text not visible"
        self.audit.append(
            KIND_ESCALATION, step_id=step_id, reason="verification_failed",
            detail=f"{kind} verification failed after {len(polls)} poll(s)",
            verify_kind=kind)
        return Verification(
            kind, passed=False, attempts=len(polls), threshold=threshold,
            timed_out=saw_timeout, reason=reason, cost=total_cost,
            evidence=evidence)

    def verify_manifest(self, manifest, *, step_id="verification"):
        """Run the versioned eight-field pack over one screenshot."""
        try:
            pack = LaunchVerifyPack(manifest)
        except ValueError as exc:
            return LaunchVerifyResult(
                passed=False, fields=[], reason=f"manifest refused: {exc}")
        blocked = self._precondition("manifest", step_id)
        if blocked is not None:
            return LaunchVerifyResult(
                passed=False, fields=[], reason=blocked.reason,
                timed_out=blocked.timed_out)
        capture_started = self._clock()
        try:
            capture = self._screen().capture(Target("vision-verification", GUI))
        except Exception as exc:
            return LaunchVerifyResult(
                passed=False, fields=[],
                reason=f"capture raised: {exc}", timed_out=True)
        capture_elapsed = self._clock() - capture_started
        if not capture.ok:
            return LaunchVerifyResult(
                passed=False, fields=[], reason=capture.detail or "no capture",
                timed_out=capture_elapsed > self.capture_timeout)
        if capture_elapsed > self.capture_timeout:
            return LaunchVerifyResult(
                passed=False, fields=[],
                reason=f"capture exceeded {self.capture_timeout:g}s",
                timed_out=True)
        asset, why = self._asset(capture.payload)
        if asset is None:
            return LaunchVerifyResult(passed=False, fields=[], reason=why)
        asked_at = self._clock()
        answers, cost, why, timed_out = self._call(
            asset, pack.questions(), step_id, "manifest", 1)
        elapsed = self._clock() - asked_at
        if elapsed > self.verdict_timeout:
            timed_out = True
            why = why or f"verdict exceeded {self.verdict_timeout:g}s"
        if answers is None:
            return LaunchVerifyResult(
                passed=False, fields=[], reason=why, cost=cost,
                timed_out=timed_out)
        result = pack.evaluate(answers)
        result.cost = cost
        result.timed_out = timed_out
        self.audit.append(
            KIND_VERIFICATION, step_id=step_id, tier="vision",
            pack=result.pack_id, version=result.version, ok=result.passed,
            reason=result.reason, cost_usd=round(cost, 9),
            timed_out=result.timed_out,
            fields=[field.to_dict() for field in result.fields])
        if not result.passed:
            self.audit.append(
                KIND_ESCALATION, step_id=step_id,
                reason="verification_failed",
                detail=f"{result.pack_id}@{result.version} {result.reason}")
        return result

    def _failed(self, kind, step_id, reason, *, attempts, timed_out):
        self.audit.append(
            KIND_VERIFICATION, step_id=step_id, tier="vision",
            verify_kind=kind, ok=False, reason=reason, cost_usd=0.0,
            timed_out=timed_out)
        return Verification(
            kind, passed=False, attempts=attempts,
            threshold=APP_VISIBILITY_THRESHOLD, timed_out=timed_out,
            reason=reason)

    def _audit_attempt(self, step_id, kind, attempt, passed, probability,
                       cost, reason, elapsed, timed_out):
        self.audit.append(
            KIND_VERIFICATION, step_id=step_id, tier="vision",
            verify_kind=kind, attempt=attempt, ok=passed,
            probability=probability,
            reason=reason, cost_usd=round(cost, 9),
            elapsed_seconds=round(elapsed, 3), timed_out=timed_out)

    def _asset(self, payload):
        if not isinstance(payload, (bytes, bytearray)):
            return None, "capture payload is not PNG bytes"
        data = bytes(payload)
        if self._image_preparer is not None:
            prepared, why = self._image_preparer(data)
        else:
            prepared, why = osal.prepare_image_bytes(data)
        if prepared is None:
            return None, why or "image preparation refused the capture"
        data = getattr(prepared, "data", prepared)
        dimensions, why = osal.validate_prepared_image(data)
        if dimensions is None:
            return None, why
        ref = f"screen-{uuid.uuid4().hex[:8]}"
        return {
            ref: {"type": "image", "format": "png",
                  "data_base64": base64.b64encode(data).decode("ascii")},
        }, ""

    def _ask(self, asset, prompt, step_id, kind, attempt):
        questions = {
            "visible": transport.noul(
                prompt,
                true="The screenshot shows the named thing.",
                false="The screenshot does not show the named thing."),
        }
        answers, cost, why, timed_out = self._call(
            asset, questions, step_id, kind, attempt)
        if answers is None:
            return None, cost, why, timed_out
        answer = answers.get("visible")
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            return None, cost, "visibility answer is missing or not a Noul",\
                timed_out
        try:
            probability = float(answer.get("noul"))
        except (TypeError, ValueError):
            return None, cost, "visibility answer was not numeric", timed_out
        if not 0.0 <= probability <= 1.0:
            return None, cost, (
                f"visibility answer {probability:g} is outside [0, 1]"), \
                timed_out
        return probability, cost, "", timed_out

    def _call(self, asset, questions, step_id, kind, attempt):
        ref = next(iter(asset))
        headers = {"Authorization": f"Bearer {self.settings.jev_api_key}"}
        route = f"{step_id}:verify-{kind}-{attempt}"

        def dispatch():
            return self._transport.call_service(
                self.url, questions, headers=headers,
                timeout=self.verdict_timeout,
                body_extra={"state": {"screen": ref},
                            "model": image_model(self.settings)},
                assets=asset, images_enabled=True)

        def report(one):
            if one.stage == "reserve":
                return None, 0.0, f"budget refused: {one.error}", False
            if one.error is not None:
                return None, one.charged, f"transport raised: {one.error}", \
                    one.stage == "dispatch"
            response = one.response
            if not response.ok:
                return None, one.charged, \
                    f"{response.outcome}: {response.detail}", False
            payload = response.payload or {}
            return payload.get("answers"), one.charged, "", False

        return self.budget.exchange(
            label=route,
            reserve_usd=estimate_call_cost(
                IMAGE_REQUEST_TOKEN_BOUND,
                price_per_million=JEV_INPUT_PRICE_PER_MILLION),
            dispatch=dispatch, report=report,
            price_per_million=JEV_INPUT_PRICE_PER_MILLION)

    def __repr__(self):
        return f"VisionVerifier(source={self._source!r})"
