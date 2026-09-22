# MCP server

Harness exposes a newline-delimited JSON-RPC MCP server through `harness-mcp`
or `python -m harness.mcp`.

## Security defaults

The server requires deliberate configuration for file edits:

- `mcp_allow_write` / `HARNESS_MCP_ALLOW_WRITE` enables writes globally;
- `mcp_allow_verify` / `HARNESS_MCP_ALLOW_VERIFY` enables verification gates
  globally;
- `mcp_allowed_roots` / `HARNESS_MCP_ALLOWED_ROOTS` is a comma-separated list
  of permitted filesystem roots;
- individual calls may provide `allow_write` and `allow_verify` confirmations.

Without an allowed root, `apply_edit` is refused. Without write authorization,
`apply_edit` is refused. Verification commands are also refused unless the
server or request authorizes them. `shell=False` is not a sandbox; see
[security](security.md).

Example configuration:

```json
{
  "mcp_allow_write": false,
  "mcp_allow_verify": false,
  "mcp_allowed_roots": "/home/me/work/project"
}
```

Use explicit per-request confirmation when possible. Restrict the root to a
specific disposable checkout rather than a home directory.

## Tools

The server supports panel verification, scoped apply, consent, deferral,
ledger status, participation reporting, spend status, and trust status.
Results include machine-readable structured content and an `isError` flag
for tool failures. Response frames correlate by request id, never by
position: lanes run concurrently, so a later request may answer first.

## Scheduling: lanes and deadlines

Tools run on three serial lanes -- `mutation` (`apply_edit`), `spendy`
(`panel_verify`, `offer_work`), and `observe` (everything else, including
unknown tool names before they fail validation). Each lane stays serial,
so a long apply or panel never head-of-line-blocks status queries, and
the engine is still driven from exactly one lane.

Every request also carries a cooperative deadline,
`mcp_tool_timeout` / `HARNESS_MCP_TOOL_TIMEOUT` (seconds, 60..7200,
default 1800), tripped through the same `cancel_check` as
`notifications/cancelled`: an uncancelled-but-overdue run stops at the
next poll point. In-flight POSTs and subprocesses still run to their own
timeouts -- the deadline bounds lane occupancy, not the transport.

The supported protocol version (`2025-06-18`) is advertised during `initialize`.
A client that requests a different version gets `2025-06-18` back (MCP version
negotiation) and decides whether to proceed; a non-string `protocolVersion` is
rejected with `-32602`. Current Claude Code (which requests `2025-11-25`)
connects this way. Notifications, including `initialize` and `tools/call`,
never receive response frames. Identified request IDs are rejected while
already in flight and remain reserved until their response has been serialized;
accepted requests drain after stdin reaches EOF. Cancellation notifications
only affect currently in-flight work and stop it cooperatively between governed
operations. Verification process cancellation should be validated on the
target platform before relying on it for hard interruption.
