"""Cheap-first routing ladder with gated escalation.

Inherits SCMessenger's "free lanes first, always" discipline: the cheapest
capable model/tier is tried first, and escalation to a more expensive model is
a gated, opt-in step — never the default.
"""


class Router:
    def __init__(self, panel, judge, apply_model, escalation_model=None,
                 allow_escalation=False):
        self.panel = list(panel)
        self.judge = judge
        self.apply_model = apply_model
        self.escalation_model = escalation_model
        self.allow_escalation = allow_escalation

    def route(self, task_type):
        """Return the spec for a task type: cheap lane first."""
        if task_type == "verify":
            return {"tier": "panel", "panel": self.panel, "judge": self.judge}
        if task_type == "code":
            return {"tier": "apply", "model": self.apply_model}
        raise ValueError(f"unknown task_type: {task_type}")

    def escalation(self, override=None):
        """Return the escalation spec, or None if not allowed/configured."""
        allowed = self.allow_escalation if override is None else override
        if allowed and self.escalation_model:
            return {"tier": "escalation", "model": self.escalation_model}
        return None