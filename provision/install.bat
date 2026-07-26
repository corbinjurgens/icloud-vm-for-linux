@echo off
REM ============================================================================
REM  install.bat — dockur/windows OEM bootstrap.
REM
REM  dockur copies this whole folder (mounted at /oem) to C:\OEM and runs this
REM  file, as Administrator, at the final step of Windows installation.
REM
REM  Two safe, no-network, no-secret steps run here:
REM    - 01-debloat.ps1  : services, tasks and inbox apps (v1 D3/D12).
REM    - watcher.ps1 -Install : registers the elevated icloud-bridge-provision
REM                      task, which is how the host app drives the rest of
REM                      provisioning (v2 plan D40). It carries no secret and
REM                      touches nothing until the app stages a trigger.
REM
REM  Deliberately NOT automated from here (they need a human, a secret, or the
REM  app's supervision):
REM    - 02-install-icloud.ps1  : msstore/winget can require an interactive Store
REM                               that isn't ready at first boot (plan section 5).
REM    - Apple ID sign-in + 2FA : inherently manual (plan section 6).
REM    - 03-create-share.ps1    : needs the sync root to exist AND the SHARE_PASS
REM                               secret, which we never bake into the image.
REM    - 04-bridge-agent.ps1    : needs the sync root and the syncshare account,
REM                               so it runs after 03 (v2 plan section 4).
REM ============================================================================

echo [OEM] Running debloat...
powershell -ExecutionPolicy Bypass -NoProfile -File "C:\OEM\01-debloat.ps1" > "C:\OEM\01-debloat.log" 2>&1

echo [OEM] Installing the provisioning watcher...
powershell -ExecutionPolicy Bypass -NoProfile -File "C:\OEM\watcher.ps1" -Install > "C:\OEM\watcher-install.log" 2>&1

echo [OEM] Writing next-steps note to the desktop...
> "C:\Users\Public\Desktop\NEXT-STEPS.txt" (
  echo iCloud-on-Linux — remaining one-time setup:
  echo.
  echo The app on the Linux host now drives setup. You only need two things:
  echo.
  echo 1. Open the iCloud Bridge app on the Linux host and start setup there.
  echo 2. When the app asks, sign in to iCloud in this VM with your Apple ID and
  echo    complete 2FA. Turn iCloud Drive ON; LEAVE Files On-Demand ON; leave
  echo    Photos/Mail/etc OFF. Do NOT pin the library — placeholders hydrate when
  echo    the Linux host reads them ^(v2 plan D14/D25^). The app continues on its own
  echo    once the sync folder appears.
  echo.
  echo ----------------------------------------------------------------------------
  echo FALLBACK — the full manual sequence, if the app cannot drive setup:
  echo.
  echo 1. Reboot once ^(debloat has been applied^).
  echo 2. Open PowerShell as Administrator and run:
  echo        powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\02-install-icloud.ps1
  echo    The -ExecutionPolicy Bypass prefix is REQUIRED: Windows blocks .ps1 files by
  echo    default, so launching the script directly fails with "running scripts is disabled".
  echo    If msstore/winget fails, install "iCloud" manually from the Microsoft Store.
  echo 3. Launch iCloud, sign in with your Apple ID, complete 2FA.
  echo    Turn iCloud Drive ON; LEAVE Files On-Demand ON; leave Photos/Mail/etc OFF.
  echo    Do NOT pin the library. Files On-Demand stays on and nothing is pinned:
  echo    placeholders hydrate when the Linux host reads them ^(v2 plan D14/D25^).
  echo    Wait for the initial metadata population to settle, then continue.
  echo 4. Edit C:\OEM\03-create-share.ps1, set SHARE_PASS, then run as Administrator:
  echo        powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\03-create-share.ps1
  echo 5. Install the bridge agent and selective sync ^(as Administrator^):
  echo        powershell -ExecutionPolicy Bypass -NoProfile -File C:\OEM\04-bridge-agent.ps1
  echo 6. Back on the Linux host: set up the CIFS mounts ^(sudo ./host/setup-host.sh^),
  echo    then run the E0 gate in docs/selective-sync.md before trusting the mount.
)

echo [OEM] Bootstrap complete. A reboot is recommended before continuing.
