---
name: isolated-request
description: Run one bounded extraction, scan, review, or implementation request in a fresh Codex subagent context, with explicit file scope and structured results. Read-only by default. Use when clean context or independent review materially helps.
---

# Isolated Request

Adapted from the user's Claude `/isolated-request`; see [provenance](references/provenance.md). Explicit invocation remains the default policy of the imported skill.

Use `collaboration.spawn_agent` with `fork_turns: "none"` for context isolation. Codex subagents share the workspace and tools: this is **not** filesystem, credential, or permission isolation. If a hard read-only sandbox is required, use an already verified runner providing that boundary or report the missing capability.

## Prepare one bounded request

Include the complete task, absolute working directory, applicable `AGENTS.md` paths, necessary facts, allowed files and actions, exclusions, success criteria, expected output schema, and stopping condition. Pass relevant raw evidence rather than the whole conversation. Do not include secrets. A read-only request prohibits edits and externally mutating commands in its instructions; it does not rely on an unavailable per-agent permission flag.

Honor these invocation options when supplied in the request:

- `--model MODEL`: choose an available Codex model. Without a forced model, use the cheapest capable available tier for mechanical extraction and bounded implementation; escalate only for demonstrated uncertainty or failure. Model aliases from Claude (`haiku`, `sonnet`, `opus`) describe intent, not valid Codex model IDs.
- `--write`: allow only already-authorized edits within the named files. Use a feature branch/worktree for repository implementation; audit and planning requests stay read-only unless local artifact creation is in scope.
- `--budget USD`: a ceiling for explicitly authorized paid API work, not a conversion of subscription usage into dollars. Native subagent tools do not enforce this dollar cap. Check/reserve any paid spend through the established Harness ledger before calling a provider; if no cap can be enforced, do no paid work.
- `--allow RULE`: describe the requested operation and check existing authorization; Claude permission-rule strings do not create Codex permissions.

Delegate only when the request can run independently alongside useful parent work. Otherwise execute the bounded request locally and say that context isolation was not used. Never create a user-owned Codex task merely to obtain a subagent.

## Run and collect

1. Spawn a named subagent with the complete scope and `fork_turns: "none"`. Set a model override only when permitted by the current task and tool instructions. Have it stop at the scope, round, or time limit and return evidence rather than a promise.
2. Do useful parent work while it runs, then collect the result. Do not close the parent turn while required child work is uncollected.
3. Check the reported files, commands, outcomes, and limitations against the request. Keep a failed or unobserved check distinct from a pass. Do not broaden permissions to repair a denial without authorization.
4. Return the result plus the actual model when known, execution mode, changed files, evidence, and usage if exposed. Use `null`/`unavailable` for unavailable tokens or dollar cost, never a fabricated zero. Subscription usage and paid Harness/API charges are separate.

Suggested receipt:

```json
{"ok":true,"result":{},"model":"actual model or unavailable","mode":"read-only instruction|scoped writes","context_isolation":"fresh subagent","permission_isolation":false,"files_changed":[],"evidence":[],"tokens":null,"cost_usd":null,"error":null}
```

The caller stores mission receipts when this request is a phase of `$isolated-mission`. A standalone request does not create a new operational plan or mission state owner.
