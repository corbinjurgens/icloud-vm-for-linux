# ============ guest-setup.ps1 — the elevated provisioning orchestrator ============
# Inspect, reconcile, verify (v2 plan D44 and section 4.2). One run is a
# desired-state reconciliation, never an unconditional replay of scripts 03
# and 04: components that are already `ok` are skipped, `missing` or enumerated
# `drifted` components invoke only their owning repair scope, and anything
# `blocked` or `unknown` stops the run before the first mutation.
#
# Runs INSIDE the Windows guest, elevated, launched ONLY by watcher.ps1 from the
# protected per-run directory it staged:
#
#   powershell -ExecutionPolicy Bypass -NoProfile ^
#     -File C:\ProgramData\icloud-bridge-provision\runs\<runId>\guest-setup.ps1 ^
#     -RunId <runId> [-ResetShareCredential]
#
# Idempotent. Re-triggering re-probes rather than trusting a previous phase, and
# every repair scope it dispatches is itself idempotent.
#
# It publishes progress to \\host.lan\Data\.provision\status.json, which is
# guest-writable and therefore explanatory only — the host validates it
# defensively and it never authorizes anything. In the other direction this
# script accepts no host-supplied phase list, path, command, script name,
# account name or share name: the only things that cross the elevation boundary
# are a run ID and a boolean, and the work plan is derived here from a fresh
# inspection through the fixed dispatch table in guest-state.ps1. There is no
# Invoke-Expression anywhere in this file, deliberately.
# =================================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunId,
    [switch]$ResetShareCredential
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'guest-state.ps1')

$SecretRemote = Join-Path $ProvisionInbox 'secret'
$SecretLocal  = Join-Path $PSScriptRoot 'secret'
$StatusPathOut = Join-Path $ProvisionStatusDir 'status.json'

$MaxLine          = 500
$HeartbeatSeconds = 25       # comfortably inside the host's 120 s stall check
$SignInPollSeconds = 15
$SecretPollSeconds = 5
$WingetAttempts   = 5
$WingetRetrySleep = 120
$WingetTimeout    = 600      # 10 min per attempt (section 4.1)
$ScriptTimeout    = 900

function Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }

# ------------------------------------------------------------ status writer --

$script:Phase  = 'staging'
$script:Detail = ''
$script:Checks = @{}
foreach ($id in $GuestCheckIds) { $script:Checks[$id] = 'pending' }
$script:Work = @()

function Get-BoundedLine {
    # Single physical line, control characters removed, capped at 500 (section
    # 4.1). Everything this script publishes goes through here.
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    $clean = ($Text -replace '[\x00-\x1F\x7F]', ' ').Trim()
    if ($clean.Length -gt $MaxLine) { $clean = $clean.Substring(0, $MaxLine) }
    return $clean
}

function ConvertTo-JsonString {
    param([string]$Value)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        switch ($ch) {
            '"'  { [void]$sb.Append('\"'); continue }
            '\'  { [void]$sb.Append('\\'); continue }
            default {
                if ([int]$ch -lt 0x20) { [void]$sb.AppendFormat('\u{0:x4}', [int]$ch) }
                else { [void]$sb.Append($ch) }
            }
        }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Write-JsonAtomic {
    # BOM-less UTF-8 + atomic replace, matching 04-bridge-agent.ps1 and the
    # agent's bridge writer (v2 plan section 2).
    param([string]$Path, [string]$Json)
    $enc = New-Object System.Text.UTF8Encoding($false)
    $dir = Split-Path -Parent $Path
    $tmp = Join-Path $dir ('.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tmp, $Json, $enc)
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($tmp, $Path, $null) }
        else { [IO.File]::Move($tmp, $Path) }
    } finally {
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
    }
}

function Write-Status {
    # The exact section 4.1 document. Hand-built rather than ConvertTo-Json so
    # the key set, the empty-array form and the escaping are pinned instead of
    # depending on PS 5.1's serializer quirks.
    param([string]$ErrorMessage)

    if ($GuestPhases -notcontains $script:Phase) { throw "refusing to publish unknown phase '$($script:Phase)'" }
    $checkJson = ($GuestCheckIds | ForEach-Object {
        $state = if ($script:Checks.ContainsKey($_)) { $script:Checks[$_] } else { 'pending' }
        if ($GuestCheckStates -notcontains $state) { throw "refusing to publish unknown state '$state'" }
        (ConvertTo-JsonString $_) + ':' + (ConvertTo-JsonString $state)
    }) -join ','
    $workJson = (@($script:Work) | Where-Object { $_ } | ForEach-Object {
        if ($GuestWorkIds -notcontains $_) { throw "refusing to publish unknown work id '$_'" }
        ConvertTo-JsonString $_
    }) -join ','
    $errJson = if ($ErrorMessage) { ConvertTo-JsonString (Get-BoundedLine $ErrorMessage) } else { 'null' }

    $json = '{"version":1,' +
            '"runId":' + (ConvertTo-JsonString $RunId) + ',' +
            '"phase":' + (ConvertTo-JsonString $script:Phase) + ',' +
            '"detail":' + (ConvertTo-JsonString (Get-BoundedLine $script:Detail)) + ',' +
            '"updatedAt":' + (ConvertTo-JsonString ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))) + ',' +
            '"error":' + $errJson + ',' +
            '"checks":{' + $checkJson + '},' +
            '"work":[' + $workJson + ']}'

    New-Item -ItemType Directory -Force -Path $ProvisionStatusDir -ErrorAction SilentlyContinue | Out-Null
    Write-JsonAtomic -Path $StatusPathOut -Json $json
}

function Set-Phase {
    param([Parameter(Mandatory)][string]$Phase, [string]$Detail = '')
    $script:Phase = $Phase
    $script:Detail = $Detail
    Step "$Phase$(if ($Detail) { " — $Detail" })"
    Write-Status
}

function Write-Heartbeat {
    # Refreshes updatedAt only. 120 s of frozen mtime is what tells the host a
    # phase has stalled, so it has to be meaningful (section 4.1).
    try { Write-Status } catch { Write-Warning "heartbeat failed: $($_.Exception.Message)" }
}

function Stop-WithError {
    param([Parameter(Mandatory)][string]$Message)
    $bounded = Get-BoundedLine $Message
    Write-Host "FAIL: $bounded" -ForegroundColor Red
    try { Write-Status -ErrorMessage $bounded } catch {
        Write-Warning "could not publish the error status: $($_.Exception.Message)"
    }
    exit 1
}

# ------------------------------------------------------------- child runner --

function Get-QuotedArgument {
    param([string]$Value)
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Invoke-GuestChild {
    # The one way this script runs anything: stdout/stderr to protected per-run
    # temporary files, a heartbeat while the child lives, an explicit exit-code
    # check, a bounded sanitized tail on failure, and the temporary files
    # deleted afterwards. A native non-zero exit is not a PowerShell terminating
    # error, which is exactly why this exists.
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory)][string]$What,
        [int]$TimeoutSeconds = 600
    )
    $stem = Join-Path $PSScriptRoot ('.child.' + [Guid]::NewGuid().ToString('N'))
    $outPath = "$stem.out"
    $errPath = "$stem.err"
    $argLine = (@($ArgumentList) | ForEach-Object { Get-QuotedArgument $_ }) -join ' '
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argLine -NoNewWindow -PassThru `
            -RedirectStandardOutput $outPath -RedirectStandardError $errPath
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        $lastBeat = Get-Date
        while (-not $proc.HasExited) {
            Start-Sleep -Seconds 2
            if (((Get-Date) - $lastBeat).TotalSeconds -ge $HeartbeatSeconds) {
                Write-Heartbeat
                $lastBeat = Get-Date
            }
            if ((Get-Date) -gt $deadline) {
                try { $proc.Kill() } catch { }
                throw "$What exceeded its ${TimeoutSeconds}s limit"
            }
        }
        $proc.WaitForExit()
        $rc = [int]$proc.ExitCode
        if ($rc -ne 0) { throw "$What failed (exit $rc): $(Get-ChildOutputTail $outPath $errPath)" }
    } finally {
        foreach ($p in @($outPath, $errPath)) {
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Get-ChildOutputTail {
    param([string]$OutPath, [string]$ErrPath)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($p in @($ErrPath, $OutPath)) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try {
            $tail = Get-Content -LiteralPath $p -Tail 5 -ErrorAction Stop
            foreach ($l in @($tail)) { if ("$l".Trim()) { $lines.Add((Get-BoundedLine $l)) } }
        } catch { }
    }
    if ($lines.Count -eq 0) { return 'no output captured' }
    return Get-BoundedLine (($lines | Select-Object -Last 3) -join ' | ')
}

# ------------------------------------------------------------- inspection ----

$script:Inspected = $null

function Update-Inspection {
    # The complete read-only checklist, used before the first mutation and again
    # in the verifying pass. It never mutates anything, so calling it twice is
    # free of consequence beyond the walk cost.
    $script:Inspected = Get-GuestChecklist -StagedDir $PSScriptRoot
    $script:Checks = ConvertTo-GuestCheckStateMap $script:Inspected
    return $script:Checks
}

function Get-BlockedDetail {
    param([string[]]$Ids)
    $parts = @()
    foreach ($id in @($Ids)) {
        $state = $script:Checks[$id]
        $detail = ''
        if ($null -ne $script:Inspected -and $script:Inspected.Contains($id)) {
            $detail = [string]$script:Inspected[$id].Detail
        }
        $parts += if ($detail) { "${id}=${state} ($detail)" } else { "${id}=${state}" }
    }
    return ($parts -join '; ')
}

# ============================ 1. validate the run ============================
# The watcher is the only caller, but validate its arguments anyway: this script
# is the elevation boundary's inside face.

if ($RunId -cnotmatch '^[0-9a-f]{32}$') {
    throw "run ID '$RunId' is not 32 lowercase hex characters"
}
$expectedRunDir = Join-Path $ProvisionRunsDir $RunId
if ([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -ne [IO.Path]::GetFullPath($expectedRunDir).TrimEnd('\')) {
    throw "refusing to run from $PSScriptRoot — the protected run directory for $RunId is $expectedRunDir"
}
foreach ($name in $ProvisionPayload) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name))) {
        throw "the protected run directory is incomplete: $name is missing"
    }
}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "guest-setup.ps1 must run elevated"
}
$reset = [bool]$ResetShareCredential

Set-Phase 'staging' 'payload staged in the protected run directory'

try {
    # ============================ 2. inspect =================================
    Set-Phase 'inspecting' 'evaluating the guest checklist'
    $checks = Update-Inspection
    $blocked = Get-GuestBlockedChecks $checks
    $plan = Get-GuestWorkPlan -Checks $checks -ResetShareCredential $reset
    $script:Work = $plan
    Write-Status

    if (@($blocked).Count -gt 0) {
        # Blocked or unknown is never treated as absence: publish the whole
        # checklist, preserve every byte of data, and stop.
        Stop-WithError ("stopping before any change: " + (Get-BlockedDetail $blocked))
    }
    $dispatch = Get-GuestRepairDispatch -Work $plan

    # ============================ 3. refresh the manual fallback =============
    # `current` is the protected bundle a diagnosed failure may tell the operator
    # to run by hand. Build a sibling, swap it, then prune, so a failure leaves
    # the previous complete bundle in place.
    Step "Refreshing the protected 'current' fallback bundle"
    $newDir = "$ProvisionCurrent.new"
    $oldDir = "$ProvisionCurrent.old"
    try {
        foreach ($d in @($newDir, $oldDir)) {
            if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force }
        }
        New-Item -ItemType Directory -Force -Path $newDir | Out-Null
        foreach ($name in $ProvisionPayload) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $newDir $name) -Force
            if (-not (Test-Path -LiteralPath (Join-Path $newDir $name))) { throw "$name did not copy" }
        }
        if (Test-Path -LiteralPath $ProvisionCurrent) {
            Move-Item -LiteralPath $ProvisionCurrent -Destination $oldDir -Force
            try { Move-Item -LiteralPath $newDir -Destination $ProvisionCurrent -Force }
            catch { Move-Item -LiteralPath $oldDir -Destination $ProvisionCurrent -Force; throw }
            Remove-Item -LiteralPath $oldDir -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Move-Item -LiteralPath $newDir -Destination $ProvisionCurrent -Force
        }
    } catch {
        Write-Warning "could not refresh $ProvisionCurrent : $($_.Exception.Message)"
    }
    # C:\OEM is refreshed for operator inspection only. It is never an execution
    # source and no failure message points at it (D42, amended D35).
    if (Test-Path -LiteralPath 'C:\OEM') {
        foreach ($name in $ProvisionPayload) {
            try { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path 'C:\OEM' $name) -Force }
            catch { Write-Warning "could not refresh C:\OEM\$name : $($_.Exception.Message)" }
        }
    }

    $performed = New-Object System.Collections.Generic.HashSet[string]

    # ============================ 4. install the iCloud client ===============
    if ($dispatch.InstallIcloud) {
        [void]$performed.Add('install-icloud')
        Set-Phase 'installing-icloud' 'installing the iCloud client from the Microsoft Store'
        $wingetArgs = @('install', '--id', $IcloudStoreId, '--source', 'msstore',
                        '--accept-package-agreements', '--accept-source-agreements')
        $installed = $false
        $lastError = ''
        for ($attempt = 1; $attempt -le $WingetAttempts -and -not $installed; $attempt++) {
            $script:Detail = "winget attempt $attempt of $WingetAttempts"
            Write-Status
            try {
                Invoke-GuestChild -FilePath 'winget.exe' -ArgumentList $wingetArgs `
                    -What 'winget install' -TimeoutSeconds $WingetTimeout
                $installed = $true
            } catch {
                # Store readiness is flaky at first boot (v1 D4, plan section 5).
                $lastError = $_.Exception.Message
                Write-Warning "winget attempt $attempt failed: $lastError"
                if ($attempt -lt $WingetAttempts) {
                    $waitUntil = (Get-Date).AddSeconds($WingetRetrySleep)
                    while ((Get-Date) -lt $waitUntil) { Start-Sleep -Seconds 10; Write-Heartbeat }
                }
            }
        }
        if (-not $installed) {
            Stop-WithError ("could not install the iCloud client after $WingetAttempts attempts " +
                            "($lastError). Install `"iCloud`" manually from the Microsoft Store in " +
                            "the VM, then run this again.")
        }
    }

    # ============================ 5. sign-in wait ============================
    if ($dispatch.WaitForSignin) {
        [void]$performed.Add('wait-for-signin')
        Set-Phase 'launching-icloud' 'starting the iCloud client for sign-in'
        try {
            # Launched through explorer.exe so the client runs in the ordinary
            # unelevated icloud session rather than inheriting this token
            # (docs/automation-notes.md section 3).
            Start-Process -FilePath 'explorer.exe' -ArgumentList $IcloudAppsFolder | Out-Null
        } catch {
            Write-Warning "could not launch the iCloud client: $($_.Exception.Message)"
        }
        Set-Phase 'waiting-for-signin' 'sign in with your Apple ID, leaving iCloud Drive and Files On-Demand ON'
        # No timeout: this is the operator's only guest interaction, and it may
        # legitimately take as long as it takes (section 4.1).
        while ((Resolve-SyncRootState (Read-SyncRootObservation)) -ne 'ok') {
            Start-Sleep -Seconds $SignInPollSeconds
            Write-Heartbeat
        }
    }

    if ($dispatch.InstallIcloud -or $dispatch.WaitForSignin) {
        # The VM may have changed while we waited, and the downstream probes were
        # 'pending' when the dependency was unmet, so re-derive from scratch.
        Set-Phase 'inspecting' 're-evaluating after the package and sign-in work'
        $checks = Update-Inspection
        $blocked = Get-GuestBlockedChecks $checks
        $plan = Get-GuestWorkPlan -Checks $checks -ResetShareCredential $reset
        $script:Work = $plan
        Write-Status
        if (@($blocked).Count -gt 0) {
            Stop-WithError ("stopping before any further change: " + (Get-BlockedDetail $blocked))
        }
        foreach ($w in @($plan)) {
            # At most one repair pass per component per run.
            if ($performed.Contains($w)) {
                Stop-WithError ("'$w' did not converge after its one repair pass; " +
                                "the checklist still reports it as outstanding")
            }
        }
        $dispatch = Get-GuestRepairDispatch -Work $plan
    }

    # ============================ 6. the secret ==============================
    if ($dispatch.NeedsSecret) {
        Set-Phase 'waiting-for-secret' 'waiting for the share password from the app'
        # Absence is normal after a GUI exit or restart: the app re-delivers.
        while (-not (Test-Path -LiteralPath $SecretRemote)) {
            Start-Sleep -Seconds $SecretPollSeconds
            Write-Heartbeat
        }
        # Copied as bytes, never decoded here: this script does not parse,
        # rewrite, log or publish the value (D41).
        [IO.File]::WriteAllBytes($SecretLocal, [IO.File]::ReadAllBytes($SecretRemote))
        if (-not (Test-Path -LiteralPath $SecretLocal)) { Stop-WithError 'the protected local copy could not be made' }
    }

    # ============================ 7. share account and data share ============
    if ($dispatch.RunShareScript) {
        Set-Phase 'creating-share' $(
            if ($dispatch.ShareMode -eq 'password-file') { 'creating the syncshare account and the data share' }
            else { 'repairing the data share, preserving the existing credential' })
        $shareScript = Join-Path $PSScriptRoot '03-create-share.ps1'
        $shareArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $shareScript)
        if ($dispatch.ShareMode -eq 'password-file') { $shareArgs += @('-PasswordFile', $SecretLocal) }
        else { $shareArgs += '-PreserveCredential' }
        foreach ($w in @('create-share-account', 'reset-share-credential', 'repair-data-share')) {
            if ($plan -contains $w) { [void]$performed.Add($w) }
        }
        Invoke-GuestChild -FilePath 'powershell.exe' -ArgumentList $shareArgs `
            -What '03-create-share.ps1' -TimeoutSeconds $ScriptTimeout
    }

    # ============================ 8. bridge boundary and agent ===============
    if ($dispatch.RunBridgeScript) {
        $bridgeScript = Join-Path $PSScriptRoot '04-bridge-agent.ps1'
        $phase = if ($dispatch.BridgeScope -eq 'Agent') { 'installing-agent' } else { 'installing-bridge-boundary' }
        Set-Phase $phase "running 04-bridge-agent.ps1 -Scope $($dispatch.BridgeScope)"
        foreach ($w in @('repair-bridge-boundary', 'update-agent')) {
            if ($plan -contains $w) { [void]$performed.Add($w) }
        }
        Invoke-GuestChild -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $bridgeScript,
                            '-Scope', $dispatch.BridgeScope) `
            -What "04-bridge-agent.ps1 -Scope $($dispatch.BridgeScope)" -TimeoutSeconds $ScriptTimeout
        if ($dispatch.BridgeScope -eq 'All') { Set-Phase 'installing-agent' 'agent installed by the All scope' }
    }

    # ============================ 9. verify ==================================
    # (The child runner that steps 4, 7 and 8 all go through is defined above;
    # step 9 in the plan's numbering is that helper, not a phase.)
    Set-Phase 'verifying' 're-evaluating the complete checklist'
    $checks = Update-Inspection
    $script:Work = @()
    Write-Status
    if (-not (Test-GuestChecklistConverged $checks)) {
        $residual = @()
        foreach ($id in $GuestCheckIds) {
            $want = if ($id -eq 'shareCredential') { 'unverifiable' } else { 'ok' }
            if ($checks[$id] -ne $want) { $residual += $id }
        }
        # Terminal and specific. No second automatic repair pass: the protected
        # fallback bundle in $ProvisionCurrent is what a human runs next.
        Stop-WithError ("provisioning did not converge: " + (Get-BlockedDetail $residual) +
                        ". The protected fallback bundle is $ProvisionCurrent")
    }

    Set-Phase 'done' $(
        if ($dispatch.NeedsSecret) { 'the share password was reset this run' }
        else { 'the existing share password was preserved' })
    Write-Host ""
    Write-Host "PASS: the guest checklist has converged" -ForegroundColor Green
    exit 0

} catch {
    Stop-WithError $_.Exception.Message
} finally {
    # The local secret dies with this process whatever happened, including a
    # failure to launch 03 at all (D41). 03 deletes it immediately after reading,
    # so this is normally a no-op.
    if (Test-Path -LiteralPath $SecretLocal) {
        Remove-Item -LiteralPath $SecretLocal -Force -ErrorAction SilentlyContinue
    }
}
# ===============================================
