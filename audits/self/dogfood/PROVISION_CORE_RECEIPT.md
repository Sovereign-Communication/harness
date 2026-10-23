# PROVISION-CORE Dogfood & Surface Wiring Receipt — 2026-10-18

Verification receipt for the delivery of host provisioning surfaces (CLI, MCP, Server, and Agent faces) over the core `harness/provision.py` module.

## 1. CLI Execution (`harness provision`)

### A. Host Probe
Command: `python -m harness.cli provision probe --json`
Output:
```json
{
  "os": "Windows",
  "arch": "amd64",
  "python_version": "3.14.6",
  "python_executable": "C:\\Python314\\python.exe",
  "gpu_present": false,
  "gpus": [],
  "free_disk_bytes": 8134172672,
  "package_managers": [
    {
      "name": "choco",
      "path": "C:\\ProgramData\\chocolatey\\bin\\choco.EXE",
      "version": "2.7.3",
      "usable": true
    },
    {
      "name": "pip",
      "path": "C:\\Python314\\Scripts\\pip.EXE",
      "version": "pip 26.1.2 from C:\\Python314\\Lib\\site-packages\\pip (python 3.14)",
      "usable": true
    },
    {
      "name": "npm",
      "path": "C:\\Program Files\\nodejs\\npm.CMD",
      "version": null,
      "usable": false
    }
  ],
  "probed_path": "C:\\Users\\SCM\\Documents\\GitHub\\Harness-surfaces"
}
```

### B. Provisioning Plan
Command: `python -m harness.cli provision plan --goal "provision ruff" --json`
Digest: `6e87ec7940b2a61b402293a3a54936d81bb1c24c9cb55158887d4f6ea8c93033`
Recipe: `venv-pip` (deterministic, 4 steps: `mkdir-root`, `create-venv`, `pip-install`, `verify-1-ruff`).

### C. Gated Apply (Dry Run by Default)
Command: `python -m harness.cli provision apply --plan-file <plan.json> --json`
Outcome: `dry_run` (exit code 0; steps previewed with `approval: missing`; zero mutations executed).

## 2. MCP Tools (`host_provision_probe`, `host_provision_plan`)
- `host_provision_probe` (observe lane): returns read-only machine facts.
- `host_provision_plan` (spendy lane): returns typed `Plan` dict and rendered markdown text.
- Human-only approval path preserved: no mutating tool execution from MCP without explicit operator approval.

## 3. Server REST API (`/api/provision/*`)
- `GET /api/provision/probe`: returns 200 with probed host facts.
- `POST /api/provision/plan`: returns 200 with planned recipe, steps, and digest.

## 4. Agent Autonomous Intent
- Intent classifier recognizes `provision host`, `probe host`, `plan provision`, etc.
- `AutonomousAgent().run_prompt("plan provision for ruff")` safely produces planned setup without unapproved mutation.

## 5. Security & Containment
All wiring conditions from `docs/security.md` are strictly preserved:
- Dry-run default enforced.
- Approval path is human-only (never model- or MCP-grantable).
- Process execution confined strictly to `osal.run_tree`.
- Allowlist argv classification is the immutable gate.
