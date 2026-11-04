"""The extraction questions, and the parse that reads their answers."""
from .. import transport
from ..errors import SchemaError
from ..schema import OPTIONAL, validate_state


#: Schema types a decision model can answer in a closed vocabulary. A
#: ``choice`` returns one of the options *code* declared, so it can express a
#: yes/no and a closed set of labels, and nothing else. Everything else in a
#: schema is a value the platform already holds exactly, and is read by code.
JUDGMENT_TYPES = ("boolean", "enum")

#: The label an enum question answers with when the field is simply not on
#: screen. Absent is a real observation and has to be nameable, or the model
#: is forced to invent a label to say "nothing".
ABSENT = "<absent>"


def _extraction_questions(schema, basis="description"):
    """One typed question per field the model can actually answer.

    The previous pack asked a single ``observed``/``unreadable`` question while
    the parser looked for ``state``/``fields`` that **no question asked for**,
    so the parse could not succeed even against a perfectly readable capture.
    Questions are now generated from the same schema the state is validated
    against: every question has an answer the parser knows how to read, and a
    field added to a schema is asked for automatically.

    Free-text fields are deliberately absent. A ``choice`` cannot return a
    window title, and a model asked to invent one would be fabricating a
    fact the platform already knows exactly -- so those are read by code and
    only the bounded judgments are asked of the model.

    ``basis`` names the evidence being judged -- the recorded words or the
    image -- and changes the wording and nothing else: the answer contract
    and the state parsing are identical in both modes.
    """
    evidence = "image" if basis == "image" else "description"
    asked = [f for f in schema.fields if f.type in JUDGMENT_TYPES]
    listed = "\n".join(
        f"- {f.name} ({f.type}"
        + (f"/{f.presence}" if f.presence != "required" else "")
        + f"): {f.description}" for f in asked)
    questions = {
        "observation": transport.choice(
            _INTROS.get(basis, _INTROS["description"])
            + "\nASKED FIELDS:\n" + (listed or "- (none)"),
            {"observed": f"The {evidence} is a real observation of a "
                         f"screen.",
             "unreadable": f"The {evidence} is empty, absent, or not a "
                           f"{evidence} of an observed screen."}),
    }
    for field in asked:
        if field.type == "boolean":
            questions[field.name] = transport.noul(
                f"According to the {evidence}, and only according to it: "
                f"{field.description}",
                true=f"The {evidence} supports this being true.",
                false=f"The {evidence} does not support this being "
                      f"true.")
        else:
            options = {ABSENT: f"The {evidence} does not say. Leave it out."}
            options.update({str(v): str(v) for v in field.values})
            questions[field.name] = transport.choice(
                f"According to the {evidence}, and only according to it: "
                f"{field.description}",
                options)
    return questions


#: What the model is being handed, per basis. Wording only -- the answer
#: types and the parse below do not vary with the evidence.
_INTROS = {
    "description":
        "You are given a plain-text description of the screen observed "
        "on this machine, read from the window manager. Judge that "
        "description only. Do not guess at anything it does not state.",
    "image":
        "You are given an image of the screen observed on this machine, "
        "sent as the asset referenced by 'screen'. Judge that image only. "
        "Do not guess at anything it does not show.",
}


def _observed_state(payload, schema, described):
    """Read a state out of a model response plus code-read facts.

    ``described`` carries the values the platform reported exactly. They are
    merged in *after* the model's bounded judgments rather than being asked
    for, because the model cannot be the source of a fact it would have to
    invent. A response that does not declare the capture observed is believed
    and yields nothing; a state that fails validation is a malformed slot, not
    one to be repaired.
    """
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers") or {}
    choice = answers.get("observation") or {}
    if choice.get("choice") != "observed":
        return None

    state = {name: value for name, value in (described or {}).items()
             if name in schema.field_names()}
    for field in schema.fields:
        if field.type not in JUDGMENT_TYPES:
            continue
        answer = answers.get(field.name)
        if not isinstance(answer, dict):
            continue
        if field.type == "boolean":
            try:
                value = float(answer.get("noul"))
            except (TypeError, ValueError):
                continue
            if not 0.0 <= value <= 1.0:
                continue
            # A noul is a probability, and an optional field whose
            # probability sits on the fence is an absent field rather than a
            # coin flip nobody observed.
            if field.presence == OPTIONAL and 0.4 <= value <= 0.6:
                continue
            state[field.name] = value >= 0.5
        else:
            picked = answer.get("choice")
            if picked in (None, ABSENT) or picked not in field.values:
                continue
            state[field.name] = picked

    try:
        return validate_state(state, schema)
    except SchemaError:
        return None
