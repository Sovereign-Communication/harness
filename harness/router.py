"""Cheap-first routing ladder with model pools, rotation, and gated escalation.

Inherits SCMessenger's "free lanes first, always" discipline: the cheapest
capable model/tier is tried first, and escalation to a more expensive model is
a gated, opt-in step — never the default. Panels and apply each carry an
ordered *pool* so a failing model can be rotated to the next one instead of
failing the whole run.
"""


def dedup(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


class Router:
    def __init__(self, panel, judge, apply_model, escalation_model=None,
                 allow_escalation=False, panel_pool=None, apply_pool=None,
                 specialist_pool=None, convergence_model=None):
        self.panel = list(panel)
        self.judge = judge
        self.apply_model = apply_model
        self.escalation_model = escalation_model
        self.allow_escalation = allow_escalation
        self.panel_pool = panel_pool or list(panel)
        self.apply_pool = apply_pool or dedup([apply_model] + list(self.panel))
        # Convergence-specialist lane: primary defaults to the judge, fallback
        # ladder strongest-first (free lane leads with GLM-5.2).
        self.convergence_model = convergence_model or judge
        self.specialist_pool = list(specialist_pool or [])

    def route(self, task_type):
        """Return the spec for a task type: cheap lane first."""
        if task_type == "verify":
            return {"tier": "panel", "panel": self.panel_pool, "judge": self.judge}
        if task_type == "code":
            return {"tier": "apply", "model": self.apply_model,
                    "pool": self.apply_pool}
        raise ValueError(f"unknown task_type: {task_type}")

    def next_model(self, kind, exclude):
        """Next model in a pool not in the excluded set, or None if exhausted."""
        pool = self.panel_pool if kind == "panel" else self.apply_pool
        for m_ in pool:
            if m_ not in exclude:
                return m_
        return None

    def escalation(self, override=None):
        """Return the escalation spec, or None if not allowed/configured."""
        allowed = self.allow_escalation if override is None else override
        if allowed and self.escalation_model:
            return {"tier": "escalation", "model": self.escalation_model}
        return None
