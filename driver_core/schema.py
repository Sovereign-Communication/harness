"""The declared extraction schema: one owner, code-owned, versioned.

A *schema* here is not documentation. It is the closed set of fields the
extraction tier is allowed to produce, each with a type, a presence rule, and
a **provenance rule** that says how two independent extractors' values are
normalised before they are compared.

That last part is the whole ballgame. Consensus is only meaningful if
``"Save"``, ``"save "`` and ``"SAVE"`` are recognised as the same answer by
deterministic code rather than by a model -- otherwise the tally reports a
disagreement that is actually a whitespace accident, and the operator is
taught to ignore real disagreement signals.

Everything in this module is hermetic: no network, no model, no clock. The
schema is the contract that the expensive tiers are measured against, so it
must be inspectable without spending anything.
"""
from .errors import SchemaError

# Field types. Deliberately tiny: a schema that can express arbitrary
# structure is a schema nothing can validate.
TYPES = ("string", "integer", "number", "boolean", "enum")

# Presence rules.
REQUIRED = "required"
OPTIONAL = "optional"

# Provenance rules -- how two extractors' values for one field are compared.
EXACT = "exact"          # byte-identical after str() and strip()
CASED = "cased"          # casefolded
NUMERIC = "numeric"      # compared as float within a tolerance
BOOLEAN = "boolean"      # truthy vocabulary, then compared as bool

PROVENANCE = (EXACT, CASED, NUMERIC, BOOLEAN)


class Field:
    """One declared field of an extraction schema."""

    __slots__ = ("name", "type", "presence", "values", "provenance", "tolerance",
                 "description", "provenance_note")

    def __init__(self, name, type, *, presence=REQUIRED, values=None,
                 provenance=EXACT, tolerance=0.0, description="",
                 provenance_note=""):
        if not isinstance(name, str) or not name.strip():
            raise SchemaError("field name must be a non-empty string")
        if type not in TYPES:
            raise SchemaError(
                f"field {name!r}: unknown type {type!r}; expected one of {TYPES}")
        if presence not in (REQUIRED, OPTIONAL):
            raise SchemaError(
                f"field {name!r}: unknown presence {presence!r}")
        if provenance not in PROVENANCE:
            raise SchemaError(
                f"field {name!r}: unknown provenance {provenance!r}; "
                f"expected one of {PROVENANCE}")
        if type == "enum" and not values:
            raise SchemaError(
                f"field {name!r}: enum fields must declare their values")
        if type != "enum" and values:
            raise SchemaError(
                f"field {name!r}: values are only meaningful for enum fields")
        if provenance == NUMERIC and type not in ("integer", "number"):
            raise SchemaError(
                f"field {name!r}: numeric provenance requires a numeric type")
        if provenance == BOOLEAN and type != "boolean":
            raise SchemaError(
                f"field {name!r}: boolean provenance requires a boolean type")
        if provenance in (NUMERIC, EXACT, CASED) and type == "boolean":
            raise SchemaError(
                f"field {name!r}: use BOOLEAN provenance for boolean fields")
        if tolerance < 0:
            raise SchemaError(f"field {name!r}: tolerance must be >= 0")

        self.name = name
        self.type = type
        self.presence = presence
        self.values = tuple(values) if values else ()
        self.provenance = provenance
        self.tolerance = float(tolerance)
        self.description = description
        self.provenance_note = provenance_note

    @property
    def required(self):
        return self.presence == REQUIRED

    def to_dict(self):
        """The wire form. A schema travels between the service and callers."""
        return {
            "name": self.name,
            "type": self.type,
            "presence": self.presence,
            "values": list(self.values),
            "provenance": self.provenance,
            "tolerance": self.tolerance,
            "description": self.description,
            "provenance_note": self.provenance_note,
        }

    def __repr__(self):
        return (f"Field({self.name!r}, {self.type}, presence={self.presence!r}, "
                f"provenance={self.provenance!r})")


class Schema:
    """An immutable, versioned set of declared fields."""

    __slots__ = ("id", "version", "fields", "_by_name")

    def __init__(self, id, version, fields):
        if not isinstance(id, str) or not id.strip():
            raise SchemaError("schema id must be a non-empty string")
        if not isinstance(version, str) or not version.strip():
            raise SchemaError("schema version must be a non-empty string")
        if not fields:
            raise SchemaError(f"schema {id!r} declares no fields")
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise SchemaError(f"schema {id!r} declares duplicate fields: {dupes}")
        if not any(f.required for f in fields):
            raise SchemaError(
                f"schema {id!r} has no required field, so a bare state could "
                f"satisfy it vacuously")

        self.id = id
        self.version = version
        self.fields = tuple(fields)
        self._by_name = {f.name: f for f in fields}

    def field(self, name):
        try:
            return self._by_name[name]
        except KeyError:
            raise SchemaError(
                f"schema {self.id!r} declares no field {name!r}") from None

    def required_names(self):
        return tuple(f.name for f in self.fields if f.required)

    def field_names(self):
        return tuple(f.name for f in self.fields)

    def to_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "fields": [f.to_dict() for f in self.fields],
        }

    def identity(self):
        """A stable string that changes if the contract changes.

        Recorded in every extraction receipt so a later reader can tell
        whether a stored state was produced under the same rules.
        """
        return f"{self.id}@{self.version}"

    def __repr__(self):
        return f"Schema({self.id!r}, {self.version!r}, {len(self.fields)} fields)"


def normalize(field, value):
    """Return the comparable form of ``value`` under ``field``'s provenance.

    Returns a *hashable* canonical form so two values that are the same under
    the declared rule compare equal in a set. Raises for values that cannot
    satisfy the field's declared type -- a value that cannot be normalised is
    a malformed extraction, and the caller must treat it as such rather than
    silently coercing it into something comparable.
    """
    if field.type == "boolean":
        return _as_boolean(field, value)
    if field.type in ("integer", "number"):
        return _as_number(field, value)
    if field.type == "enum":
        if not isinstance(value, str):
            raise SchemaError(
                f"field {field.name!r}: enum values must be strings, got "
                f"{type(value).__name__}")
        if value not in field.values:
            raise SchemaError(
                f"field {field.name!r}: {value!r} is not a declared enum value")
        return value.casefold() if field.provenance == CASED else value
    # string
    if not isinstance(value, str):
        raise SchemaError(
            f"field {field.name!r}: expected a string, got "
            f"{type(value).__name__}")
    stripped = value.strip()
    return stripped.casefold() if field.provenance == CASED else stripped


_TRUE = {"true", "yes", "1", "on", "checked", "selected", "enabled"}
_FALSE = {"false", "no", "0", "off", "unchecked", "unselected", "disabled", ""}


def _as_boolean(field, value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise SchemaError(
        f"field {field.name!r}: {value!r} is not in the declared boolean "
        f"vocabulary")


def _as_number(field, value):
    if isinstance(value, bool):
        # bool is an int subclass in Python; an extractor that answered
        # "True" for a count field has not answered the question.
        raise SchemaError(
            f"field {field.name!r}: a boolean is not a {field.type}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SchemaError(
            f"field {field.name!r}: {value!r} is not a {field.type}") from None
    if field.type == "integer":
        if not float(number).is_integer():
            raise SchemaError(
                f"field {field.name!r}: {value!r} is not an integer")
        number = int(number)
    if field.provenance == NUMERIC and field.tolerance:
        # Round to the declared tolerance so two values inside the same
        # tolerance band produce the SAME canonical key. Without this the
        # tolerance would only ever be applied pairwise at compare time and
        # a bucketed tally could not group them.
        step = field.tolerance
        number = round(number / step) * step
        if field.type == "integer":
            number = int(round(number))
    return number


def validate_state(state, schema):
    """Validate an extracted state against ``schema``; return it normalised.

    Fails closed. Every required field must be present and well-typed, and
    every optional field that *is* present must still be well-typed -- an
    optional field with a bad value is a malformed extraction, not an absent
    one, and treating it as absent is how a hallucinated value becomes
    invisible instead of rejected.

    Returns a new dict of normalised values; the input is never mutated.
    """
    if not isinstance(state, dict):
        raise SchemaError(
            f"state must be a mapping, got {type(state).__name__}")

    unknown = sorted(set(state) - set(schema.field_names()))
    if unknown:
        raise SchemaError(
            f"schema {schema.identity()}: undeclared field(s) {unknown}")

    normalised = {}
    for field in schema.fields:
        if field.name not in state:
            if field.required:
                raise SchemaError(
                    f"schema {schema.identity()}: missing required field "
                    f"{field.name!r}")
            continue
        try:
            normalised[field.name] = normalize(field, state[field.name])
        except SchemaError as exc:
            raise SchemaError(
                f"schema {schema.identity()}: {exc}") from None
    return normalised
