"""MCP lane scheduling policy: which serial worker runs each tool.

Extracted from harness/mcp.py so the protocol adapter reads as framing +
dispatch + cancellation lifecycle; this module owns the lane-selection
policy and its constants. mcp.py imports it at module level (the interface
rule) and builds one serial pool per lane.

Tool lanes: one serial worker each. Mutation (file writes) stays strictly
serial for single-session engine semantics; spendy lanes (network calls
that can run minutes on a saturated tier) no longer head-of-line-block
the observation lane, so status/report queries always answer promptly.
Governor spend accounting and ledger appends are lock-guarded, and the
engine is only ever driven from the mutation lane, so lanes are safe to
run concurrently with each other.
"""

# The lane registry, in pool-creation order.
LANES = ("mutation", "spendy", "observe")

MUTATION_LANE = {"apply_edit", "plan_and_execute"}
SPENDY_LANE = {"panel_verify", "offer_work"}


def lane_for(tool_name):
    """Which serial lane runs a tool. Unknown/missing names ride the
    observe lane and fail validation in the worker, as before."""
    if tool_name in MUTATION_LANE:
        return "mutation"
    if tool_name in SPENDY_LANE:
        return "spendy"
    return "observe"
