# Security model

Harness treats model output as untrusted input. It may propose file content,
protocol markers, JSON, or verification-related prose, but the host policy
remains in Harness.

## Important limitation

Tokenization and `shell=False` prevent shell metacharacters from being
interpreted. They do **not** sandbox a verification command. An approved
executable still runs on the host with the Harness process's privileges and
may access files, the network, environment variables, and child processes.
Review every `verify_cmd`, use disposable checkouts for untrusted work, and do
not expose MCP write/execute access without deliberate configuration.

Since `PLAT-cmd-data` the tokenizer is `harness.gate_runner.split_command`
rather than bare POSIX `shlex`: POSIX shlex eats backslashes, so a Windows
path silently arrived at the OS as `C:Usersxgate.bat`. Path-shaped spans
(drive letters, UNC prefixes, quoted paths) are protected before tokenizing.
This is a correctness fix, not a permission change -- the "no shell" property
is unchanged and still no sandbox.

## UI server and driver routes

- **Authentication.** With a UI token configured, every `/api` route requires
  it in `X-Harness-Auth` or `Authorization: Bearer`, compared in constant time
  (`hmac.compare_digest`). A token in a URL query string is not accepted, and
  any `token=` value that reaches the request log is redacted. A refused
  request (401/403) drains a bounded request body and closes the connection, so
  a keep-alive client cannot smuggle a second request in the body of the first.
- **Desktop token.** `harness desktop` persists a generated token in
  `desktop_token` under the config directory, created owner-only (0600 on
  POSIX; on Windows rely on the profile ACL, as with key files above).
- **Driver token.** The in-process driver uses `DRIVER_TOKEN` if declared and
  otherwise a random per-start token held only by the adapter; there is no
  fixed fallback.
- **`verify` on `/api/driver/drive` (and the other run kinds).** The `verify`
  field is a gate command the *server process* runs on the host, with that
  process's privileges (see the limitation above). There is no server-side
  allowlist of permitted executables today: the gate is tokenized without a
  shell, preflighted where the run kind calls `validate_gate`, and bounded by a
  timeout, but a caller who can reach an authenticated `/api` route can run any
  program the server user can. Keep the UI token secret and the server on
  loopback. A server-side allowlist is an open hardening item.

## Platform-specific protections (and their limits)

| Control | Linux | macOS | Windows |
|---|---|---|---|
| Key-file permission warning | enforced (`mode & 077`) | enforced | **not modelled** |
| Allowed-roots containment | realpath, case-sensitive | realpath + case-normalized | realpath + case-normalized |
| UI loopback port reuse | `SO_REUSEADDR` kept | `SO_REUSEADDR` kept | `SO_REUSEADDR` **dropped** |
| Evidence line endings | LF | LF | LF (forced) |

- **Key files (`osal.keyfile_mode`).** A leaked OpenRouter key spends real
  money, so a group/world-readable key file is warned about loudly. Windows has
  no POSIX mode on a credential file -- the ACL is the real control and lives
  outside this process -- so the honest answer there is "not modelled", and
  Harness warns nothing rather than pretending a `0o600` exists. Windows users
  should rely on the file's ACL.
- **Desktop token file.** `harness-desktop` persists its token in
  `desktop_token` under the config directory. On POSIX it is created
  `O_EXCL` with mode `0600` as a temp sibling and moved over the target with
  `os.replace`, so a pre-existing wide-open (or attacker-pre-created) file is
  never written to, and a symlink at that path is refused. On Windows the mode
  bits are ignored: the file inherits the ACL of the config directory (normally
  per-user under `%APPDATA%`/the profile), which Harness does not inspect or
  tighten, and `os.replace` over a file another process holds open can fail
  (the token then simply is not persisted). Rely on the directory ACL there.
- **Allowed roots (`osal.is_within`).** macOS and Windows filesystems are
  case-insensitive by default, so a case-sensitive string comparison refuses
  a legitimate in-tree path (and, worse, could treat two spellings as two
  different roots). Roots and targets are compared as realpath + case-folded,
  which is what the filesystem itself does.
- **UI port reuse (`osal.HARDEN_REUSE`).** On Windows, `SO_REUSEADDR` lets a
  *second* process bind a port that is already being served, which would hand
  the UI -- and the auth token that authorizes `/api` writes -- to another
  process. The UI server therefore drops the flag on Windows and keeps it on
  POSIX, where it is needed to rebind after `TIME_WAIT`. The cost on Windows is
  that an immediate restart can report the port busy; that is the intended
  trade.
- **Line endings.** Every evidence write (ledger lines, JSON reports, the site
  bundle) uses an explicit `newline=""`/`"\n"`, so the same chain produces the
  same bytes on every platform. `.gitattributes` pins the working tree to LF
  for the same reason: a CRLF checkout must not be able to change what the
  parity hashes cover.

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
- All operating-system contact is confined to `harness/osal.py`; an enforced
  check (`tests/test_osal_boundary.py`) fails the build if any other module
  launches a process, probes the platform, or opens a browser directly.

## Host provisioning (`harness/provision.py`)

`provision.py` can plan and run package installs on the operator's machine, so
its safety rests on the way a surface is wired to it as much as on the module.
A surface (GUI card, MCP tool, agent intent) MUST keep these conditions:

1. **A human approves every mutating step, one by one.** The approval card
   shows each step's argv (`render_plan` escapes every field and prints argv
   as a JSON list), its class and its network use. Plan-level approval is
   convenience for READ and MUTATING steps only; an IRREVERSIBLE step always
   needs its own approval. Approving an install approves running that
   package's code in later steps (a venv interpreter loads its own
   site-packages; an installed tool is whatever its package says), and the UI
   must say so.
2. **Dry-run is the default.** Show the dry-run report first; only an explicit
   operator action sets `dry_run=False`.
3. **The approval path is human-only.** No parameter that decides consent may
   be settable by an agent, a model, or an MCP caller: not `approver`, `ask`,
   `ApprovalGate`, `runner`, `which`, `environ`, `done`, `policy`, `ledger`,
   nor a plan's `review_required`. These are in-process seams for tests and the
   host application; the wire format carries only a plan and a human's
   decision.
4. **The policy is built server-side** with `policy_for_probe(probe, roots)`
   from a probe the server ran itself. Approved roots are an operator setting.
5. **Plan JSON is untrusted.** Rebuild it with `plan_from_dict` and run
   `validate_plan` on every call, never reuse a previously validated object a
   client could have swapped. Approvals are bound to the plan digest, so an
   edited plan has no approval.
6. **Escape what you display.** A step's stdout/stderr and leftover names come
   from programs the plan ran; render them as inert text.
7. **Evidence is not optional.** A real run needs the harness's verified
   `AutonomyLedger`; if the ledger fails mid-run the report is still returned
   with `audit_failed=True` and the run stops. Surface `audit_failed`,
   `rollback_hints` and `leftovers` to the operator.

What the module enforces regardless: an argv allowlist (no shell, sudo,
deletion, formatting, registry edits, local-file or URL installs); every step
re-validated immediately before it runs; each step run in a fresh empty private
directory inside an approved root, with python isolated (`-I`), pip
`--isolated`, an allowlisted environment, stdin set to the null device and a
hard timeout that kills the process tree (`osal.run_tree`); programs
resolved to absolute PATH paths and refused inside the working tree; anything
that runs code from inside an approved root classed at least MUTATING;
`done=` honoured only for steps the ledger proves completed.

Known limits: a project `.npmrc` inside the `--prefix` directory is still read
by npm; a grandchild whose parent already exited cannot be found by
`taskkill /T` on Windows (the call still returns on time and abandons its
pipes); on Windows an npm that is only a `.cmd` shim is not used.

## Reporting

Report exploitable security issues privately through the repository's security
process rather than publishing working exploits in ordinary issues.
