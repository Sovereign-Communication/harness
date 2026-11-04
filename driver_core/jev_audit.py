"""A comprehensive audit: every symbol, every semantic dimension, through Jev.

The rest of this package asks Jev exactly one question at production time --
"which declared action comes next" -- and it is deliberately the *narrowest*
question in the system, because a wide question with a confident answer is how
a model gets to influence something. That narrowness is a property of the
decision path. It is not a property of an *audit*.

An audit is the one place where asking a model a great many questions is the
point. So this module enumerates every class and function in a tree, and for
each one asks a fixed pack of declared semantic dimensions: does it keep its
contract, does it contain a defect, does it fail closed, how is it reached, is
it guarded by a test, can it disclose a secret or captured content, does it
duplicate a fact that already has an owner, is there a materially simpler
shape, what does it touch outside itself, and is its docstring true. Ten
dimensions, one call per symbol, plus the context protocol.

**The questions are declared, not generated.** Every dimension is a closed
``noul`` or ``choice`` in :data:`DIMENSIONS`, for the same reason the action
vocabulary is closed: a free-text answer can be neither aggregated, nor
compared across symbols, nor checked against a mechanism. A tally of ``noul``s
can be.

**Every call carries the context protocol.** Each request also asks
:mod:`driver_core.jev_context`'s three protocol questions -- how much more
context is needed, which declared *kind* of context would most improve the
answer, and which declared dimension drove it -- and
:func:`~driver_core.jev_context.ask_until_grounded` then supplies the requested
kind and asks again. A verdict is therefore never accepted from a model that
said it was still under-informed; it comes back ``ungrounded`` instead.

**Context buckets are associated with the questions that need them.** A
fourteenth question asks *which dimension* would most improve with more
context, so the report can count the pairs rather than assert a theory: "the
model asked for the tests while answering ``regression_guarded`` 41 times" is
a fact, and it is the fact that says where to spend effort next. Each
dimension also *declares* the buckets it draws on, so the declared expectation
sits beside the observed count.

**The model is cross-checked against a mechanism.** Before any call, each
symbol gets a *dossier* computed by reading the code: how many files name it,
how many tests mention it, what it raises, what it calls, how many branches it
holds. The ``facts`` bucket supplies that dossier, and the report prints the
model's verdicts beside it. A verdict that calls a symbol unguarded while
several tests name it is a verdict the reader can see is wrong -- which is the
whole reason both numbers sit in one table.

**What this module will not do.** It will not supply a bucket it cannot really
supply. ``counterexample`` has no supplier, because manufacturing a falsifying
input means *running* the code, and a model asked to imagine one is being asked
to be confident about a guess. When a symbol's loop asks for it, the loop stops
and reports ``ungrounded`` rather than inventing the evidence. That limit is
reported, not hidden: :func:`aggregate` counts the buckets the model asked for
that nothing could answer.
"""
import ast
import collections
import json
import pathlib
import re

from . import transport
from .budget import estimate_call_cost
from .config import JEV_INPUT_PRICE_PER_MILLION
from .jev_context import (
    CONTEXT_BUCKETS, UNSETTLED_UNANSWERABLE as UNANSWERABLE,
    ask_until_grounded,
)

#: The question that associates a *kind* of context with a *dimension*. Asked
#: of every symbol and recorded without gating the loop, because a diagnostic
#: is worth knowing and not worth holding an answer open for.
DIMENSION_NEEDING_CONTEXT = "dimension_needing_most_context"

_NOUL = "noul"
_CHOICE = "choice"


class Dimension:
    """One declared semantic dimension, asked of every symbol."""

    __slots__ = ("name", "kind", "instructions", "criteria", "context")

    def __init__(self, name, kind, instructions, *, criteria=None, context=()):
        self.name = name
        self.kind = kind
        self.instructions = instructions
        self.criteria = dict(criteria or {})
        self.context = tuple(context)

    def to_question(self):
        """This dimension as one declared transport question.

        Built by the shared builders rather than by a private copy of them: a
        second implementation of "what a choice question looks like" is
        precisely the duplication this module's own ``duplicated_fact``
        dimension exists to find.
        """
        if self.kind == _CHOICE:
            return transport.choice(self.instructions, self.criteria)
        return transport.noul(
            self.instructions,
            true=self.criteria.get("true"),
            false=self.criteria.get("false"))

    def to_dict(self):
        return {"name": self.name, "type": self.kind,
                "declared_context_buckets": list(self.context)}


#: The ten semantic dimensions. Each is a closed question with a fixed option
#: set, and each declares the context buckets that would ground it.
DIMENSIONS = (
    Dimension(
        "contract_fidelity", _NOUL,
        "Does this implementation do exactly what its own declared contract -- "
        "its docstring, and the names it uses -- says it does? Answer 'true' "
        "only if you can name no place where the code and the declaration "
        "disagree.",
        criteria={
            "true": "The code and its declared contract agree everywhere.",
            "false": "There is a concrete place where the code and its "
                     "declaration disagree.",
        },
        context=("contract", "code")),
    Dimension(
        "behaviour_defect", _NOUL,
        "On some input reachable through this symbol's own declared interface, "
        "would this produce a wrong result -- a wrong value, a wrong branch, or "
        "an exception the declaration does not admit? Answer 'false' if you "
        "cannot name such an input, or if naming one would be a guess.",
        criteria={
            "true": "A reachable input makes this produce a wrong result.",
            "false": "No such input can be named.",
        },
        context=("code", "counterexample")),
    Dimension(
        "fails_closed", _NOUL,
        "On its error and refusal paths, does this refuse rather than proceed "
        "-- returning a failure the caller must notice, rather than a default, "
        "a bare exception, or a value that reads as success?",
        criteria={
            "true": "Every refusal path is explicit and visible to the caller.",
            "false": "Some path proceeds on missing, malformed, or unexpected "
                     "input in a way a caller would read as success.",
        },
        context=("code", "constraints")),
    Dimension(
        "reachability", _CHOICE,
        "Which of these best describes how this symbol is reached? 'reachable' "
        "means a product entry point or the shipped pipeline can arrive here; "
        "'test_only' means only a test reaches it; 'unreachable' means nothing "
        "in the tree calls or references it.",
        criteria={
            "reachable": "A product entry point or the shipped pipeline "
                         "arrives here.",
            "test_only": "Only a test reaches this.",
            "unreachable": "Nothing in the tree references this at all.",
        },
        context=("facts",)),
    Dimension(
        "regression_guarded", _NOUL,
        "Would an existing test in this tree fail if this symbol's behaviour "
        "changed in a way its docstring does not permit? Answer 'true' only if "
        "you can attribute that failure to a test that actually exercises this "
        "path.",
        criteria={
            "true": "A test in this tree exercises this path and would fail.",
            "false": "No test in this tree would notice the change.",
        },
        context=("tests", "code")),
    Dimension(
        "disclosure_risk", _NOUL,
        "Could anything here put credential material, captured screen content, "
        "or a local filesystem path into a written record, a log, or an "
        "outbound request body -- including as an unexpected field, an echoed "
        "value, or an interpolated message?",
        criteria={
            "true": "Some value here can reach a record, a log or the wire "
                    "that should not.",
            "false": "Every field it writes or sends is a value it is meant to "
                     "carry.",
        },
        context=("code", "constraints")),
    Dimension(
        "duplicated_fact", _NOUL,
        "Does this compute, restate or store a fact or rule that another "
        "module in this tree already owns -- so that the two can disagree?",
        criteria={
            "true": "A fact or rule here is already owned elsewhere.",
            "false": "Every fact here is either this symbol's own or imported "
                     "from its owner.",
        },
        context=("code", "alternatives")),
    Dimension(
        "simpler_shape", _NOUL,
        "Is there a materially simpler implementation that removes no "
        "capability and no refusal -- fewer branches, fewer names, or fewer "
        "places for one fact to live?",
        criteria={
            "true": "A concretely simpler shape exists and can be named.",
            "false": "This is already about as small as the behaviour allows.",
        },
        context=("alternatives", "code")),
    Dimension(
        "side_effect_class", _CHOICE,
        "What does this most directly touch outside its own process and "
        "memory?",
        criteria={
            "pure": "Nothing outside itself: values in, values out.",
            "local_state": "Its own object or module state only.",
            "filesystem": "The filesystem.",
            "network": "The network.",
            "machine_input": "The machine's input devices, windows or "
                             "processes.",
        },
        context=("code",)),
    Dimension(
        "docstring_accurate", _NOUL,
        "Is the docstring accurate about what this does -- making no promise "
        "the code does not keep, and omitting no refusal the code performs?",
        criteria={
            "true": "Every claim the docstring makes is kept by the code.",
            "false": "The docstring promises something the code does not do, "
                     "or omits a refusal it does.",
        },
        context=("contract", "code")),
)

#: Dimension names, in the order asked.
DIMENSION_NAMES = tuple(d.name for d in DIMENSIONS)

#: The buckets a supplier really answers, and the one that deliberately has
#: none. ``counterexample`` is the interesting absence: it needs a *run*, and
#: this audit runs nothing, so a model asking for it is told plainly that
#: nothing can answer rather than being handed a plausible fabrication.
SUPPLIED_BUCKETS = ("facts", "code", "contract", "tests", "constraints",
                    "alternatives")
UNSUPPLIED_BUCKETS = ("counterexample",)

#: Prose invariants this tree holds that have no single machine owner to be
#: derived from. Everything derivable is derived, in
#: :func:`_derived_constraints`, because a copy of a fact that has an owner is
#: the defect one of the dimensions above exists to find.
DECLARED_INVARIANTS = (
    "Consent is a capability for one exact (action, params) pair; there is no "
    "wildcard for any action class.",
    "Parameters come from the caller and never from the decision tier.",
    "Registration and consent are two independent gates and both are "
    "required.",
    "A dry run performs no side effect and spends no irreversible consent.",
    "Nothing may put captured pixels, a local path, or credential material "
    "into a record, a log, or an outbound request body.",
    "A refusal must be diagnosable: it names the fault the caller can fix.",
)


class Symbol:
    """One class or function, with a mechanically computed dossier."""

    __slots__ = ("path", "module", "qualname", "kind", "lineno", "name",
                 "source", "docstring", "dossier")

    def __init__(self, path, module, qualname, kind, lineno, name, source,
                 docstring, dossier=None):
        self.path = path
        self.module = module
        self.qualname = qualname
        self.kind = kind
        self.lineno = lineno
        self.name = name
        self.source = source
        self.docstring = docstring
        self.dossier = dict(dossier or {})

    @property
    def key(self):
        return f"{self.module}:{self.qualname}"

    def to_dict(self):
        return {
            "key": self.key,
            "path": self.path,
            "module": self.module,
            "qualname": self.qualname,
            "kind": self.kind,
            "lineno": self.lineno,
            "lines": self.dossier.get("lines"),
            "has_docstring": bool(self.docstring),
            "dossier": self.dossier,
        }

    def __repr__(self):
        return f"Symbol({self.key!r}, {self.kind})"


#: Directories never worth walking into. A tree can hold a build output, an
#: installed copy, a cache or a virtual environment beside the source, and
#: counting a symbol twice because a copy of the package sits in ``dist/``
#: would make every tally in the report wrong without ever failing.
_PRUNED = frozenset({"__pycache__", ".git", ".hg", ".svn", ".ruff_cache",
                     ".mypy_cache", ".pytest_cache", ".venv", "venv", "env",
                     "node_modules", "build", "dist", "htmlcov", ".eggs",
                     ".tox", "site-packages"})

#: An identifier, for the per-file token index below.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read(path):
    return path.read_text(encoding="utf-8")


def _pruned(parts):
    return any(part in _PRUNED or part.endswith(".egg-info")
               for part in parts)


def _walk_files(root, suffixes=(".py", ".md")):
    """Every source and document file under ``root``, in a stable order.

    Pruning is named rather than left to a glob that happens to work: an
    audit that walks into ``build/`` and ``dist/`` reports each symbol two or
    three times, which doubles every count in the report and never fails.
    """
    root = pathlib.Path(root)
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if _pruned(path.relative_to(root).parts[:-1]):
            continue
        yield path


def _module_name(root, path):
    """The dotted module path of ``path``, relative to the audit root.

    Dotted rather than a bare file stem, because two files named
    ``jev_audit.py`` in one tree -- a package module and the runner beside it
    -- would otherwise yield two symbols with one key, and every count in the
    report would quietly be about the wrong one.
    """
    try:
        parts = list(pathlib.Path(path).relative_to(pathlib.Path(root))
                     .with_suffix("").parts)
    except ValueError:                                   # pragma: no cover
        return pathlib.Path(path).stem
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or pathlib.Path(path).stem


def token_index(texts):
    """``{path: the identifiers in it}``, computed once per audit run.

    The dossier asks "which files name this symbol?" once per symbol, which
    over a few hundred symbols and a whole tree is hundreds of thousands of
    searches. Tokenising each file once turns each of those into a set
    lookup, and is the difference between an audit that finishes and one that
    looks like it has hung.
    """
    return {path: frozenset(_WORD.findall(text))
            for path, text in texts.items()}


def _iter_symbols(tree):
    """Every class and function node, with its qualname parents."""

    def recurse(body, parents):
        for child in body:
            if isinstance(child, (ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                yield child, parents
                yield from recurse(child.body, (*parents, child.name))

    yield from recurse(tree.body, ())


def collect(root):
    """Every symbol in ``root`` that parses, sorted by path then line.

    Deterministic order is what makes a checkpoint resumable and two runs
    comparable, so it is a property of this function rather than of the loop
    that consumes it.
    """
    root = pathlib.Path(root)
    for path in _walk_files(root, (".py",)):
        try:
            text = _read(path)
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        module = _module_name(root, path)
        for node, parents in _iter_symbols(tree):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            qualname = ".".join([*parents, node.name])
            yield Symbol(
                str(path), module, qualname, kind, node.lineno, node.name,
                "\n".join(lines[node.lineno - 1:node.end_lineno]),
                ast.get_docstring(node, clean=True)), node, lines


def package_text(root):
    """Every readable source and document file under ``root``, by path."""
    texts = {}
    for path in _walk_files(root):
        try:
            texts[str(path)] = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def _mentions(index, needle, *, only=None):
    """Paths whose text names ``needle``, optionally filtered."""
    return sorted(path for path, tokens in index.items()
                  if needle in tokens and (only is None or only(path)))


def _call_targets(node):
    """Every name this subtree calls, by attribute or by bare name."""
    found = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            found.add(func.attr)
        elif isinstance(func, ast.Name):
            found.add(func.id)
    return sorted(found)


def _branch_count(node):
    return sum(1 for child in ast.walk(node)
               if isinstance(child, (ast.If, ast.For, ast.While, ast.Try,
                                     ast.With)))


def _raised(node):
    """Exception names this subtree raises, by name only."""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
            func = child.exc.func
            names.add(func.id if isinstance(func, ast.Name)
                      else getattr(func, "attr", "?"))
    return sorted(names)


def _decorators(node):
    return sorted(
        decorator.id if isinstance(decorator, ast.Name)
        else getattr(decorator, "attr", "?")
        for decorator in getattr(node, "decorator_list", ()))


def build_dossier(symbol, node, index, *, tests_root):
    """The mechanically computed facts about one symbol.

    Computed from the source and never from a model: this is the number the
    model's verdicts are printed against, and a number a model produced could
    not be used for that.
    """
    tests_root = pathlib.Path(tests_root) if tests_root else None

    def under_tests(path):
        if tests_root is None:
            return "test" in pathlib.Path(path).name
        try:
            return pathlib.Path(path).is_relative_to(tests_root)
        except (OSError, ValueError):                    # pragma: no cover
            return False

    test_files = _mentions(index, symbol.name, only=under_tests)
    elsewhere = _mentions(index, symbol.name,
                          only=lambda p: pathlib.Path(p) != pathlib.Path(
                              symbol.path))
    return {
        "lines": max(1, node.end_lineno - node.lineno + 1),
        "branches": _branch_count(node),
        "raises": _raised(node),
        "calls": _call_targets(node),
        "decorators": _decorators(node),
        "has_docstring": bool(symbol.docstring),
        "test_files_mentioning": len(test_files),
        "test_files": test_files[:8],
        "files_mentioning": len(elsewhere),
        "kind_is_dunder": symbol.name.startswith("__"),
        "kind_is_private": symbol.name.startswith("_")
        and not symbol.name.startswith("__"),
    }


def symbols(root, *, tests_root=None):
    """Every symbol in ``root`` with its dossier filled in.

    The token index is built once here rather than per symbol -- see
    :func:`token_index` for why that is not a micro-optimisation.
    """
    index = token_index(package_text(root))
    for symbol, node, _lines in collect(root):
        symbol.dossier = build_dossier(symbol, node, index,
                                       tests_root=tests_root)
        yield symbol


def siblings(symbol, all_symbols):
    """Other symbols in the same module and of the same kind."""
    return [s for s in all_symbols
            if s.module == symbol.module and s.kind == symbol.kind
            and s.qualname != symbol.qualname]


def _derived_constraints():
    """The invariants this tree can state about itself, read from itself."""
    derived = []
    try:
        from .config import ENV_PREFIX, FOREIGN_PREFIXES
        derived.append(
            f"Every setting this package reads begins {ENV_PREFIX!r}; it must "
            f"never read {', '.join(FOREIGN_PREFIXES)}.")
    except Exception:                                    # pragma: no cover
        pass
    try:
        from . import audit as audit_module
        kinds = sorted(value for name, value in vars(audit_module).items()
                       if name.startswith("KIND_"))
        derived.append(
            "A record kind is a declared constant (" + ", ".join(kinds) +
            ") owned only by driver_core.audit; nothing else writes the "
            "string.")
    except Exception:                                    # pragma: no cover
        pass
    try:
        from .actions import ACTION_CLASSES
        derived.append(
            "An action's class is one of " + ", ".join(ACTION_CLASSES) +
            ": read-only observes, mutating needs current consent, "
            "irreversible needs a fresh human confirmation.")
    except Exception:                                    # pragma: no cover
        pass
    return derived


def constraints_text():
    """The invariant set handed to a model that asked for constraints."""
    return "\n".join(f"- {line}" for line in
                     [*_derived_constraints(), *DECLARED_INVARIANTS])


def pack_for(symbol=None):
    """The declared question pack: ten dimensions, plus the bucket pairing.

    ``symbol`` is accepted so a caller can build a per-symbol pack later
    without every call site changing; the questions themselves are
    symbol-independent by construction, which is what makes the tallies across
    symbols mean anything.
    """
    pack = {dimension.name: dimension.to_question() for dimension in DIMENSIONS}
    pack[DIMENSION_NEEDING_CONTEXT] = transport.choice(
        "Which single dimension above would most improve if you were given "
        "more of the context you named?",
        {name: f"the {name} question" for name in DIMENSION_NAMES})
    return pack


def _first_doc_line(symbol):
    text = (symbol.docstring or "").strip()
    return text.splitlines()[0].strip() if text else "<undocumented>"


def _test_blocks(text, name):
    """The test functions whose own source mentions ``name``."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")
    blocks = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if pattern.search(segment):
            blocks.append(segment)
    return blocks


def make_supplier(symbol, texts, *, peers=()):
    """A supplier answering each bucket with real repository material.

    Returns ``None`` for a bucket it genuinely cannot supply, which is how
    :func:`~driver_core.jev_context.ask_until_grounded` learns that an answer
    is ungrounded rather than merely unfinished.
    """
    held = {}

    def supply(bucket, _round):
        if bucket in held:
            return held[bucket]
        text = None
        if bucket == "facts":
            text = json.dumps(symbol.to_dict(), indent=2, sort_keys=True,
                              default=str)
        elif bucket == "code":
            text = (f"# {symbol.path}:{symbol.lineno} {symbol.qualname}\n"
                    f"{symbol.source}")
        elif bucket == "contract":
            text = (f"# the declared contract of {symbol.qualname}\n"
                    f"{symbol.docstring or '<no docstring declared>'}")
        elif bucket == "tests":
            text = _tests_text(symbol, texts)
        elif bucket == "constraints":
            text = "# invariants this tree holds\n" + constraints_text()
        elif bucket == "alternatives":
            text = _alternatives_text(peers)
        else:
            return None
        held[bucket] = text
        return text

    return supply


def _tests_text(symbol, texts):
    """The test source that mentions this symbol, verbatim."""
    chunks = []
    for path, text in sorted(texts.items()):
        if "test" not in pathlib.Path(path).name:
            continue
        if not re.search(r"\b" + re.escape(symbol.name) + r"\b", text):
            continue
        for block in _test_blocks(text, symbol.name):
            chunks.append(f"# {path}\n{block}")
    if not chunks:
        return f"# no test in this tree mentions {symbol.name!r} by name"
    return "\n\n".join(chunks[:6])


def _alternatives_text(peers):
    """The sibling symbols this one could be compared against."""
    if not peers:
        return "# nothing else in this module is of this kind"
    return "\n".join(
        f"- {peer.qualname} ({peer.dossier.get('lines', '?')} lines, "
        f"{peer.dossier.get('branches', '?')} branches): "
        f"{_first_doc_line(peer)}"
        for peer in peers)


def _reading(answer):
    """An answer's own value, as a short comparable string."""
    if not isinstance(answer, dict):
        return None
    kind = answer.get("type")
    if kind == "noul":
        try:
            return "true" if float(answer.get("noul")) >= 0.5 else "false"
        except (TypeError, ValueError):
            return None
    if kind == "choice":
        value = answer.get("choice")
        return None if value is None else str(value)
    if kind == "score":
        return answer.get("score")
    return None


def need_band(needed):
    """A ``context_needed`` score, banded for counting.

    The endpoint answers a ``score`` question on a *continuous* scale: the ten
    declared levels are anchors, not the only values it may return, and the
    audit observed 1.26, 1.37 and 4.71 alongside whole numbers. Counting exact
    values therefore produces one row per decimal place. Banding them keeps
    the distribution readable without pretending the answer was discrete.
    """
    if needed is None:
        return "unscored"
    try:
        value = float(needed)
    except (TypeError, ValueError):
        return "unscored"
    if value <= 0.0:
        return "0 nothing further"
    for upper in (1, 2, 3, 4):
        if value <= upper:
            return f"{upper - 1}–{upper}"
    return "above 4"


def _top_driver(drivers):
    """The single most probable driving bucket, or ``None``."""
    if not drivers:
        return None
    try:
        return max(drivers.items(), key=lambda item: float(item[1]))[0]
    except (TypeError, ValueError):                      # pragma: no cover
        return None


def audit_symbol(symbol, all_symbols, texts, *, settings, gate=0.9,
                 need_floor=3.0, max_rounds=3, transport_module=None):
    """Push one symbol through every dimension, with the context protocol.

    Returns plain data -- the symbol, its dossier, the grounding, and one
    reading per question -- so a caller can checkpoint, aggregate or discard it
    without this module having to decide what a verdict *means*.
    """
    supplier = make_supplier(symbol, texts,
                             peers=siblings(symbol, all_symbols))
    state = {
        "audit": "one symbol of a Python package, judged across declared "
                 "semantic dimensions",
        "symbol": {"module": symbol.module, "qualname": symbol.qualname,
                   "kind": symbol.kind, "path": symbol.path,
                   "lineno": symbol.lineno},
        "dossier": symbol.dossier,
        "dimensions": [dimension.to_dict() for dimension in DIMENSIONS],
        "instruction": "Answer every question about this symbol. Ask for the "
                       "context you need; the harness will supply it and ask "
                       "again.",
    }
    grounding = ask_until_grounded(
        pack_for(symbol), state=state, settings=settings, supplier=supplier,
        gate=gate, need_floor=need_floor, max_rounds=max_rounds,
        optional=(DIMENSION_NEEDING_CONTEXT,),
        transport_module=transport_module)

    readings = {}
    for name in pack_for(symbol):
        answer = grounding.answers.get(name)
        readings[name] = {
            "answer": _reading(answer),
            "confidence": grounding.confidences.get(name),
            "declared_buckets": (
                [] if name == DIMENSION_NEEDING_CONTEXT
                else list(next((d.context for d in DIMENSIONS if d.name == name),
                               ()) or ())),
        }
    # The protocol answers are read by the loop itself; keep them in the record
    # too, so a reader never has to reconstruct them from the grounding.
    readings["context_bucket"] = {"answer": grounding.bucket,
                                  "confidence": None, "declared_buckets": []}
    readings["driving_bucket"] = {"answer": _top_driver(grounding.drivers),
                                  "confidence": None, "declared_buckets": []}
    readings["context_needed"] = {"answer": grounding.needed,
                                  "confidence": None, "declared_buckets": []}

    record = symbol.to_dict()
    record["readings"] = readings
    record["context_needed"] = grounding.needed
    record["context_bucket"] = grounding.bucket
    record["driving_bucket"] = _top_driver(grounding.drivers)
    record["driving_probabilities"] = grounding.drivers
    record["dimension_needing_most_context"] = (
        readings.get(DIMENSION_NEEDING_CONTEXT, {}).get("answer"))
    record["grounding"] = grounding.to_dict()
    record["ungrounded"] = grounding.ungrounded
    record["ungrounded_reason"] = grounding.reason
    record["reason_kind"] = grounding.reason_kind
    record["rounds"] = grounding.rounds
    record["input_tokens"] = grounding.input_tokens
    record["cost_usd"] = grounding.cost
    return record


def audit(root, *, settings, tests_root=None, gate=0.9, need_floor=3.0,
          max_rounds=3, budget_usd=0.50, limit=None, modules=None,
          checkpoint=None, progress=None, transport_module=None, done=()):
    """Audit every symbol in ``root``, one call each, inside a budget.

    Yields records as they are produced, so a caller can checkpoint each one
    and a long run is interruptible without losing what it already paid for.
    ``done`` names symbols already audited, which is what makes a killed run
    resumable. Stops when the budget is spent and says so in a final record
    carrying ``stopped: True`` -- a run that stopped early without saying so
    would read as a complete audit.
    """
    texts = package_text(root)
    chosen = [s for s in symbols(root, tests_root=tests_root)
              if not modules or s.module in modules]
    if limit:
        chosen = chosen[:limit]
    total = len(chosen)
    spent = 0.0
    audited = 0
    for index, symbol in enumerate(chosen, start=1):
        if symbol.key in done:
            continue
        if spent >= budget_usd:
            yield {"audit": "budget exhausted", "stopped": True,
                   "spent_usd": round(spent, 9), "budget_usd": budget_usd,
                   "audited": audited, "total": total}
            return
        if progress:
            progress(index, total, symbol, spent)
        record = audit_symbol(
            symbol, chosen, texts, settings=settings, gate=gate,
            need_floor=need_floor, max_rounds=max_rounds,
            transport_module=transport_module)
        spent += record["cost_usd"]
        audited += 1
        record["index"] = index
        record["total"] = total
        record["spent_usd"] = round(spent, 9)
        if checkpoint:
            checkpoint(record)
        yield record
    yield {"audit": "complete", "stopped": False, "spent_usd": round(spent, 9),
           "budget_usd": budget_usd, "audited": audited, "total": total}


def _blank_dimension_blocks():
    return {name: {} for name in DIMENSION_NAMES}


def aggregate(records):
    """Turn a run's records into the counts the report is made of.

    Counts rather than prose, for the same reason the questions are closed:
    "the model called three symbols unguarded" is checkable against each
    dossier, and "the model felt uneasy about the package" is not.
    """
    records = [r for r in records if "readings" in r]
    summary = {
        "symbols_audited": len(records),
        "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in records), 9),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in records),
        "rounds": sum(int(r.get("rounds") or 0) for r in records),
        "ungrounded": sum(1 for r in records if r.get("ungrounded")),
        "by_dimension": _blank_dimension_blocks(),
        "context_bucket_asked_for": collections.Counter(),
        "bucket_by_flagged_dimension": collections.Counter(),
        "declared_context_by_dimension": {
            name: list(d.context) for name, d in
            ((dimension.name, dimension) for dimension in DIMENSIONS)},
        "context_needed_histogram": collections.Counter(),
        "unanswered_buckets": collections.Counter(),
        "unsettled_by_kind": collections.Counter(),
        "unsettled_reasons": collections.Counter(),
        "verdicts_contradicting_the_dossier": [],
        "contradiction_count": 0,
    }
    for record in records:
        readings = record.get("readings") or {}
        dossier = record.get("dossier") or {}
        bucket = record.get("context_bucket") or record.get(
            "readings", {}).get("context_bucket", {}).get("answer")
        if bucket:
            summary["context_bucket_asked_for"][bucket] += 1
        summary["context_needed_histogram"][
            need_band(record.get("context_needed"))] += 1
        if record.get("ungrounded"):
            kind = record.get("reason_kind") or "unknown"
            summary["unsettled_by_kind"][kind] += 1
            if kind == UNANSWERABLE:
                # The model named a kind of context no supplier has. That is a
                # missing input, and it is counted apart from a question that
                # simply has no confident answer from this evidence.
                summary["unanswered_buckets"][bucket or "<none named>"] += 1
            summary["unsettled_reasons"][
                (record.get("ungrounded_reason") or "")[:110]] += 1
        if bucket and record.get("dimension_needing_most_context"):
            summary["bucket_by_flagged_dimension"][
                (record["dimension_needing_most_context"], bucket)] += 1
        for name in DIMENSION_NAMES:
            reading = readings.get(name) or {}
            block = summary["by_dimension"][name]
            block.setdefault("answers", collections.Counter())
            block.setdefault("confidences", [])
            block.setdefault("asked_for_context", collections.Counter())
            answer = reading.get("answer")
            if answer is None:
                block["answers"]["unanswered"] += 1
            else:
                block["answers"][str(answer)] += 1
                if reading.get("confidence") is not None:
                    block["confidences"].append(float(reading["confidence"]))
            if bucket and record.get("dimension_needing_most_context") == name:
                block["asked_for_context"][bucket] += 1
        # The cross-check: a verdict that a symbol's contract is unfaithful, or
        # that no test guards it, is printed beside the count of test files
        # that actually name it. A reader can then see which verdicts a
        # mechanism contradicts.
        for name, expectation in (("contract_fidelity", "false"),
                                  ("regression_guarded", "false"),
                                  ("docstring_accurate", "false")):
            reading = readings.get(name) or {}
            if reading.get("answer") != expectation:
                continue
            tests = int(dossier.get("test_files_mentioning") or 0)
            summary["verdicts_contradicting_the_dossier"].append({
                "key": record.get("key"),
                "dimension": name,
                "verdict": expectation,
                "test_files_mentioning": tests,
                "lines": dossier.get("lines"),
            })
            summary["contradiction_count"] += 1
    for _name, block in summary["by_dimension"].items():
        confidences = block.get("confidences") or []
        block["answers"] = dict(block.get("answers") or {})
        block["asked_for_context"] = dict(block.get("asked_for_context") or {})
        block["answered"] = sum(count for answer, count in block["answers"]
                                .items() if answer != "unanswered")
        block["mean_confidence"] = (round(sum(confidences) / len(confidences),
                                          4) if confidences else None)
        block.pop("confidences", None)
    summary["context_bucket_asked_for"] = dict(
        summary["context_bucket_asked_for"])
    summary["bucket_by_flagged_dimension"] = {
        f"{dimension} <- {bucket}": count
        for (dimension, bucket), count in
        summary["bucket_by_flagged_dimension"].items()}
    summary["unanswered_buckets"] = dict(summary["unanswered_buckets"])
    summary["unsettled_by_kind"] = dict(summary["unsettled_by_kind"])
    summary["unsettled_reasons"] = dict(summary["unsettled_reasons"])
    order = ["0 nothing further", "0–1", "1–2", "2–3", "3–4", "above 4",
             "unscored"]
    summary["context_needed_histogram"] = {
        band: summary["context_needed_histogram"][band]
        for band in order if band in summary["context_needed_histogram"]}
    return summary


def render_report(summary, *, root=None, model=None, gate=None):
    """The audit as Markdown, so a reader sees the counts *and* the caveats."""
    lines = [
        "# Comprehensive Jev audit — every symbol, every semantic dimension",
        "",
        f"*{summary['symbols_audited']} symbols, each asked "
        f"{len(DIMENSION_NAMES)} declared semantic dimensions plus the "
        "context-protocol questions, through "
        "`driver_core.jev_context.ask_until_grounded`.*",
        "",
    ]
    if root or model or gate is not None:
        lines += [f"*Root `{root}`, model `{model}`, gate `{gate}`.*", ""]
    lines += [
        "## What the run cost",
        "",
        "| metric | value |",
        "|---|---|",
        f"| symbols audited | {summary['symbols_audited']} |",
        f"| rounds | {summary['rounds']} |",
        f"| input tokens | {summary['input_tokens']:,} |",
        f"| spend | ${summary['cost_usd']:.6f} |",
        f"| symbols whose loop ended ungrounded | {summary['ungrounded']} |",
        "",
        "## Verdicts by dimension",
        "",
        "| dimension | answered | verdicts | mean confidence | declared "
        "buckets |",
        "|---|---|---|---|---|",
    ]
    for name in DIMENSION_NAMES:
        block = summary["by_dimension"][name]
        verdicts = ", ".join(f"`{k}` {v}"
                             for k, v in sorted(block["answers"].items()))
        mean = block["mean_confidence"]
        declared = ", ".join(summary["declared_context_by_dimension"][name])
        lines.append(f"| `{name}` | {block['answered']} | {verdicts} | "
                     f"{mean if mean is not None else '—'} | {declared} |")
    lines += [
        "",
        "## Which kinds of context these questions needed",
        "",
        "The model was asked which declared kind of context would most "
        "improve its answer, and which dimension that was for. Each row is an "
        "observed pairing, not a theory.",
        "",
        "| dimension that would most improve | context kind it needs | times "
        "|",
        "|---|---|---|",
    ]
    pairings = summary["bucket_by_flagged_dimension"]
    for pairing, count in sorted(pairings.items(), key=lambda item: -item[1]):
        dimension, _, bucket = pairing.partition(" <- ")
        lines.append(f"| `{dimension}` | `{bucket}` | {count} |")
    if not pairings:
        lines.append("| — | — | 0 |")
    lines += [
        "",
        "Buckets asked for across all symbols:",
        "",
    ]
    for bucket, count in sorted(summary["context_bucket_asked_for"].items(),
                                key=lambda item: -item[1]):
        lines.append(f"- `{bucket}` × {count}")
    lines += [
        "",
        "## Outstanding need, as the model scored it",
        "",
        "A ``score`` question answers on a continuous scale -- the declared",
        "levels are anchors, not the only values returned -- so the scores are",
        "banded. Bands above 0 are the symbols where the model asked for",
        "context and then said it needed no more, which is the honest",
        "\"not confident enough to settle\" signal.",
        "",
        "| context still needed | symbols |",
        "|---|---|",
    ]
    for score, count in summary["context_needed_histogram"].items():
        lines.append(f"| {score} | {count} |")
    lines += [
        "",
        "## Where nothing could answer",
        "",
        "These buckets were named by the model and have no supplier at all:",
        "supplying them would mean *running* the code rather than reading it.",
        "A verdict resting on one of these is reported ungrounded instead of",
        "being reported confidently, and the material is real -- it exists, and",
        "this audit did not obtain it. A bucket the supplier *does* have, asked",
        "for a second time with no new content behind it, is not counted here;",
        "it appears as `no_new_evidence` below, which asks for the opposite",
        "response.",
        "",
        "| bucket | times named |",
        "|---|---|",
    ]
    for bucket, count in sorted(summary["unanswered_buckets"].items(),
                                key=lambda item: -item[1]):
        lines.append(f"| `{bucket}` | {count} |")
    if not summary["unanswered_buckets"]:
        lines.append("| — | 0 |")
    lines += [
        "",
        "## Where the loop ended unsettled, and why",
        "",
        "Every symbol below ran out of loop without a settled answer, and the",
        "kind says which condition it was. They are listed apart because they",
        "are not the same thing, and a bare count of \"ungrounded\" would",
        "average them into one number that answers nothing:",
        "",
        "* `unanswerable` -- the model named a kind of context no supplier",
        "  has; the material is real and this run did not obtain it.",
        "* `no_new_evidence` -- it asked again for a bucket already holding",
        "  exactly the content supplied. The material is not missing; the",
        "  evidence this supplier has is exhausted.",
        "* `still_short` -- every bucket it asked for was supplied, it then",
        "  said it needed nothing further, and a declared answer is still",
        "  below the gate. The question has no confident answer from this",
        "  evidence, which is a result and not a failure of the run.",
        "* `rounds_exhausted` -- it was still asking for new context when the",
        "  round budget ran out. Nothing is missing; there were too few",
        "  rounds to settle it inside this run's budget.",
        "* `transport` -- the call itself never completed.",
        "",
        "| how the loop ended | symbols |",
        "|---|---|",
    ]
    for kind, count in sorted(summary["unsettled_by_kind"].items(),
                              key=lambda item: -item[1]):
        lines.append(f"| `{kind}` | {count} |")
    if not summary["unsettled_by_kind"]:
        lines.append("| — | 0 |")
    if summary["unsettled_reasons"]:
        lines += ["", "Most common reasons:", ""]
        for reason, count in sorted(summary["unsettled_reasons"].items(),
                                    key=lambda item: -item[1])[:6]:
            lines.append(f"- {count}× {reason}")
    lines += [
        "",
        "## Verdicts a mechanism can contradict",
        "",
        "Symbols the model called contract-unfaithful, unguarded, or wrongly",
        "documented, printed beside the number of test files that actually",
        "name them. A verdict of \"unguarded\" against a symbol several tests",
        "name is one a reader can see is wrong -- which is why both numbers",
        "sit in one table.",
        "",
        f"Total such verdicts: **{summary['contradiction_count']}**.",
        "",
        "| symbol | dimension | test files naming it | lines |",
        "|---|---|---|---|",
    ]
    for item in summary["verdicts_contradicting_the_dossier"][:60]:
        lines.append(f"| `{item['key']}` | `{item['dimension']}` | "
                     f"{item['test_files_mentioning']} | {item['lines']} |")
    if not summary["verdicts_contradicting_the_dossier"]:
        lines.append("| — | — | — | — |")
    lines.append("")
    return "\n".join(lines)


def load_checkpoint(path):
    """Read a checkpoint back, skipping a truncated final line.

    A run killed mid-write leaves a partial line, and a resume that refused
    everything because of it would throw away the whole paid-for run.
    """
    records = []
    path = pathlib.Path(path)
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def bucket_names():
    """The declared context buckets, by name, as the protocol declares them."""
    return tuple(name for name, _text in CONTEXT_BUCKETS)


def cost_of(tokens):
    """A run's spend for a token count, through the one declared price."""
    return estimate_call_cost(tokens,
                              price_per_million=JEV_INPUT_PRICE_PER_MILLION)


__all__ = [
    "DECLARED_INVARIANTS", "DIMENSIONS", "DIMENSION_NAMES",
    "DIMENSION_NEEDING_CONTEXT", "Dimension", "SUPPLIED_BUCKETS", "Symbol",
    "UNSUPPLIED_BUCKETS", "aggregate", "audit", "audit_symbol", "bucket_names",
    "build_dossier", "collect", "constraints_text", "cost_of",
    "load_checkpoint", "make_supplier", "need_band", "pack_for",
    "package_text", "render_report", "siblings", "symbols", "token_index",
    "UNANSWERABLE",
]
