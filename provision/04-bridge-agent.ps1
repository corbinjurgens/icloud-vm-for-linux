# ============ 04-bridge-agent.ps1 — run as Administrator ============
# Install the v2 bridge: control share, guest agent task, Access-Based
# Enumeration on the data share, and the ACL boundaries the agent depends on.
#
# Runs INSIDE the Windows guest, AFTER scripts 01-03 and after the initial
# iCloud Drive sync has produced C:\Users\icloud\iCloudDrive.
#
#   powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\04-bridge-agent.ps1
#
# Idempotent: safe to re-run after a Windows feature update (v1 plan section 10).
# A rerun preserves exclusions.json and the agent's private state; it never
# manufactures an empty exclusion list over an existing install.
#
# Implements v2 plan section 4 (D16, D27, D28) and section 2.
# =====================================================================

$ErrorActionPreference = 'Stop'

# --- exact constants; never derived from the elevated process's profile ------
$SyncRoot  = "C:\Users\icloud\iCloudDrive"
$BaseDir   = "C:\ProgramData\icloud-bridge"
$IoDir     = Join-Path $BaseDir "io"
$StateDir  = Join-Path $BaseDir "state"
$AgentUser = "icloud"
$ShareUser = "syncshare"

$SourceScript = "C:\OEM\agent.ps1"
$AgentScript  = Join-Path $BaseDir "agent.ps1"
$TaskName     = "icloud-bridge-agent"
$ShareName    = "bridge"
$DataShare    = "icloud"
$ConfigPath   = Join-Path $IoDir "exclusions.json"

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
    if ($rc -ne 0) { throw "$What failed (icacls exit $rc): icacls $($Arguments -join ' ')" }
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
Step "Preflight"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this script from an elevated PowerShell (Administrator)"
}
if (-not (Test-Path -LiteralPath $SourceScript)) {
    throw "missing $SourceScript — copy guest-agent/agent.ps1 into provision/ before building, or drop it into C:\OEM"
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
$agentSid = (Get-LocalUser -Name $AgentUser).SID.Value
Write-Host "    sync root : $SyncRoot"
Write-Host "    agent SID : $agentSid"
Write-Host "    install   : $(if ($freshInstall) { 'first install' } else { 'existing (config preserved)' })"

# ============================ 1. stop the task, make directories =============
Step "1/9 Stopping any running agent and creating directories"
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
foreach ($d in @($BaseDir, $IoDir, (Join-Path $IoDir 'requests'), (Join-Path $IoDir 'responses'), $StateDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# ============================ 2. install the agent script ===================
Step "2/9 Installing agent.ps1"
Copy-Item -LiteralPath $SourceScript -Destination $AgentScript -Force

# ============================ 3. normalize the data-root syncshare ACL ======
# v1's 03-create-share.ps1 used /T, which stamped explicit allows on every
# descendant. An explicit allow outranks an inherited folder deny, so a known
# child path would stay readable through an exclusion. Remove only syncshare
# grants, then re-add one inheritable Modify grant at the root (v2 plan D15).
Step "3/9 Normalizing the syncshare ACL on the sync root (this walks the library)"

# Refuse to walk a tree containing junctions or symlinks: icacls /T and the
# protected-DACL scan below would follow one out of the sync root and mutate
# ACLs on unrelated objects with admin rights. Cloud placeholder directories
# also carry FILE_ATTRIBUTE_REPARSE_POINT, but PS 5.1's LinkType script
# property resolves only mount points and symlinks -- exactly the two reparse
# tags that redirect traversal -- so it is the discriminator needed here (the
# agent applies the same rule to its own walks, v2 plan section 2.1).
Step "    scanning for junctions/symlinks that would redirect the walk"
$reparseLinks = New-Object System.Collections.Generic.List[string]
function Test-IsTraversalLink {
    param([IO.FileSystemInfo]$Entry)
    if (($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) { return $false }
    $lt = $Entry.LinkType
    return ($lt -eq 'Junction' -or $lt -eq 'SymbolicLink')
}
function Find-ReparseLinks {
    param([string]$Dir)
    $children = @()
    try { $children = Get-ChildItem -LiteralPath $Dir -Force -ErrorAction Stop } catch {
        Write-Warning "cannot enumerate ${Dir}: $($_.Exception.Message)"
        return
    }
    foreach ($c in $children) {
        if (Test-IsTraversalLink $c) { $reparseLinks.Add($c.FullName); continue }  # never descend through a link
        if ($c -is [IO.DirectoryInfo]) { Find-ReparseLinks $c.FullName }
    }
}
Find-ReparseLinks $SyncRoot
if ($reparseLinks.Count -gt 0) {
    throw ("refusing to touch ACLs: junction/symlink(s) inside the sync root would redirect " +
           "the recursive walk outside it: " + ($reparseLinks -join '; ') +
           " -- remove them and re-run")
}

Invoke-Icacls @($SyncRoot, '/remove:g', $ShareUser, '/T', '/C', '/Q') 'normalizing syncshare ACLs'
Invoke-Icacls @($SyncRoot, "/grant", "${ShareUser}:(OI)(CI)M", '/Q') 'granting sync-root access'

Step "    scanning for protected child DACLs that do not inherit"
$protected = New-Object System.Collections.Generic.List[string]
function Find-ProtectedDacls {
    param([string]$Dir)
    $children = @()
    try { $children = Get-ChildItem -LiteralPath $Dir -Force -ErrorAction Stop } catch { return }
    foreach ($c in $children) {
        if (Test-IsTraversalLink $c) { continue }   # TOCTOU guard; the link scan above already threw
        $isDir = $c -is [IO.DirectoryInfo]
        try {
            $sec = if ($isDir) { [IO.Directory]::GetAccessControl($c.FullName, [Security.AccessControl.AccessControlSections]::Access) }
                   else        { [IO.File]::GetAccessControl($c.FullName, [Security.AccessControl.AccessControlSections]::Access) }
            if ($sec.AreAccessRulesProtected) { $protected.Add($c.FullName) }
        } catch { }
        if ($isDir) { Find-ProtectedDacls $c.FullName }
    }
}
Find-ProtectedDacls $SyncRoot
if ($protected.Count -gt 0) {
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
if ($protected.Count -gt 0) {
    throw ("$($protected.Count) object(s) below the sync root carry a protected DACL, so they no " +
           "longer inherit the root syncshare grant and are unreachable over SMB. The agent's " +
           "RC,WDAC authority on them has been repaired. Re-enable inheritance on each " +
           "(icacls <path> /inheritance:e) and re-run this script: " + ($protected -join '; '))
}

# ============================ 5. bridge NTFS boundary (D27) =================
# SMB exports only ...\io. The scheduled script and the agent's trusted private
# state live outside the share so SMB credentials cannot grant code execution.
# Modify deliberately excludes WRITE_DAC.
Step "5/9 Setting bridge NTFS permissions"
Invoke-Icacls @($BaseDir, '/remove:g', $ShareUser, '/T', '/C', '/Q') 'removing legacy syncshare grants from the bridge'
Invoke-Icacls @($BaseDir,  '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to the bridge base'
Invoke-Icacls @($StateDir, '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to private state'
Invoke-Icacls @($IoDir,    '/grant', "${AgentUser}:(OI)(CI)M", '/Q') 'granting the agent access to the io directory'
Invoke-Icacls @($IoDir,    '/grant', "${ShareUser}:(OI)(CI)M", '/Q') 'granting syncshare access to the io directory'
Invoke-Icacls @($AgentScript, '/grant', "${AgentUser}:RX", '/Q') 'granting the agent read/execute on its script'

# The boundary that matters is WRITE: syncshare must not be able to replace the
# scheduled script or forge the agent's trusted state. Read access inherited
# from BUILTIN\Users on C:\ProgramData is not a violation and is not flagged
# here -- and SMB never exports these paths in the first place.
$WriteRights = [Security.AccessControl.FileSystemRights]::Write -bor
               [Security.AccessControl.FileSystemRights]::Modify -bor
               [Security.AccessControl.FileSystemRights]::FullControl -bor
               [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
               [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
               [Security.AccessControl.FileSystemRights]::Delete
function Assert-NoShareUserWrite {
    param([string]$Path, [bool]$IsDirectory)
    $sections = [Security.AccessControl.AccessControlSections]::Access
    $acl = if ($IsDirectory) { [IO.Directory]::GetAccessControl($Path, $sections) }
           else              { [IO.File]::GetAccessControl($Path, $sections) }
    foreach ($r in $acl.GetAccessRules($true, $true, [Security.Principal.NTAccount])) {
        if ($r.IdentityReference.Value -notlike "*\$ShareUser") { continue }
        if ($r.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if (([int]$r.FileSystemRights -band [int]$WriteRights) -ne 0) {
            throw "$ShareUser has write access to $Path — the D27 privilege boundary is not in place"
        }
    }
}
Assert-NoShareUserWrite $AgentScript $false
Assert-NoShareUserWrite $StateDir $true

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

# ============================ 8. register the agent task ====================
# Interactive principal in the auto-logged-on icloud session: no stored
# password, no elevation (D28 keeps the task RunLevel Limited).
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
Start-ScheduledTask -TaskName $TaskName

# ============================ 9. verify =====================================
Step "9/9 Verifying"
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

Write-Host ""
Write-Host "bridge ready" -ForegroundColor Green
Write-Host "  share  : \\<guest>\$ShareName  ->  $IoDir"
Write-Host "  task   : $TaskName (Running, restarts on failure)"
Write-Host "  data   : \\<guest>\$DataShare  (FolderEnumerationMode = AccessBased)"
Write-Host ""
Write-Host "Next, on the Linux host:  sudo ./host/setup-host.sh  &&  ./host/acceptance-tests.sh"
# ===============================================
