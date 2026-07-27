# test-guest-state.ps1 -- exhaust the guest checklist and work-plan state machine.
#
# Runs on the LINUX host under PowerShell 7 (`make lint-ps` fetches it into
# build/pwsh), NOT in the guest. Invoked as:
#
#   pwsh -NoProfile -NonInteractive -File packaging/test-guest-state.ps1
#
# It dot-sources provision/guest-state.ps1 and drives only the pure half of that
# library: the fixed vocabularies, the observation normalizers, the work-plan
# derivation and the repair dispatch. That half is deliberately free of Windows
# cmdlets so this matrix can be exhausted off Windows -- which is the whole
# reason inspection and reasoning are separated in the first place.
#
# Scope limit, stated because it is easy to overclaim: nothing here executes a
# Windows probe, an ACL read, an SMB cmdlet or a scheduled task, and PowerShell 7
# on Linux parses a superset of the Windows PowerShell 5.1 the guest runs. This
# proves the state machine's contract and nothing else. M5 on the real host is
# the Windows proof.
#
# Idempotent and read-only: it reads one script and writes nothing.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $repo 'provision/guest-state.ps1')

$script:fail = 0
$script:pass = 0

function Check {
    param([string]$Label, [string]$Got, [string]$Want)
    if ($Got -ceq $Want) {
        $script:pass++
    } else {
        Write-Host "  FAIL: $Label"
        Write-Host "        want: $Want"
        Write-Host "        got : $Got"
        $script:fail++
    }
}

function CheckThrows {
    param([string]$Label, [scriptblock]$Action)
    try {
        & $Action | Out-Null
        Write-Host "  FAIL: $Label (no error was raised)"
        $script:fail++
    } catch {
        $script:pass++
    }
}

# The test states the vocabularies and the ordering itself rather than importing
# them, so a silent edit to the library shows up here as a failure.
$WantStates = @('pending', 'ok', 'missing', 'drifted', 'blocked', 'unknown', 'unverifiable')
$WantChecks = @('icloudPackage', 'syncRoot', 'shareAccount', 'shareCredential',
                'dataShare', 'bridgeBoundary', 'agentInstall', 'agentRuntime')
$WantWork   = @('install-icloud', 'wait-for-signin', 'create-share-account',
                'reset-share-credential', 'repair-data-share',
                'repair-bridge-boundary', 'update-agent')
$WantPhases = @('staging', 'inspecting', 'installing-icloud', 'launching-icloud',
                'waiting-for-signin', 'waiting-for-secret', 'creating-share',
                'installing-bridge-boundary', 'installing-agent', 'verifying', 'done')

function Get-OrderedWork {
    # Order a set of work IDs by this test's own copy of the dependency order.
    param([string[]]$Ids)
    $out = @()
    foreach ($id in $WantWork) { if ($Ids -contains $id) { $out += $id } }
    return , $out
}

Write-Host '==> fixed vocabularies'
Check 'check states' ($GuestCheckStates -join ',') ($WantStates -join ',')
Check 'check ids'    ($GuestCheckIds -join ',')    ($WantChecks -join ',')
Check 'work ids'     ($GuestWorkIds -join ',')     ($WantWork -join ',')
Check 'phases'       ($GuestPhases -join ',')      ($WantPhases -join ',')

Write-Host '==> typed check results'
Check 'accepts a valid pair' (New-GuestCheckResult -Id 'syncRoot' -State 'ok').State 'ok'
CheckThrows 'rejects an unknown check id'    { New-GuestCheckResult -Id 'nope' -State 'ok' }
CheckThrows 'rejects an unknown check state' { New-GuestCheckResult -Id 'syncRoot' -State 'green' }

Write-Host '==> native exit codes'
CheckThrows 'a non-zero exit throws' { Assert-NativeExitCode -ExitCode 5 -What 'icacls' }
CheckThrows 'a null exit throws'     { Assert-NativeExitCode -ExitCode $null -What 'icacls' }
Assert-NativeExitCode -ExitCode 0 -What 'icacls'
$script:pass++

# --------------------------------------------------------- normalizers -------
# Every normalizer must be total, and an observation that could not be taken at
# all must be 'unknown' rather than absence.

Write-Host '==> icloudPackage normalizer'
Check 'probe failed'  (Resolve-IcloudPackageState @{ Present = $true; Error = 'boom' }) 'unknown'
Check 'null obs'      (Resolve-IcloudPackageState $null) 'unknown'
Check 'registered'    (Resolve-IcloudPackageState @{ Present = $true; Error = $null })  'ok'
Check 'absent'        (Resolve-IcloudPackageState @{ Present = $false; Error = $null }) 'missing'

Write-Host '==> syncRoot normalizer'
Check 'probe failed' (Resolve-SyncRootState @{ Exists = $true; IsDirectory = $true; Accessible = $true; Error = 'x' }) 'unknown'
Check 'absent'       (Resolve-SyncRootState @{ Exists = $false; IsDirectory = $false; Accessible = $false }) 'missing'
Check 'wrong type'   (Resolve-SyncRootState @{ Exists = $true; IsDirectory = $false; Accessible = $false }) 'blocked'
Check 'unreadable'   (Resolve-SyncRootState @{ Exists = $true; IsDirectory = $true; Accessible = $false }) 'blocked'
Check 'healthy'      (Resolve-SyncRootState @{ Exists = $true; IsDirectory = $true; Accessible = $true }) 'ok'

Write-Host '==> shareAccount normalizer'
$acctOk = @{ Exists = $true; Enabled = $true; PasswordNeverExpires = $true
             AccountNeverExpires = $true; HiddenFromLogon = $true; Error = $null }
Check 'healthy' (Resolve-ShareAccountState $acctOk) 'ok'
Check 'absent'  (Resolve-ShareAccountState @{ Exists = $false }) 'missing'
Check 'probe failed' (Resolve-ShareAccountState @{ Exists = $true; Error = 'x' }) 'unknown'
foreach ($flag in @('Enabled', 'PasswordNeverExpires', 'AccountNeverExpires', 'HiddenFromLogon')) {
    $obs = @{}; foreach ($k in $acctOk.Keys) { $obs[$k] = $acctOk[$k] }
    $obs[$flag] = $false
    Check "drift on $flag" (Resolve-ShareAccountState $obs) 'drifted'
}

Write-Host '==> shareCredential normalizer'
# Windows never reveals or validates a local account password: this row is
# always 'unverifiable', and the GUI must never render it as a green claim.
Check 'always unverifiable' (Resolve-ShareCredentialState) 'unverifiable'

Write-Host '==> dataShare normalizer'
$dataOk = @{ DependencyMet = $true; ShareExists = $true; SharePath = 'X'; ExpectedPath = 'X'
             ShareAccessOk = $true; ServiceRunning = $true; ServiceAutomatic = $true
             FirewallEnabled = $true; SigningDisabled = $true; EncryptionDisabled = $true
             RootAceOk = $true; Error = $null }
Check 'healthy'          (Resolve-DataShareState $dataOk) 'ok'
Check 'probe failed'     (Resolve-DataShareState @{ DependencyMet = $true; Error = 'x' }) 'unknown'
Check 'dependency unmet' (Resolve-DataShareState @{ DependencyMet = $false }) 'pending'
Check 'absent'           (Resolve-DataShareState @{ DependencyMet = $true; ShareExists = $false }) 'missing'
$wrongPath = @{}; foreach ($k in $dataOk.Keys) { $wrongPath[$k] = $dataOk[$k] }
$wrongPath['SharePath'] = 'Y'
Check 'wrong share path' (Resolve-DataShareState $wrongPath) 'drifted'
foreach ($flag in @('ShareAccessOk', 'ServiceRunning', 'ServiceAutomatic',
                    'FirewallEnabled', 'SigningDisabled', 'EncryptionDisabled', 'RootAceOk')) {
    $obs = @{}; foreach ($k in $dataOk.Keys) { $obs[$k] = $dataOk[$k] }
    $obs[$flag] = $false
    Check "drift on $flag" (Resolve-DataShareState $obs) 'drifted'
}

Write-Host '==> bridgeBoundary normalizer'
$bndOk = @{ DependencyMet = $true; ExclusionsSafe = $true; TraversalLinkCount = 0
            ProtectedDaclCount = 0; LegacyAllowCount = 0; ShareExists = $true
            SharePath = 'X'; ExpectedPath = 'X'; ShareAccessOk = $true; AbeEnabled = $true
            AgentAuthorityOk = $true; PrivilegeBoundaryOk = $true; Error = $null }
function Get-BoundaryObservation {
    param([hashtable]$Overrides)
    $obs = @{}; foreach ($k in $bndOk.Keys) { $obs[$k] = $bndOk[$k] }
    foreach ($k in $Overrides.Keys) { $obs[$k] = $Overrides[$k] }
    return $obs
}
Check 'healthy'          (Resolve-BridgeBoundaryState $bndOk) 'ok'
Check 'probe failed'     (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ Error = 'x' })) 'unknown'
Check 'dependency unmet' (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ DependencyMet = $false })) 'pending'
# The three strange states: never reinterpreted as absence, never repaired past.
Check 'exclusions lost over an install' (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ ExclusionsSafe = $false })) 'blocked'
Check 'traversal link'   (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ TraversalLinkCount = 1 })) 'blocked'
Check 'protected DACL'   (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ ProtectedDaclCount = 3 })) 'blocked'
# blocked outranks a missing share: data safety wins over convenience.
Check 'blocked beats missing' (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ TraversalLinkCount = 1; ShareExists = $false })) 'blocked'
Check 'share absent'     (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ ShareExists = $false })) 'missing'
Check 'wrong share path' (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ SharePath = 'Y' })) 'drifted'
Check 'legacy explicit allow' (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ LegacyAllowCount = 2 })) 'drifted'
foreach ($flag in @('ShareAccessOk', 'AbeEnabled', 'AgentAuthorityOk', 'PrivilegeBoundaryOk')) {
    Check "drift on $flag" (Resolve-BridgeBoundaryState (Get-BoundaryObservation @{ $flag = $false })) 'drifted'
}

Write-Host '==> agentInstall normalizer'
$insOk = @{ DependencyMet = $true; ScriptPresent = $true; TaskPresent = $true
            HashMatches = $true; TaskDefinitionMatches = $true; ScriptAclOk = $true; Error = $null }
function Get-InstallObservation {
    param([hashtable]$Overrides)
    $obs = @{}; foreach ($k in $insOk.Keys) { $obs[$k] = $insOk[$k] }
    foreach ($k in $Overrides.Keys) { $obs[$k] = $Overrides[$k] }
    return $obs
}
Check 'healthy'          (Resolve-AgentInstallState $insOk) 'ok'
Check 'probe failed'     (Resolve-AgentInstallState (Get-InstallObservation @{ Error = 'x' })) 'unknown'
Check 'dependency unmet' (Resolve-AgentInstallState (Get-InstallObservation @{ DependencyMet = $false })) 'pending'
Check 'nothing installed' (Resolve-AgentInstallState (Get-InstallObservation @{ ScriptPresent = $false; TaskPresent = $false })) 'missing'
Check 'script without task' (Resolve-AgentInstallState (Get-InstallObservation @{ TaskPresent = $false })) 'drifted'
Check 'task without script' (Resolve-AgentInstallState (Get-InstallObservation @{ ScriptPresent = $false })) 'drifted'
foreach ($flag in @('HashMatches', 'TaskDefinitionMatches', 'ScriptAclOk')) {
    Check "drift on $flag" (Resolve-AgentInstallState (Get-InstallObservation @{ $flag = $false })) 'drifted'
}

Write-Host '==> agentRuntime normalizer'
$runOk = @{ DependencyMet = $true; TaskRunning = $true; StatusFresh = $true
            ProtocolSupported = $true; AgentBuildMatches = $true; Error = $null }
function Get-RuntimeObservation {
    param([hashtable]$Overrides)
    $obs = @{}; foreach ($k in $runOk.Keys) { $obs[$k] = $runOk[$k] }
    foreach ($k in $Overrides.Keys) { $obs[$k] = $Overrides[$k] }
    return $obs
}
Check 'healthy'          (Resolve-AgentRuntimeState $runOk) 'ok'
Check 'probe failed'     (Resolve-AgentRuntimeState (Get-RuntimeObservation @{ Error = 'x' })) 'unknown'
Check 'dependency unmet' (Resolve-AgentRuntimeState (Get-RuntimeObservation @{ DependencyMet = $false })) 'pending'
Check 'task not running' (Resolve-AgentRuntimeState (Get-RuntimeObservation @{ TaskRunning = $false })) 'missing'
foreach ($flag in @('StatusFresh', 'ProtocolSupported', 'AgentBuildMatches')) {
    Check "drift on $flag" (Resolve-AgentRuntimeState (Get-RuntimeObservation @{ $flag = $false })) 'drifted'
}

# ------------------------------------------------- checklist completeness ----

Write-Host '==> checklist completeness and convergence'
function Get-TestChecklist {
    param([hashtable]$Overrides = @{})
    $c = @{}
    foreach ($id in $WantChecks) { $c[$id] = 'ok' }
    $c['shareCredential'] = 'unverifiable'
    foreach ($k in $Overrides.Keys) { $c[$k] = $Overrides[$k] }
    return $c
}
Check 'a full checklist is complete' ([string](Test-GuestChecklistComplete (Get-TestChecklist))) 'True'
$short = Get-TestChecklist; $short.Remove('agentRuntime')
Check 'a short checklist is not'     ([string](Test-GuestChecklistComplete $short)) 'False'
$badState = Get-TestChecklist @{ syncRoot = 'green' }
Check 'an invented state is not'     ([string](Test-GuestChecklistComplete $badState)) 'False'

# Convergence: every row 'ok' except the labelled unverifiable credential.
Check 'converged'                 ([string](Test-GuestChecklistConverged (Get-TestChecklist))) 'True'
Check 'credential must not be ok' ([string](Test-GuestChecklistConverged (Get-TestChecklist @{ shareCredential = 'ok' }))) 'False'
foreach ($id in $WantChecks) {
    if ($id -eq 'shareCredential') { continue }
    foreach ($state in $WantStates) {
        if ($state -eq 'ok') { continue }
        Check "not converged with $id=$state" `
            ([string](Test-GuestChecklistConverged (Get-TestChecklist @{ $id = $state }))) 'False'
    }
}

Write-Host '==> blocked-state gate'
foreach ($id in $WantChecks) {
    foreach ($state in $WantStates) {
        $want = if ($state -eq 'blocked' -or $state -eq 'unknown') { $id } else { '' }
        Check "gate for $id=$state" `
            (((Get-GuestBlockedChecks (Get-TestChecklist @{ $id = $state })) -join ',')) $want
    }
}
CheckThrows 'an incomplete checklist cannot derive work' { Get-GuestWorkPlan -Checks $short }

# ------------------------------------------------------ the work matrix ------
# Every (check, state) pair, with everything else healthy, both with and without
# the operator's explicit credential reset. This is the whole derivation
# contract: 'ok', 'pending', 'blocked', 'unknown' and 'unverifiable' schedule
# nothing, and only 'missing'/'drifted' with a named repair owner do.

Write-Host '==> work derivation matrix'
$expected = @{
    'icloudPackage|missing'  = @('install-icloud')
    'icloudPackage|drifted'  = @('install-icloud')
    'syncRoot|missing'       = @('wait-for-signin')
    'shareAccount|missing'   = @('create-share-account')
    'shareAccount|drifted'   = @('repair-data-share')
    'dataShare|missing'      = @('repair-data-share')
    'dataShare|drifted'      = @('repair-data-share')
    'bridgeBoundary|missing' = @('repair-bridge-boundary')
    'bridgeBoundary|drifted' = @('repair-bridge-boundary')
    'agentInstall|missing'   = @('update-agent')
    'agentInstall|drifted'   = @('update-agent')
    'agentRuntime|missing'   = @('update-agent')
    'agentRuntime|drifted'   = @('update-agent')
}

foreach ($id in $WantChecks) {
    foreach ($state in $WantStates) {
        $checks = Get-TestChecklist @{ $id = $state }
        $base = @()
        if ($expected.ContainsKey("$id|$state")) { $base = $expected["$id|$state"] }

        $got = (Get-GuestWorkPlan -Checks $checks -ResetShareCredential $false) -join ','
        Check "plan for $id=$state" $got ((Get-OrderedWork $base) -join ',')

        # An explicit reset adds exactly one work ID -- except when the account
        # is missing, because creating it establishes the credential anyway.
        $withReset = $base
        if ($id -ne 'shareAccount' -or $state -ne 'missing') { $withReset = $base + 'reset-share-credential' }
        $gotReset = (Get-GuestWorkPlan -Checks $checks -ResetShareCredential $true) -join ','
        Check "plan for $id=$state with reset" $gotReset ((Get-OrderedWork $withReset) -join ',')
    }
}

Write-Host '==> work ordering'
# A fresh VM: package, sign-in, account, then share, boundary and agent, in
# dependency order rather than the order the checks happen to be declared in.
$fresh = Get-TestChecklist @{
    icloudPackage = 'missing'; syncRoot = 'missing'; shareAccount = 'missing'
    dataShare = 'missing'; bridgeBoundary = 'missing'
    agentInstall = 'missing'; agentRuntime = 'missing'
}
Check 'fresh VM plan is dependency ordered' `
    ((Get-GuestWorkPlan -Checks $fresh -ResetShareCredential $true) -join ',') `
    'install-icloud,wait-for-signin,create-share-account,repair-data-share,repair-bridge-boundary,update-agent'
Check 'a healthy VM plans nothing' `
    ((Get-GuestWorkPlan -Checks (Get-TestChecklist) -ResetShareCredential $false) -join ',') ''

# ------------------------------------------------------- repair dispatch -----
# Exhaust every subset of the work vocabulary (2^7) against the dispatch rules,
# stated here independently of the library.

Write-Host '==> repair dispatch over every work subset'
$subsets = 0
for ($mask = 0; $mask -lt [Math]::Pow(2, $WantWork.Count); $mask++) {
    $work = @()
    for ($bit = 0; $bit -lt $WantWork.Count; $bit++) {
        if (($mask -band [Math]::Pow(2, $bit)) -ne 0) { $work += $WantWork[$bit] }
    }
    $d = Get-GuestRepairDispatch -Work $work
    $subsets++

    $createOrReset = ($work -contains 'create-share-account') -or ($work -contains 'reset-share-credential')
    $anyShare      = $createOrReset -or ($work -contains 'repair-data-share')
    $boundary      = $work -contains 'repair-bridge-boundary'
    $agent         = $work -contains 'update-agent'

    $wantMode = if ($createOrReset) { 'password-file' }
                elseif ($work -contains 'repair-data-share') { 'preserve-credential' }
                else { 'none' }
    $wantScope = if ($boundary -and $agent) { 'All' }
                 elseif ($boundary) { 'Boundary' }
                 elseif ($agent) { 'Agent' }
                 else { 'none' }

    $label = "[$($work -join ' ')]"
    Check "$label InstallIcloud"   ([string]$d.InstallIcloud)   ([string]($work -contains 'install-icloud'))
    Check "$label WaitForSignin"   ([string]$d.WaitForSignin)   ([string]($work -contains 'wait-for-signin'))
    Check "$label RunShareScript"  ([string]$d.RunShareScript)  ([string]$anyShare)
    Check "$label ShareMode"       $d.ShareMode                 $wantMode
    Check "$label BridgeScope"     $d.BridgeScope               $wantScope
    Check "$label RunBridgeScript" ([string]$d.RunBridgeScript) ([string]($boundary -or $agent))
    # The secret is requested if and only if the account is being created or the
    # credential explicitly reset. Nothing else may ever pull it into the guest.
    Check "$label NeedsSecret"     ([string]$d.NeedsSecret)     ([string]$createOrReset)
}
Check 'every subset was exercised' ([string]$subsets) '128'
CheckThrows 'an unknown work id is rejected' { Get-GuestRepairDispatch -Work @('rm -rf') }

Write-Host '==> agent-only run (the D35 recovery case)'
# The load-bearing scenario: a bundled agent newer than the installed one, with
# everything else healthy. Script 03 must not run, the boundary repair scope must
# not run, and no secret may be requested.
$agentOnly = Get-TestChecklist @{ agentInstall = 'drifted'; agentRuntime = 'drifted' }
$agentPlan = Get-GuestWorkPlan -Checks $agentOnly -ResetShareCredential $false
Check 'plans only update-agent' ($agentPlan -join ',') 'update-agent'
$agentDispatch = Get-GuestRepairDispatch -Work $agentPlan
Check 'script 03 does not run'        ([string]$agentDispatch.RunShareScript) 'False'
Check 'no share mode is selected'     $agentDispatch.ShareMode 'none'
Check 'no secret is requested'        ([string]$agentDispatch.NeedsSecret) 'False'
Check 'the boundary scope is skipped' $agentDispatch.BridgeScope 'Agent'
Check 'no iCloud install'             ([string]$agentDispatch.InstallIcloud) 'False'
Check 'no sign-in wait'               ([string]$agentDispatch.WaitForSignin) 'False'

Write-Host '==> the inspections a first run really performs'
# The work matrix above feeds each check independently; this is the sequence a
# fresh VM produces, one inspection at a time. A probe whose dependency is unmet
# reports 'pending' and schedules nothing, so the boundary repair can only enter
# the plan at the inspection that follows the share work. A dispatch derived
# before that carries no boundary work at all - which is why guest-setup.ps1
# re-derives after script 03 instead of reusing the sign-in pass's dispatch.

# 1. Nothing yet: no package and no sync root, so every dependent probe pends.
$firstPass = Get-TestChecklist @{
    icloudPackage = 'missing'; syncRoot = 'missing'; shareAccount = 'missing'
    dataShare = 'pending'; bridgeBoundary = 'pending'
    agentInstall = 'pending'; agentRuntime = 'pending'
}
$firstPlan = Get-GuestWorkPlan -Checks $firstPass -ResetShareCredential $false
Check 'the first pass plans only what it can see' `
    ($firstPlan -join ',') 'install-icloud,wait-for-signin,create-share-account'
Check 'and dispatches no bridge scope' (Get-GuestRepairDispatch -Work $firstPlan).BridgeScope 'none'

# 2. After the sign-in: the sync root exists, so the agent probes answer, but the
#    syncshare account does not exist yet and the boundary probe still cannot.
$afterSignin = Get-TestChecklist @{
    shareAccount = 'missing'; dataShare = 'pending'; bridgeBoundary = 'pending'
    agentInstall = 'missing'; agentRuntime = 'missing'
}
$signinPlan = Get-GuestWorkPlan -Checks $afterSignin -ResetShareCredential $false
Check 'the sign-in pass cannot plan the boundary' ($signinPlan -join ',') 'create-share-account,update-agent'
Check 'so its scope would install the agent alone' `
    (Get-GuestRepairDispatch -Work $signinPlan).BridgeScope 'Agent'

# 3. After script 03: the account and the data share exist, the boundary answers
#    at last, and one 'All' scope builds both halves of the bridge.
$afterShare = Get-TestChecklist @{
    bridgeBoundary = 'missing'; agentInstall = 'missing'; agentRuntime = 'missing'
}
$sharePlan = Get-GuestWorkPlan -Checks $afterShare -ResetShareCredential $false
Check 'the share pass plans the boundary and the agent' `
    ($sharePlan -join ',') 'repair-bridge-boundary,update-agent'
Check 'and its scope covers both' (Get-GuestRepairDispatch -Work $sharePlan).BridgeScope 'All'

Write-Host ''
if ($script:fail -gt 0) {
    Write-Host "FAIL: $($script:fail) assertion(s) failed, $($script:pass) passed."
    exit 1
}
Write-Host "PASS: $($script:pass) assertions over the guest check/work state matrix."
exit 0
