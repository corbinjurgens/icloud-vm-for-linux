# ============ 01-debloat.ps1 - run as Administrator ============
# Idempotent. Auto-run by provision/install.bat at first boot, and safe to re-run
# after a Windows feature update reset something (plan section 10).
#
# Everything here targets the same two costs: resident RAM in a 3 GB guest whose
# desktop session is logged on forever, and background disk churn that inflates
# the thin-provisioned qcow2 image on the host (v2 plan section 8.1).
$ErrorActionPreference = "Continue"

# --- The RTC is UTC, and Windows must be told so ---
# QEMU presents the emulated clock in UTC; Windows assumes the RTC holds local
# time. Left alone, a guest that Setup placed in Pacific time reads a UTC RTC as
# a Pacific wall clock and computes a UTC that is hours ahead of the host's -
# observed live as a 7-hour skew. Nothing in the guest looks wrong (the desktop
# clock even matches the host's UTC by coincidence), but every UTC stamp the
# agent and the orchestrator publish is future-dated, and D23 reads a
# future-dated status exactly as it should: not fresh. Both halves are set, so
# the guest is correct whichever way a later Windows build resolves the RTC, and
# the displayed local time is then the same UTC the host reasons in.
$tzKey = "HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
Set-ItemProperty -Path $tzKey -Name RealTimeIsUniversal -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
& tzutil.exe /s UTC
# Takes full effect at the next boot, when Windows re-reads the RTC. A running
# session is nudged now so the first provisioning run does not publish skewed
# stamps; a failure here is not fatal, because the boot after this fixes it.
# The resync only works while W32Time is running: on the live guest the service
# was stopped, so the call failed silently (0x80070426) and the whole first
# session stayed hours skewed until the next boot. Automatic keeps the service
# up for later sessions too, so they do not drift between boots.
Set-Service W32Time -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service W32Time -ErrorAction SilentlyContinue
$resync = & w32tm.exe /resync /force 2>&1
if ($LASTEXITCODE -ne 0) {
  # 2>&1 above folds w32tm's stderr into $resync, so one line carries the reason.
  Write-Warning "time resync failed: $((($resync | ForEach-Object { "$_".Trim() }) -join ' ').Trim())"
}

# --- Services not needed on a sync appliance ---
# NEVER extend this list with: AppXSvc, ClipSVC, InstallService, LicenseManager,
# StorSvc, DoSvc, wuauserv, cryptsvc (Store/servicing stack - hard rule 5, D3/D12),
# TermService (the RDP maintenance path), LanmanServer (the whole bridge),
# Schedule (the agent's logon task, D17), W32Time (Kerberos/TLS and the Apple
# session need sane time), CldFlt/FltMgr (Files On-Demand, D14), or
# TabletInputService/TextInputManagementService (on Windows 11 that breaks
# keyboard entry into Start, Settings and UWP apps - and iCloud is a Store app
# whose sign-in the operator types into).
$services = @(
  "WSearch",        # Search indexer: the classic CPU/RAM hog over big sync folders
  "SysMain",        # Superfetch
  "DiagTrack",      # Telemetry
  "WMPNetworkSvc",  # Media sharing
  "MapsBroker",
  "Fax",
  "RemoteRegistry",
  # printing: no printer is reachable from this guest
  "Spooler", "PrintNotify",
  # diagnostics policy/tracing hosts (WerSvc itself is configured below, not disabled)
  "wercplsupport", "DPS", "WdiServiceHost", "WdiSystemHost", "PcaSvc",
  "dmwappushservice",
  # hardware this QEMU guest does not have
  "lfsvc", "WbioSrvc", "bthserv", "BTAGService", "stisvc", "WiaRpc",
  "SCardSvr", "ScDeviceEnum", "SEMgrSvc",
  # consumer/entertainment surfaces
  "XblAuthManager", "XblGameSave", "XboxNetApiSvc", "XboxGipSvc",
  "PhoneSvc", "WalletService", "RetailDemo", "WpcMonSvc",
  # networking features unused behind dockur's NAT
  "icssvc", "SSDPSRV", "upnphost", "DusmSvc"
)
foreach ($s in $services) {
  Stop-Service $s -Force -ErrorAction SilentlyContinue
  Set-Service  $s -StartupType Disabled -ErrorAction SilentlyContinue
}

# --- Error reporting: silent local capture, never a dialog ---
# An unattended appliance wants crashes recorded and nothing shown. WerSvc used to
# be in the list above, and with WER also Disabled=1 the 2026-07-27 iCloudHome.exe
# fast-fail (0xC0000409, twice) produced no Event 1000, no report and no dump,
# while the kernel hard-error path parked a modal "System Error" box on a desktop
# nobody is watching. So: restore the service, force reporting back on, suppress
# all UI. WerSvc is trigger-started (it runs only while a crash is being recorded),
# so it costs no resident RAM in the 3 GB guest. Nothing is uploaded either - the
# QueueReporting task below stays disabled, which makes capture purely local.
Set-Service WerSvc -StartupType Manual -ErrorAction SilentlyContinue
# Live system key (Consent, ExcludedApplications and friends live under it), so
# create only what is missing, for the same reason the DeviceGuard block below does.
$wer = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting"
if (-not (Test-Path $wer)) { New-Item -Path $wer -Force | Out-Null }
Set-ItemProperty $wer -Name Disabled   -Value 0 -Type DWord -Force   # observed as 1 on the live guest
Set-ItemProperty $wer -Name DontShowUI -Value 1 -Type DWord -Force
# Per-app LocalDumps only, so a crash of one of iCloud's own processes leaves a
# minidump behind. No global LocalDumps key: that would dump every process in the
# guest. No DumpFolder either - the default %LOCALAPPDATA%\CrashDumps of the
# crashing user needs no pre-created directory or extra ACL.
foreach ($exe in @("iCloudHome.exe", "iCloudDrive.exe", "iCloudCKKS.exe", "ApplePhotoStreams.exe")) {
  $ld = Join-Path $wer "LocalDumps\$exe"
  if (-not (Test-Path $ld)) { New-Item -Path $ld -Force | Out-Null }
  Set-ItemProperty $ld -Name DumpType  -Value 1 -Type DWord -Force   # 1 = minidump
  Set-ItemProperty $ld -Name DumpCount -Value 3 -Type DWord -Force
}

# --- Maintenance tasks that scan or rewrite the whole volume ---
# ScheduledDefrag is deliberately NOT here: on an SSD-presented volume it performs
# retrim, which is exactly what hands blocks freed by the D26 reclamation sweep
# back to the sparse qcow2 image. UpdateOrchestrator\* (protected, and D12 keeps
# Update alive) and MicrosoftEdgeUpdate* (services WebView2, which iCloud sign-in
# uses) are left alone for the same reason.
$tasks = @(
  '\Microsoft\Windows\Application Experience\ProgramDataUpdater',
  '\Microsoft\Windows\Application Experience\StartupAppTask',
  '\Microsoft\Windows\Application Experience\MareBackup',
  '\Microsoft\Windows\Application Experience\PcaPatchDbTask',
  '\Microsoft\Windows\Customer Experience Improvement Program\Consolidator',
  '\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip',
  '\Microsoft\Windows\Windows Error Reporting\QueueReporting',
  '\Microsoft\Windows\Maintenance\WinSAT',
  '\Microsoft\Windows\Maps\MapsUpdateTask',
  '\Microsoft\Windows\Maps\MapsToastTask',
  '\Microsoft\XblGameSave\XblGameSaveTask',
  '\Microsoft\Windows\Power Efficiency Diagnostics\AnalyzeSystem',
  '\Microsoft\Windows\Speech\SpeechModelDownloadTask',
  '\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector'
)
foreach ($t in $tasks) {
  Disable-ScheduledTask -TaskName (Split-Path $t -Leaf) `
    -TaskPath ((Split-Path $t) + '\') -ErrorAction SilentlyContinue | Out-Null
}

# --- Defender: exclude the sync root; kill scheduled scans (D11) ---
# Real-time protection stays ON: the guest holds a live Apple session, and a full
# disable fights Tamper Protection anyway. Only paths this project itself churns
# are excluded - never powershell.exe as a process.
$icloudPath = "$env:USERPROFILE\iCloudDrive"
Add-MpPreference -ExclusionPath $icloudPath
# The bridge control directory: the agent rewrites status.json every 15 s and the
# host writes requests into it over SMB, so every one of those writes would
# otherwise be scanned. It is JSON-only by construction and the executable agent
# and its private state live outside it (D27). Created later by 04-bridge-agent.ps1;
# excluding a not-yet-existing path is fine and keeps the Defender policy in one file.
Add-MpPreference -ExclusionPath "C:\ProgramData\icloud-bridge\io"
Add-MpPreference -ExclusionProcess "iCloudServices.exe","iCloudDrive.exe","secd.exe"
Set-MpPreference -ScanScheduleDay 8            # 8 = never
Set-MpPreference -DisableCatchupFullScan  $true
Set-MpPreference -DisableCatchupQuickScan $true

# --- Windows Update: notify-only, never auto-reboot (D12) ---
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty $au -Name AUOptions -Value 2 -Type DWord                       # notify before download
Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord

# Delivery Optimization: HTTP only (mode 0). This kills peer caching and its
# cache-scan I/O while leaving the DoSvc *service* running - disabling the service
# would break Store/winget downloads and therefore iCloud updates (hard rule 5).
$do = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization"
New-Item -Path $do -Force | Out-Null
Set-ItemProperty $do -Name DODownloadMode -Value 0 -Type DWord

# Telemetry: 1 = Required/Basic, the lowest value Pro honours. DiagTrack is
# already disabled above; this stops the queueing side from doing work at all.
$dc = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection"
New-Item -Path $dc -Force | Out-Null
Set-ItemProperty $dc -Name AllowTelemetry -Value 1 -Type DWord

# --- Disable VBS/HVCI (RAM + perf win under KVM) ---
# If any VBS scenario re-arms, Windows runs its own hypervisor nested under KVM:
# every syscall and page-table operation pays nested-virtualization cost and the
# Secure Kernel pins a few hundred MB. Close all the re-enable paths, not just the
# top-level switch. Verify after a reboot with msinfo32 or Get-CimInstance
# Win32_DeviceGuard. iCloud has no dependency on VBS or Credential Guard.
# These are live system keys, so create only what is missing rather than using
# New-Item -Force, which would take the whole key with it.
$dg = "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard"
if (-not (Test-Path $dg)) { New-Item -Path $dg -Force | Out-Null }
Set-ItemProperty $dg -Name EnableVirtualizationBasedSecurity -Value 0 -Type DWord -ErrorAction SilentlyContinue
foreach ($scenario in @("HypervisorEnforcedCodeIntegrity", "CredentialGuard")) {
  $key = Join-Path $dg "Scenarios\$scenario"
  if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
  Set-ItemProperty $key -Name Enabled -Value 0 -Type DWord -ErrorAction SilentlyContinue
}
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Lsa" -Name LsaCfgFlags -Value 0 -Type DWord -ErrorAction SilentlyContinue
bcdedit /set hypervisorlaunchtype off | Out-Null

# --- Power: never sleep, never hibernate, display off is fine ---
# The monitor timeout matters here: the `icloud` user is logged on forever, so
# without it DWM composites for a screen nobody is watching and QEMU keeps an
# active display device. Sync services are session services, not UI-bound.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 5
powercfg /hibernate off

# --- Storage: stop the guest inflating the sparse qcow2 image ---
# Fixed pagefile. System-managed sizing grows and shrinks pagefile.sys, and blocks
# the qcow2 has once allocated never come back without host-side compaction. Do
# NOT remove the pagefile: 3 GB of RAM plus Defender plus iCloud's initial metadata
# sync genuinely needs commit headroom (the D10 floor is 2.5 GB for a reason), and
# memory compression stays on (default) so the guest mostly stays off it anyway.
$PagefileMB = 4096
try {
  $cs = Get-CimInstance Win32_ComputerSystem
  if ($cs.AutomaticManagedPagefile) {
    $cs | Set-CimInstance -Property @{ AutomaticManagedPagefile = $false }
  }
  $pf = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue
  if ($null -eq $pf) {
    # Set-WmiInstance rather than New-CimInstance: creating a Win32_PageFileSetting
    # instance is one of the cases the CIM cmdlet does not support on 5.1.
    Set-WmiInstance -Class Win32_PageFileSetting `
      -Arguments @{ Name = "C:\pagefile.sys"; InitialSize = $PagefileMB; MaximumSize = $PagefileMB } | Out-Null
  } elseif ($pf.InitialSize -ne $PagefileMB -or $pf.MaximumSize -ne $PagefileMB) {
    $pf | Set-CimInstance -Property @{ InitialSize = $PagefileMB; MaximumSize = $PagefileMB }
  }
} catch {
  Write-Warning "pagefile sizing skipped: $($_.Exception.Message)"
}

# Reserved Storage: Windows 11 holds back ~7 GB for update staging. The D26 sweep
# already guarantees a 20 GB free floor on this volume, so updates have scratch
# space without the reservation, and the reclaimed space raises the distance to
# that floor - directly fewer sweep episodes. This is Microsoft's supported knob
# and does not touch the servicing stack. It fails while servicing is in flight;
# re-running the script later applies it.
try {
  if ((DISM /Online /Get-ReservedStorageState) -match "Enabled") {
    DISM /Online /Set-ReservedStorageState /State:Disabled | Out-Null
  }
} catch {
  Write-Warning "reserved storage state unchanged: $($_.Exception.Message)"
}

# --- Misc quieting ---
# Edge preload
$edge = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $edge -Force | Out-Null
Set-ItemProperty $edge -Name "StartupBoostEnabled" -Value 0 -Type DWord
# Widgets board (its WebView2 hosts are the largest reclaimable RAM block in the
# always-logged-on session). This is the Widgets *app* and its policy - the
# Evergreen WebView2 runtime that iCloud sign-in uses is a separate component and
# stays installed (hard rule 5).
$dsh = "HKLM:\SOFTWARE\Policies\Microsoft\Dsh"
New-Item -Path $dsh -Force | Out-Null
Set-ItemProperty $dsh -Name "AllowNewsAndInterests" -Value 0 -Type DWord
# Copilot: policy plus the AppX removals below, because which one applies depends
# on the build.
$copilot = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot"
New-Item -Path $copilot -Force | Out-Null
Set-ItemProperty $copilot -Name "TurnOffWindowsCopilot" -Value 1 -Type DWord
# Content delivery / suggested apps, and best-performance visual effects for a
# desktop nobody looks at. These are HKCU writes: dockur runs install.bat from the
# unattend first-logon command in the auto-logon `icloud` session, so they land in
# that profile. Re-running the script as `icloud` restores them if a feature update
# resets the profile.
$cdm = "HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
Set-ItemProperty $cdm -Name "SilentInstalledAppsEnabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue
$vfx = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"
if (-not (Test-Path $vfx)) { New-Item -Path $vfx -Force | Out-Null }
Set-ItemProperty $vfx -Name "VisualFXSetting" -Value 2 -Type DWord -ErrorAction SilentlyContinue

# --- Remove obvious inbox bloat (safe list only; do NOT touch Store/AppX infra, D3) ---
$bloat = @("Microsoft.XboxApp","Microsoft.XboxGamingOverlay","Microsoft.ZuneMusic",
           "Microsoft.ZuneVideo","Microsoft.BingNews","Microsoft.BingWeather",
           "Microsoft.GamingApp","Microsoft.People","Microsoft.Todos",
           "MicrosoftTeams","MSTeams",                      # old and current package names
           "Microsoft.Copilot","Microsoft.Windows.Ai.Copilot.Provider",
           "MicrosoftWindows.Client.WebExperience",         # Widgets
           "Microsoft.549981C3F5F10")   # last = Cortana
foreach ($b in $bloat) {
  Get-AppxPackage -AllUsers $b | Remove-AppxPackage -ErrorAction SilentlyContinue
}

# --- OneDrive: a second Files On-Demand engine competing with iCloud ---
# Removing the OneDrive *app* does not remove cldflt.sys; the Cloud Files filter is
# an inbox driver that iCloud's placeholders keep using. Never disable CldFlt itself.
$onedrive = "$env:SystemRoot\SysWOW64\OneDriveSetup.exe"
if (-not (Test-Path $onedrive)) { $onedrive = "$env:SystemRoot\System32\OneDriveSetup.exe" }
if (Get-Process OneDrive -ErrorAction SilentlyContinue) {
  Stop-Process -Name OneDrive -Force -ErrorAction SilentlyContinue
}
if (Test-Path $onedrive) {
  Start-Process $onedrive -ArgumentList "/uninstall" -Wait -ErrorAction SilentlyContinue
}

# --- Deliberately left alone (do not "optimise" these later) ---
#   * WpnService / WpnUserService: iCloud is a Store/MSIX app and WNS is the
#     platform notification path. ~20 MB is not worth risking Store plumbing.
#   * NTFS last-access updates: the agent's D26 reclamation sweep sorts LRU by
#     LastAccessTime and checks NtfsDisableLastAccessUpdate. Setting
#     `fsutil behavior set disablelastaccess 1` would silently degrade eviction
#     ordering to LastWriteTime.
#   * Memory compression (Disable-MMAgent -MemoryCompression): in a 3 GB guest it
#     trades cheap CPU for avoided pagefile I/O - the right trade on qcow2.
#   * Defender real-time protection: stays on (D11).

Write-Host "`nDebloat complete. Reboot now with: Restart-Computer" -ForegroundColor Green
# ================================================================
