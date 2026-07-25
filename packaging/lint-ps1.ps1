# lint-ps1.ps1 — parse the repo's PowerShell scripts and report syntax errors.
#
# Runs on the LINUX host under PowerShell 7, which `make lint-ps` fetches into
# build/pwsh. Invoked as:
#
#   pwsh -NoProfile -File packaging/lint-ps1.ps1 provision/01-debloat.ps1 ...
#
# What this proves, precisely: PS 7 parses a superset of the PS 5.1 the guest
# actually runs, so a PASS means the file is syntactically valid but does NOT
# mean it is 5.1-compatible, and nothing here executes the Windows-only parts
# (cfapi.dll/kernel32 interop, System.Security.AccessControl, Get-LocalUser,
# attrib, scheduled tasks, SMB cmdlets).
#
# Exits 1 only on a parse failure. PSScriptAnalyzer findings are printed but do
# not fail the target: several are inherent to what these scripts do (the SMB test
# harness genuinely needs a plaintext share password to mount with) and a gate
# that is red out of the box is a gate everyone learns to ignore. Read them.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Paths
)

if (-not $Paths -or $Paths.Count -eq 0) {
    Write-Host 'No PowerShell files given.'
    exit 1
}

$failed = $false

foreach ($path in $Paths) {
    $resolved = (Resolve-Path -LiteralPath $path).Path
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $resolved, [ref]$null, [ref]$errors) | Out-Null

    if ($errors -and $errors.Count -gt 0) {
        $failed = $true
        Write-Host "FAIL: $path"
        foreach ($e in $errors) {
            Write-Host ("  line {0}: {1}" -f $e.Extent.StartLineNumber, $e.Message)
        }
    }
    else {
        Write-Host "PASS: $path"
    }
}

# PSScriptAnalyzer is not part of the PowerShell tarball. Use it when the operator
# has installed it, and say plainly when it is absent rather than implying the
# scripts were analyzed.
if (Get-Module -ListAvailable -Name PSScriptAnalyzer) {
    Import-Module PSScriptAnalyzer
    Write-Host ''
    Write-Host '==> PSScriptAnalyzer (advisory — does not fail this target)'
    $total = 0
    foreach ($path in $Paths) {
        # Write-Host is the intended output mechanism for these operator-facing
        # scripts, so that rule is noise here; everything else is worth seeing.
        $findings = Invoke-ScriptAnalyzer -Path $path -Severity Error, Warning |
            Where-Object { $_.RuleName -ne 'PSAvoidUsingWriteHost' }
        if (-not $findings) { continue }
        Write-Host "  $path"
        foreach ($f in $findings) {
            $total++
            Write-Host ("    line {0} [{1}] {2}" -f $f.Line, $f.Severity, $f.RuleName)
            Write-Host ("      {0}" -f $f.Message)
        }
    }
    if ($total -eq 0) {
        Write-Host '  none (excluding PSAvoidUsingWriteHost)'
    }
}
else {
    Write-Host 'SKIP: PSScriptAnalyzer is not installed (parse check only).'
    Write-Host '      Install with: pwsh -c "Install-Module PSScriptAnalyzer -Scope CurrentUser"'
}

if ($failed) {
    Write-Host ''
    Write-Host 'FAIL: at least one script did not parse.'
    exit 1
}
exit 0
