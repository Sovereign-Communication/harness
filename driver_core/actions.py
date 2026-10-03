"""The declared action vocabulary: a closed set the decision tier may choose from.

This is the boundary that keeps a language model from inventing behaviour.
The decision tier returns the *name* of an action; it never returns a
command, a path, a selector, a keystroke, or a program. Every one of those is
declared here in code, with its parameters, its executor, and its class.

The classification drives the consent model, so it is deliberately coarse and
deliberately fail-safe:

* ``READ_ONLY``  -- observes. No side effect. Safe to repeat.
* ``MUTATING``   -- changes observable state but is recoverable. Requires
  current consent.
* ``IRREVERSIBLE`` -- cannot be undone by this system. Requires a fresh,
  explicit, human confirmation, and is never eligible for batching.

``Vocabulary.resolve`` is the single most important refusal in this package.
An action name the code did not declare is an action the code cannot execute,
cannot bound, cannot consent to, and cannot log -- so it raises rather than
falling back to anything.

The vocabulary covers all four target classes (CLI, MCP, DOM, native GUI)
behind one uniform shape, which is what lets the driver be target-agnostic:
the decision tier never learns which one it is looking at.
"""
from .errors import VocabularyError

READ_ONLY = "read_only"
MUTATING = "mutating"
IRREVERSIBLE = "irreversible"

ACTION_CLASSES = (READ_ONLY, MUTATING, IRREVERSIBLE)

#: Class ordering by consequence. Used to enforce that a playbook may not
#: escalate in severity silently, and by the consent model to pick a gate.
_CLASS_SEVERITY = {READ_ONLY: 0, MUTATING: 1, IRREVERSIBLE: 2}

#: No single playbook step may exceed this many irreversible actions. A
#: driver that proposes three destructive steps in one decision is a driver
#: that has lost the thread, and the batch is refused rather than trimmed.
MAX_IRREVERSIBLE_PER_STEP = 1


class Action:
    """One declared action."""

    __slots__ = ("name", "action_class", "executor", "params", "description",
                 "target", "normalisers")

    def __init__(self, name, action_class, executor, params, description,
                 target, normalisers=None):
        if not isinstance(name, str) or not name.strip():
            raise VocabularyError("action name must be a non-empty string")
        if action_class not in ACTION_CLASSES:
            raise VocabularyError(
                f"action {name!r}: unknown class {action_class!r}")
        if not isinstance(executor, str) or not executor.strip():
            raise VocabularyError(f"action {name!r}: executor must be named")
        if not isinstance(params, (list, tuple)):
            raise VocabularyError(f"action {name!r}: params must be a sequence")
        if not target.strip():
            raise VocabularyError(f"action {name!r}: target must be named")
        undeclared = sorted(set(normalisers or {}) - set(params))
        if undeclared:
            raise VocabularyError(
                f"action {name!r}: normaliser declared for undeclared "
                f"parameter(s) {undeclared}")
        self.name = name
        self.action_class = action_class
        self.executor = executor
        self.params = tuple(params)
        self.description = description
        self.target = target
        #: ``{param name: normaliser name}``. Names, not callables: the
        #: executor tier resolves them to functions so that this module
        #: stays a pure declaration and no platform logic leaks into it.
        self.normalisers = dict(normalisers or {})

    @property
    def severity(self):
        return _CLASS_SEVERITY[self.action_class]

    @property
    def requires_consent(self):
        """Read-only actions observe; everything else needs consent."""
        return self.action_class != READ_ONLY

    @property
    def requires_human_confirmation(self):
        """Only irreversible actions stop for a human."""
        return self.action_class == IRREVERSIBLE

    def check_params(self, params):
        """Validate a proposed parameter set against this action's declaration.

        Unknown parameters are refused rather than ignored. A parameter the
        vocabulary does not declare is the same class of problem as an
        undeclared action: something outside this contract is trying to
        influence execution.
        """
        if not isinstance(params, dict):
            raise VocabularyError(
                f"action {self.name!r}: params must be a mapping, got "
                f"{type(params).__name__}")
        unknown = sorted(set(params) - set(self.params))
        if unknown:
            raise VocabularyError(
                f"action {self.name!r}: undeclared parameter(s) {unknown}")
        missing = sorted(set(self.params) - set(params))
        if missing:
            raise VocabularyError(
                f"action {self.name!r}: missing required parameter(s) {missing}")
        return dict(params)

    def to_dict(self):
        return {
            "name": self.name,
            "class": self.action_class,
            "executor": self.executor,
            "params": list(self.params),
            "target": self.target,
            "description": self.description,
            "normalisers": dict(self.normalisers),
        }

    def __repr__(self):
        return f"Action({self.name!r}, {self.action_class!r})"


def _declare():
    """The single declared vocabulary.

    Grouped by target class. Adding a target means adding actions here --
    never adding a branch somewhere downstream that interprets an undeclared
    string.
    """
    actions = [
        # ---- read-only, target-agnostic -------------------------------
        Action("observe", READ_ONLY, "observe", (), "Capture the current "
               "target state and produce a verified extraction of the declared "
               "schema.", "any"),
        Action("read_value", READ_ONLY, "read_value", ("field",),
               "Read one already-extracted field from the current verified "
               "state.", "state"),
        Action("no_action", READ_ONLY, "no_action", (), "Declare that the "
               "correct response to the current state is to change nothing.",
               "any"),

        # ---- read-only, structured targets (no pixels) ----------------
        Action("run_probe", READ_ONLY, "run_probe", ("command",),
               "Run a declared read-only command and return its stdout, exit "
               "code and stderr.", "cli"),
        Action("call_read_tool", READ_ONLY, "call_read_tool", ("tool",),
               "Call a declared read-only MCP tool and return its structured "
               "result.", "mcp"),
        Action("read_dom", READ_ONLY, "read_dom", ("selector",),
               "Read text content from a declared DOM or accessibility tree "
               "node.", "dom"),

        # ---- mutating --------------------------------------------------
        Action("focus", MUTATING, "focus", ("target",),
               "Bring a declared window or element to the foreground.", "any"),
        Action("click", MUTATING, "click", ("target",),
               "Click a declared, resolved element.", "any"),
        Action("type_text", MUTATING, "type_text", ("text",),
               "Type a literal string into the focused element.", "any"),
        Action("press_key", MUTATING, "press_key", ("keys",),
               "Send a declared key combination to the focused element.",
               "any"),
        Action("scroll", MUTATING, "scroll", ("direction",),
               "Scroll the focused scrollable region.", "any"),
        Action("write_file", MUTATING, "write_file", ("path", "content"),
               "Write a file through the backup-and-atomic-write policy.",
               "filesystem", normalisers={"path": "resolve_path"}),

        # ---- irreversible ----------------------------------------------
        Action("delete_file", IRREVERSIBLE, "delete_file", ("path",),
               "Delete a file. Never batched; always a fresh human "
               "confirmation.", "filesystem",
               normalisers={"path": "resolve_path"}),
        Action("submit_irreversible", IRREVERSIBLE, "submit_irreversible",
               ("target",), "Activate a control whose effect cannot be undone "
               "by this system. Never batched; always a fresh human "
               "confirmation.", "any"),
    ]
    return actions


class Vocabulary:
    """A closed, immutable set of declared actions."""

    __slots__ = ("id", "version", "_actions", "_by_name")

    def __init__(self, id, version, actions):
        self.id = id
        self.version = version
        names = [a.name for a in actions]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise VocabularyError(
                f"vocabulary {id!r} declares duplicate actions: {dupes}")
        self._actions = tuple(actions)
        self._by_name = {a.name: a for a in actions}

    def resolve(self, name):
        """Return the declared action, or raise.

        There is deliberately no default, no fuzzy match, and no
        case-insensitive fallback. A near-miss is a bug in the caller and is
        reported as one rather than quietly resolved to something adjacent.
        """
        try:
            return self._by_name[name]
        except (KeyError, TypeError):
            raise VocabularyError(
                f"action {name!r} is not in the declared vocabulary "
                f"{self.identity()}; the decision tier may only name one of "
                f"{sorted(self._by_name)}") from None

    def names(self):
        return tuple(sorted(self._by_name))

    def actions(self):
        """The declared actions, in declaration order."""
        return self._actions

    def of_class(self, action_class):
        return tuple(a for a in self._actions
                     if a.action_class == action_class)

    def to_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "actions": [a.to_dict() for a in self._actions],
        }

    def identity(self):
        return f"{self.id}@{self.version}"

    def __len__(self):
        return len(self._actions)

    def __repr__(self):
        return f"Vocabulary({self.id!r}, {self.version!r}, {len(self)} actions)"


#: The one declared vocabulary. A caller may build its own for tests, but
#: production has exactly one.
DEFAULT_VOCABULARY = Vocabulary("driver-core-actions", "1.0.0", _declare())


def check_batch(proposed, vocabulary):
    """Validate a proposed batch of actions against the consent policy.

    Refuses a batch that mixes an irreversible action with anything else,
    which is how a single "yes" would otherwise launder several consequences
    past a human who was only asked about one.
    """
    if not isinstance(proposed, (list, tuple)):
        raise VocabularyError("a proposed batch must be a sequence of actions")
    if len(proposed) > MAX_IRREVERSIBLE_PER_STEP:
        irreversible = [n for n in proposed
                        if vocabulary.resolve(n).requires_human_confirmation]
        if len(irreversible) > 1:
            raise VocabularyError(
                f"a single step may propose at most "
                f"{MAX_IRREVERSIBLE_PER_STEP} irreversible action, got "
                f"{sorted(irreversible)}")
    return list(proposed)
