# test-smb-hydration.ps1 -- does an SMB read hydrate a dataless placeholder?
#
# HISTORICAL EVIDENCE, kept so the finding can be reproduced. This was the
# empirical test of plan D5's core assumption. v1 asserted that dataless
# placeholders "stall or fail" when served over SMB, which is why it pinned
# everything.
#
# It ran on 2026-07-22/23 and the answer was YES: placeholders hydrate on
# demand, with correct content and checksums. D5 is therefore disproven and
# superseded by v2 D14/D25 -- Files On-Demand stays on and nothing is pinned.
# The results are recorded in docs/plan-gui-selective-sync.md section 0.5.
#
# Note the scope limit that E0 exists to close: this test drives userland
# `smbclient`, not the kernel cifs client the real mount uses, and the largest
# file it read was 1.17 MB.
#
# Run in the guest as Administrator:
#   powershell -ExecutionPolicy Bypass -File \\host.lan\Data\test-smb-hydration.ps1 -Password '<SHARE_PASS>'
#
# DELIBERATELY MINIMAL / NON-DESTRUCTIVE:
#   * Grants NTFS rights on ONE small test folder -- never the whole 101 GB root.
#   * READ-ONLY share (-ReadAccess), so an SMB client cannot modify your files.
#   * Creates nothing inside the sync root; deletes nothing, ever.
#   * Undo with:  tools/test-smb-hydration.ps1 -Cleanup
[CmdletBinding()]
param(
  [string]$Password,
  [string]$RelPath  = "",          # folder under the sync root; "" = auto-pick smallest
  [string]$ShareName = "icloudtest",
  [switch]$Cleanup
)

$ErrorActionPreference = "Stop"
$root = "$env:USERPROFILE\iCloudDrive"
$user = "syncshare"

if ($Cleanup) {
  Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue | Remove-SmbShare -Force
  Write-Host "removed share $ShareName (local account and ACLs left intact)" -ForegroundColor Yellow
  exit 0
}

# --- pick a SMALL folder so any hydration downloads trivial data ---
if (-not $RelPath) {
  $cand = Get-ChildItem $root -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $f = @(Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue)
    [pscustomobject]@{
      Name  = $_.Name
      Full  = $_.FullName
      Bytes = ($f | Measure-Object -Property Length -Sum).Sum
      Count = $f.Count
    }
  } | Where-Object { $_.Count -ge 1 -and $_.Bytes -gt 0 } | Sort-Object Bytes | Select-Object -First 1
  if (-not $cand) { Write-Host "no suitable test folder found"; exit 1 }
  $RelPath = $cand.Name
  Write-Host ("auto-picked smallest folder: {0}  ({1:N2} MB, {2} files)" -f $cand.Name, ($cand.Bytes/1MB), $cand.Count)
}
$test = Join-Path $root $RelPath
if (-not (Test-Path $test)) { Write-Host "missing: $test"; exit 1 }

# --- local account for SMB auth (idempotent; no logon rights change) ---
if (-not (Get-LocalUser -Name $user -ErrorAction SilentlyContinue)) {
  if (-not $Password) { Write-Host "-Password required to create $user"; exit 1 }
  $sec = ConvertTo-SecureString $Password -AsPlainText -Force
  New-LocalUser -Name $user -Password $sec -PasswordNeverExpires -AccountNeverExpires `
    -Description "SMB access for Linux host" | Out-Null
  Write-Host "created local user $user"
} elseif ($Password) {
  Set-LocalUser -Name $user -Password (ConvertTo-SecureString $Password -AsPlainText -Force)
  Write-Host "reset password for existing $user"
}
# Hide from the logon screen (plan D8)
$wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
New-Item -Path $wl -Force | Out-Null
New-ItemProperty -Path $wl -Name $user -Value 0 -PropertyType DWord -Force | Out-Null

# --- READ-ONLY rights, scoped to the test folder only ---
icacls $test /grant "${user}:(OI)(CI)RX" /T /Q | Out-Null

if (-not (Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name $ShareName -Path $test -ReadAccess $user | Out-Null
}
Set-Service -Name LanmanServer -StartupType Automatic
Start-Service LanmanServer -ErrorAction SilentlyContinue
Enable-NetFirewallRule -DisplayGroup "File and Printer Sharing" -ErrorAction SilentlyContinue

# --- report the placeholder state of the test data ---
$RECALL = 0x00400000; $PINNED = 0x00080000; $UNPINNED = 0x00100000
Write-Host "`nshare  : \\<guest>\$ShareName  ->  $test" -ForegroundColor Green
Write-Host "files  :"
Get-ChildItem $test -Recurse -Force -File -ErrorAction SilentlyContinue |
  Select-Object -First 12 | ForEach-Object {
    $a = [int]$_.Attributes
    $state = if ($a -band $RECALL) { "DATALESS" } elseif ($a -band $PINNED) { "pinned" } else { "local" }
    "{0,-46} {1,10:N0} B  {2}" -f $_.Name, $_.Length, $state
  }
Write-Host "`nNow read a DATALESS file from the Linux host and see whether it hydrates."
