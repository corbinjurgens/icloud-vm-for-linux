# test-bridge-json.ps1 -- byte-identity check for the agent's JSON serializer.
#
# Runs on the Linux host under PowerShell 7 (`make test-ps` fetches it into
# build/pwsh), NOT in the guest. It dot-sources nothing: it extracts the
# serializer region from guest-agent/agent.ps1 by marker, compiles the native
# helper the same way the agent does, and drives the result over a fixture set.
#
#   ./build/pwsh/pwsh -NoLogo -File tools/test-bridge-json.ps1
#
# Why this exists. v2 plan section 2 pins the *output* of the bridge documents,
# not how they are produced, so the serializer is free to be rewritten for speed
# -- but only if it emits the same bytes. The GUI's readers are strict (D35) and
# a single changed escape would break every reader at once. The expectations
# below were captured from the pre-optimization implementation and then asserted
# against the rewrite, so this is a genuine regression test rather than a
# restatement of whatever the code currently does.
#
# Scope limit, stated because it is easy to overclaim: PowerShell 7 on Linux
# parses a superset of the Windows PowerShell 5.1 the agent actually runs under,
# and nothing here exercises CfAPI, ACLs or the filesystem. This proves the
# serializer's output contract and nothing else about the agent.
#
# Idempotent and read-only: it reads the agent script and writes nothing.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $repo 'guest-agent/agent.ps1'

if (-not (Test-Path $agent)) { throw "cannot find $agent" }
$text = Get-Content -Raw $agent

# Pull the C# helper out of its here-string and compile it, so JsonString is the
# same code the agent runs.
$nativeMatch = [regex]::Match($text, "(?s)\`$nativeSource\s*=\s*@'\r?\n(.*?)\r?\n'@")
if (-not $nativeMatch.Success) { throw 'could not locate $nativeSource in agent.ps1' }
Add-Type -TypeDefinition $nativeMatch.Groups[1].Value -Language CSharp

# Pull the serializer functions out by marker and define them here.
$region = [regex]::Match(
    $text, '(?s)# --- JSON output -+\r?\n(.*?)\r?\nfunction Write-JsonAtomic')
if (-not $region.Success) { throw 'could not locate the JSON output region in agent.ps1' }
Invoke-Expression $region.Groups[1].Value

$fail = 0
function Check([string]$label, [string]$got, [string]$want) {
    if ($got -ceq $want) {
        Write-Host "  PASS: $label"
    } else {
        Write-Host "  FAIL: $label"
        Write-Host "        want: $want"
        Write-Host "        got : $got"
        $script:fail++
    }
}

Write-Host '==> string escaping'
Check 'plain'              (ConvertTo-BridgeJsonString 'Documents')      '"Documents"'
Check 'empty'              (ConvertTo-BridgeJsonString '')               '""'
Check 'quote'              (ConvertTo-BridgeJsonString 'a"b')            '"a\"b"'
Check 'backslash'          (ConvertTo-BridgeJsonString 'a\b')            '"a\\b"'
Check 'both'               (ConvertTo-BridgeJsonString '"\')             '"\"\\"'
Check 'backspace'          (ConvertTo-BridgeJsonString "a$([char]8)b")   '"a\bb"'
Check 'tab'                (ConvertTo-BridgeJsonString "a`tb")           '"a\tb"'
Check 'newline'            (ConvertTo-BridgeJsonString "a`nb")           '"a\nb"'
Check 'formfeed'           (ConvertTo-BridgeJsonString "a$([char]12)b")  '"a\fb"'
Check 'carriage return'    (ConvertTo-BridgeJsonString "a`rb")           '"a\rb"'
Check 'other control (1)'  (ConvertTo-BridgeJsonString "a$([char]1)b")   '"a\u0001b"'
Check 'other control (31)' (ConvertTo-BridgeJsonString "a$([char]31)b")  '"a\u001fb"'
Check 'DEL stays raw'      (ConvertTo-BridgeJsonString "a$([char]127)b") "`"a$([char]127)b`""
Check 'non-ASCII stays raw' (ConvertTo-BridgeJsonString ('caf' + [char]0xE9)) ('"caf' + [char]0xE9 + '"')
Check 'astral stays raw'   (ConvertTo-BridgeJsonString "a$([char]0xD83D)$([char]0xDE00)b") "`"a$([char]0xD83D)$([char]0xDE00)b`""

Write-Host '==> scalars'
Check 'null'        (ConvertTo-BridgeJson $null)            'null'
Check 'true'        (ConvertTo-BridgeJson $true)            'true'
Check 'false'       (ConvertTo-BridgeJson $false)           'false'
Check 'int'         (ConvertTo-BridgeJson 42)               '42'
Check 'negative'    (ConvertTo-BridgeJson (-7))             '-7'
Check 'int64'       (ConvertTo-BridgeJson ([int64]9007199254740993)) '9007199254740993'
Check 'byte'        (ConvertTo-BridgeJson ([byte]255))      '255'
Check 'double'      (ConvertTo-BridgeJson ([double]1.5))    '1.5'
Check 'double int'  (ConvertTo-BridgeJson ([double]2))      '2'
Check 'string'      (ConvertTo-BridgeJson 'hi')             '"hi"'

Write-Host '==> collections'
Check 'empty array'   (ConvertTo-BridgeJson @())                    '[]'
Check 'array'         (ConvertTo-BridgeJson @(1, 2, 3))             '[1,2,3]'
Check 'mixed array'   (ConvertTo-BridgeJson @(1, 'a', $true, $null)) '[1,"a",true,null]'
Check 'nested array'  (ConvertTo-BridgeJson @(@(1), @(2, 3)))       '[[1],[2,3]]'
Check 'empty object'  (ConvertTo-BridgeJson ([ordered]@{}))         '{}'

$ordered = [ordered]@{ zebra = 1; apple = 2; middle = 'x' }
Check 'key order preserved' (ConvertTo-BridgeJson $ordered) '{"zebra":1,"apple":2,"middle":"x"}'

$nested = [ordered]@{
    version = 1
    root    = [ordered]@{ name = ''; dirs = @() }
    list    = @([ordered]@{ path = 'a\b'; bytes = [int64]10 })
}
Check 'document shape' (ConvertTo-BridgeJson $nested) `
    '{"version":1,"root":{"name":"","dirs":[]},"list":[{"path":"a\\b","bytes":10}]}'

Write-Host '==> depth guard'
$deep = [ordered]@{ v = 1 }
for ($i = 0; $i -lt 70; $i++) { $deep = [ordered]@{ v = $deep } }
$threw = $false
try { [void](ConvertTo-BridgeJson $deep) } catch { $threw = $true }
if ($threw) { Write-Host '  PASS: nesting past the depth limit throws' }
else { Write-Host '  FAIL: nesting past the depth limit did not throw'; $fail++ }

Write-Host ''
if ($fail -gt 0) {
    Write-Host "FAIL: $fail serializer check(s) failed"
    exit 1
}
Write-Host 'PASS: the bridge JSON serializer emits the expected bytes'
exit 0
