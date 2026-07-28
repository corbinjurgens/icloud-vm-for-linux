# ============ watcher.ps1 - the elevated provisioning watcher ============
# The only elevated code that reacts to the host. It polls the read-only
# \\host.lan\Provision inbox for a trigger, copies the fixed payload allowlist
# into an administrator-only per-run directory, and runs that protected copy of
# guest-setup.ps1 (v2 plan D40/D42).
#
# Runs INSIDE the Windows guest. Two modes:
#
#   Install (elevated, once per VM - provision/install.bat does this at OEM time,
#   or the operator pastes it by hand on a VM created before this feature):
#     powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\watcher.ps1 -Install
#     powershell -ExecutionPolicy Bypass -NoProfile -File \\host.lan\Provision\watcher.ps1 -Install
#
#   Poll loop (what the registered task runs; not invoked by hand):
#     powershell -ExecutionPolicy Bypass -NoProfile -File C:\ProgramData\icloud-bridge-provision\watcher.ps1
#
# Idempotent. -Install re-registers with -Force and re-hardens the directory;
# the loop consumes each run ID exactly once through a local accepted-run marker
# and re-triggering an already-converged VM is a no-op reconciliation. The loop
# also reconciles the account's console delegation (D51) and exits once, so the
# keep-alive brings it back with a console that is genuinely hidden.
#
# Two rules this file exists to enforce, neither of which may be relaxed:
#
#   1. Nothing under \\host.lan\Data or C:\OEM is ever an execution source.
#      dockur serves Data writable, guest-only, force user = root, so any process
#      in the guest can replace a script staged there; executing it elevated
#      would turn the deliberately limited D28 agent into an administrator.
#      Only the protected copy under C:\ProgramData\icloud-bridge-provision runs.
#   2. The trigger carries a version, an action, a UUID and a boolean - never a
#      path, a command, a script name, or a work list. The orchestrator derives
#      the work from its own inspection.
#
# This file deliberately restates its handful of constants instead of
# dot-sourcing guest-state.ps1: only watcher.ps1 is installed into the protected
# directory, so its envelope has to stand alone. D42 pins that envelope -
# changing it requires re-running the bootstrap, because a running old watcher
# cannot safely upgrade the protocol that authenticates its own replacement.
# =========================================================================

[CmdletBinding()]
param([switch]$Install)

$ErrorActionPreference = 'Stop'

# --- the stable envelope (D42): fixed names, fixed version, nothing derived ---
$ProvisionDir     = 'C:\ProgramData\icloud-bridge-provision'
$RunsDir          = Join-Path $ProvisionDir 'runs'
$InstalledWatcher = Join-Path $ProvisionDir 'watcher.ps1'
$AcceptedPath     = Join-Path $ProvisionDir 'accepted-run.txt'
$TaskName         = 'icloud-bridge-provision'
$AgentUser        = 'icloud'
$AdminsSid        = 'S-1-5-32-544'      # BUILTIN\Administrators, never the localized name
$SystemSid        = 'S-1-5-18'

$Inbox        = '\\host.lan\Provision'
$TriggerPath  = Join-Path $Inbox 'trigger.json'
$StatusDir    = '\\host.lan\Data\.provision'
$StatusPath   = Join-Path $StatusDir 'status.json'
$BeaconPath   = Join-Path $StatusDir 'watcher.json'
$AgentBuild   = 9

$PayloadFiles = @(
    '03-create-share.ps1',
    '04-bridge-agent.ps1',
    'agent.ps1',
    'guest-state.ps1',
    'guest-setup.ps1',
    'watcher.ps1'
)
$CheckIds = @('icloudPackage', 'syncRoot', 'shareAccount', 'shareCredential',
              'dataShare', 'bridgeBoundary', 'agentInstall', 'agentRuntime')

$PollSeconds    = 30
$MaxTriggerSize = 65536
$MaxLine        = 500

# Console delegation (D51). Windows 11 hands every new console to Windows
# Terminal, which does not honor the task action's -WindowStyle Hidden; conhost
# does. Both values are REG_SZ under the per-user Console\%%Startup key.
$ConhostGuid   = '{B23D10C0-E52E-411E-9D5B-C09FDF709C7D}'
$DelegationKey = 'Console\%%Startup'

function Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }

# ---------------------------------------------------------------- helpers ----

function Get-BoundedLine {
    # One physical line, control characters removed, capped. Every string this
    # script puts into status.json goes through here.
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
            '"'  { [void]$sb.Append('\"');  continue }
            '\'  { [void]$sb.Append('\\');  continue }
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
    # BOM-less UTF-8 + atomic replace, matching Write-JsonAtomic in
    # 04-bridge-agent.ps1 and the agent's bridge writer (v2 plan section 2).
    param([string]$Path, [string]$Json)
    $enc = New-Object System.Text.UTF8Encoding($false)
    $dir = Split-Path -Parent $Path
    $tmp = Join-Path $dir ('.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tmp, $Json, $enc)
        if (-not [IO.File]::Exists($Path)) { [IO.File]::Move($tmp, $Path) }
        # [NullString]::Value, never $null: PowerShell marshals $null to the
        # empty string when binding a [string] parameter, and File.Replace
        # rejects "" as a backup path with "The path is not of a legal form" -
        # on every destination, local or UNC. Passing $null made every write
        # after the first one (which takes the Move branch) throw.
        else { [IO.File]::Replace($tmp, $Path, [NullString]::Value) }
    } finally {
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
    }
}

function Write-WatcherBeacon {
    # The outbox is guest-writable, so this is only a liveness hint to the host,
    # never an authority. Keep its failure out of both installation and polling.
    try {
        New-Item -ItemType Directory -Force -Path $StatusDir -ErrorAction Stop | Out-Null
        $json = '{"version":1,' +
                '"taskName":' + (ConvertTo-JsonString $TaskName) + ',' +
                '"agentBuild":' + $AgentBuild + ',' +
                '"registeredAt":' + (ConvertTo-JsonString ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))) +
                '}'
        Write-JsonAtomic -Path $BeaconPath -Json $json
    } catch { }
}

function Write-WatcherError {
    # A watcher-level failure still owes the host a complete, schema-valid
    # document: the GUI treats a malformed one as unreadable, not as progress.
    param([string]$RunId, [string]$Message)
    try {
        New-Item -ItemType Directory -Force -Path $StatusDir -ErrorAction SilentlyContinue | Out-Null
        $checks = ($CheckIds | ForEach-Object { (ConvertTo-JsonString $_) + ':"pending"' }) -join ','
        $json = '{"version":1,' +
                '"runId":' + (ConvertTo-JsonString $RunId) + ',' +
                '"phase":"staging",' +
                '"detail":"the watcher could not start this run",' +
                '"updatedAt":' + (ConvertTo-JsonString ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))) + ',' +
                '"error":' + (ConvertTo-JsonString (Get-BoundedLine $Message)) + ',' +
                '"checks":{' + $checks + '},' +
                '"work":[]}'
        Write-JsonAtomic -Path $StatusPath -Json $json
    } catch {
        Write-Warning "could not write the error status: $($_.Exception.Message)"
    }
}

function Test-RunId {
    param([object]$Value)
    return ($Value -is [string] -and $Value -cmatch '^[0-9a-f]{32}$')
}

function Get-AcceptedToken {
    if (-not (Test-Path -LiteralPath $AcceptedPath)) { return '' }
    try { return (Get-Content -LiteralPath $AcceptedPath -Raw -ErrorAction Stop).Trim() } catch { return '' }
}

function Set-AcceptedToken {
    param([string]$Token)
    $enc = New-Object System.Text.UTF8Encoding($false)
    $tmp = Join-Path $ProvisionDir ('.accepted-run.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tmp, $Token, $enc)
        # [NullString]::Value, never $null - see Write-JsonAtomic above.
        if ([IO.File]::Exists($AcceptedPath)) { [IO.File]::Replace($tmp, $AcceptedPath, [NullString]::Value) }
        else { [IO.File]::Move($tmp, $AcceptedPath) }
    } finally {
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
    }
}

function Remove-StaleSecret {
    # A guest reboot between delivery and 03 consuming the value would strand a
    # protected local copy. Clear them at task start and before each new run;
    # never touch the run currently executing (the loop is synchronous, so no
    # run is active here).
    foreach ($p in @(Join-Path $ProvisionDir 'secret')) {
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $RunsDir) {
        Get-ChildItem -LiteralPath $RunsDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $s = Join-Path $_.FullName 'secret'
            if (Test-Path -LiteralPath $s) { Remove-Item -LiteralPath $s -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Remove-SupersededRun {
    # Keep the run just executed; prune the rest. Never touch 'current', which is
    # the protected manual fallback bundle.
    param([string]$KeepRunId)
    if (-not (Test-Path -LiteralPath $RunsDir)) { return }
    Get-ChildItem -LiteralPath $RunsDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Name -eq $KeepRunId) { return }
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------- hidden console ---

function Set-ConsoleDelegation {
    # Point one user's console delegation at conhost.exe, which honors
    # -WindowStyle Hidden (D51). Windows Terminal does not, so the watcher's
    # console rendered forever and measured a constant ~6.3% of one core in a
    # GPU-less VM - the largest single idle consumer, and a window a user could
    # close to kill the watcher.
    #
    # $RootPath is a registry provider path for the hive to write ('...\CURRENT_USER'
    # for this process's own, '...\USERS\<sid>' for another loaded profile).
    # Returns $true when it actually changed something, so a caller can read
    # back by calling again.
    param([string]$RootPath)
    # Plain concatenation, not Join-Path: this is a provider path, not a
    # filesystem one, and the key does not exist yet on a fresh profile.
    $key = $RootPath + '\' + $DelegationKey
    $changed = $false
    if (-not (Test-Path -LiteralPath $key)) {
        New-Item -Path $key -Force | Out-Null
        $changed = $true
    }
    foreach ($name in @('DelegationConsole', 'DelegationTerminal')) {
        $current = $null
        try { $current = (Get-ItemProperty -LiteralPath $key -Name $name -ErrorAction Stop).$name }
        catch { $current = $null }
        if ("$current" -ne $ConhostGuid) {
            Set-ItemProperty -LiteralPath $key -Name $name -Value $ConhostGuid -Type String
            $changed = $true
        }
    }
    return $changed
}

function Confirm-HiddenConsole {
    # Applied by the loop, not only by -Install: the delegation is a per-user
    # setting and the loop is the only part of this file that provably runs as
    # $AgentUser, so this is what reaches a guest built before D51 and a fresh
    # guest whose profile did not exist at OEM time. Delegation is read at
    # console creation, so the fix lands on the NEXT start - reported here as
    # $true so the caller can exit into the keep-alive, exactly as a refreshed
    # watcher copy does.
    try {
        if (-not (Set-ConsoleDelegation -RootPath 'Registry::HKEY_CURRENT_USER')) { return $false }
        # Read back before acting on it. An exit that did not actually fix the
        # console would repeat every minute forever, which is worse than the
        # window it is trying to remove.
        if (Set-ConsoleDelegation -RootPath 'Registry::HKEY_CURRENT_USER') {
            Write-Warning "the console delegation did not persist; leaving this window as it is"
            return $false
        }
        return $true
    } catch {
        Write-Warning "could not point the console delegation at conhost: $($_.Exception.Message)"
        return $false
    }
}

# ------------------------------------------------------- liveness envelope ---

function New-KeepAliveTrigger {
    # What actually keeps the watcher alive. Not RestartCount: Task Scheduler's
    # restart-on-failure does not fire when the action itself exits non-zero -
    # measured in the guest, where a task exiting 3 with RestartCount 3 and a
    # one-minute interval was never relaunched in three minutes. A repetition
    # trigger does fire: with MultipleInstances IgnoreNew it is a no-op while the
    # watcher is running, and it starts the watcher again within a minute of any
    # exit - a crash, a kill, or the deliberate exit below. Measured the same
    # way: relaunches at 09:56:03, 09:57:03, 09:58:02 after clean exits.
    #
    # The repetition is dated from midnight today so it is already inside its
    # window when registered, and bounded by a decade rather than an infinite
    # duration, which Task Scheduler rejects through this cmdlet.
    New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

function Get-WatcherDigest {
    # Identity of the file this process was started from, so a refresh of the
    # installed copy is detectable from inside the loop.
    try { return (Get-FileHash -LiteralPath $InstalledWatcher -Algorithm SHA256).Hash }
    catch { return '' }
}

function Confirm-KeepAlive {
    # A guest registered before the keep-alive existed would otherwise never get
    # it: the task definition is written only by -Install, and the operator has
    # no reason to re-run it while the watcher looks fine. The watcher is
    # already elevated, and this adds the missing trigger to its own task
    # without disturbing the running instance. A failure here is a warning: the
    # watcher still works, it just is not self-restarting yet.
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        foreach ($t in @($task.Triggers)) {
            if ($null -ne $t.Repetition -and -not [string]::IsNullOrEmpty($t.Repetition.Interval)) { return }
        }
        Step "Adding the keep-alive repetition to the '$TaskName' task"
        Set-ScheduledTask -TaskName $TaskName `
            -Trigger (@($task.Triggers) + (New-KeepAliveTrigger)) | Out-Null
    } catch {
        Write-Warning "could not add the keep-alive repetition: $($_.Exception.Message)"
    }
}

# ------------------------------------------------------------- install mode --

function Install-Watcher {
    Step "Installing the '$TaskName' watcher"

    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "run this from an elevated PowerShell (Administrator)"
    }

    $user = Get-LocalUser -Name $AgentUser -ErrorAction SilentlyContinue
    if ($null -eq $user) { throw "local account '$AgentUser' does not exist - this is not a provisioned dockur guest" }
    $userSid = $user.SID.Value

    # dockur's current answer file puts its configured local user in
    # Administrators, but the image is unpinned, so assert it at runtime. Assert,
    # never repair: silently adding icloud to Administrators would be a privilege
    # grant nobody asked for.
    $members = @()
    try { $members = @(Get-LocalGroupMember -SID $AdminsSid -ErrorAction Stop) } catch {
        throw "cannot enumerate the built-in Administrators group ($AdminsSid): $($_.Exception.Message)"
    }
    $isAdmin = $false
    foreach ($m in $members) {
        $sid = $null
        try { $sid = $m.SID.Value } catch { $sid = $null }
        if ($sid -eq $userSid) { $isAdmin = $true }
    }
    if (-not $isAdmin) {
        throw ("'$AgentUser' ($userSid) is not a member of the built-in Administrators group " +
               "($AdminsSid). The elevated watcher task cannot run without it. Add the account " +
               "deliberately, or rebuild the VM; this installer will not grant it.")
    }

    # Best effort, and only when that profile's hive is already loaded: at OEM
    # time the account has never signed in, so there is nothing to write to.
    # The loop applies it at first logon either way (D51), which is why this is
    # never fatal and never loads a hive of its own.
    $hive = "Registry::HKEY_USERS\$userSid"
    if (Test-Path -LiteralPath $hive) {
        Step "Pointing $AgentUser's console delegation at conhost (D51)"
        try { Set-ConsoleDelegation -RootPath $hive | Out-Null }
        catch { Write-Warning "could not set the console delegation: $($_.Exception.Message)" }
    }

    Step "Hardening $ProvisionDir (SYSTEM + Administrators only)"
    New-Item -ItemType Directory -Force -Path $ProvisionDir | Out-Null
    New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
    $global:LASTEXITCODE = 0
    & icacls.exe $ProvisionDir /inheritance:r `
        /grant "*${SystemSid}:(OI)(CI)F" /grant "*${AdminsSid}:(OI)(CI)F" /Q 2>&1 | Out-Null
    if ([int]$LASTEXITCODE -ne 0) { throw "hardening $ProvisionDir failed (icacls exit $LASTEXITCODE)" }

    Step "Copying the watcher into the protected directory"
    $self = $PSCommandPath
    if (-not $self) { throw "cannot determine this script's own path" }
    if ([IO.Path]::GetFullPath($self) -ne [IO.Path]::GetFullPath($InstalledWatcher)) {
        Copy-Item -LiteralPath $self -Destination $InstalledWatcher -Force
    }

    # Stop a running instance before touching the definition. This command is
    # the operator's whole recovery path, and the state it most needs to repair
    # is a watcher that is *alive* but running superseded code: registering over
    # it leaves that process untouched, and `IgnoreNew` then makes the
    # Start-ScheduledTask below a silent no-op. Stopping first is what makes
    # -Install actually reinstall.
    Step "Stopping any running '$TaskName' instance"
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null } catch { }

    # Mirrors 04-bridge-agent.ps1 step 8 exactly, except RunLevel Highest and the
    # protected target: interactive principal in the auto-logged-on icloud
    # session, no stored password, infinite loop with restart, IgnoreNew (D17/D40).
    Step "Registering the '$TaskName' scheduled task (RunLevel Highest)"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
      -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $InstalledWatcher"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $AgentUser
    $principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$AgentUser" `
      -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
      -RestartInterval (New-TimeSpan -Minutes 1) `
      -MultipleInstances IgnoreNew `
      -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # no time limit
    Register-ScheduledTask -TaskName $TaskName -Action $action `
      -Trigger @($trigger, (New-KeepAliveTrigger)) `
      -Principal $principal -Settings $settings -Force | Out-Null

    Write-WatcherBeacon

    try { Start-ScheduledTask -TaskName $TaskName } catch {
        # At OEM time the icloud session does not exist yet; the logon trigger
        # starts it at first sign-in. That is not a failure.
        Write-Host "    not started yet (no '$AgentUser' session): the logon trigger will start it"
    }

    Write-Host ""
    Write-Host "PASS: watcher installed" -ForegroundColor Green
    Write-Host "  script : $InstalledWatcher"
    Write-Host "  task   : $TaskName (RunLevel Highest, restarted every minute if it stops)"
    Write-Host "  inbox  : $Inbox (read-only to this guest)"
}

# --------------------------------------------------------------- poll loop ---

function Invoke-TriggerPass {
    if (-not (Test-Path -LiteralPath $TriggerPath)) { return }

    $info = Get-Item -LiteralPath $TriggerPath -ErrorAction Stop
    if ($info.Length -gt $MaxTriggerSize) {
        throw "trigger.json is $($info.Length) bytes, over the $MaxTriggerSize byte cap"
    }
    $bytes = [IO.File]::ReadAllBytes($TriggerPath)

    # Consume-once identity. A valid run is keyed by its UUID; a trigger we
    # cannot even parse is keyed by its content hash, so a bad trigger is
    # rejected once instead of failing forever in a 30 s loop.
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '' }
    finally { $sha.Dispose() }

    $accepted = Get-AcceptedToken
    $runId = ''
    $reset = $false
    $parseError = ''
    try {
        $doc = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if ([int]$doc.version -ne 1) { throw "unsupported trigger version '$($doc.version)'" }
        if ("$($doc.action)" -cne 'reconcile') { throw "unsupported action '$($doc.action)'" }
        if (-not (Test-RunId $doc.runId)) { throw "runId is not 32 lowercase hex characters" }
        if (-not ($doc.resetShareCredential -is [bool])) { throw "resetShareCredential must be a JSON boolean" }
        $runId = [string]$doc.runId
        $reset = [bool]$doc.resetShareCredential
    } catch {
        $parseError = $_.Exception.Message
    }

    $token = if ($parseError) { "invalid-$digest" } else { $runId }
    if ($accepted -eq $token) { return }     # already consumed; never loop on it

    if ($parseError) {
        Set-AcceptedToken $token
        Write-WatcherError -RunId '' -Message "rejected trigger.json: $parseError"
        Write-Warning "rejected trigger.json: $parseError"
        return
    }

    Step "Accepting run $runId"
    Remove-StaleSecret

    $runDir = Join-Path $RunsDir $runId
    try {
        # The run directory inherits the SYSTEM + Administrators DACL that
        # -Install set on $ProvisionDir with /inheritance:r, so it is protected
        # from the moment it exists.
        New-Item -ItemType Directory -Force -Path $runDir | Out-Null
        foreach ($name in $PayloadFiles) {
            $src = Join-Path $Inbox $name
            $dst = Join-Path $runDir $name
            Copy-Item -LiteralPath $src -Destination $dst -Force
            if (-not (Test-Path -LiteralPath $dst)) { throw "payload file '$name' did not copy" }
            if ((Get-Item -LiteralPath $dst).Length -le 0) { throw "payload file '$name' copied empty" }
        }
    } catch {
        Set-AcceptedToken $token
        Write-WatcherError -RunId $runId -Message "staging failed: $($_.Exception.Message)"
        Write-Warning "staging failed: $($_.Exception.Message)"
        return
    }

    # Refresh the installed watcher for its NEXT task start (D42). A failure here
    # is not fatal: the current envelope still works.
    try { Copy-Item -LiteralPath (Join-Path $runDir 'watcher.ps1') -Destination $InstalledWatcher -Force }
    catch { Write-Warning "could not refresh $InstalledWatcher : $($_.Exception.Message)" }

    # Record acceptance BEFORE executing: a crash mid-run must not re-execute the
    # same run at the next logon.
    #
    # A failure here is the one case that cannot be marked consumed, because the
    # marker is what failed - so it must at least reach the host. Without this,
    # the pass throws into the loop's catch, the trigger is still unconsumed 30 s
    # later, and the watcher re-accepts and re-stages the same run forever while
    # the app polls a run ID that no status ever mentions. That is
    # indistinguishable from a guest with no watcher at all, which is the one
    # thing the app cannot diagnose for the operator. Seen live, when a watcher
    # still running the pre-fix File.Replace code could not rewrite an existing
    # accepted-run.txt.
    try { Set-AcceptedToken $token }
    catch {
        Write-WatcherError -RunId $runId -Message "could not record run acceptance: $($_.Exception.Message)"
        Write-Warning "could not record run acceptance: $($_.Exception.Message)"
        return
    }

    $setup = Join-Path $runDir 'guest-setup.ps1'
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $setup, '-RunId', $runId)
    # The boolean crosses as presence/absence, not as a string: `-File` would
    # turn "false" into $true.
    if ($reset) { $argv += '-ResetShareCredential' }

    Step "Running the protected guest-setup.ps1 for $runId"
    $global:LASTEXITCODE = 0
    & powershell.exe @argv
    $rc = [int]$LASTEXITCODE
    if ($rc -ne 0) {
        # guest-setup.ps1 writes its own error status; this covers the case where
        # it could not start at all.
        Write-Warning "guest-setup.ps1 exited $rc for run $runId"
    }

    Remove-StaleSecret
    Remove-SupersededRun -KeepRunId $runId
}

# -------------------------------------------------------------------- main ---

if ($Install) {
    Install-Watcher
    exit 0
}

New-Item -ItemType Directory -Force -Path $RunsDir -ErrorAction SilentlyContinue | Out-Null
Remove-StaleSecret
Confirm-KeepAlive
Write-WatcherBeacon
if (Confirm-HiddenConsole) {
    Step "Console delegation now points at conhost - exiting so the next start is windowless"
    exit 0
}
$StartedFromDigest = Get-WatcherDigest
Step "Watching $TriggerPath every $PollSeconds s"

while ($true) {
    # D42 refreshes the installed watcher "for its next task start" - this is
    # how that start now happens. No run is in flight at the top of the loop
    # (a pass runs guest-setup.ps1 synchronously), so exiting here loses
    # nothing, and the keep-alive brings the new copy up within a minute.
    #
    # Without it, a watcher fix reached the running process only at the next
    # logon, and a watcher that was alive but superseded was indistinguishable
    # to the host from no watcher at all: it can accept a run, fail on the new
    # code path, and retry silently forever while the app polls a run ID that no
    # status ever mentions. That cost an evening.
    $digest = Get-WatcherDigest
    if ($digest -ne '' -and $digest -ne $StartedFromDigest) {
        Step "The installed watcher changed - exiting so the keep-alive starts the new copy"
        exit 0
    }

    try { Invoke-TriggerPass } catch {
        # The loop is the product: a malformed trigger, an unreachable share, or
        # a failed launch must never take the watcher down.
        Write-Warning "watcher pass failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $PollSeconds
}
# ===============================================
