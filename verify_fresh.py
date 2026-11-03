"""Minimal fresh-state verification for the overnight lane.

The prior verify attempt called `gh branch/log/worktree` by mistake.
Use git for git facts, gh for PR/CI/issues."""
import subprocess
import json

def git(args):
    try:
        return subprocess.check_output(
            ["git"] + args, text=True, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        return "GIT_ERROR rc={}\n{}".format(e.returncode, (e.stdout or e.stderr or ""))

def gh(args):
    try:
        return subprocess.check_output(
            ["gh"] + args, text=True, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        return "GH_ERROR rc={}\n{}".format(e.returncode, (e.stdout or e.stderr or ""))

print("== git branch --show-current ==")
print(git(["branch", "--show-current"]))
print()
print("== git rev-parse HEAD ==")
print(git(["rev-parse", "HEAD"]))
print()
print("== git rev-parse main ==")
print(git(["rev-parse", "main"]))
print()
print("== git log main -5 --oneline ==")
print(git(["log", "main", "-5", "--oneline"]))
print()
print("== git worktree list ==")
print(git(["worktree", "list"]))
print()
print("== git status --short ==")
print(git(["status", "--short"]))
print()
print("== diff main..HEAD --name-only ==")
print(git(["diff", "main..HEAD", "--name-only"]))
print()
print("== gh pr view 431 (title, state, headRefName, headRefOid, baseRefName, isDraft, url) ==")
view = gh(["pr", "view", "431", "--json", "title,state,headRefName,headRefOid,baseRefName,isDraft,url"])
print(view)
print()
print("== gh pr checks 431 (CI) ==")
checks = gh(["pr", "checks", "431"])
print(checks)
print()
print("== open issue count ==")
issues = gh(["search", "issues", "--owner", "Sovereign-Communication",
             "--repo", "harness", "is:open", "type:issue",
             "--json", "number", "--limit", "500"])
try:
    nums = json.loads(issues)
    print("open issue count:", len(nums))
    print("numbers:", sorted(n["number"] for n in nums))
except Exception:
    print(issues)
