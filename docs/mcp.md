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
ledger status, participation reporting, and spend status. Results include
machine-readable structured content and an `isError` flag for tool failures.

The supported protocol version is advertised during `initialize`; unsupported
versions are rejected. Notifications, including `initialize` and `tools/call`,
never receive response frames. Identified request IDs are rejected while
already in flight and remain reserved until their response has been serialized;
accepted requests drain after stdin reaches EOF. Cancellation notifications
only affect currently in-flight work and stop it cooperatively between governed
operations. Verification process cancellation should be validated on the
target platform before relying on it for hard interruption.
