@echo off
REM ============================================================================
REM  install.bat — dockur/windows OEM bootstrap.
REM
REM  dockur copies this whole folder (mounted at /oem) to C:\OEM and runs this
REM  file, as Administrator, at the final step of Windows installation.
REM
REM  We ONLY auto-run the safe, no-network, no-secret step here: 01-debloat.ps1.
REM
REM  Deliberately NOT automated (they need a human or a secret):
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

echo [OEM] Writing next-steps note to the desktop...
> "C:\Users\Public\Desktop\NEXT-STEPS.txt" (
  echo iCloud-on-Linux — remaining one-time setup ^(see docs/implementation-plan.md^):
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
