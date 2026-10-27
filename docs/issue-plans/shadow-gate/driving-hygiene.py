#!/usr/bin/env python3
"""driving-hygiene: deterministic shadow-gate checks (no model calls, no secrets).

Checks (all deterministic, stdlib only):
  1. negated-close: a PR-body line pairing a negation with a close/fix/resolve
     keyword plus an issue reference (the #409 trap that auto-closed #306).
  2. undeclared-close: any other close/fix/resolve keyword + #N in the PR body
     that is not on a declaration line (``Closes #123`` / ``Fixes #123`` /
     ``Resolves #123``) and not in an ``Allowed closes:`` line.
  3. credential-pattern: added diff lines assigning a quoted, non-placeholder,
     non-reference value to a secret-named variable.
  4. planning-structure: changed docs/issue-plans/*.md files (except README.md
     and CANONICAL.md) must have an ALL-CAPS-leaning header, a numbered step,
     and an end-state marker (``END-STATE`` or ``Done looks like``).

Usage:
  python3 driving-hygiene.py --body-file BODY --diff-file DIFF --changed-files LIST
Exit: 0 pass, 1 findings, 2 usage error. Findings print as ``FAIL: <check>: <detail>``.

SCOPE LIMIT (by design): this script NEVER judges voice, tone, or bias. Those
stay in the driving-agent loop (Lucas Gate v2). Any proposal to add judgment
here needs a fresh explicit decision from Lucas.
"""

import argparse
import re
import sys

NEGATION_RE = re.compile(
    r"\b(does not|doesn't|do not|don't|not|never|won't|will not|isn't|is not)\b",
    re.IGNORECASE,
)
KEYWORD_REF_RE = re.compile(
    r"\b(closes?|closed|fixes?|fixed|resolves?|resolved)\b\s+#(\d+)",
    re.IGNORECASE,
)
DECL_RE = re.compile(r"^\s*(closes?|fixes?|resolves?)\b.*#\d+", re.IGNORECASE)
ALLOWED_RE = re.compile(r"^\s*allowed closes\s*:\s*(.+)$", re.IGNORECASE)
CRED_RE = re.compile(
    r"\b(api[_-]?key|apikey|secret|token|passwd|password|private[_-]?key)\b"
    r"\s*[:=]\s*[\"']([^\"'\n]+)[\"']",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r"test|example|mock|fake|placeholder|xxx|dummy|changeme|sample",
    re.IGNORECASE,
)
REFERENCE_RE = re.compile(r"secrets\.|environ|os\.getenv|\$\{|^\$[A-Za-z_]")


def check_body(body_text):
    """Return findings for checks 1 and 2."""
    findings = []
    lines = body_text.splitlines()
    allowed = set()
    for line in lines:
        m = ALLOWED_RE.match(line)
        if m:
            allowed.update(re.findall(r"#(\d+)", m.group(1)))
    for i, line in enumerate(lines, start=1):
        if NEGATION_RE.search(line) and KEYWORD_REF_RE.search(line):
            findings.append(
                f"negated-close: line {i}: negation + close keyword + issue ref "
                f"(GitHub parses this as an auto-close): {line.strip()[:160]}"
            )
            continue
        if DECL_RE.match(line):
            continue
        for kw, num in KEYWORD_REF_RE.findall(line):
            if num in allowed:
                continue
            findings.append(
                f"undeclared-close: line {i}: '{kw} #{num}' is not on a "
                f"declaration line (Closes/Fixes/Resolves #N) and not in "
                f"'Allowed closes:'; convert to a declaration or remove it"
            )
    return findings


def check_diff(diff_text):
    """Return findings for check 3 (credential patterns on added lines)."""
    findings = []
    current_file = "unknown"
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current_file = line[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        m = CRED_RE.search(content)
        if not m:
            continue
        value = m.group(2)
        if PLACEHOLDER_RE.search(value) or REFERENCE_RE.search(value):
            continue
        findings.append(
            f"credential-pattern: {current_file}: secret-named assignment "
            f"with a non-placeholder value: {content.strip()[:160]}"
        )
    return findings


def _is_caps_header(line):
    text = line.lstrip("#").strip()
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    caps = sum(1 for c in letters if c.isupper())
    return caps / len(letters) >= 0.7


def check_plans(changed_files, read_file):
    """Return findings for check 4 (planning structure)."""
    findings = []
    for path in changed_files:
        path = path.strip()
        if not path:
            continue
        if not (path.startswith("docs/issue-plans/") and path.endswith(".md")):
            continue
        base = path.rsplit("/", 1)[-1]
        if base in ("README.md", "CANONICAL.md"):
            continue
        try:
            text = read_file(path)
        except OSError as exc:
            findings.append(f"planning-structure: {path}: unreadable ({exc})")
            continue
        lines = text.splitlines()
        has_header = any(
            line.startswith("#") and _is_caps_header(line) for line in lines
        )
        has_step = any(
            re.match(r"\s*\d+[.)]\s+\S", line)
            or re.match(r"\s*#{0,6}\s*STEP\s+\d+", line, re.IGNORECASE)
            for line in lines
        )
        has_end = any(
            "END-STATE" in line or "Done looks like" in line for line in lines
        )
        missing = [
            name
            for name, ok in (
                ("ALL-CAPS header", has_header),
                ("numbered step", has_step),
                ("end-state marker (END-STATE or 'Done looks like')", has_end),
            )
            if not ok
        ]
        if missing:
            findings.append(
                f"planning-structure: {path}: missing {', '.join(missing)}"
            )
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--changed-files", required=True)
    args = parser.parse_args(argv)
    try:
        with open(args.body_file, encoding="utf-8") as fh:
            body_text = fh.read()
        with open(args.diff_file, encoding="utf-8") as fh:
            diff_text = fh.read()
        with open(args.changed_files, encoding="utf-8") as fh:
            changed = fh.read().splitlines()
    except OSError as exc:
        print(f"ERROR: cannot read input: {exc}", file=sys.stderr)
        return 2

    def read_file(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    findings = []
    findings += check_body(body_text)
    findings += check_diff(diff_text)
    findings += check_plans(changed, read_file)
    if findings:
        for finding in findings:
            print(f"FAIL: {finding}")
        return 1
    print("PASS: driving-hygiene: no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
