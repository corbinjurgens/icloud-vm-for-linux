# ============ guest-state.ps1 — dot-sourced library, no side effects ============
# The single definition of the guest's desired state: the fixed constants, the
# fixed check-state and work-ID vocabularies, read-only probes for the eight
# checklist rows, and the dependency-ordered work-plan derivation (v2 plan D44).
#
# Runs INSIDE the Windows guest, dot-sourced by its siblings:
#
#   . (Join-Path $PSScriptRoot 'guest-state.ps1')
#
# consumed by guest-setup.ps1 (the elevated orchestrator), 03-create-share.ps1
# and 04-bridge-agent.ps1. One definition is the point: the orchestrator must
# not be able to call a component healthy under weaker rules than the script
# that repairs and verifies it.
#
# Idempotent, and stronger than that: loading this file has no side effects. It
# defines constants and functions and does nothing else. Nothing here repairs
# anything, writes status, or mutates the guest — even creating a missing
# directory counts as repair and belongs to a repair scope.
#
# Two layers, deliberately separated so the state machine can be tested off
# Windows (packaging/test-guest-state.ps1 runs under PowerShell 7 on Linux):
#
#   Read-*Observation   Windows-only reads. Each returns a plain hashtable of
#                       facts and never throws: a failure becomes Error.
#   Resolve-*State      PURE. Maps one observation hashtable to one check
#                       state. No cmdlets, no filesystem, no clock.
#
# Get-GuestWorkPlan and Get-GuestRepairDispatch are pure for the same reason.
# Keep them that way.
# ===============================================================================

# ------------------------------------------------------------- constants -----
# Exact paths. Never derived from the elevated process's profile (v2 plan
# section 4). Dot-sourcing defines these names in the caller's scope; 03 and 04
# consume them instead of restating them.

# Written out in full rather than composed with Join-Path: this file is
# dot-sourced by packaging/test-guest-state.ps1 under PowerShell 7 on Linux,
# where Join-Path resolves the drive and would fail on a C:\ path.
$SyncRoot   = "C:\Users\icloud\iCloudDrive"
$BaseDir    = "C:\ProgramData\icloud-bridge"
$IoDir      = "C:\ProgramData\icloud-bridge\io"
$StateDir   = "C:\ProgramData\icloud-bridge\state"
$AgentUser  = "icloud"
$ShareUser  = "syncshare"

$AgentScript = "C:\ProgramData\icloud-bridge\agent.ps1"
$TaskName    = "icloud-bridge-agent"
$ShareName   = "bridge"
$DataShare   = "icloud"
$ConfigPath  = "C:\ProgramData\icloud-bridge\io\exclusions.json"
$StatusPath  = "C:\ProgramData\icloud-bridge\io\status.json"

$WinlogonUserListKey =
    "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"

# The provisioning channel (v2 plan D40/D42). The watcher deliberately restates
# these few paths rather than dot-sourcing this file: only watcher.ps1 itself is
# installed into the protected directory, so its envelope has to stand alone.
$ProvisionDir       = "C:\ProgramData\icloud-bridge-provision"
$ProvisionRunsDir   = "C:\ProgramData\icloud-bridge-provision\runs"
$ProvisionCurrent   = "C:\ProgramData\icloud-bridge-provision\current"
$ProvisionTaskName  = "icloud-bridge-provision"
$ProvisionInbox     = "\\host.lan\Provision"
$ProvisionStatusDir = "\\host.lan\Data\.provision"
$ProvisionPayload   = @(
    '03-create-share.ps1',
    '04-bridge-agent.ps1',
    'agent.ps1',
    'guest-state.ps1',
    'guest-setup.ps1',
    'watcher.ps1'
)

# The iCloud client, by the identifiers the plan pins (section 5 and
# docs/automation-notes.md section 3).
$IcloudPackageName = 'AppleInc.iCloud'
$IcloudStoreId     = '9PKTQ5699M62'
$IcloudAppsFolder  = 'shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud'

# ------------------------------------------------------- fixed vocabularies --
# These three rosters are the protocol. The GUI validates against exactly this
# set and renders its own labels; adding a member is a protocol change.

$GuestCheckStates = @('pending', 'ok', 'missing', 'drifted', 'blocked', 'unknown', 'unverifiable')

$GuestCheckIds = @(
    'icloudPackage',
    'syncRoot',
    'shareAccount',
    'shareCredential',
    'dataShare',
    'bridgeBoundary',
    'agentInstall',
    'agentRuntime'
)

# Declaration order IS the dependency order: package/sign-in before share work,
# share work before the bridge boundary, the agent last (D44). Get-GuestWorkPlan
# emits in this order, so the ordering cannot drift away from the roster.
$GuestWorkIds = @(
    'install-icloud',
    'wait-for-signin',
    'create-share-account',
    'reset-share-credential',
    'repair-data-share',
    'repair-bridge-boundary',
    'update-agent'
)

$GuestPhases = @(
    'staging',
    'inspecting',
    'installing-icloud',
    'launching-icloud',
    'waiting-for-signin',
    'waiting-for-secret',
    'creating-share',
    'installing-bridge-boundary',
    'installing-agent',
    'verifying',
    'done'
)

# ------------------------------------------------------------ typed results --

function New-GuestCheckResult {
    # The typed probe result. Validating here is what stops a probe inventing a
    # state string the GUI cannot render.
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$State,
        [string]$Detail = ''
    )
    if ($GuestCheckIds -notcontains $Id) { throw "unknown check id '$Id'" }
    if ($GuestCheckStates -notcontains $State) { throw "unknown check state '$State' for '$Id'" }
    [pscustomobject]@{ Id = $Id; State = $State; Detail = $Detail }
}

function Assert-NativeExitCode {
    # A native non-zero exit is not a PowerShell terminating error, so every
    # native call in 03 and 04 has to be checked explicitly. Pure: it inspects an
    # integer the caller already captured and throws; it runs nothing itself.
    param(
        [Parameter(Mandatory)][AllowNull()][object]$ExitCode,
        [Parameter(Mandatory)][string]$What,
        [string]$CommandLine = ''
    )
    if ($null -eq $ExitCode) { throw "$What failed: the native command reported no exit code" }
    $rc = [int]$ExitCode
    if ($rc -ne 0) {
        $suffix = if ($CommandLine) { ": $CommandLine" } else { "" }
        throw "$What failed (exit $rc)$suffix"
    }
}

# ------------------------------------------------------- pure normalizers ----
# Each takes the matching Read-*Observation hashtable. All are total: any
# combination of inputs yields exactly one state, and an observation that could
# not be taken at all (Error set) is 'unknown', never absence.

function Get-ObservationField {
    param([hashtable]$Observation, [string]$Name)
    if ($null -eq $Observation) { return $null }
    if (-not $Observation.ContainsKey($Name)) { return $null }
    return $Observation[$Name]
}

function Test-ObservationFailed {
    param([hashtable]$Observation)
    if ($null -eq $Observation) { return $true }
    $err = Get-ObservationField $Observation 'Error'
    return ($null -ne $err -and "$err" -ne '')
}

function Resolve-IcloudPackageState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'Present') -eq $true) { return 'ok' }
    return 'missing'
}

function Resolve-SyncRootState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'Exists') -ne $true) { return 'missing' }
    # An object of the wrong type at the exact sync-root path is never deleted
    # and never reinterpreted as absence (D44 strange-state policy).
    if ((Get-ObservationField $Observation 'IsDirectory') -ne $true) { return 'blocked' }
    if ((Get-ObservationField $Observation 'Accessible') -ne $true) { return 'blocked' }
    return 'ok'
}

function Resolve-ShareAccountState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'Exists') -ne $true) { return 'missing' }
    foreach ($flag in @('Enabled', 'PasswordNeverExpires', 'AccountNeverExpires', 'HiddenFromLogon')) {
        if ((Get-ObservationField $Observation $flag) -ne $true) { return 'drifted' }
    }
    return 'ok'
}

function Resolve-ShareCredentialState {
    # Windows never reveals or validates a local account password, so this row is
    # honest rather than green: it is always 'unverifiable' (D44). Whether the
    # credential was reset this run or preserved is work-plan information, not a
    # probe result, and the GUI must say which.
    return 'unverifiable'
}

function Resolve-DataShareState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'DependencyMet') -ne $true) { return 'pending' }
    if ((Get-ObservationField $Observation 'ShareExists') -ne $true) { return 'missing' }
    if ((Get-ObservationField $Observation 'SharePath') -ne (Get-ObservationField $Observation 'ExpectedPath')) {
        return 'drifted'
    }
    foreach ($flag in @('ShareAccessOk', 'ServiceRunning', 'ServiceAutomatic',
                        'FirewallEnabled', 'SigningDisabled', 'EncryptionDisabled', 'RootAceOk')) {
        if ((Get-ObservationField $Observation $flag) -ne $true) { return 'drifted' }
    }
    return 'ok'
}

function Resolve-BridgeBoundaryState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'DependencyMet') -ne $true) { return 'pending' }
    # Blocked beats missing: a vanished exclusions.json over an existing install,
    # a link that could redirect an elevated walk, or a child DACL that does not
    # inherit must stop the run, not look like a fresh install (D44).
    if ((Get-ObservationField $Observation 'ExclusionsSafe') -ne $true) { return 'blocked' }
    if ([int](Get-ObservationField $Observation 'TraversalLinkCount') -gt 0) { return 'blocked' }
    if ([int](Get-ObservationField $Observation 'ProtectedDaclCount') -gt 0) { return 'blocked' }
    if ((Get-ObservationField $Observation 'ShareExists') -ne $true) { return 'missing' }
    if ((Get-ObservationField $Observation 'SharePath') -ne (Get-ObservationField $Observation 'ExpectedPath')) {
        return 'drifted'
    }
    # A legacy explicit syncshare allow left by v1's /T is drift, not a block:
    # 04's boundary scope removes it non-destructively (icacls /remove:g /T).
    if ([int](Get-ObservationField $Observation 'LegacyAllowCount') -gt 0) { return 'drifted' }
    foreach ($flag in @('ShareAccessOk', 'AbeEnabled', 'AgentAuthorityOk', 'PrivilegeBoundaryOk')) {
        if ((Get-ObservationField $Observation $flag) -ne $true) { return 'drifted' }
    }
    return 'ok'
}

function Resolve-AgentInstallState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'DependencyMet') -ne $true) { return 'pending' }
    $script = (Get-ObservationField $Observation 'ScriptPresent') -eq $true
    $task   = (Get-ObservationField $Observation 'TaskPresent') -eq $true
    if (-not $script -and -not $task) { return 'missing' }
    if (-not $script -or -not $task) { return 'drifted' }
    foreach ($flag in @('HashMatches', 'TaskDefinitionMatches', 'ScriptAclOk')) {
        if ((Get-ObservationField $Observation $flag) -ne $true) { return 'drifted' }
    }
    return 'ok'
}

function Resolve-AgentRuntimeState {
    param([hashtable]$Observation)
    if (Test-ObservationFailed $Observation) { return 'unknown' }
    if ((Get-ObservationField $Observation 'DependencyMet') -ne $true) { return 'pending' }
    if ((Get-ObservationField $Observation 'TaskRunning') -ne $true) { return 'missing' }
    foreach ($flag in @('StatusFresh', 'ProtocolSupported', 'AgentBuildMatches')) {
        if ((Get-ObservationField $Observation $flag) -ne $true) { return 'drifted' }
    }
    return 'ok'
}

# ------------------------------------------------ pure checklist reasoning ---

function Test-GuestChecklistComplete {
    param([hashtable]$Checks)
    if ($null -eq $Checks) { return $false }
    foreach ($id in $GuestCheckIds) {
        if (-not $Checks.ContainsKey($id)) { return $false }
        if ($GuestCheckStates -notcontains $Checks[$id]) { return $false }
    }
    return $true
}

function Get-GuestBlockedChecks {
    # The gate that stops the orchestrator before its first mutation. 'blocked'
    # and 'unknown' are the only two states that mean "do not proceed"; 'pending'
    # means an unmet dependency makes the probe meaningless and is re-probed.
    param([hashtable]$Checks)
    $blocked = New-Object System.Collections.Generic.List[string]
    foreach ($id in $GuestCheckIds) {
        $state = if ($null -ne $Checks -and $Checks.ContainsKey($id)) { $Checks[$id] } else { 'unknown' }
        if ($state -eq 'blocked' -or $state -eq 'unknown') { $blocked.Add($id) }
    }
    return , $blocked.ToArray()
}

function Test-GuestChecklistConverged {
    # D44's convergence rule, and the only definition of 'done': every required
    # invariant is 'ok', with shareCredential's labelled 'unverifiable' exception.
    param([hashtable]$Checks)
    if (-not (Test-GuestChecklistComplete $Checks)) { return $false }
    foreach ($id in $GuestCheckIds) {
        $want = if ($id -eq 'shareCredential') { 'unverifiable' } else { 'ok' }
        if ($Checks[$id] -ne $want) { return $false }
    }
    return $true
}

function Get-GuestWorkPlan {
    # PURE. checks + the operator's reset intent -> the fixed, dependency-ordered
    # work list. 'ok' components are skipped; 'pending', 'blocked', 'unknown' and
    # 'unverifiable' schedule nothing, because none of them is evidence that a
    # named non-destructive repair applies.
    param(
        [Parameter(Mandatory)][hashtable]$Checks,
        [bool]$ResetShareCredential = $false
    )
    if (-not (Test-GuestChecklistComplete $Checks)) {
        throw "incomplete checklist: expected exactly $($GuestCheckIds -join ', ')"
    }

    $repairable = @('missing', 'drifted')
    $wanted = New-Object System.Collections.Generic.HashSet[string]

    if ($repairable -contains $Checks['icloudPackage']) { [void]$wanted.Add('install-icloud') }
    if ($Checks['syncRoot'] -eq 'missing')              { [void]$wanted.Add('wait-for-signin') }

    if ($Checks['shareAccount'] -eq 'missing') {
        # Creating the account establishes the credential, so it is never paired
        # with a separate reset.
        [void]$wanted.Add('create-share-account')
    } elseif ($ResetShareCredential) {
        [void]$wanted.Add('reset-share-credential')
    }
    if ($Checks['shareAccount'] -eq 'drifted')            { [void]$wanted.Add('repair-data-share') }
    if ($repairable -contains $Checks['dataShare'])       { [void]$wanted.Add('repair-data-share') }
    if ($repairable -contains $Checks['bridgeBoundary'])  { [void]$wanted.Add('repair-bridge-boundary') }
    if ($repairable -contains $Checks['agentInstall'])    { [void]$wanted.Add('update-agent') }
    if ($repairable -contains $Checks['agentRuntime'])    { [void]$wanted.Add('update-agent') }

    $plan = New-Object System.Collections.Generic.List[string]
    foreach ($id in $GuestWorkIds) { if ($wanted.Contains($id)) { $plan.Add($id) } }
    return , $plan.ToArray()
}

function Get-GuestRepairDispatch {
    # PURE. work list -> the fixed repair dispatch. This is the whole dispatch
    # table: guest-setup.ps1 switches on these fields and nothing else, so no
    # host-supplied or guest-writable string can ever select what runs, and
    # Invoke-Expression is never needed.
    param([string[]]$Work)

    # Not named $work: PowerShell variable names are case-insensitive, so that
    # would silently overwrite the $Work parameter before it is read.
    $requested = @()
    foreach ($w in @($Work)) {
        if ($null -eq $w) { continue }
        if ($GuestWorkIds -notcontains $w) { throw "unknown work id '$w'" }
        $requested += $w
    }

    $createAccount = $requested -contains 'create-share-account'
    $resetCred     = $requested -contains 'reset-share-credential'
    $repairShare   = $requested -contains 'repair-data-share'
    $boundary      = $requested -contains 'repair-bridge-boundary'
    $agent         = $requested -contains 'update-agent'

    $shareMode =
        if ($createAccount -or $resetCred) { 'password-file' }
        elseif ($repairShare)              { 'preserve-credential' }
        else                               { 'none' }

    $bridgeScope =
        if ($boundary -and $agent) { 'All' }
        elseif ($boundary)         { 'Boundary' }
        elseif ($agent)            { 'Agent' }
        else                       { 'none' }

    [pscustomobject]@{
        InstallIcloud   = ($requested -contains 'install-icloud')
        WaitForSignin   = ($requested -contains 'wait-for-signin')
        RunShareScript  = ($shareMode -ne 'none')
        ShareMode       = $shareMode
        # The secret is requested only when the account is being created or the
        # credential explicitly reset. An agent-only plan must never reach here.
        NeedsSecret     = ($shareMode -eq 'password-file')
        RunBridgeScript = ($bridgeScope -ne 'none')
        BridgeScope     = $bridgeScope
    }
}

# ------------------------------------------------- read-only Windows probes --
# Everything below reads the guest and returns facts. No mutation, no status.
# These are not exercised by the Linux fixture test; the normalizers above are.

function Get-StagedAgentBuild {
    # The bundled agent's $AgentBuild constant (D35), read from the staged source
    # rather than assumed, so an agent-build comparison cannot silently pass.
    param([Parameter(Mandatory)][string]$StagedAgentPath)
    if (-not (Test-Path -LiteralPath $StagedAgentPath)) { return $null }
    $text = Get-Content -LiteralPath $StagedAgentPath -Raw -ErrorAction SilentlyContinue
    if ($null -eq $text) { return $null }
    $m = [regex]::Match($text, '(?m)^\$AgentBuild\s*=\s*(\d+)\s*$')
    if (-not $m.Success) { return $null }
    return [int]$m.Groups[1].Value
}

function Test-IsTraversalLink {
    # Cloud placeholder directories also carry FILE_ATTRIBUTE_REPARSE_POINT, but
    # PS 5.1's LinkType resolves only mount points and symlinks — exactly the two
    # reparse tags that redirect traversal — so it is the discriminator to use
    # (v2 plan section 4 step 3; the agent applies the same rule to its walks).
    param([IO.FileSystemInfo]$Entry)
    if (($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) { return $false }
    $lt = $Entry.LinkType
    return ($lt -eq 'Junction' -or $lt -eq 'SymbolicLink')
}

function Add-TraversalLinkPath {
    # Recursive worker for Get-TraversalLinkPath. Never descends through a link.
    param([string]$Dir, [System.Collections.Generic.List[string]]$Found)
    $children = @()
    try { $children = Get-ChildItem -LiteralPath $Dir -Force -ErrorAction Stop } catch {
        Write-Warning "cannot enumerate ${Dir}: $($_.Exception.Message)"
        return
    }
    foreach ($c in $children) {
        if (Test-IsTraversalLink $c) { $Found.Add($c.FullName); continue }
        if ($c -is [IO.DirectoryInfo]) { Add-TraversalLinkPath -Dir $c.FullName -Found $Found }
    }
}

function Get-TraversalLinkPath {
    # Read-only scan for junctions/symlinks that would take a recursive icacls or
    # a protected-DACL walk outside the sync root.
    param([Parameter(Mandatory)][string]$Path)
    $found = New-Object System.Collections.Generic.List[string]
    Add-TraversalLinkPath -Dir $Path -Found $found
    return , $found.ToArray()
}

function Add-BridgeAclScan {
    # Recursive worker for Get-BridgeAclScan. One walk, two findings, because the
    # per-entry DACL read is the expensive part and the plan asks for both.
    param(
        [string]$Dir,
        [System.Collections.Generic.List[string]]$Protected,
        [System.Collections.Generic.List[string]]$LegacyAllow
    )
    $children = @()
    try { $children = Get-ChildItem -LiteralPath $Dir -Force -ErrorAction Stop } catch { return }
    foreach ($c in $children) {
        if (Test-IsTraversalLink $c) { continue }   # TOCTOU guard
        $isDir = $c -is [IO.DirectoryInfo]
        try {
            $sec = if ($isDir) {
                [IO.Directory]::GetAccessControl($c.FullName, [Security.AccessControl.AccessControlSections]::Access)
            } else {
                [IO.File]::GetAccessControl($c.FullName, [Security.AccessControl.AccessControlSections]::Access)
            }
            if ($sec.AreAccessRulesProtected) { $Protected.Add($c.FullName) }
            # v1's 03-create-share.ps1 used /T, which stamped an explicit
            # syncshare allow on every descendant. An explicit allow outranks an
            # inherited folder deny, so any leftover would keep a known child
            # path readable through a v2 exclusion (D15). Only NON-inherited
            # allows count: the root's inheritable grant is the correct one.
            foreach ($r in $sec.GetAccessRules($true, $false, [Security.Principal.NTAccount])) {
                if ($r.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
                if ($r.IdentityReference.Value -notlike "*\$ShareUser") { continue }
                $LegacyAllow.Add($c.FullName)
                break
            }
        } catch { }
        if ($isDir) { Add-BridgeAclScan -Dir $c.FullName -Protected $Protected -LegacyAllow $LegacyAllow }
    }
}

function Get-BridgeAclScan {
    # Read-only sweep below the sync root for the two conditions §4.2 names:
    # children whose DACL does not inherit (so the root syncshare grant never
    # reaches them), and legacy explicit syncshare allows left by v1's /T.
    param([Parameter(Mandatory)][string]$Path)
    $protected = New-Object System.Collections.Generic.List[string]
    $legacy = New-Object System.Collections.Generic.List[string]
    Add-BridgeAclScan -Dir $Path -Protected $protected -LegacyAllow $legacy
    [pscustomobject]@{ Protected = $protected.ToArray(); LegacyAllow = $legacy.ToArray() }
}

function Get-ProtectedDaclPath {
    # The protected-DACL half of the scan on its own, for 04's boundary scope.
    param([Parameter(Mandatory)][string]$Path)
    return , (Get-BridgeAclScan -Path $Path).Protected
}

function Test-AceGrant {
    # Does $Path carry an allow ACE for $Identity (account name or *SID) that
    # includes every right in $Rights?
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Identity,
        [Parameter(Mandatory)][int]$Rights,
        [bool]$IsDirectory = $true
    )
    $sections = [Security.AccessControl.AccessControlSections]::Access
    $acl = if ($IsDirectory) { [IO.Directory]::GetAccessControl($Path, $sections) }
           else              { [IO.File]::GetAccessControl($Path, $sections) }
    foreach ($r in $acl.GetAccessRules($true, $true, [Security.Principal.NTAccount])) {
        if ($r.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $who = $r.IdentityReference.Value
        if ($who -ne $Identity -and $who -notlike "*\$Identity") { continue }
        if ((([int]$r.FileSystemRights) -band $Rights) -eq $Rights) { return $true }
    }
    return $false
}

function Test-NoShareUserWrite {
    # The D27 boundary: syncshare must not be able to replace the scheduled agent
    # script or forge its trusted state. Read inherited rules too.
    param([Parameter(Mandatory)][string]$Path, [bool]$IsDirectory = $true)
    $writeRights = [int]([Security.AccessControl.FileSystemRights]::Write -bor
                         [Security.AccessControl.FileSystemRights]::Modify -bor
                         [Security.AccessControl.FileSystemRights]::FullControl -bor
                         [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
                         [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
                         [Security.AccessControl.FileSystemRights]::Delete)
    $sections = [Security.AccessControl.AccessControlSections]::Access
    $acl = if ($IsDirectory) { [IO.Directory]::GetAccessControl($Path, $sections) }
           else              { [IO.File]::GetAccessControl($Path, $sections) }
    foreach ($r in $acl.GetAccessRules($true, $true, [Security.Principal.NTAccount])) {
        if ($r.IdentityReference.Value -notlike "*\$ShareUser") { continue }
        if ($r.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if ((([int]$r.FileSystemRights) -band $writeRights) -ne 0) { return $false }
    }
    return $true
}

function Read-IcloudPackageObservation {
    param([string]$UserSid)
    $obs = @{ Present = $false; Error = $null }
    try {
        $present = $false
        $packages = @(Get-AppxPackage -Name $IcloudPackageName -AllUsers -ErrorAction SilentlyContinue)
        foreach ($p in $packages) {
            foreach ($u in @($p.PackageUserInformation)) {
                if ($null -eq $u) { continue }
                $sid = $null
                try { $sid = $u.UserSecurityId.Sid } catch { $sid = $null }
                if ($UserSid -and $sid -ne $UserSid) { continue }
                if ("$($u.InstallState)" -eq 'Installed') { $present = $true }
            }
        }
        $obs.Present = $present
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Read-SyncRootObservation {
    $obs = @{ Exists = $false; IsDirectory = $false; Accessible = $false; Error = $null }
    try {
        if (Test-Path -LiteralPath $SyncRoot) {
            $obs.Exists = $true
            $item = Get-Item -LiteralPath $SyncRoot -Force
            $obs.IsDirectory = ($item -is [IO.DirectoryInfo])
            if ($obs.IsDirectory) {
                Get-ChildItem -LiteralPath $SyncRoot -Force -ErrorAction Stop | Out-Null
                $obs.Accessible = $true
            }
        }
    } catch { $obs.Accessible = $false }
    return $obs
}

function Read-ShareAccountObservation {
    $obs = @{
        Exists = $false; Enabled = $false; PasswordNeverExpires = $false
        AccountNeverExpires = $false; HiddenFromLogon = $false; Error = $null
    }
    try {
        $u = Get-LocalUser -Name $ShareUser -ErrorAction SilentlyContinue
        if ($null -eq $u) { return $obs }
        $obs.Exists = $true
        $obs.Enabled = [bool]$u.Enabled
        $obs.PasswordNeverExpires = ($null -eq $u.PasswordExpires)
        $obs.AccountNeverExpires = ($null -eq $u.AccountExpires)
        $value = $null
        try {
            $value = (Get-ItemProperty -Path $WinlogonUserListKey -Name $ShareUser -ErrorAction Stop).$ShareUser
        } catch { $value = $null }
        $obs.HiddenFromLogon = ($null -ne $value -and [int]$value -eq 0)
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Read-DataShareObservation {
    param([bool]$DependencyMet)
    $obs = @{
        DependencyMet = $DependencyMet; ShareExists = $false
        SharePath = $null; ExpectedPath = $SyncRoot; ShareAccessOk = $false
        ServiceRunning = $false; ServiceAutomatic = $false; FirewallEnabled = $false
        SigningDisabled = $false; EncryptionDisabled = $false; RootAceOk = $false
        Error = $null
    }
    if (-not $DependencyMet) { return $obs }
    try {
        $share = Get-SmbShare -Name $DataShare -ErrorAction SilentlyContinue
        if ($null -ne $share) {
            $obs.ShareExists = $true
            $obs.SharePath = $share.Path
            $access = @(Get-SmbShareAccess -Name $DataShare -ErrorAction SilentlyContinue)
            foreach ($a in $access) {
                if ("$($a.AccountName)" -like "*$ShareUser" -and
                    "$($a.AccessRight)" -eq 'Full' -and "$($a.AccessControlType)" -eq 'Allow') {
                    $obs.ShareAccessOk = $true
                }
            }
        }
        $svc = Get-Service -Name LanmanServer -ErrorAction Stop
        $obs.ServiceRunning = ("$($svc.Status)" -eq 'Running')
        $obs.ServiceAutomatic = ("$($svc.StartType)" -eq 'Automatic')

        $rules = @(Get-NetFirewallRule -DisplayGroup "File and Printer Sharing" -ErrorAction SilentlyContinue)
        $obs.FirewallEnabled = ($rules.Count -gt 0 -and -not ($rules | Where-Object { "$($_.Enabled)" -ne 'True' }))

        # v2 plan D32: wire protection is off on this transport, deliberately.
        $smb = Get-SmbServerConfiguration -ErrorAction Stop
        $obs.SigningDisabled = (-not $smb.RequireSecuritySignature)
        $obs.EncryptionDisabled = ((-not $smb.EncryptData) -and (-not $smb.RejectUnencryptedAccess))

        # One inheritable Modify grant at the root only (D15) — never /T.
        $modify = [int][Security.AccessControl.FileSystemRights]::Modify
        $obs.RootAceOk = (Test-AceGrant -Path $SyncRoot -Identity $ShareUser -Rights $modify -IsDirectory $true)
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Read-BridgeBoundaryObservation {
    param([bool]$DependencyMet)
    $obs = @{
        DependencyMet = $DependencyMet; ExclusionsSafe = $true
        TraversalLinkCount = 0; ProtectedDaclCount = 0; LegacyAllowCount = 0
        ShareExists = $false; SharePath = $null; ExpectedPath = $IoDir
        ShareAccessOk = $false; AbeEnabled = $false
        AgentAuthorityOk = $false; PrivilegeBoundaryOk = $false
        Error = $null; Detail = ''
    }
    if (-not $DependencyMet) { return $obs }
    try {
        # Script 04's fail-closed rule, evaluated read-only: a missing
        # exclusions.json alongside any other install marker is 'blocked', never
        # an invitation to manufacture an empty list.
        $markers = @()
        if ($null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) { $markers += "task $TaskName" }
        if ($null -ne (Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue))        { $markers += "share $ShareName" }
        if (Test-Path -LiteralPath (Join-Path $StateDir 'applied.json'))          { $markers += 'applied.json' }
        if (Test-Path -LiteralPath (Join-Path $StateDir 'v1-pin-cleared.marker')) { $markers += 'migration marker' }
        if (Test-Path -LiteralPath $AgentScript)                                  { $markers += 'installed agent.ps1' }
        if (-not (Test-Path -LiteralPath $ConfigPath) -and $markers.Count -gt 0) {
            $obs.ExclusionsSafe = $false
            $obs.Detail = "exclusions.json is missing but this looks like an existing install (" +
                          ($markers -join ', ') + ")"
            return $obs
        }

        $links = Get-TraversalLinkPath -Path $SyncRoot
        $obs.TraversalLinkCount = @($links).Count
        if ($obs.TraversalLinkCount -gt 0) {
            $obs.Detail = "junction/symlink inside the sync root: " + (@($links)[0])
            return $obs
        }
        $scan = Get-BridgeAclScan -Path $SyncRoot
        $obs.ProtectedDaclCount = @($scan.Protected).Count
        $obs.LegacyAllowCount = @($scan.LegacyAllow).Count
        if ($obs.ProtectedDaclCount -gt 0) {
            $obs.Detail = "$($obs.ProtectedDaclCount) object(s) below the sync root have a protected DACL"
            return $obs
        }
        if ($obs.LegacyAllowCount -gt 0) {
            $obs.Detail = "$($obs.LegacyAllowCount) object(s) below the sync root carry a legacy explicit $ShareUser allow"
        }

        $share = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
        if ($null -ne $share) {
            $obs.ShareExists = $true
            $obs.SharePath = $share.Path
            $access = @(Get-SmbShareAccess -Name $ShareName -ErrorAction SilentlyContinue)
            foreach ($a in $access) {
                if ("$($a.AccountName)" -like "*$ShareUser" -and
                    "$($a.AccessRight)" -eq 'Full' -and "$($a.AccessControlType)" -eq 'Allow') {
                    $obs.ShareAccessOk = $true
                }
            }
        }

        $data = Get-SmbShare -Name $DataShare -ErrorAction SilentlyContinue
        $obs.AbeEnabled = ($null -ne $data -and "$($data.FolderEnumerationMode)" -eq 'AccessBased')

        # D28: the icloud SID carries exactly RC,WDAC on the sync root.
        $agentSid = (Get-LocalUser -Name $AgentUser -ErrorAction Stop).SID.Value
        $rcWdac = [int]([Security.AccessControl.FileSystemRights]::ReadPermissions -bor
                        [Security.AccessControl.FileSystemRights]::ChangePermissions)
        $obs.AgentAuthorityOk = (Test-AceGrant -Path $SyncRoot -Identity "*$agentSid" -Rights $rcWdac -IsDirectory $true)
        if (-not $obs.AgentAuthorityOk) {
            $obs.AgentAuthorityOk = (Test-AceGrant -Path $SyncRoot -Identity $AgentUser -Rights $rcWdac -IsDirectory $true)
        }

        # D27: syncshare has no write anywhere outside io.
        $boundaryOk = $true
        if (Test-Path -LiteralPath $StateDir) {
            if (-not (Test-NoShareUserWrite -Path $StateDir -IsDirectory $true)) { $boundaryOk = $false }
        } else { $boundaryOk = $false }
        if (Test-Path -LiteralPath $IoDir) {
            $modify = [int][Security.AccessControl.FileSystemRights]::Modify
            if (-not (Test-AceGrant -Path $IoDir -Identity $ShareUser -Rights $modify -IsDirectory $true)) { $boundaryOk = $false }
            if (-not (Test-AceGrant -Path $IoDir -Identity $AgentUser -Rights $modify -IsDirectory $true)) { $boundaryOk = $false }
        } else { $boundaryOk = $false }
        $obs.PrivilegeBoundaryOk = $boundaryOk
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Read-AgentInstallObservation {
    param([bool]$DependencyMet, [string]$StagedAgentPath)
    $obs = @{
        DependencyMet = $DependencyMet; ScriptPresent = $false; TaskPresent = $false
        HashMatches = $false; TaskDefinitionMatches = $false; ScriptAclOk = $false
        Error = $null
    }
    if (-not $DependencyMet) { return $obs }
    try {
        $obs.ScriptPresent = Test-Path -LiteralPath $AgentScript
        if ($obs.ScriptPresent -and $StagedAgentPath -and (Test-Path -LiteralPath $StagedAgentPath)) {
            $a = (Get-FileHash -LiteralPath $AgentScript -Algorithm SHA256).Hash
            $b = (Get-FileHash -LiteralPath $StagedAgentPath -Algorithm SHA256).Hash
            $obs.HashMatches = ($a -eq $b)
        }
        if ($obs.ScriptPresent) {
            $rx = [int][Security.AccessControl.FileSystemRights]::ReadAndExecute
            $obs.ScriptAclOk = (Test-AceGrant -Path $AgentScript -Identity $AgentUser -Rights $rx -IsDirectory $false) -and
                               (Test-NoShareUserWrite -Path $AgentScript -IsDirectory $false)
        }
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            $obs.TaskPresent = $true
            $action = @($task.Actions)[0]
            $trigger = @($task.Triggers)[0]
            # Not $matches: that is PowerShell's automatic regex-capture variable.
            $definitionOk =
                ("$($action.Execute)" -match 'powershell(\.exe)?$') -and
                ("$($action.Arguments)" -like "*$AgentScript*") -and
                ("$($task.Principal.LogonType)" -eq 'Interactive') -and
                ("$($task.Principal.RunLevel)" -eq 'Limited') -and
                ("$($task.Principal.UserId)" -like "*$AgentUser") -and
                ($null -ne $trigger -and "$($trigger.CimClass.CimClassName)" -like '*LogonTrigger*') -and
                ([int]$task.Settings.RestartCount -ge 1) -and
                ("$($task.Settings.MultipleInstances)" -eq 'IgnoreNew')
            $obs.TaskDefinitionMatches = [bool]$definitionOk
        }
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Read-AgentRuntimeObservation {
    param([bool]$DependencyMet, [string]$StagedAgentPath, [int]$FreshSeconds = 90)
    $obs = @{
        DependencyMet = $DependencyMet; TaskRunning = $false; StatusFresh = $false
        ProtocolSupported = $false; AgentBuildMatches = $false; Error = $null
    }
    if (-not $DependencyMet) { return $obs }
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $obs.TaskRunning = ($null -ne $task -and "$($task.State)" -eq 'Running')
        if (Test-Path -LiteralPath $StatusPath) {
            $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $StatusPath).LastWriteTimeUtc).TotalSeconds
            $obs.StatusFresh = ($age -ge 0 -and $age -lt $FreshSeconds)
            $doc = Get-Content -LiteralPath $StatusPath -Raw -ErrorAction Stop | ConvertFrom-Json
            # D35: one supported protocol version, compared for equality.
            $obs.ProtocolSupported = ([int]$doc.version -eq 1)
            $staged = Get-StagedAgentBuild -StagedAgentPath $StagedAgentPath
            $obs.AgentBuildMatches = ($null -ne $staged -and $null -ne $doc.agentBuild -and
                                      [int]$doc.agentBuild -eq [int]$staged)
        }
    } catch { $obs.Error = $_.Exception.Message }
    return $obs
}

function Get-GuestChecklist {
    # The complete read-only inspection, run before the first mutation and again
    # in the verifying pass. Downstream probes whose dependency is unmet report
    # 'pending' rather than a meaningless answer (D44).
    param([Parameter(Mandatory)][string]$StagedDir)

    $stagedAgent = Join-Path $StagedDir 'agent.ps1'
    $agentSid = $null
    try { $agentSid = (Get-LocalUser -Name $AgentUser -ErrorAction Stop).SID.Value } catch { $agentSid = $null }

    $result = [ordered]@{}
    $result['icloudPackage'] = New-GuestCheckResult -Id 'icloudPackage' `
        -State (Resolve-IcloudPackageState (Read-IcloudPackageObservation -UserSid $agentSid))

    $syncObs = Read-SyncRootObservation
    $syncState = Resolve-SyncRootState $syncObs
    $result['syncRoot'] = New-GuestCheckResult -Id 'syncRoot' -State $syncState
    $syncOk = ($syncState -eq 'ok')

    $accountObs = Read-ShareAccountObservation
    $accountState = Resolve-ShareAccountState $accountObs
    $result['shareAccount'] = New-GuestCheckResult -Id 'shareAccount' -State $accountState

    $result['shareCredential'] = New-GuestCheckResult -Id 'shareCredential' `
        -State (Resolve-ShareCredentialState) `
        -Detail 'Windows never reveals a local account password'

    $dataObs = Read-DataShareObservation -DependencyMet ($syncOk -and $accountState -ne 'missing')
    $dataState = Resolve-DataShareState $dataObs
    $result['dataShare'] = New-GuestCheckResult -Id 'dataShare' -State $dataState

    $boundaryObs = Read-BridgeBoundaryObservation -DependencyMet ($syncOk -and $accountState -ne 'missing')
    $result['bridgeBoundary'] = New-GuestCheckResult -Id 'bridgeBoundary' `
        -State (Resolve-BridgeBoundaryState $boundaryObs) -Detail ([string]$boundaryObs['Detail'])

    $installObs = Read-AgentInstallObservation -DependencyMet $syncOk -StagedAgentPath $stagedAgent
    $result['agentInstall'] = New-GuestCheckResult -Id 'agentInstall' `
        -State (Resolve-AgentInstallState $installObs)

    $runtimeObs = Read-AgentRuntimeObservation -DependencyMet $syncOk -StagedAgentPath $stagedAgent
    $result['agentRuntime'] = New-GuestCheckResult -Id 'agentRuntime' `
        -State (Resolve-AgentRuntimeState $runtimeObs)

    return $result
}

function ConvertTo-GuestCheckStateMap {
    # [ordered]{id -> result object} -> plain {id -> state}, the shape the pure
    # reasoning functions and the status document both take.
    param([Parameter(Mandatory)]$Checklist)
    $map = @{}
    foreach ($id in $GuestCheckIds) {
        if ($Checklist.Contains($id)) { $map[$id] = $Checklist[$id].State } else { $map[$id] = 'unknown' }
    }
    return $map
}
# ===============================================================================
