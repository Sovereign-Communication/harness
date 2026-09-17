"""Cheap-first routing ladder with model pools, rotation, and gated escalation.

Inherits SCMessenger's "free lanes first, always" discipline: the cheapest
capable model/tier is tried first, and escalation to a more expensive model is
a gated, opt-in step — never the default. Panels and apply each carry an
ordered *pool* so a failing model can be rotated to the next one instead of
failing the whole run.

Auto-escalation (judge-driven): the escalation_pool is an ordered ladder
cheapest->most-capable. The judge condenses context and directs rung-by-rung
stepping. After the escalated model produces a plan, the system de-escalates
back to the last tier that needed escalation (judge-gated, data-driven).
"""


from .sliding_scale import (
    classify_task_tier,
    resolve_frontier_model,
    tier_cost_ceiling,
    tier_model_ladder,
)


def dedup(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


class Router:
    def __init__(self, panel, judge, apply_model, escalation_model=None,
                 allow_escalation=False, panel_pool=None, apply_pool=None,
                 specialist_pool=None, convergence_model=None,
                 escalation_pool=None, frontier_model=None, use_free=True):
        self.panel = list(panel)
        self.judge = judge
        self.apply_model = apply_model
        # Single escalation_model retained for backward-compat (single-rung mode).
        self.escalation_model = escalation_model
        self.allow_escalation = allow_escalation
        self.panel_pool = panel_pool or list(panel)
        self.apply_pool = apply_pool or dedup([apply_model] + list(self.panel))
        # Convergence-specialist lane: primary defaults to the judge, fallback
        # ladder strongest-first (free lane is live-validated).
        self.convergence_model = convergence_model or judge
        self.specialist_pool = list(specialist_pool or [])
        # Escalation ladder (ordered cheapest -> most capable). New multi-rung
        # mode replaces single escalation_model when present.
        self.escalation_pool = list(escalation_pool or [])
        self._escalation_rung = 0  # current rung index during auto-escalation
        self.use_free = use_free
        self.frontier_model = resolve_frontier_model(frontier_model, use_free=use_free)

    def route_tier(self, tier: int):
        # Return the spec for an upfront sliding-scale complexity tier.
        ladder = tier_model_ladder(tier, use_free=self.use_free, custom_frontier=self.frontier_model)
        primary = ladder[0] if ladder else self.apply_model
        ceiling = tier_cost_ceiling(tier, use_free=self.use_free)
        return {
            "tier": tier,
            "primary_model": primary,
            "pool": ladder,
            "cost_ceiling": ceiling,
        }

    def classify_and_route(self, instruction, target_files=None,
                           diff_size=None, dependency_depth=0, is_leaf=True,
                           previous_failures=0):
        # Classify task complexity and produce a routed execution spec.
        classification = classify_task_tier(
            instruction=instruction,
            target_files=target_files,
            diff_size=diff_size,
            dependency_depth=dependency_depth,
            is_leaf=is_leaf,
            previous_failures=previous_failures,
            use_free=self.use_free,
            custom_frontier=self.frontier_model,
        )
        spec = self.route_tier(classification.tier)
        spec["classification"] = classification
        return spec

    def route(self, task_type):
        """Return the spec for a task type: cheap lane first."""
        if task_type == "verify":
            return {"tier": "panel", "panel": self.panel_pool, "judge": self.judge}
        if task_type == "code":
            return {"tier": "apply", "model": self.apply_model,
                    "pool": self.apply_pool}
        raise ValueError(f"unknown task_type: {task_type}")

    def escalation(self, override=None):
        """Return the escalation spec for the current rung, or None if not allowed/configured.

        In multi-rung mode (escalation_pool populated), returns the next rung.
        In single-rung mode (legacy), returns escalation_model if allowed.
        """
        allowed = self.allow_escalation if override is None else override
        if not allowed:
            return None
        # Multi-rung ladder mode
        if self.escalation_pool:
            if self._escalation_rung < len(self.escalation_pool):
                model = self.escalation_pool[self._escalation_rung]
                return {"tier": "escalation", "model": model,
                        "rung": self._escalation_rung, "total_rungs": len(self.escalation_pool)}
            return None  # ladder exhausted
        # Legacy single-rung mode
        if self.escalation_model:
            return {"tier": "escalation", "model": self.escalation_model, "rung": 0, "total_rungs": 1}
        return None

    def advance_escalation_rung(self):
        """Advance to the next escalation rung. Returns True if a rung remains."""
        if self.escalation_pool and self._escalation_rung < len(self.escalation_pool) - 1:
            self._escalation_rung += 1
            return True
        return False

    def reset_escalation(self):
        """Reset escalation rung to 0 (for de-escalation / new task)."""
        self._escalation_rung = 0

    def de_escalate_to_rung(self, target_rung):
        """De-escalate to a specific rung (judge-gated descent)."""
        if 0 <= target_rung < len(self.escalation_pool):
            self._escalation_rung = target_rung
            return True
        return False

    def current_escalation_rung(self):
        """Return current rung index and model, or None if not in escalation."""
        if self.escalation_pool and self._escalation_rung < len(self.escalation_pool):
            return self._escalation_rung, self.escalation_pool[self._escalation_rung]
        return None

