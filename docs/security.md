# Security model

Harness treats model output as untrusted input. It may propose file content,
protocol markers, JSON, or verification-related prose, but the host policy
remains in Harness.

## Important limitation

`shlex` tokenization and `shell=False` prevent shell metacharacters from being
interpreted. They do **not** sandbox a verification command. An approved
executable still runs on the host with the Harness process's privileges and
may access files, the network, environment variables, and child processes.
Review every `verify_cmd`, use disposable checkouts for untrusted work, and do
not expose MCP write/execute access without deliberate configuration.

## Controls

- Model requests never include a `tools` key.
- Network calls pass through the spend governor before dispatch and record
  provider-reported cost afterward.
- Apply targets are regular files; writes are atomic and refuse symlink targets.
- Failed gated edits are rewound to the pre-run content.
- Continuations bind the saved verification command and target baseline.
- MCP writes require explicit write authorization and configured allowed roots.
- MCP verification commands require server or request authorization.
- MCP tools run on serial mutation/spendy/observe lanes with a cooperative
  per-tool deadline, so one long run cannot starve status queries forever.
- Mutation requires earned trust: bipolar scores per host/model/author refuse,
  force preview-only, or ration ceilings; every denial is ledgered evidence.
- The ledger is hash-chained and reports corruption, but filesystem access can
  still rewrite or delete it; it is tamper-evident, not tamper-proof.
  Rotation anchors each segment boundary, and a pruned prefix reports as an
  explicit cut, never as a complete chain.
- Backups, snapshots, and atomic writes refuse planted symlinks; sandbox
  paths resolve parent symlinks before containment checks.

## Reporting

Report exploitable security issues privately through the repository's security
process rather than publishing working exploits in ordinary issues.
