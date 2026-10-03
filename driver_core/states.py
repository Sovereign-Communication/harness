"""The concrete declared schemas the driver actually extracts.

:mod:`driver_core.schema` owns the mechanism; this module owns the two
declarations the product ships with. They live apart on purpose: the
mechanism is the reusable piece, these are the opinions, and mixing them
means every consumer of the mechanism inherits this product's field names.

The provenance rules are the interesting part and are chosen per field, not
globally. ``CASED`` on a window title stops ``"Untitled - Notepad"`` and
``"untitled - notepad"`` from reading as a disagreement. ``NUMERIC`` with a
tolerance on a line number stops a one-digit OCR wobble from escalating a
run. Getting these wrong does not break the system -- it makes it
needlessly trigger-happy, and an operator who learns to ignore spurious
disagreements has lost the signal entirely.
"""
from .perception import CLI, DOM, GUI, MCP
from .schema import (
    BOOLEAN, CASED, EXACT, OPTIONAL, REQUIRED, Field, Schema,
)

#: What the driver extracts from a screen.
#:
#: Deliberately small. Every field is something a decision about "what should
#: I do next" could plausibly depend on. A schema that tried to capture
#: everything on screen would have a dozen optional fields that are almost
#: always absent, which drags the honest-answer rate down and makes the
#: shortfall signal fire for uninteresting reasons.
SCREEN_SCHEMA = Schema(
    "driver-core-screen", "1.0.0",
    [
        Field("window_title", "string", presence=REQUIRED, provenance=CASED,
              description="Title bar text of the foreground window.",
              provenance_note="Casefolded: two extractors differing only in "
                              "capitalisation observed the same title."),
        Field("foreground_app", "string", presence=REQUIRED, provenance=CASED,
              description="Application name owning the foreground window."),
        Field("visible_text", "string", presence=OPTIONAL, provenance=CASED,
              description="Text legible on screen, when the target is text.",
              provenance_note="Casefolded and stripped; long free text is the "
                              "field most likely to produce a genuine "
                              "disagreement, and that is reported rather "
                              "than resolved."),
        Field("error_dialog_present", "boolean", presence=REQUIRED,
              provenance=BOOLEAN,
              description="Whether a modal error or warning dialog is up.",
              provenance_note="Boolean vocabulary is wide on purpose "
                              "(yes/1/checked/selected) because models "
                              "disagree on the token far more often than on "
                              "the meaning."),
        Field("dialog_kind", "enum", presence=OPTIONAL,
              values=("error", "warning", "confirmation", "information", "file_picker"),
              provenance=EXACT,
              description="Kind of modal, when one is present.",
              provenance_note="Exact: the vocabulary is closed, so a "
                              "casefold would let an undeclared label slip "
                              "through as a near-match."),
        Field("focused_field_label", "string", presence=OPTIONAL,
              provenance=CASED,
              description="Label of the element that currently has focus."),
        Field("blocking_controls", "string", presence=OPTIONAL,
              provenance=CASED,
              description="Labels of controls that would commit something "
                          "irreversible, as observed on screen."),
    ],
)

#: What the driver extracts from a command's output.
#:
#: No pixels, no model -- a command either produced output or it did not.
#: This schema exists so the same decision tier can be fed from a structured
#: target, and so the driver is target-agnostic above this line.
CLI_SCHEMA = Schema(
    "driver-core-cli", "1.0.0",
    [
        Field("exit_code", "integer", presence=REQUIRED, provenance=EXACT,
              description="Process exit status."),
        Field("stdout", "string", presence=OPTIONAL, provenance=EXACT,
              description="Standard output.",
              provenance_note="Exact: command output is already "
                              "deterministic, so normalising it would hide "
                              "a real difference between runs."),
        Field("stderr", "string", presence=OPTIONAL, provenance=EXACT,
              description="Standard error."),
    ],
)

#: What a fetched document can honestly supply. Separate from
#: :data:`SCREEN_SCHEMA` because a document genuinely cannot report the
#: foreground application's name or whether a modal dialog is up -- and
#: reusing the screen schema for it would mean asking a reader to invent two
#: of its three required fields, which is the failure this project is built
#: against. A DOM target that cannot read a title reports a shortfall, which
#: is the truth, rather than a confident guess.
DOM_SCHEMA = Schema(
    "driver-core-dom", "1.0.0",
    [
        Field("window_title", "string", presence=REQUIRED, provenance=CASED,
              description="The document's <title>."),
        Field("visible_text", "string", presence=OPTIONAL, provenance=CASED,
              description="Readable text of the document body.",
              provenance_note="Casefolded and stripped; long free text is the "
                              "field most likely to produce a genuine "
                              "disagreement, and that is reported rather "
                              "than resolved."),
    ],
)

#: The names the service accepts on the wire, each bound to a target class
#: **and** its schema in one place. This is the only map over these names: a
#: second one is what let a request arrive with a schema and a class that
#: disagreed, and an undeclared class permits any source to answer -- which
#: quietly reopens the vision tier.
#:
#: The table is the whole set: there is deliberately no default for an absent
#: name, because a default would have to be one of the four declared classes
#: and whichever it was would silently observe a different machine than the
#: caller asked for. So the service asks instead.
WIRE_TARGETS = {
    "cli": (CLI, CLI_SCHEMA),
    "mcp": (MCP, CLI_SCHEMA),
    "dom": (DOM, DOM_SCHEMA),
    "gui": (GUI, SCREEN_SCHEMA),
    "screen": (GUI, SCREEN_SCHEMA),
}


def resolve_wire_target(name):
    """Bind one wire ``schema`` name to its target class and schema.

    The single place that rule lives, so the CLI and the service cannot
    answer the same question two ways -- which they did, one defaulting and
    one refusing. The refusals are :class:`~driver_core.errors.PerceptionUnavailable`,
    chosen because this is the *caller's* declaration that is wrong, not a
    perception failure and not a malformed schema; a service turns that into
    a 400 and a CLI prints it, and neither has to re-derive the rule or
    re-word it differently.
    """
    from .errors import PerceptionUnavailable

    if name is None or not str(name).strip():
        raise PerceptionUnavailable(
            f"schema is required; declare one of {sorted(WIRE_TARGETS)}")
    key = str(name).strip().lower()
    if key not in WIRE_TARGETS:
        raise PerceptionUnavailable(
            f"unknown schema {name!r}; declared schemas are "
            f"{sorted(WIRE_TARGETS)}")
    return WIRE_TARGETS[key]
