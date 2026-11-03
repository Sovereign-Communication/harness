[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repository = 'Sovereign-Communication/harness'

if ($DryRun -and $Apply) {
    throw 'Choose either -DryRun or -Apply, not both.'
}

# Dry-run is the safe default. Posting requires the explicit -Apply switch.
if (-not $Apply) {
    $DryRun = $true
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI (gh) was not found on PATH. Install it before running this script.'
}

function Invoke-Gh {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    # Windows PowerShell 5.1 treats native stderr as an error record when the
    # global preference is Stop. Capture gh output locally, then restore the
    # caller's preference and report the CLI's own exit code and message.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & gh @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0) {
        $details = ($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        $message = 'GitHub CLI command failed with exit code ' + $exitCode + ': gh ' + ($Arguments -join ' ')
        if (-not [string]::IsNullOrWhiteSpace($details)) {
            $message += [Environment]::NewLine + $details.Trim()
        }
        throw $message
    }

    return $output
}

$comments = @(
    [pscustomobject]@{
        Issue = 418
        Title = '[desktop] Preserve and recover long-running requests after restarts'
        Body = @'
## Follow-up evidence  -  2026-10-27 session audit

The latest checkpoint for the long research request is labeled running, but its candidate is refused, planning is deferred with no_evidence_cited, context/execution/verification remain pending, and no execution steps completed. The restart guard says there is no resumable stage runner and consent must be renewed before dispatch. The earlier matching checkpoint is failed and explicitly non-resumable.

This confirms that preserving a prompt/checkpoint does not by itself make the work resumable. Recovery should show the refusal/stale-consent state honestly, preserve the prompt, and offer a fresh consent-bound restart. The empty live run list is captured separately in #419.
'@
    }
    [pscustomobject]@{
        Issue = 419
        Title = '[desktop] Separate local server health from backend and request status'
        Body = @'
## Follow-up evidence  -  status and diagnostic coverage (2026-10-27)

The desktop API returned status=ok with run_count=0, while the latest saved checkpoint still said running even though its candidate was refused and execution had not started.

The pasted provider trace was not present in local chat_history or orchestrator-stderr.log, and the ledger retained only a subset of model outcomes. Explicit nonzero ledger cost rows total $0.03076174, but that is incomplete rather than the full session cost. Diagnostics should show the last confirmed checkpoint time and request ID, distinguish API health from request state, and label trace/cost coverage as partial when the raw event stream is unavailable.
'@
    }
    [pscustomobject]@{
        Issue = 342
        Title = '[research] Implement the missing shared model-proposed ResearchPlan and native Jev review lifecycle'
        Body = @'
## Follow-up reproduction  -  research request routing (2026-10-27)

The audited request was classified as edit, had no target files, and was condensed to an approximately 141-token brief. The heuristic plan nevertheless proposed docs/reports/... outputs. The latest checkpoint then deferred with no_evidence_cited; its context recorded zero declared sources and zero citation windows. The pasted trace repeatedly handed off because the current lane lacked live web retrieval.

This is direct session evidence for the missing shared research lifecycle described here. Acceptance should include preserving research intent through condensation, checking retrieval capability before expensive synthesis, and stopping with a clear evidence-blocked result until source retrieval and claim-to-source verification have run. No report paths should be represented as completed outputs before execution.
'@
    }
    [pscustomobject]@{
        Issue = 280
        Title = '[efficiency] Calibration-driven first-rung selection + earlier rotation'
        Body = @'
## Follow-up evidence  -  repeated structured-planner failures (2026-10-27)

The ledger records six planner cycles with 18 unparseable verdicts across Qwen 3.8 Max, GLM 5.3 Flash, and DeepSeek V4 Pro. GPT-4.1 produced five amended verdicts with $0.023333 tracked cost, yet the plan still deferred for missing evidence. The visible calls were attempt 1/4, so the repeated work came from orchestration rounds rather than visible transport retries. One GLM call took 327.9 seconds and hit the token cap.

This real task trace supports a bounded per-task repair/retry budget, early rotation or deferral after a structured-output failure, and explicit logging of the failure class so repeated invalid plans cannot consume long rounds without new evidence.
'@
    }
    [pscustomobject]@{
        Issue = 319
        Title = ('route(models): automated model intake pipeline ' + [char]0x2014 + ' all OpenRouter models auto-screened into pools')
        Body = @'
## Follow-up catalog observation  -  2026-10-27

The local capabilities.json snapshot was fetched October 23 and contained 468 model IDs. A live OpenRouter text-catalog request on October 27 returned 464 entries. openai/gpt-6.1-terra and moonshotai/kimi-k3.1-pro were present live but absent from the local snapshot; both advertise response_format, structured_outputs, and tools. This is a discovery/probation signal, not evidence that either model performs better.

The heuristic plan also named openai/gpt-5.6-luna while the active escalation pool names openai/gpt-5.6-sol; both IDs appear in the local and live catalogs. Please refresh the capability inventory and validate planned model IDs against the active screened pool. This concerns capability discovery and does not contradict the separate routing-time pricing TTL noted above.

Catalog source: https://openrouter.ai/api/v1/models?output_modalities=text&sort=newest
'@
    }
)

Write-Host "Mode: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"
Write-Host "Repository: $Repository"

# Authentication and repository checks are read-only in both modes.
try {
    $null = Invoke-Gh -Arguments @('auth', 'status', '--hostname', 'github.com')
} catch {
    throw ('GitHub CLI authentication preflight failed. Authenticate with gh auth login, then retry. Details: ' + $_.Exception.Message)
}

$repoName = ((Invoke-Gh -Arguments @('repo', 'view', $Repository, '--json', 'nameWithOwner', '--jq', '.nameWithOwner')) -join '').Trim()
if ($repoName -ne $Repository) {
    throw "Repository preflight returned '$repoName'; expected '$Repository'."
}

# Preflight every target and build the full posting plan before any write.
$plan = foreach ($comment in $comments) {
    $issueJson = (Invoke-Gh -Arguments @('issue', 'view', [string]$comment.Issue, '--repo', $Repository, '--json', 'number,title,state')) -join [Environment]::NewLine
    $issueInfo = $issueJson | ConvertFrom-Json
    if (-not [string]::Equals([string]$issueInfo.title, $comment.Title, [StringComparison]::Ordinal)) {
        throw "Issue #$($comment.Issue) title changed; expected '$($comment.Title)'. No comments were posted."
    }
    if (-not [string]::Equals([string]$issueInfo.state, 'OPEN', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Issue #$($comment.Issue) is not open; no comments were posted."
    }

    $canonicalBody = [System.Text.RegularExpressions.Regex]::Replace(
        $comment.Body.Trim(),
        "\r\n?",
        [string][char]10
    )
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonicalBody)
        $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }

    $marker = "<!-- freebuff-session-audit:v1:issue=$($comment.Issue):sha256=$digest -->"
    $postedBody = $canonicalBody + [Environment]::NewLine + [Environment]::NewLine + $marker
    $endpoint = "repos/$Repository/issues/$($comment.Issue)/comments"
    # Keep the jq expression free of embedded quotes for Windows PowerShell's
    # native argument passing; scan the returned bodies for our exact marker.
    $existingBodies = Invoke-Gh -Arguments @('api', $endpoint, '--paginate', '--jq', '.[].body')
    $existingBodyText = ($existingBodies | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    $duplicateCount = [System.Text.RegularExpressions.Regex]::Matches(
        $existingBodyText,
        [System.Text.RegularExpressions.Regex]::Escape($marker)
    ).Count

    [pscustomobject]@{
        Issue = $comment.Issue
        Title = $issueInfo.title
        Body = $postedBody
        Marker = $marker
        DuplicateCount = $duplicateCount
    }
}

if ($DryRun) {
    foreach ($item in $plan) {
        if ($item.DuplicateCount -gt 0) {
            Write-Host "SKIP #$($item.Issue): matching marker already exists."
            continue
        }

        Write-Host "`n--- DRY RUN: #$($item.Issue)  -  $($item.Title) ---"
        Write-Host $item.Body
    }

    Write-Host "`nDry run complete. No GitHub comments were posted. Use -Apply to post the comments."
    return
}

foreach ($item in $plan) {
    if ($item.DuplicateCount -gt 0) {
        Write-Host "SKIP #$($item.Issue): matching marker already exists."
        continue
    }

    $tempPath = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tempPath, $item.Body, [System.Text.UTF8Encoding]::new($false))
        $result = Invoke-Gh -Arguments @('issue', 'comment', [string]$item.Issue, '--repo', $Repository, '--body-file', $tempPath)
        Write-Host "POSTED #$($item.Issue): $($item.Title)"
        foreach ($line in $result) {
            Write-Host $line
        }
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host 'Posting pass complete.'
