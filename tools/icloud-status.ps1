# icloud-status.ps1 -- report iCloud Drive hydration state inside the guest.
#
# Delivered to the guest via the container's Samba share (\\host.lan\Data) and
# run with:
#   powershell -ExecutionPolicy Bypass -File \\host.lan\Data\icloud-status.ps1
#
# Tells you whether files are real bytes on disk or dataless placeholders.
#
# Note: online-only placeholders are the NORMAL state. v1's D5 (Files On-Demand
# off, everything pinned) was disproven by live testing on 2026-07-22/23 and is
# superseded by v2 D14/D25: Files On-Demand stays on, this project pins nothing,
# and a placeholder hydrates when the host reads it. A non-zero "placeholder"
# count is expected and is not a fault. What matters is the E0 gate: that a real
# kernel-CIFS read of a placeholder completes and hashes correctly.

$root = "$env:USERPROFILE\iCloudDrive"
if (-not (Test-Path $root)) { Write-Host "SYNC ROOT MISSING: $root"; exit 1 }

$files = @(Get-ChildItem $root -Recurse -Force -File -ErrorAction SilentlyContinue)

# Windows Cloud Files attribute bits
$RECALL_ON_DATA_ACCESS = 0x00400000
$OFFLINE               = 0x00001000
$PINNED                = 0x00080000

$placeholder = @($files | Where-Object {
  ([int]$_.Attributes -band $RECALL_ON_DATA_ACCESS) -ne 0 -or
  ([int]$_.Attributes -band $OFFLINE) -ne 0
})
$pinned = @($files | Where-Object { ([int]$_.Attributes -band $PINNED) -ne 0 })
$sum = ($files | Measure-Object -Property Length -Sum).Sum

Write-Host "root        : $root"
Write-Host "files       : $($files.Count)"
Write-Host "placeholder : $($placeholder.Count)   (online-only Files On-Demand state; expected)"
Write-Host "pinned      : $($pinned.Count)   (v2 pins nothing; a non-zero count is legacy v1 intent)"
Write-Host ("logical GB  : {0:N2}" -f ($sum / 1GB))
