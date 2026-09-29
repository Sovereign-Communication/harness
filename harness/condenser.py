"""Source extraction for condensed context (#PR-3).

Owns the extraction primitives -- structural AST signatures, the heuristic
declaration extractor for non-Python or unparseable sources, and error-log
condensation -- plus the backward-compatible `MicroBrief`/`distill_context`
path used by `agent.py` and `waist.py`. The evidence-bearing context brief has
its own owner in `harness.context_brief`, which builds on the
`extract_source_evidence` seam exposed here; brief schema, validation, and
rendering deliberately do not live in this module.
"""
import ast
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .tokens import estimate_prompt_tokens


@dataclass(frozen=True)
class MicroBrief:
    """A condensed architectural context packet."""
    summary: str
    file_signatures: Tuple[Tuple[str, str], ...] = ()
    condensed_errors: str = ""
    estimated_tokens: int = 0

    def to_prompt_context(self) -> str:
        """Format micro-brief into prompt context lines."""
        lines = []
        if self.summary:
            lines.append(f"CONTEXT SUMMARY:\n{self.summary.strip()}\n")
        if self.file_signatures:
            lines.append("CONDENSED FILE INTERFACES:")
            for path, sigs in self.file_signatures:
                lines.append(f"--- File: {path} ---")
                lines.append(sigs.strip())
            lines.append("")
        if self.condensed_errors:
            lines.append(f"CONDENSED ERROR / FAILURE TRACE:\n{self.condensed_errors.strip()}\n")
        return "\n".join(lines).strip()


def extract_python_signatures(source: str, focus_symbols: Optional[Sequence[str]] = None) -> str:
    """Extract class/function/type signatures from Python source via AST, pruning bodies."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback to line-based truncation
        return _heuristic_signatures(source)

    focus_set = set(focus_symbols or ())
    lines: List[str] = []

    # Extract module docstring
    doc = ast.get_docstring(tree)
    if doc:
        lines.append(f'"""{doc[:200]}..."""' if len(doc) > 200 else f'"""{doc}"""')

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            lines.append(f"\nclass {node.name}{base_str}:")
            class_doc = ast.get_docstring(node)
            if class_doc:
                lines.append(f'    """{class_doc[:150]}"""')
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def " if isinstance(item, ast.AsyncFunctionDef) else "def "
                    args = ast.unparse(item.args)
                    ret = f" -> {ast.unparse(item.returns)}" if item.returns else ""
                    if item.name in focus_set:
                        # Full function for focused symbols
                        lines.append("    # [FOCUS SYMBOL]")
                        lines.append(f"    {prefix}{item.name}({args}){ret}:")
                        fn_doc = ast.get_docstring(item)
                        if fn_doc:
                            lines.append(f'        """{fn_doc[:100]}"""')
                        lines.append("        ...")
                    else:
                        lines.append(f"    {prefix}{item.name}({args}){ret}: ...")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            args = ast.unparse(node.args)
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            if node.name in focus_set:
                lines.append(f"\n# [FOCUS SYMBOL]\n{prefix}{node.name}({args}){ret}:")
                fn_doc = ast.get_docstring(node)
                if fn_doc:
                    lines.append(f'    """{fn_doc[:100]}"""')
                lines.append("    ...")
            else:
                lines.append(f"{prefix}{node.name}({args}){ret}: ...")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                target_str = ast.unparse(target)
                # Keep uppercase constants
                if target_str.isupper():
                    lines.append(f"{target_str} = {ast.unparse(node.value)}")

    return "\n".join(lines).strip()


def _heuristic_signature_lines(source: str) -> List[str]:
    """Collect declaration-like lines for the heuristic signature extractor."""
    result = []
    for line in source.splitlines():
        trimmed = line.strip()
        if (trimmed.startswith(("def ", "class ", "async def ", "fn ", "pub fn ", "function ", "interface ")) or
                trimmed.startswith(("#", "//", "export "))):
            result.append(line)
    return result


def _heuristic_signatures(source: str) -> str:
    """Fallback line-based signature extractor for non-Python or unparseable source."""
    return "\n".join(_heuristic_signature_lines(source)[:80]).strip()


def _heuristic_signatures_with_status(source: str) -> Tuple[str, bool]:
    """Return heuristic signatures and whether the historical 80-line cap applied."""
    lines = _heuristic_signature_lines(source)
    return "\n".join(lines[:80]).strip(), len(lines) > 80


def extract_source_evidence(path: str, content: str,
                            focus_symbols: Optional[Sequence[str]] = None
                            ) -> Tuple[str, bool]:
    """Extract one source's retained text, plus whether an extractor cap cut it.

    Python sources go through the AST signature extractor, falling back to the
    heuristic line extractor when the source will not parse. The boolean
    reports the heuristic extractor's 80-line cap so a caller can label the
    result as truncated rather than silently complete. The truncation marker
    itself belongs to the brief artifact, not to extraction, so none is added
    here.
    """
    if not path.endswith(".py"):
        return _heuristic_signatures_with_status(content)
    try:
        ast.parse(content)
    except (RecursionError, SyntaxError, ValueError):
        return _heuristic_signatures_with_status(content)
    try:
        return extract_python_signatures(content, focus_symbols=focus_symbols), False
    except (RecursionError, ValueError):
        return _heuristic_signatures_with_status(content)


def condense_error_log(error_log: str, max_chars: int = 1200) -> str:
    """Condense verbose stack traces or test failures into essential error lines."""
    if not error_log or not error_log.strip():
        return ""

    lines = error_log.strip().splitlines()
    key_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        # Keep failure markers, exceptions, and assertion lines
        if any(marker in stripped for marker in (
            "FAIL:", "ERROR:", "AssertionError", "Exception", "Error:",
            "Traceback (most recent call last):", "FAILED (", "=== FAILURES ==="
        )) or stripped.startswith(("E   ", ">   ", "assert ")):
            key_lines.append(stripped)
        elif "File " in stripped and "line " in stripped:
            key_lines.append(stripped)

    condensed = "\n".join(key_lines) if key_lines else error_log.strip()
    if len(condensed) > max_chars:
        condensed = condensed[-max_chars:]
    return condensed


def distill_context(
    files: Dict[str, str],
    error_log: Optional[str] = None,
    max_tokens: int = 1500,
    focus_symbols: Optional[Sequence[str]] = None,
    summary: str = "",
) -> MicroBrief:
    """Distill source files and errors into a compact MicroBrief under max_tokens."""
    signatures: List[Tuple[str, str]] = []
    for path, content in sorted(files.items()):
        if path.endswith(".py"):
            sig = extract_python_signatures(content, focus_symbols=focus_symbols)
        else:
            sig = _heuristic_signatures(content)
        signatures.append((path, sig))

    condensed_err = condense_error_log(error_log or "")

    brief = MicroBrief(
        summary=summary,
        file_signatures=tuple(signatures),
        condensed_errors=condensed_err,
    )
    rendered = brief.to_prompt_context()
    tokens = estimate_prompt_tokens(rendered)

    # If exceeding max_tokens, prune signatures further
    if tokens > max_tokens and signatures:
        truncated_sigs = [
            (p, s[:int(len(s) * (max_tokens / max(tokens, 1)))])
            for p, s in signatures
        ]
        brief = MicroBrief(
            summary=summary,
            file_signatures=tuple(truncated_sigs),
            condensed_errors=condensed_err[:600],
        )
        rendered = brief.to_prompt_context()
        tokens = estimate_prompt_tokens(rendered)

    return MicroBrief(
        summary=brief.summary,
        file_signatures=brief.file_signatures,
        condensed_errors=brief.condensed_errors,
        estimated_tokens=tokens,
    )
