"""Shared request assembly for the interfaces (the canonical service layer).

The web server's runners and the CLI's verify path used to assemble the same
verify run in parallel -- duplicated claims-window reading, duplicated
cancellation envelopes, and two places for a default to drift. This module
is the ONE owner of that assembly; ``harness server``, ``harness verify``,
and MCP all consume it, and neither re-derives lane policy (the CONTRIBUTING
one-owner rule at the service seam).

Scope (deliberately small): the verify/claims run assembly, the prompt
reader, the cancelled-run spend envelope, and the meta/cost attach. The
apply lane stays composed via :func:`harness.session.apply_session` at each
call site -- apply request validation is the engine's own boundary contract
(``ApplyEngine._prepare``), not a service-layer concern.
"""
import uuid

from .errors import HarnessError, ToolCancelled
from .panel import panel_judge
from ._http import HttpTransport
from .saturation import pre_run_warning
from .claims import build_claims_prompt, load_claims_manifest, load_definitions_file
from .session import governor_for, ledger_for, run_meta

# BOM-tolerant reader: Windows tooling (PowerShell ``>`` redirects) emits
# BOM'd text; ONE reader for every interface (the utf-8-sig rule).
PROMPT_ENCODING = "utf-8-sig"


def read_text_file(path, what="input file"):
    """Read a prompt/claims text file BOM-tolerantly."""
    try:
        with open(path, encoding=PROMPT_ENCODING) as f:
            return f.read()
    except OSError as e:
        raise HarnessError(
            f"{what} not readable: {path} ({e.strerror or e})") from e


def cancelled_envelope(gov, settings, *, include_meta=True):
    """The honest spend envelope for a cooperatively cancelled run.

    In-flight calls that billed before the cancel landed are real spend and
    must reach the consumer (CLI, MCP, or UI) with the same shape every
    terminal status uses. MCP may retain its established protocol-level
    cancellation error while this envelope is still built here.
    """
    envelope = {"status": "cancelled", "verdict": None,
                "judge_synthesis_status": "cancelled",
                "panel_results": [], "panel_failures": [],
                "actual_cost": gov.spent, "max_cost_ceiling": gov.max_cost,
                "cost_by_model": gov.cost_by_model()}
    if include_meta and settings is not None:
        envelope["meta"] = run_meta(settings, gov)
    return envelope


def _assemble_verify_prompt(*, prompt=None, prompt_file=None, claims_file=None,
                            source_file=None, definitions_file=None,
                            claim_context=None):
    """Build the prompt once and retain the parsed claims for lane policy."""
    if claims_file:
        if not source_file:
            raise ValueError(
                "structured claims verify requires the source window "
                "(source_file) the panel will review")
        quoted = read_text_file(source_file, "--source-file")
        manifest_ctx, claims = load_claims_manifest(claims_file)
        defs = (load_definitions_file(definitions_file)
                if definitions_file else {})
        context = claim_context if claim_context else manifest_ctx
        prompt, lint = build_claims_prompt(claims, quoted, source_index=defs,
                                           context=context)
        return prompt, lint, claims
    if prompt_file:
        return read_text_file(prompt_file, "--prompt-file"), None, None
    if prompt:
        return prompt, None, None
    raise ValueError("verify requires 'prompt', 'prompt_file', or 'claims_file'")


def build_verify_prompt(*, prompt=None, prompt_file=None, claims_file=None,
                        source_file=None, definitions_file=None,
                        claim_context=None):
    """Assemble the verify prompt from its input sources, ONE way.

    Returns ``(prompt, claims_lint_or_None)``. ``claims_lint`` is non-None
    only in structured-claims mode; an ungrounded claim set yields
    ``lint["ok"] is False`` and NO network spend (the caller finishes the
    run as ``rejected``). Raises ValueError when no input source is given.
    """
    built_prompt, lint, _claims = _assemble_verify_prompt(
        prompt=prompt, prompt_file=prompt_file, claims_file=claims_file,
        source_file=source_file, definitions_file=definitions_file,
        claim_context=claim_context)
    return built_prompt, lint


def prepare_verify(*, prompt=None, prompt_file=None, claims_file=None,
                   source_file=None, definitions_file=None, claim_context=None):
    """Prepare interface input and claims-derived verify-lane flags.

    The CLI only presents these values; file reading, manifest parsing,
    grounding, convergence activation, and reassurance polarity all belong to
    this service seam.
    """
    built_prompt, lint, claims = _assemble_verify_prompt(
        prompt=prompt, prompt_file=prompt_file, claims_file=claims_file,
        source_file=source_file, definitions_file=definitions_file,
        claim_context=claim_context)
    return {
        "prompt": built_prompt,
        "claims_lint": lint,
        "converge": bool(claims_file),
        "reassurance_claims": ",".join(
            c.claim_id for c in (claims or []) if c.kind == "reassurance"),
    }


class ResolvedVerifyInputs:
    """Immutable resolved inputs for one ``run_verify`` execution.

    Construction applies every caller override EXACTLY ONCE, in the
    preserved fallback order caller arg -> session router -> settings --
    the same move that fixed the specialist reserve/call mismatch
    (:class:`harness.config.PanelLanePolicy`): there is ONE place to look
    up where a lane input came from, instead of eight parallel
    ``arg or router.X or settings.X`` chains threaded through the body.
    ``run_verify`` builds this once at the top; everything downstream
    (``pre_run_warning``, the ``panel_judge`` call) reads the resolved
    fields. ``convergence_model`` resolves after ``judge`` because its
    final fallback is the judge model itself.
    """

    __slots__ = ("_use_free", "_panel", "_judge", "_convergence_model",
                 "_specialist_pool", "_reasoning_effort",
                 "_reasoning_token_budget", "_max_panelists")

    def __init__(self, *, settings, router, panel, judge, convergence_model,
                 specialist_pool, reasoning_effort, reasoning_token_budget,
                 max_panelists, free_tier):
        def router_or_settings(attr, settings_default):
            # Router value wins unless it is absent (None); then settings,
            # then the field's own default. A falsy-but-present router value
            # (e.g. "") still wins over settings -- the historical semantics.
            value = getattr(router, attr, None) if router is not None else None
            if value is None:
                value = (getattr(settings, attr, settings_default)
                         if settings is not None else settings_default)
            return value

        self._use_free = (settings.use_free if settings is not None
                          else bool(free_tier))
        self._panel = list(panel if panel is not None else
                           router_or_settings("panel_pool", []))
        default_judge = router_or_settings("judge", None)
        self._judge = judge or default_judge
        self._reasoning_effort = (reasoning_effort if reasoning_effort is not None
                                  else getattr(settings, "reasoning_effort", "auto"))
        self._reasoning_token_budget = (
            reasoning_token_budget if reasoning_token_budget is not None else
            getattr(settings, "reasoning_token_budget", 0.4))
        self._max_panelists = (max_panelists if max_panelists is not None else
                               getattr(settings, "max_panelists", 3))
        default_convergence = router_or_settings("convergence_model", None)
        self._convergence_model = (convergence_model or default_convergence
                                   or self._judge)
        default_specialists = router_or_settings("specialist_pool", [])
        self._specialist_pool = (specialist_pool if specialist_pool is not None
                                 else list(default_specialists or []))

    @property
    def use_free(self):
        return self._use_free

    @property
    def panel(self):
        return self._panel

    @property
    def judge(self):
        return self._judge

    @property
    def convergence_model(self):
        return self._convergence_model

    @property
    def specialist_pool(self):
        return self._specialist_pool

    @property
    def reasoning_effort(self):
        return self._reasoning_effort

    @property
    def reasoning_token_budget(self):
        return self._reasoning_token_budget

    @property
    def max_panelists(self):
        return self._max_panelists


def run_verify(settings=None, *, prompt, task_id=None, cancel_check=None,
               max_cost=None, judge=None, reasoning_effort=None, panel=None,
               converge=False, convergence_model=None, specialist_pool=None,
               reassurance_claims="", max_tokens=None, api_key=None,
               governor=None, ledger=None, transport=None,
               reasoning_token_budget=None, max_panelists=None,
               free_tier=None, router=None, generate_task_id=True,
               attach_meta=True):
    """The ONE verify-lane execution: governor, ledger, pre-run look-ahead,
    panel_judge, cost attribution, meta. The web server's verify runner,
    the CLI's verify command, and MCP's panel tool all call this; a behavior
    change (budget policy, rotation, envelope field) lands exactly here.

    ``governor``/``ledger``/``transport``/``api_key`` are optional injected
    session dependencies for MCP. When omitted, the normal CLI/server
    composition is used. ``router`` supplies MCP's already-composed defaults
    without making the protocol layer assemble panel/judge policy. The
    ``attach_meta`` flag is a presentation contract: MCP keeps its historical
    structured result shape while the service still owns the common
    cancellation/cost bookkeeping.

    `converge` runs the structured-claims convergence step (deterministic
    tally); `reassurance_claims` is a comma-separated claim-id list mapped to
    reassurance polarity. Both are lane concerns, not interface concerns, so
    they live here rather than in each caller's kwargs threading.
    """
    if governor is None:
        if settings is None:
            raise ValueError("verify requires settings or an injected governor")
        api_key, gov = governor_for(settings, max_cost)
    else:
        gov = governor
        if api_key is None:
            api_key = getattr(gov, "api_key", None)
    if ledger is None:
        if settings is None:
            raise ValueError("verify requires settings or an injected ledger")
        ledger = ledger_for(settings)

    resolved = ResolvedVerifyInputs(
        settings=settings, router=router, panel=panel, judge=judge,
        convergence_model=convergence_model, specialist_pool=specialist_pool,
        reasoning_effort=reasoning_effort,
        reasoning_token_budget=reasoning_token_budget,
        max_panelists=max_panelists, free_tier=free_tier)
    pre_run_warning(governor=gov, ledger=ledger, use_free=resolved.use_free)
    if task_id is None and generate_task_id:
        task_id = uuid.uuid4().hex[:8]
    try:
        result = panel_judge(
            transport=transport or HttpTransport(), api_key=api_key, governor=gov,
            prompt=prompt, panel=resolved.panel, judge=resolved.judge,
            max_tokens=max_tokens, reasoning_effort=resolved.reasoning_effort,
            reasoning_token_budget=resolved.reasoning_token_budget,
            task_id=task_id, ledger=ledger,
            max_panelists=resolved.max_panelists,
            run_convergence=converge,
            convergence_model=resolved.convergence_model,
            specialist_pool=resolved.specialist_pool,
            claim_polarity={cid.strip(): "reassurance" for cid in
                            (reassurance_claims or "").split(",")
                            if cid.strip()},
            free_tier=resolved.use_free, cancel_check=cancel_check)
    except ToolCancelled:
        return cancelled_envelope(gov, settings, include_meta=attach_meta)
    if attach_meta:
        result["cost_by_model"] = gov.cost_by_model()
        if settings is not None:
            result["meta"] = run_meta(settings, gov)
    return result
