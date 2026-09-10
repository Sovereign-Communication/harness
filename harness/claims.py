"""P0 self-grounding for structured claims: source_refs lint + verbatim auto-expansion.

Fixes the prompt-assertion class of failure (the 04b lesson from the
SCMessenger audit): a claim that asserts a load-bearing fact -- "no cap",
"unbounded", "never", "always", "only" -- must point at the line(s) in the
quoted source that justify it, and a claim that cites an identifier defined
OUTSIDE the quoted window (MAX_SKIP_KEYS, get_message_key) auto-resolves the
definition and appends it verbatim before the panel ever sees the prompt.
5/5 unanimity on a premise the panel cannot check is worth nothing; this makes
the premise checkable, or rejects the claim before any model is called.

Rules (all deterministic, hermetic):

  R1 UNGROUNDED-ASSERTION (error)
     A claim containing a load-bearing absence/universal phrase must carry at
     least one source_ref into the quoted window. No refs => rejected.
  R2 OUT-OF-WINDOW-REF (error)
     Every source_ref must be a 1-based line that exists inside the quoted
     window (auto-expansions are appended AFTER the window, so the original
     line numbers stay stable).
  R3 CONTRADICTED-BY-SOURCE (error, after expansion)
     If an absence-styled claim ("no cap", "unbounded", "no upper bound")
     cites an identifier whose verbatim definition contains a bound (a
     MAX_* constant/limit, a `len() > MAX_` guard), the claim asserts absence
     of a bound the source itself shows -- reject with the evidence.

Auto-expansion (never an error): identifiers referenced by the claims or the
context prose that exist in the definitions index and are absent from the
quoted window are appended verbatim (deduplicated, transitive over constants)
to the source the panel sees. Unresolvable backticked snake_case identifiers
are reported as warnings. Reassurance claims are not forced to carry refs
unless they happen to use a load-bearing word.
"""
import json
import re
from dataclasses import dataclass, field

from .errors import HarnessError

# ------------------------- load-bearing assertion words -------------------------

_ABSENCE_PHRASES = [
    r"no\s+(?:size\s+)?cap", r"no\s+upper\s+bound", r"no\s+limit",
    r"no\s+bound", r"no\s+ceiling", r"uncapped", r"unbounded",
    r"without\s+bound", r"grows\s+without\s+bound",
]
_UNIVERSAL_WORDS = ["never", "always", "only"]

_ABSENCE_RE = re.compile(r"\b(?:" + "|".join(_ABSENCE_PHRASES) + r")\b", re.I)
_UNIVERSAL_RE = re.compile(r"\b(?:" + "|".join(_UNIVERSAL_WORDS) + r")\b", re.I)

_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\(")
_SNAKE_HAS_LOWER = re.compile(r"[a-z]")
_BOUND_MARKER_RE = re.compile(r"\bMAX_[A-Z0-9_]+\b|\blen\(\)\s*>\s*MAX_")

VALID_KINDS = ("defect", "reassurance")
MAX_EXPAND_DEPTH = 4
# Rust + Python definitions in-window. Word-boundary is load-bearing: a raw
# \x08 here once disabled this entire suppression path.
_DEFN_RE = re.compile(
    r"\b(?:pub(?:\s*\([^)]*\))?\s+)?"
    r"(?:fn|const|static|struct|enum|trait|type|def|class)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)


def _defined_in(source):
    """Names whose DEFINITION appears in source (fn/const/static/...). A bare
    call like `cloned.get_message_key(...)` is a reference, NOT a definition,
    so the callee's definition is still auto-expanded -- the 04b lesson."""
    return {m.group(1) for m in _DEFN_RE.finditer(source)}



def is_load_bearing(text):
    """True if the claim text asserts an absence/universal that needs grounding."""
    return bool(_ABSENCE_RE.search(text) or _UNIVERSAL_RE.search(text))


def is_absence_styled(text):
    """True if the claim asserts absence of a bound/limit (R3 check applies)."""
    return bool(_ABSENCE_RE.search(text))


# ------------------------- data model -------------------------

@dataclass
class Claim:
    claim_id: str
    text: str
    kind: str = "defect"            # "defect" | "reassurance"
    source_refs: list = field(default_factory=list)  # 1-based lines into the quoted window


def parse_claims(data):
    """Normalize a manifest (a list, or {'context': ..., 'claims': [...]}) into
    (context, [Claim]). Raises ValueError on malformed entries."""
    if isinstance(data, dict):
        context = data.get("context")
        raw = data.get("claims")
    else:
        context = None
        raw = data
    if not isinstance(raw, list):
        raise ValueError(
            "claims manifest must be a JSON list or {'context': ..., 'claims': [...]}")
    claims = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"claim #{i + 1} must be a JSON object")
        cid = str(item.get("id") or item.get("claim_id") or f"claim_{i + 1}")
        text = item.get("text")
        if not text or not str(text).strip():
            raise ValueError(f"claim '{cid}' has no text")
        kind = str(item.get("kind", "defect")).strip().lower()
        if kind not in VALID_KINDS:
            raise ValueError(f"claim '{cid}' kind '{kind}' invalid (use defect|reassurance)")
        raw_refs = item.get("source_refs") or []
        refs = []
        for r in raw_refs:
            try:
                refs.append(int(r))
            except (TypeError, ValueError):
                raise ValueError(f"claim '{cid}' source_ref '{r}' is not an integer line number") from None
        claims.append(Claim(claim_id=cid, text=str(text).strip(),
                            kind=kind, source_refs=refs))
    return context, claims


def load_claims_manifest(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise HarnessError(f"claims manifest not readable: {path} ({e.strerror or e})") from e
    except ValueError as e:
        raise HarnessError(f"claims manifest is not valid JSON: {path} ({e})") from e
    try:
        return parse_claims(data)
    except (ValueError, KeyError, TypeError) as e:
        raise HarnessError(f"claims manifest is malformed: {path} ({e})") from e


def curate_claims_from_ledger(entries, *, window=500, max_claims=3):
    """Turn the ledger's own task evidence into a dogfood claims manifest.

    The self-hosting loop's seed: the harness's next self-audit is derived
    from what its runs actually recorded, instead of a hand-authored fixture.
    Deterministic -- same entries produce the same manifest (no timestamps,
    stable rule ranking), so a re-run audits a fixed target. Rules, ranked by
    how much a confirmed defect would cost:
      1. a model that repeatedly fail-closed its runs at the verification gate;
      2. a model repeatedly paid for HTTP 200s with no usable content;
      3. free-tier rate limiting dominating recent dispatches (recoverable).
    Claim texts are factual, numbered propositions grounded in the evidence
    the caller shows the panel (the ledger tail), deliberately phrased to
    avoid absence/universal wording so the hermetic lint judges them on
    substance. Returns ``(manifest, evidence_summary)``; a manifest with zero
    claims means the evidence is not curation-worthy and the caller decides
    whether that is fatal.
    """
    recent = list(entries)[-int(window):] if window else list(entries)
    fail_closed = {}   # model -> runs that exhausted verify rounds
    unusable = {}      # model -> paid calls with no usable content
    rate_attempts = 0
    dispatches = 0
    for e in recent:
        ev = e.get("event")
        if ev == "model_result" and e.get("status") == "error":
            model = str(e.get("model") or "(unknown)")
            if "no usable content" in str(e.get("reason") or ""):
                unusable[model] = unusable.get(model, 0) + 1
            elif e.get("http_status") == 429 or "HTTP 429" in str(e.get("error") or ""):
                rate_attempts += 1
        elif (ev == "abort" and e.get("reason") == "verify rounds exhausted"
              and e.get("model")):
            fail_closed[str(e["model"])] = fail_closed.get(str(e["model"]), 0) + 1
        elif ev == "dispatch_start":
            dispatches += 1

    candidates = []  # (rank, sort key, text)
    for model in sorted(fail_closed):
        n = fail_closed[model]
        if n >= 2:
            candidates.append((0, model,
                f"Apply runs led by model '{model}' ended with verification "
                f"rounds exhausted {n} times in recent ledger evidence; a "
                f"model that cannot pass the gate wastes every round it leads."))
    for model in sorted(unusable):
        n = unusable[model]
        if n >= 3:
            candidates.append((1, model,
                f"Model '{model}' was paid for {n} recent calls that returned "
                f"HTTP 200 with no usable content; the harness receives "
                f"nothing for the spend."))
    if rate_attempts >= 3:
        candidates.append((2, "tier",
            f"Recent runs hit free-tier rate limiting (HTTP 429) {rate_attempts} "
            f"times across {max(dispatches, 1)} dispatches."))
    candidates.sort(key=lambda c: (c[0], c[1]))

    claims = [{"id": f"c{i + 1}", "text": text, "source_refs": []}
              for i, (_, _, text) in enumerate(candidates[:max_claims])]
    manifest = {
        "context": ("Curated by 'harness dogfood --from-ledger' from the local "
                    "autonomy ledger: the harness auditing its own recorded "
                    "run evidence."),
        "claims": claims,
    }
    evidence = {"entries_scanned": len(recent), "dispatches": dispatches,
                "fail_closed_by_model": fail_closed,
                "unusable_by_model": unusable,
                "rate_limited_attempts": rate_attempts,
                "curated_claims": len(claims)}
    return manifest, evidence


def normalize_definitions(data):
    """Accept either {identifier: snippet} or {identifier: {snippet|definition|source: ..}}
    or a list of such objects. Empty snippets are dropped."""
    out = {}
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("identifier") or item.get("id")
                if name:
                    items.append((str(name), item))
    else:
        return out
    for name, val in items:
        if isinstance(val, dict):
            val = val.get("snippet") or val.get("definition") or val.get("source")
        if val and str(val).strip():
            out[str(name)] = str(val)
    return out


def load_definitions_file(path):
    """Same contract as the claims manifest loader: a missing or malformed
    definitions file is a clean HarnessError, never a raw traceback -- the
    interface layer turns it into a pre-network [FATAL]."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise HarnessError(f"definitions file not readable: {path} ({e.strerror or e})") from e
    except ValueError as e:
        raise HarnessError(f"definitions file is not valid JSON: {path} ({e})") from e
    return normalize_definitions(data)

# ------------------------- identifier scanning -------------------------

def _code_identifiers(text):
    """Code-shaped identifiers in prose: backticked names, UPPER_SNAKE
    constants, and tight function/method calls `name(`."""
    found = set()
    for m in _BACKTICK_RE.finditer(text):
        found.add(m.group(1))
    for m in _CONST_RE.finditer(text):
        found.add(m.group(0))
    for m in _CALL_RE.finditer(text):
        found.add(m.group(1))
    return found


# ------------------------- lint + auto-expansion -------------------------

def lint_claims(claims, quoted_source, source_index=None, context=None):
    """Run the self-grounding lint over claims against the quoted source window.

    Returns {"ok", "issues", "expansions", "expanded_source"}. Issues carry
    {claim_id, code, severity: error|warning, message}. ok == no error issues.
    This is pure and hermetic -- no network, no files.
    """
    issues = []
    source_index = dict(source_index or {})
    window = len(quoted_source.splitlines()) if quoted_source.strip() else 0

    if window == 0:
        issues.append({"claim_id": None, "code": "empty-source", "severity": "error",
                       "message": "quoted source window is empty; no claim can be grounded."})

    # ---- collect identifiers referenced by the claims + context prose ----
    ref_texts = [c.text for c in claims]
    if context:
        ref_texts.append(str(context))
    referenced = set()
    for t in ref_texts:
        referenced |= _code_identifiers(t)
    backtick_snake = set()
    for t in ref_texts:
        for m in _BACKTICK_RE.finditer(t):
            if _SNAKE_HAS_LOWER.search(m.group(1)):
                backtick_snake.add(m.group(1))

    # ---- auto-expansion: verbatim definitions, deduped, transitive over constants ----
    # An identifier is "already visible" only if its DEFINITION is in the window
    # (fn/const/...), not merely referenced by a call. The 04b lesson: the cap
    # in `get_message_key` was call-referenced in the window but never defined,
    # so the panel never saw it -- we must append it regardless of the call.
    defined_in_window = _defined_in(quoted_source)
    discovered = set(referenced & set(source_index))
    depth = 0
    while depth < MAX_EXPAND_DEPTH:
        depth += 1
        grew = False
        for ident in list(discovered):
            for m in _CONST_RE.finditer(source_index[ident]):
                child = m.group(0)
                if child in source_index and child not in discovered:
                    discovered.add(child)
                    grew = True
        if not grew:
            break
    expansions = []
    for ident in sorted(discovered):
        if ident in defined_in_window:
            continue  # definition already visible in the window; nothing to append
        expansions.append({"identifier": ident, "snippet": source_index[ident]})

    # ---- warnings: backticked snake_case identifiers we could not resolve ----
    for ident in sorted(backtick_snake - set(source_index)):
        if ident in quoted_source:
            continue
        issues.append({"claim_id": None, "code": "unresolved-identifier",
                       "severity": "warning",
                       "message": (f"identifier '{ident}' is referenced but has no "
                                   f"definition in the definitions index and is not in the "
                                   f"quoted window; auto-expansion cannot resolve it.")})

    # ---- per-claim rules ----
    for c in claims:
        if is_load_bearing(c.text) and not c.source_refs:
            issues.append({
                "claim_id": c.claim_id, "code": "ungrounded-assertion",
                "severity": "error",
                "message": ("claim uses a load-bearing word (no cap / unbounded / "
                            "never / always / only) but cites no source_refs: an "
                            "absence/universal assertion must point at the line(s) "
                            "that justify it.")})
        for r in c.source_refs:
            if not isinstance(r, int) or r < 1 or r > window:
                issues.append({
                    "claim_id": c.claim_id, "code": "out-of-window-ref",
                    "severity": "error",
                    "message": f"source_ref {r} is outside the quoted window (1..{window})."})
        if is_absence_styled(c.text):
            for exp in expansions:
                hit = _BOUND_MARKER_RE.search(exp["snippet"])
                if hit:
                    snippet = exp["snippet"]
                    if len(snippet) > 300:
                        snippet = snippet[:300] + " ..."
                    issues.append({
                        "claim_id": c.claim_id, "code": "contradicted-by-source",
                        "severity": "error",
                        "message": (f"claim asserts absence of a bound but the auto-resolved "
                                    f"definition of '{exp['identifier']}' shows one "
                                    f"({hit.group(0)!r}): {snippet}")})
                    break

    extra = []
    for exp in expansions:
        extra.append(f"----- auto-resolved definition of {exp['identifier']} "
                     "(referenced by the claims/context but outside the "
                     "quoted window) -----")
        extra.append(exp["snippet"])
    expanded_source = quoted_source + ("\n" + "\n".join(extra) if extra else "")

    return {
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "expansions": expansions,
        "expanded_source": expanded_source,
    }


# ------------------------- prompt building -------------------------

_DEFAULT_ROLE = (
    "You are an expert security reviewer. Decide on each SPECIFIC claim about the "
    "source below. Claims are DEFECT propositions: real:true means the stated "
    "defect genuinely exists. Answer ONLY as one JSON object, no prose: "
    '{"<claim_id>": {"real": true|false, "severity": "critical|high|medium|low|info", '
    '"confidence": <0.0-1.0>, "why": "<one line>"}}. real:false = not a genuine '
    "defect in context. Be skeptical."
)


def build_claims_prompt(claims, quoted_source, source_index=None, context=None,
                        role=None):
    """Lint claims and render the final panel prompt (numbered source window +
    expansions + claims with their refs). Returns (prompt, lint_report)."""
    report = lint_claims(claims, quoted_source, source_index=source_index,
                         context=context)
    lines = [role or _DEFAULT_ROLE, "",
             "Source under review (line numbers are what source_refs cite):",
             "```text"]
    for i, line in enumerate(report["expanded_source"].splitlines(), 1):
        lines.append(f"{i:4d} | {line}")
    lines.append("```")
    if context:
        lines += ["", "Context: " + str(context)]
    lines += ["", "Claims:"]
    claims_out = {}
    for c in claims:
        label = c.text
        if c.source_refs:
            label += f" [refs: lines {', '.join(str(r) for r in c.source_refs)}]"
        if c.kind == "reassurance":
            label += " [REASSURANCE claim: real:true means the stated correctness holds]"
        claims_out[c.claim_id] = label
    lines.append(json.dumps(claims_out, indent=2))
    lines.append("Respond with exactly one JSON object keyed by claim id.")
    return "\n".join(lines), report
