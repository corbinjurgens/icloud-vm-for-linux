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
REM ============================================================================

echo [OEM] Running debloat...
powershell -ExecutionPolicy Bypass -NoProfile -File "C:\OEM\01-debloat.ps1" > "C:\OEM\01-debloat.log" 2>&1

echo [OEM] Writing next-steps note to the desktop...
> "C:\Users\Public\Desktop\NEXT-STEPS.txt" (
  echo iCloud-on-Linux — remaining one-time setup ^(see docs/implementation-plan.md^):
  echo.
  echo 1. Reboot once ^(debloat has been applied^).
  echo 2. Open PowerShell as Administrator and run:
  echo        C:\OEM\02-install-icloud.ps1
  echo    If msstore/winget fails, install "iCloud" manually from the Microsoft Store.
  echo 3. Launch iCloud, sign in with your Apple ID, complete 2FA.
  echo    Turn iCloud Drive ON; DISABLE Files On-Demand; leave Photos/Mail/etc OFF.
  echo    Wait for the initial sync to finish, then pin everything:
  echo        attrib +P -U "%%USERPROFILE%%\iCloudDrive\*" /S /D
  echo 4. Edit C:\OEM\03-create-share.ps1, set SHARE_PASS, run it as Administrator.
  echo 5. Back on the Linux host: set up the CIFS mount ^(host/ systemd units^).
)

echo [OEM] Bootstrap complete. A reboot is recommended before continuing.
