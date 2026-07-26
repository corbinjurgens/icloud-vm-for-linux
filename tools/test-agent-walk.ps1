# test-agent-walk.ps1 -- emission-order check for the agent's directory walks.
#
# Runs on the Linux host under PowerShell 7 (`make test-ps` fetches it into
# build/pwsh), NOT in the guest. Like test-bridge-json.ps1 it dot-sources
# nothing: it extracts the native helper and Compare-RelPathDfs from
# guest-agent/agent.ps1 and drives them over fixtures.
#
# Why this exists. The ordered walks (Get-SweepCandidates, Build-Node) and the
# ACL resume cursor share one ordering contract: names sort OrdinalIgnoreCase
# with an Ordinal tiebreak, and Compare-RelPathDfs must rank relative paths in
# exactly the DFS-preorder sequence such a walk emits. If the two ever
# disagree, a resumed reconciliation pass can permanently skip subtrees it has
# never visited. The expected orders below were captured from the
# pre-optimization scriptblock comparator, so this is a regression test for the
# compiled SortByName rather than a restatement of it.
#
# Scope limit: nothing here touches a real filesystem, CfAPI or Windows
# PowerShell 5.1. Enumerate itself cannot run on Linux; SortByName and
# Compare-RelPathDfs are pure managed/PowerShell code and can.
#
# Idempotent and read-only: it reads the agent script and writes nothing.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $repo 'guest-agent/agent.ps1'

if (-not (Test-Path $agent)) { throw "cannot find $agent" }
$text = Get-Content -Raw $agent

$nativeMatch = [regex]::Match($text, "(?s)\`$nativeSource\s*=\s*@'\r?\n(.*?)\r?\n'@")
if (-not $nativeMatch.Success) { throw 'could not locate $nativeSource in agent.ps1' }
Add-Type -TypeDefinition $nativeMatch.Groups[1].Value -Language CSharp

$cmpMatch = [regex]::Match($text, '(?sm)^function Compare-RelPathDfs \{.*?^\}')
if (-not $cmpMatch.Success) { throw 'could not locate Compare-RelPathDfs in agent.ps1' }
Invoke-Expression $cmpMatch.Value

$fail = 0
function Check([string]$label, [bool]$ok) {
    if ($ok) { Write-Host "  PASS: $label" }
    else     { Write-Host "  FAIL: $label"; $script:fail++ }
}

function New-Entry([string]$Name, [bool]$IsDirectory) {
    $e = New-Object NativeEntry
    $e.Name = $Name
    $e.IsDirectory = $IsDirectory
    return $e
}

Write-Host '==> SortByName matches the retired scriptblock comparator'
# Includes the tiebreak cases the contract exists for: case-fold groups ordered
# by Ordinal (ALPHA < Alpha < alpha), separators below and above letters
# ('a b' < 'a-b' < 'a.b' < 'ab'), and non-ASCII above ASCII.
$eacuteLower = [string][char]0x00E9
$eacuteUpper = [string][char]0x00C9
$omega       = [string][char]0x03A9
$cjk         = [string][char]0x732B
$shuffled = @(
    'zeta', 'a b', 'ALPHA', 'file10', '.hidden', 'a-b', 'Alpha', 'file2',
    'ab', 'alpha', '!bang', 'a.b', 'Z last', 'File2', 'a',
    $eacuteLower, $eacuteUpper, $cjk, $omega
)
$expected = @(
    '!bang', '.hidden', 'a', 'a b', 'a-b', 'a.b', 'ab', 'ALPHA', 'Alpha',
    'alpha', 'file10', 'File2', 'file2', 'Z last', 'zeta',
    $eacuteUpper, $eacuteLower, $omega, $cjk
)
$entries = [NativeEntry[]]@($shuffled | ForEach-Object { New-Entry $_ $false })
$entries = [IcloudBridgeNative]::SortByName($entries)
Check 'captured order reproduced' (@($entries | ForEach-Object { $_.Name }) -join '|' -ceq ($expected -join '|'))

Write-Host '==> SortByName tolerates what Get-Entries actually returns'
# Callers assign collected function output: Object[] for many entries, a bare
# NativeEntry for one, $null for an empty directory (see Get-Entries).
$null1 = [IcloudBridgeNative]::SortByName($null)
Check 'null in, null out' ($null -eq $null1)
function Get-OneEntry { return [NativeEntry[]]@(New-Entry 'only' $false) }
$one = Get-OneEntry               # collected to a bare scalar by PowerShell
$one = [IcloudBridgeNative]::SortByName($one)
Check 'scalar in, one-element array out' ($one -is [array] -and @($one).Count -eq 1 -and $one[0].Name -eq 'only')
function Get-TwoEntries { return [NativeEntry[]]@((New-Entry 'b' $false), (New-Entry 'a' $false)) }
$two = Get-TwoEntries             # collected to Object[] by PowerShell
$two = [IcloudBridgeNative]::SortByName($two)
Check 'Object[] in, sorted NativeEntry[] out' ($two -is [NativeEntry[]] -and $two[0].Name -eq 'a' -and $two[1].Name -eq 'b')

Write-Host '==> DFS-preorder emission agrees with Compare-RelPathDfs'
# A model of Build-Node's traversal: sort each directory with SortByName, emit
# each entry's relative path, recurse into directories in place. The fixture
# plants the documented trap: sibling names containing characters below '/'
# (space) around a directory, where a flat string compare disagrees with the
# walk ('a b' < 'a/z' as strings, but the walk emits a/z first).
$tree = [ordered]@{
    'a'       = [ordered]@{ 'z' = $null; '!x' = $null }
    'a b'     = $null
    'ALPHA'   = [ordered]@{ 'sub' = [ordered]@{ 'deep' = $null } }
    'Alpha b' = $null
    'Z z'     = [ordered]@{ 'q' = $null }
}
function Get-EmittedPaths([System.Collections.Specialized.OrderedDictionary]$Node, [string]$Rel) {
    $entries = [NativeEntry[]]@($Node.Keys | ForEach-Object { New-Entry $_ ($null -ne $Node[$_]) })
    $entries = [IcloudBridgeNative]::SortByName($entries)
    $out = @()
    foreach ($e in $entries) {
        $childRel = if ($Rel -eq '') { $e.Name } else { "$Rel/$($e.Name)" }
        $out += $childRel
        if ($e.IsDirectory) { $out += Get-EmittedPaths $Node[$e.Name] $childRel }
    }
    return $out
}
$emitted = @(Get-EmittedPaths $tree '')
$wantEmitted = @('a', 'a/!x', 'a/z', 'a b', 'ALPHA', 'ALPHA/sub', 'ALPHA/sub/deep', 'Alpha b', 'Z z', 'Z z/q')
Check 'preorder sequence matches the captured expectation' (($emitted -join '|') -ceq ($wantEmitted -join '|'))

$monotonic = $true
for ($i = 0; $i -lt $emitted.Count - 1; $i++) {
    if ((Compare-RelPathDfs $emitted[$i] $emitted[$i + 1]) -ge 0) {
        Write-Host "        not increasing at: '$($emitted[$i])' vs '$($emitted[$i + 1])'"
        $monotonic = $false
    }
}
Check 'Compare-RelPathDfs ranks the emission strictly increasing' $monotonic

# The trap spelled out: a flat compare orders these siblings' subtrees wrongly.
Check 'flat compare would disagree (guards the reason this test exists)' `
    (([string]::Compare('a b', 'a/z', [StringComparison]::OrdinalIgnoreCase) -lt 0) -and
     ((Compare-RelPathDfs 'a/z' 'a b') -lt 0))
Check 'ancestors rank before descendants' ((Compare-RelPathDfs 'a' 'a/z') -lt 0)

Write-Host ''
if ($fail -gt 0) {
    Write-Host "FAIL: $fail walk-order check(s) failed"
    exit 1
}
Write-Host 'PASS: walk emission order and the DFS cursor comparator agree'
exit 0
