# ============ 01-debloat.ps1 — run as Administrator ============
# Idempotent. Auto-run by provision/install.bat at first boot, and safe to re-run
# after a Windows feature update reset something (plan section 10).
$ErrorActionPreference = "Continue"

# --- Services not needed on a sync appliance ---
$services = @(
  "WSearch",        # Search indexer: the classic CPU/RAM hog over big sync folders
  "SysMain",        # Superfetch
  "DiagTrack",      # Telemetry
  "WMPNetworkSvc",  # Media sharing
  "MapsBroker",
  "Fax",
  "RemoteRegistry"
)
foreach ($s in $services) {
  Stop-Service $s -Force -ErrorAction SilentlyContinue
  Set-Service  $s -StartupType Disabled -ErrorAction SilentlyContinue
}

# --- Defender: exclude the sync root; kill scheduled scans (D11) ---
$icloudPath = "$env:USERPROFILE\iCloudDrive"
Add-MpPreference -ExclusionPath $icloudPath
Add-MpPreference -ExclusionProcess "iCloudServices.exe","iCloudDrive.exe","secd.exe"
Set-MpPreference -ScanScheduleDay 8            # 8 = never
Set-MpPreference -DisableCatchupFullScan  $true
Set-MpPreference -DisableCatchupQuickScan $true

# --- Windows Update: notify-only, never auto-reboot (D12) ---
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty $au -Name AUOptions -Value 2 -Type DWord                       # notify before download
Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord

# --- Disable VBS/HVCI (RAM + perf win under KVM) ---
$dg = "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard"
Set-ItemProperty $dg -Name EnableVirtualizationBasedSecurity -Value 0 -Type DWord -ErrorAction SilentlyContinue

# --- Power: never sleep, never hibernate, display off is fine ---
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

# --- Misc quieting ---
# Edge preload
$edge = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $edge -Force | Out-Null
Set-ItemProperty $edge -Name "StartupBoostEnabled" -Value 0 -Type DWord
# Content delivery / suggested apps
$cdm = "HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
Set-ItemProperty $cdm -Name "SilentInstalledAppsEnabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue

# --- Remove obvious inbox bloat (safe list only; do NOT touch Store/AppX infra, D3) ---
$bloat = @("Microsoft.XboxApp","Microsoft.XboxGamingOverlay","Microsoft.ZuneMusic",
           "Microsoft.ZuneVideo","Microsoft.BingNews","Microsoft.BingWeather",
           "Microsoft.GamingApp","Microsoft.People","Microsoft.Todos",
           "MicrosoftTeams","Microsoft.549981C3F5F10")   # last = Cortana
foreach ($b in $bloat) {
  Get-AppxPackage -AllUsers $b | Remove-AppxPackage -ErrorAction SilentlyContinue
}

Write-Host "`nDebloat complete. Reboot now with: Restart-Computer" -ForegroundColor Green
# ================================================================
