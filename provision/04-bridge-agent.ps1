# ============ 04-bridge-agent.ps1 — run as Administrator ============
# Install the v2 bridge: control share, guest agent task, Access-Based
# Enumeration on the data share, and the ACL boundaries the agent depends on.
#
# Runs INSIDE the Windows guest, AFTER scripts 01-03 and after the initial
# iCloud Drive sync has produced C:\Users\icloud\iCloudDrive.
#
#   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\04-bridge-agent.ps1
#
# -Scope selects how much to reconcile (v2 plan section 4.2):
#
#   All       (default) everything below, in order. This is the manual fallback
#             and behaves exactly as it always has.
#   Boundary  the sync-root ACL normalization and scans, the D27/D28 ACL
#             boundaries, exclusions safety, the bridge share and ABE.
#   Agent     only the installed agent.ps1, its ACL, its scheduled task and its
#             runtime. It does NOT walk or normalize the iCloud tree, which is
#             what makes an agent-build update cheap and non-invasive.
#
# The source agent.ps1 is resolved as this script's own sibling
# (Join-Path $PSScriptRoot 'agent.ps1'), never as a hard-coded C:\OEM path: OEM
# copies go stale the moment the repository moves, and the automated path stages
# a coherent bundle into a protected per-run directory (v2 plan D42). For the
# manual fallback from C:\OEM this resolves to the same file it always did.
#
# Idempotent: safe to re-run after a Windows feature update (v1 plan section 10).
# A rerun preserves exclusions.json and the agent's private state; it never
# manufactures an empty exclusion list over an existing install.
#
# Implements v2 plan section 4 (D16, D27, D28) and section 2.
# =====================================================================

[CmdletBinding()]
param([ValidateSet('Agent', 'Boundary', 'All')][string]$Scope = 'All')

$ErrorActionPreference = 'Stop'

# The exact constants and the read-only probes live in one place, so this script
# and the orchestrator that dispatches it cannot disagree about what "correct"
# means. Never derived from the elevated process's profile (v2 plan section 4).
$stateLib = Join-Path $PSScriptRoot 'guest-state.ps1'
if (-not (Test-Path -LiteralPath $stateLib)) {
    throw "missing $stateLib — guest-state.ps1 must sit beside this script"
}
. $stateLib

$SourceScript = Join-Path $PSScriptRoot 'agent.ps1'

$doAgent    = ($Scope -eq 'Agent' -or $Scope -eq 'All')
$doBoundary = ($Scope -eq 'Boundary' -or $Scope -eq 'All')

function Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }

function Invoke-Icacls {
    param([string[]]$Arguments, [string]$What)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        & icacls.exe @Arguments 2>&1 | Out-Null
        $rc = [int]$LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    Assert-NativeExitCode -ExitCode $rc -What $What -CommandLine "icacls $($Arguments -join ' ')"
}

function Write-JsonAtomic {
    # BOM-less UTF-8 + atomic replace, matching the agent's bridge writer
    # (v2 plan section 2). Set-Content -Encoding UTF8 would emit a BOM.
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

# ============================ preflight (change nothing yet) =================
# The preflight is shared by every scope, so `All` performs it exactly once.
Step "Preflight (-Scope $Scope)"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this script from an elevated PowerShell (Administrator)"
}
if ($doAgent -and -not (Test-Path -LiteralPath $SourceScript)) {
    throw "missing $SourceScript — agent.ps1 must sit beside this script (copy guest-agent/agent.ps1 into provision/ before building)"
}
if (-not (Test-Path -LiteralPath $SyncRoot)) {
    throw "missing sync root $SyncRoot — finish the Apple ID sign-in and the initial sync first"
}
foreach ($u in @($AgentUser, $ShareUser)) {
    if (-not (Get-LocalUser -Name $u -ErrorAction SilentlyContinue)) {
        throw "local account '$u' does not exist — run 03-create-share.ps1 first"
    }
}

$existingTask  = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
$priorMarkers  = New-Object System.Collections.Generic.List[string]
if ($null -ne $existingTask)  { $priorMarkers.Add("scheduled task $TaskName") }
if ($null -ne $existingShare) { $priorMarkers.Add("SMB share $ShareName") }
if (Test-Path -LiteralPath (Join-Path $StateDir 'applied.json'))          { $priorMarkers.Add("private applied.json") }
if (Test-Path -LiteralPath (Join-Path $StateDir 'v1-pin-cleared.marker')) { $priorMarkers.Add("migration marker") }
if (Test-Path -LiteralPath $AgentScript)                                  { $priorMarkers.Add("installed agent.ps1") }

$freshInstall = -not (Test-Path -LiteralPath $ConfigPath)
if ($freshInstall -and $priorMarkers.Count -gt 0) {
    throw ("exclusions.json is missing but this looks like an existing install (" +
           ($priorMarkers -join ', ') + "). Refusing to manufacture an empty exclusion list, " +
           "which would re-include everything. Restore the file, or write an explicitly chosen " +
           "config to $ConfigPath and re-run.")
}
if ($freshInstall -and -not $doBoundary) {
    throw ("-Scope $Scope cannot run on a guest with no exclusions.json: the bridge boundary has " +
           "never been installed. Run with -Scope All (or -Scope Boundary) first.")
}
$agentSid = (Get-LocalUser -Name $AgentUser).SID.Value
Write-Host "    sync root : $SyncRoot"
Write-Host "    agent SID : $agentSid"
Write-Host "    install   : $(if ($freshInstall) { 'first install' } else { 'existing (config preserved)' })"

# ============================ 1. stop the task, make directories =============
# Both scopes need this: the boundary scope must not rewrite ACLs under a
# running agent, and the agent scope must not replace a script in use.
Step "1/9 Stopping any running agent and creating directories"
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
foreach ($d in @($BaseDir, $IoDir, (Join-Path $IoDir 'requests'), (Join-Path $IoDir 'responses'), $StateDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# ============================ 2. install the agent script ===================
if ($doAgent) {
Step "2/9 Installing agent.ps1"
Copy-Item -LiteralPath $SourceScript -Destination $AgentScript -Force
}

$protected = @()
if ($doBoundary) {
# ============================ 3. normalize the data-root syncshare ACL ======
# v1's 03-create-share.ps1 used /T, which stamped explicit allows on every
# descendant. An explicit allow outranks an inherited folder deny, so a known
# child path would stay readable through an exclusion. Remove only syncshare
# grants, then re-add one inheritable Modify grant at the root (v2 plan D15).
Step "3/9 Normalizing the syncshare ACL on the sync root (this walks the library)"

# Refuse to walk a tree containing junctions or symlinks: icacls /T and the
# protected-DACL scan below would follow one out of the sync root and mutate
# ACLs on unrelated objects with admin rights. Both scans live in
# guest-state.ps1 so the inspection that decides this component is healthy uses
# exactly the rules this repair enforces.
Step "    scanning for junctions/symlinks that would redirect the walk"
$reparseLinks = Get-TraversalLinkPath -Path $SyncRoot
if (@($reparseLinks).Count -gt 0) {
    throw ("refusing to touch ACLs: junction/symlink(s) inside the sync root would redirect " +
           "the recursive walk outside it: " + ($reparseLinks -join '; ') +
           " -- remove them and re-run")
}

Invoke-Icacls @($SyncRoot, '/remove:g', $ShareUser, '/T', '/C', '/Q') 'normalizing syncshare ACLs'
Invoke-Icacls @($SyncRoot, "/grant", "${ShareUser}:(OI)(CI)M", '/Q') 'granting sync-root access'

Step "    scanning for protected child DACLs that do not inherit"
$protected = Get-ProtectedDaclPath -Path $SyncRoot
if (@($protected).Count -gt 0) {
    Write-Warning "$($protected.Count) object(s) below the sync root have a protected DACL and do not inherit:"
    foreach ($p in ($protected | Select-Object -First 50)) { Write-Host "      $p" }
    if ($protected.Count -gt 50) { Write-Host "      ... and $($protected.Count - 50) more" }
    Write-Host "    step 4 repairs the agent's ACL authority on them, then this script fails"
    Write-Host "    so you can restore inheritance deliberately (icacls <path> /inheritance:e)"
}

# ============================ 4. agent ACL authority (D28) ==================
# Editing a DACL needs WRITE_DAC. An owner has it implicitly, but ownership of
# a new object depends on who created it — cloud-created items are owned by
# icloud, items created through SMB may be owned by syncshare — so grant it
# explicitly by SID and nothing else. No WO, no D, no data access. The ACE
# names icloud, never syncshare, so it cannot weaken an exclusion deny.
Step "4/9 Granting the agent RC,WDAC on the sync root"
Invoke-Icacls @($SyncRoot, '/grant', "*${agentSid}:(OI)(CI)(RC,WDAC)", '/Q') 'granting agent ACL-management rights'

# A protected DACL does not inherit the root ACEs, so step 3 left these
# objects without any syncshare allow. Deliberately do NOT grant syncshare
# here: an explicit allow on an object under an excluded root would outrank
# the inherited exclusion deny (v2 plan D28: the repair ACE names icloud,
# never syncshare). Repair only the agent's authority, then fail listing the
# paths so the operator restores inheritance deliberately and re-runs.
$failedRepairs = New-Object System.Collections.Generic.List[string]
foreach ($p in $protected) {
    try {
        Invoke-Icacls @($p, '/grant', "*${agentSid}:(OI)(CI)(RC,WDAC)", '/Q') "repairing agent authority on $p"
    } catch {
        $failedRepairs.Add($p)
    }
}
if ($failedRepairs.Count -gt 0) {
    throw ("could not repair the protected DACLs on: " + ($failedRepairs -join '; ') +
           " — fix these objects by hand and re-run; nothing else was reset")
}
if (@($protected).Count -gt 0) {
    throw ("$(@($protected).Count) object(s) below the sync root carry a protected DACL, so they no " +
           "longer inherit the root syncshare grant and are unreachable over SMB. The agent's " +
           "RC,WDAC authority on them has been repaired. Re-enable inheritance on each " +
           "(icacls <path> /inheritance:e) and re-run this script: " + ($protected -join '; '))
}
}   # end of the Boundary-scope tree work (steps 3-4)

# ============================ 5. bridge NTFS boundary (D27) =================
# SMB exports only ...\io. The scheduled script and the agent's trusted private
# state live outside the share so SMB credentials cannot grant code execution.
# Modify deliberately excludes WRITE_DAC.
#
# The boundary that matters is WRITE: syncshare must not be able to replace the
# scheduled script or forge the agent's trusted state. Read access inherited
# from BUILTIN\Users on C:\ProgramData is not a violation and is not flagged
# here -- and SMB never exports these paths in the first place. The rule itself
# lives in guest-state.ps1, so inspection and repair share one definition.
function Assert-NoShareUserWrite {
    param([string]$Path, [bool]$IsDirectory)
    if (-not (Test-NoShareUserWrite -Path $Path -IsDirectory $IsDirectory)) {
        throw "$ShareUser has write access to $Path — the D27 privilege boundary is not in place"
    }
}

Step "5/9 Setting bridge NTFS permissions"
if ($doBoundary) {
Invoke-Icacls @($BaseDir, '/remove:g', $ShareUser, '/T', '/C', '/Q') 'removing legacy syncshare grants from the bridge'
Invoke-Icacls @($BaseDir,  '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to the bridge base'
Invoke-Icacls @($StateDir, '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to private state'
Invoke-Icacls @($IoDir,    '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to the io directory'
Invoke-Icacls @($IoDir,    '/grant', "${ShareUser}:(OI)(CI)M", '/Q') 'granting syncshare access to the io directory'
}
if ($doAgent) {
Invoke-Icacls @($AgentScript, '/grant', "${AgentUser}:RX", '/Q') 'granting the agent read/execute on its script'
Assert-NoShareUserWrite $AgentScript $false
}
if ($doBoundary) {
Assert-NoShareUserWrite $StateDir $true
}

if ($doBoundary) {
# ============================ 6. config + bridge share ======================
Step "6/9 Creating the bridge share"
if ($freshInstall) {
    Write-JsonAtomic $ConfigPath '{"version":1,"revision":0,"exclusions":[]}'
    Write-Host "    wrote a first-install exclusions.json (revision 0, nothing excluded)"
} else {
    Write-Host "    keeping the existing exclusions.json"
}

if ($null -ne $existingShare -and $existingShare.Path -ne $IoDir) {
    Write-Host "    existing '$ShareName' share points at $($existingShare.Path); recreating it at $IoDir (no files are deleted)"
    Remove-SmbShare -Name $ShareName -Force
    $existingShare = $null
}
if ($null -eq $existingShare) {
    New-SmbShare -Name $ShareName -Path $IoDir -FullAccess $ShareUser | Out-Null
} else {
    Grant-SmbShareAccess -Name $ShareName -AccountName $ShareUser -AccessRight Full -Force | Out-Null
}
$check = Get-SmbShare -Name $ShareName
if ($check.Path -ne $IoDir) { throw "the '$ShareName' share resolved to $($check.Path), expected $IoDir" }

# ============================ 7. ABE on the data share ======================
# Access-Based Enumeration is what actually hides an excluded item from
# `ls /mnt/icloud`: SMB omits entries the connecting user cannot read (D15).
Step "7/9 Enabling Access-Based Enumeration on the '$DataShare' share"
if (-not (Get-SmbShare -Name $DataShare -ErrorAction SilentlyContinue)) {
    throw "the '$DataShare' share does not exist — run 03-create-share.ps1 first"
}
Set-SmbShare -Name $DataShare -FolderEnumerationMode AccessBased -Force | Out-Null
}   # end of the Boundary-scope share/config work (steps 6-7)

# ============================ 8. register the agent task ====================
# Interactive principal in the auto-logged-on icloud session: no stored
# password, no elevation (D28 keeps the task RunLevel Limited).
if ($doAgent) {
Step "8/9 Registering the '$TaskName' scheduled task"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $AgentScript"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $AgentUser
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$AgentUser" `
  -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # no time limit
Register-ScheduledTask -TaskName $TaskName -Action $action `
  -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
# Step 1 stopped the task for every scope, so every scope restarts it — a
# Boundary-only run must not leave the guest without a running agent.
if ($null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName $TaskName
}

# ============================ 9. verify =====================================
Step "9/9 Verifying"
if ($doBoundary) {
    $bridgeShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
    if ($null -eq $bridgeShare -or $bridgeShare.Path -ne $IoDir) {
        throw "the '$ShareName' share is missing or does not point at $IoDir"
    }
    $dataShareNow = Get-SmbShare -Name $DataShare -ErrorAction SilentlyContinue
    if ($null -eq $dataShareNow -or "$($dataShareNow.FolderEnumerationMode)" -ne 'AccessBased') {
        throw "Access-Based Enumeration is not in force on the '$DataShare' share"
    }
    Write-Host "PASS: bridge share and Access-Based Enumeration"
}

# The agent runtime is verified whenever this run touched the agent, and also
# after a Boundary-only run that stopped an existing task in step 1. A
# Boundary-only run on a guest that has no agent task yet has nothing to wait
# for -- the agent scope has simply not been run there.
$verifyRuntime = $doAgent -or ($null -ne $existingTask)
if ($verifyRuntime) {
$deadline = (Get-Date).AddSeconds(90)
$statusPath = Join-Path $IoDir 'status.json'
$taskRunning = $false
$statusFresh = $false
while ((Get-Date) -lt $deadline -and -not ($taskRunning -and $statusFresh)) {
    Start-Sleep -Seconds 5
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $t -and $t.State -eq 'Running') { $taskRunning = $true }
    if (Test-Path -LiteralPath $statusPath) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $statusPath).LastWriteTimeUtc).TotalSeconds
        if ($age -lt 30) { $statusFresh = $true }
    }
}
if (-not $taskRunning) {
    throw "the '$TaskName' task did not reach Running — check Task Scheduler; the agent only runs in the logged-on '$AgentUser' session"
}
if (-not $statusFresh) {
    throw "no fresh $statusPath appeared within 90 s — run the agent by hand to see its error: powershell -NoProfile -ExecutionPolicy Bypass -File $AgentScript"
}
}   # end of the agent-runtime verification

Write-Host ""
Write-Host "bridge ready (-Scope $Scope)" -ForegroundColor Green
if ($doBoundary) { Write-Host "  share  : \\<guest>\$ShareName  ->  $IoDir" }
if ($verifyRuntime) { Write-Host "  task   : $TaskName (Running, restarts on failure)" }
if ($doBoundary) { Write-Host "  data   : \\<guest>\$DataShare  (FolderEnumerationMode = AccessBased)" }
if ($Scope -eq 'All') {
    Write-Host ""
    Write-Host "Next, on the Linux host:  sudo ./host/setup-host.sh  &&  ./host/acceptance-tests.sh"
}
# ===============================================
