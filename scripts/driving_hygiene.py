"""Deterministic pull-request checks for the driving-hygiene CI job.

This module uses only the event payload, local Git data, and repository files.
It makes no network or model calls and never reads secret values.
"""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


_CLOSING_REFERENCE = re.compile(
    r"\b(?P<keyword>close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\b"
    r"\s*:?\s+(?P<reference>(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)\b",
    re.IGNORECASE,
)
_NEGATED_PREFIX = re.compile(
    r"\b(?:not|never|no|cannot|can't|won't|wouldn't|shouldn't|mustn't|"
    r"don't|doesn't|didn't|isn't|aren't|wasn't|weren't|without)\b"
    r"(?:\s+\w+){0,4}\s*$",
    re.IGNORECASE,
)
_PLAN_H1 = re.compile(r"^#\s+\S")
_PLAN_H2 = re.compile(r"^##\s+\S")
_HUNK_NEW_LINE = re.compile(r"\+(\d+)(?:,\d+)?")
_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_CREDENTIAL_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("private-key-header", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.IGNORECASE)),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("credential-assignment", re.compile(
        r"\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
        r"client[_-]?secret|secret|password|authorization)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_=-]{20,}",
        re.IGNORECASE,
    )),
)


@dataclass(frozen=True)
class Finding:
    check: str
    location: str
    message: str

    def __str__(self):
        return "[{}] {}: {}".format(self.check, self.location, self.message)


def _added_diff_lines(diff_text):
    """Yield (path, line number, text) for added lines, excluding headers."""
    path = "PR diff"
    line_number: Optional[int] = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            line_number = None
        elif line.startswith("@@"):
            match = _HUNK_NEW_LINE.search(line)
            line_number = int(match.group(1)) if match else None
        elif line.startswith("+") and not line.startswith("+++"):
            yield path, line_number or 1, line[1:]
            if line_number is not None:
                line_number += 1
        elif line.startswith(" ") and line_number is not None:
            line_number += 1


def _sources(pr_text, diff_text):
    yield "PR text", pr_text, None
    for path, line_number, text in _added_diff_lines(diff_text):
        yield path, text, line_number


def _is_negated(text, start):
    boundary = max(
        text.rfind("\n", 0, start),
        text.rfind(".", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
    )
    return _NEGATED_PREFIX.search(text[boundary + 1:start]) is not None


def _close_reference_findings(pr_text, diff_text, *, negated):
    findings = []
    for source, text, fixed_line in _sources(pr_text, diff_text):
        for match in _CLOSING_REFERENCE.finditer(text):
            is_negated = _is_negated(text, match.start())
            if is_negated != negated:
                continue
            line_number = (fixed_line if fixed_line is not None else
                           text.count("\n", 0, match.start()) + 1)
            location = "{}:{}".format(source, line_number)
            reference = match.group("reference")
            keyword = match.group("keyword")
            if negated:
                message = (
                    "negated '{}' reference to {} can still link or close an issue"
                    .format(keyword, reference))
                check = "negated-close-language"
            else:
                message = "unapproved '{}' reference to {}".format(
                    keyword, reference)
                check = "close-keyword"
            findings.append(Finding(check, location, message))
    return findings


def check_negated_close_language(pr_text, diff_text):
    """Find negated GitHub auto-close references in PR text and added diff."""
    return _close_reference_findings(pr_text, diff_text, negated=True)


def check_close_keywords(pr_text, diff_text):
    """Find positive GitHub auto-close references in PR text and added diff."""
    return _close_reference_findings(pr_text, diff_text, negated=False)


def check_credentials(pr_text, diff_text):
    """Find credential-like values without echoing matched material."""
    findings = []
    for source, text, fixed_line in _sources(pr_text, diff_text):
        for line_number, line in enumerate(text.splitlines(), 1):
            for name, pattern in _CREDENTIAL_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(
                        "credential", "{}:{}".format(
                            source, fixed_line or line_number),
                        "credential-like material matched {}".format(name)))
                    break
    return findings


def _markdown_headings(text):
    headings = []
    fence = None
    for line_number, line in enumerate(text.splitlines(), 1):
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        if _PLAN_H1.match(line):
            headings.append((1, line_number, line))
        elif _PLAN_H2.match(line):
            headings.append((2, line_number, line))
    return headings


def check_planning_structure(repo_root):
    """Check the established title/section shape of issue-plan Markdown."""
    root = Path(repo_root)
    plan_dir = root / "docs" / "issue-plans"
    files = sorted(plan_dir.glob("*.md"))
    findings = []
    if not files:
        return [Finding("planning-structure", str(plan_dir),
                        "no issue-plan Markdown files found")]

    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(Finding(
                "planning-structure", relative,
                "file must be readable as UTF-8"))
            continue

        lines = text.splitlines()
        first_content = next((line for line in lines if line.strip()), "")
        headings = _markdown_headings(text)
        h1s = [heading for heading in headings if heading[0] == 1]
        h2s = [heading for heading in headings if heading[0] == 2]

        if not _PLAN_H1.match(first_content):
            findings.append(Finding(
                "planning-structure", relative,
                "first non-empty line must be a level-one title"))
        if len(h1s) != 1:
            findings.append(Finding(
                "planning-structure", relative,
                "document must contain exactly one level-one title"))
        if len(h2s) < 2:
            findings.append(Finding(
                "planning-structure", relative,
                "document must contain at least two level-two sections"))
        if path.stem.isdigit() and h1s:
            issue_id = re.compile(r"#\s*{}\b".format(re.escape(path.stem)),
                                  re.IGNORECASE)
            if not issue_id.search(h1s[0][2]):
                findings.append(Finding(
                    "planning-structure", relative,
                    "title must include the issue number from its filename"))
    return findings


def _event_data(event_path, head_sha=None):
    try:
        with Path(event_path).open("r", encoding="utf-8") as source:
            event = json.load(source)
    except (OSError, ValueError) as error:
        raise ValueError("cannot read pull-request event payload: {}".format(error)) from error
    pull_request = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull_request, dict):
        raise ValueError("GitHub event payload does not contain a pull request")
    try:
        base_sha = pull_request["base"]["sha"]
        head_sha = head_sha if head_sha is not None else os.environ["GITHUB_SHA"]
        title = pull_request.get("title") or ""
        body = pull_request.get("body") or ""
    except (KeyError, TypeError) as error:
        raise ValueError("pull-request event is missing a required field") from error
    if not _SHA.fullmatch(base_sha) or not _SHA.fullmatch(head_sha):
        raise ValueError("pull-request base or merge SHA is malformed")
    return "{}\n{}".format(title, body), base_sha, head_sha


def _pull_request_diff(repo_root, base_sha, head_sha):
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-color", "--unified=0",
         "{}...{}".format(base_sha, head_sha), "--"],
        cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git diff failed"
        raise RuntimeError(detail)
    return result.stdout


def collect_findings(pr_text, diff_text, repo_root):
    return (
        check_negated_close_language(pr_text, diff_text)
        + check_close_keywords(pr_text, diff_text)
        + check_credentials(pr_text, diff_text)
        + check_planning_structure(repo_root)
    )


def main(repo_root=None, event_path=None, head_sha=None):
    repo_root = (Path(repo_root) if repo_root is not None else
                 Path(__file__).resolve().parents[1])
    event_path = (event_path if event_path is not None else
                  os.environ.get("GITHUB_EVENT_PATH"))
    if not event_path:
        print("FAIL driving-hygiene: GITHUB_EVENT_PATH is unavailable.",
              file=sys.stderr)
        return 2
    try:
        pr_text, base_sha, selected_head_sha = _event_data(event_path, head_sha)
        diff_text = _pull_request_diff(repo_root, base_sha, selected_head_sha)
    except (RuntimeError, ValueError) as error:
        print("FAIL driving-hygiene: {}".format(error), file=sys.stderr)
        return 2

    findings = collect_findings(pr_text, diff_text, repo_root)
    if findings:
        print("FAIL driving-hygiene: {} finding(s)".format(len(findings)))
        for finding in findings:
            print("- {}".format(finding))
        return 1
    print("PASS driving-hygiene: all four deterministic checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
