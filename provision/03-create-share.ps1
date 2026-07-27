# ============ 03-create-share.ps1 - run as Administrator ============
# Create/repair the `syncshare` account and the `icloud` data share (v1 D8, v2
# D15/D32). Runs INSIDE the Windows guest, AFTER the Apple ID sign-in and the
# initial iCloud Drive sync, so that C:\Users\icloud\iCloudDrive already exists.
#
# Three mutually exclusive modes:
#
#   Manual (no switches) - the documented fallback. Set $plain below to the
#   SHARE_PASS value from your host .env, then:
#     powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\03-create-share.ps1
#
#   Automated, credential-setting (v2 D41). The app delivers the value and the
#   orchestrator passes the protected local copy, which this script reads once
#   and deletes before it changes the account:
#     ... -File <run>\03-create-share.ps1 -PasswordFile <protected-local-path>
#
#   Automated, credential-preserving (v2 D44). Reconciles only the account's
#   non-secret properties and the data share. Requires an existing account and
#   never constructs or sets a password:
#     ... -File <run>\03-create-share.ps1 -PreserveCredential
#
# Idempotent, and drift-only: an already-correct account, share, service or SMB
# setting is verified and left alone. Safe to re-run after a Windows feature
# update (v1 plan section 10). Exit zero means the desired state was re-probed
# and verified after the change, not merely that the cmdlets were called - an
# orchestrator may not treat a bare exit zero as proof otherwise, because most
# of what this script drives is non-terminating or native.
# =====================================================================

[CmdletBinding(DefaultParameterSetName = 'Manual')]
param(
    [Parameter(Mandatory, ParameterSetName = 'PasswordFile')][string]$PasswordFile,
    [Parameter(Mandatory, ParameterSetName = 'PreserveCredential')][switch]$PreserveCredential
)

$ErrorActionPreference = 'Stop'

# The fixed guest invariants live in one place, so this script and the
# orchestrator that dispatches it cannot disagree about what "correct" means.
$stateLib = Join-Path $PSScriptRoot 'guest-state.ps1'
if (-not (Test-Path -LiteralPath $stateLib)) {
    throw "missing $stateLib - guest-state.ps1 must sit beside this script"
}
. $stateLib

# The parameter sets already make the two switches mutually exclusive; naming
# them here keeps the selected mode obvious at the point of use.
$Mode = if ($PasswordFile) { 'PasswordFile' }
        elseif ($PreserveCredential) { 'PreserveCredential' }
        else { 'Manual' }

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

function Read-DeliveredPassword {
    # Read the whole BOM-less UTF-8 file exactly as delivered - no newline added
    # or removed, no quote processing - and delete it immediately, in a finally,
    # before the account is touched. The grammar is deliberately tiny (v2 plan
    # section 4.1) so the guest account and /etc/credentials-icloud cannot
    # silently receive different passwords.
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path)) { throw "the password file does not exist" }
        $bytes = [IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -eq 0) { throw "the password file is empty" }
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            throw "the password file starts with a UTF-8 BOM"
        }
        foreach ($b in $bytes) {
            if ($b -eq 0x00) { throw "the password file contains a NUL byte" }
            if ($b -eq 0x0D -or $b -eq 0x0A) { throw "the password file contains a line break" }
        }
        return (New-Object System.Text.UTF8Encoding($false, $true)).GetString($bytes)
    } finally {
        if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue }
    }
}

# ============================ preflight (change nothing yet) =================
Step "Preflight ($Mode)"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this script from an elevated PowerShell (Administrator)"
}
if (-not (Test-Path -LiteralPath $SyncRoot)) {
    throw "missing sync root $SyncRoot - finish the Apple ID sign-in and the initial sync first"
}
if (-not ((Get-Item -LiteralPath $SyncRoot -Force) -is [IO.DirectoryInfo])) {
    throw "$SyncRoot exists but is not a directory - diagnose this by hand; nothing here deletes it"
}

$existingUser = Get-LocalUser -Name $ShareUser -ErrorAction SilentlyContinue

$pass = $null
switch ($Mode) {
    'PreserveCredential' {
        if ($null -eq $existingUser) {
            throw ("-PreserveCredential requires an existing '$ShareUser' account and there is none. " +
                   "Re-run with -PasswordFile, or use the manual path.")
        }
    }
    'PasswordFile' {
        $plain = Read-DeliveredPassword $PasswordFile
        $pass = ConvertTo-SecureString $plain -AsPlainText -Force
        $plain = $null
    }
    'Manual' {
        # --- SET THIS: must equal SHARE_PASS in the host .env ---
        $plain = "STRONG_PASSWORD_HERE"
        if ($plain -eq "STRONG_PASSWORD_HERE") {
            throw ("set the value on the marked line above to the SHARE_PASS value from your host " +
                   ".env before running this script, or let the app drive provisioning instead")
        }
        $pass = ConvertTo-SecureString $plain -AsPlainText -Force
        $plain = $null
    }
}

# ============================ 1. the syncshare account =======================
# D8: dedicated password-protected account, SMB use only, hidden from logon.
Step "1/4 Reconciling the '$ShareUser' account"
if ($null -eq $existingUser) {
    New-LocalUser -Name $ShareUser -Password $pass -PasswordNeverExpires `
        -AccountNeverExpires -Description "SMB access for Linux host" | Out-Null
    Write-Host "    created"
} else {
    if ($null -ne $pass) {
        Set-LocalUser -Name $ShareUser -Password $pass
        Write-Host "    password set"
    } else {
        Write-Host "    password preserved"
    }
    # Non-secret property drift, repaired only where it has actually drifted.
    if (-not $existingUser.Enabled) { Enable-LocalUser -Name $ShareUser; Write-Host "    re-enabled" }
    if ($null -ne $existingUser.PasswordExpires) {
        Set-LocalUser -Name $ShareUser -PasswordNeverExpires $true
        Write-Host "    password expiry cleared"
    }
    if ($null -ne $existingUser.AccountExpires) {
        Set-LocalUser -Name $ShareUser -AccountNeverExpires $true
        Write-Host "    account expiry cleared"
    }
}
$pass = $null

# Hide from the Windows login screen.
$hidden = $null
try { $hidden = (Get-ItemProperty -Path $WinlogonUserListKey -Name $ShareUser -ErrorAction Stop).$ShareUser }
catch { $hidden = $null }
if ($null -eq $hidden -or [int]$hidden -ne 0) {
    New-Item -Path $WinlogonUserListKey -Force | Out-Null
    New-ItemProperty -Path $WinlogonUserListKey -Name $ShareUser -Value 0 -PropertyType DWord -Force | Out-Null
    Write-Host "    hidden from the logon screen"
}

# ============================ 2. the sync-root ACE ===========================
# One inheritable grant at the root only -- deliberately NOT /T. A recursive
# grant stamps explicit allow ACEs on every descendant, and an explicit allow
# outranks an inherited folder deny, which would let a known child path stay
# readable through a v2 exclusion (v2 plan D15). Script 04 cleans up the
# explicit descendant grants left by earlier runs of this script.
Step "2/4 Reconciling the '$ShareUser' grant on the sync root"
$modify = [int][Security.AccessControl.FileSystemRights]::Modify
if (Test-AceGrant -Path $SyncRoot -Identity $ShareUser -Rights $modify -IsDirectory $true) {
    Write-Host "    already granted"
} else {
    Invoke-Icacls @($SyncRoot, '/grant', "${ShareUser}:(OI)(CI)M", '/Q') 'granting sync-root access'
}

# ============================ 3. the SMB share and service ===================
Step "3/4 Reconciling the '$DataShare' share, LanmanServer and the SMB settings"
$share = Get-SmbShare -Name $DataShare -ErrorAction SilentlyContinue
if ($null -ne $share -and $share.Path -ne $SyncRoot) {
    Write-Host "    existing '$DataShare' share points at $($share.Path); recreating it at $SyncRoot (no files are deleted)"
    Remove-SmbShare -Name $DataShare -Force
    $share = $null
}
if ($null -eq $share) {
    New-SmbShare -Name $DataShare -Path $SyncRoot -FullAccess $ShareUser | Out-Null
    Write-Host "    share created"
} else {
    # An already-correct share is never recreated; only missing access is added.
    $hasAccess = $false
    foreach ($a in @(Get-SmbShareAccess -Name $DataShare -ErrorAction SilentlyContinue)) {
        if ("$($a.AccountName)" -like "*$ShareUser" -and "$($a.AccessRight)" -eq 'Full' -and
            "$($a.AccessControlType)" -eq 'Allow') { $hasAccess = $true }
    }
    if ($hasAccess) {
        Write-Host "    share already correct"
    } else {
        Grant-SmbShareAccess -Name $DataShare -AccountName $ShareUser -AccessRight Full -Force | Out-Null
        Write-Host "    share access granted"
    }
}

$svc = Get-Service -Name LanmanServer
if ("$($svc.StartType)" -ne 'Automatic') { Set-Service -Name LanmanServer -StartupType Automatic }
if ("$($svc.Status)" -ne 'Running') { Start-Service LanmanServer }

# The guest firewall only ever sees the container network; keep the scope tight anyway.
$fwRules = @(Get-NetFirewallRule -DisplayGroup "File and Printer Sharing" -ErrorAction SilentlyContinue)
if ($fwRules.Count -eq 0 -or ($fwRules | Where-Object { "$($_.Enabled)" -ne 'True' })) {
    Enable-NetFirewallRule -DisplayGroup "File and Printer Sharing"
}

# Wire protection off on this transport (v2 plan D32). The whole path is
# host loopback -> docker-proxy -> container NAT -> QEMU tap: anyone positioned on
# it already has root on the host, so signing and sealing buy nothing and cost a
# per-byte HMAC/GMAC (and AES-GCM) pass on both ends of every hydration read.
# Since 24H2 a stock Windows 11 Pro *requires* signing by default, so this must be
# turned off explicitly; cifs.ko then negotiates an unsigned session on its own and
# the host mount needs no `sign`/`seal` option. Authentication (D8) and the
# exclusion model (D15, ACLs + ABE) are untouched, and SMB 3.1.1 pre-auth integrity
# still protects negotiation. The encryption line is an assertion, not a change:
# it is already the default, and re-running this script after a feature update
# (plan section 10) is what corrects a future Microsoft default-flip.
$smb = Get-SmbServerConfiguration
if ($smb.RequireSecuritySignature) { Set-SmbServerConfiguration -RequireSecuritySignature $false -Force }
if ($smb.EncryptData -or $smb.RejectUnencryptedAccess) {
    Set-SmbServerConfiguration -EncryptData $false -RejectUnencryptedAccess $false -Force
}

# ============================ 4. verify ======================================
Step "4/4 Verifying"
$failures = New-Object System.Collections.Generic.List[string]

$accountState = Resolve-ShareAccountState (Read-ShareAccountObservation)
if ($accountState -eq 'ok') { Write-Host "PASS: the '$ShareUser' account matches the desired state" }
else { $failures.Add("the '$ShareUser' account is '$accountState'") }

$dataState = Resolve-DataShareState (Read-DataShareObservation -DependencyMet $true)
if ($dataState -eq 'ok') { Write-Host "PASS: the '$DataShare' share, service and SMB settings match the desired state" }
else { $failures.Add("the '$DataShare' data share is '$dataState'") }

if ($failures.Count -gt 0) { throw ("the desired state was not reached: " + ($failures -join '; ')) }

# The password itself is never verifiable here: Windows does not reveal or
# validate a local account password, so shareCredential stays 'unverifiable'
# (v2 plan section 4.2). The authenticated host mount is the end-to-end proof.
Write-Host ""
Write-Host "Share ready: \\<guest>\$DataShare as user $ShareUser" -ForegroundColor Green
if ($Mode -eq 'PreserveCredential') { Write-Host "  credential: preserved (not changed by this run)" }
else { Write-Host "  credential: set this run (Windows cannot read it back to confirm)" }
exit 0
# ===============================================
