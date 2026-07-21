# ============ 02-install-icloud.ps1 — run as Administrator ============
# Installs Apple's official iCloud for Windows client.
#
# Fallback (if winget/msstore errors, e.g. region or Store-source issues):
#   open Microsoft Store from Start, search "iCloud", install manually. No
#   Microsoft account sign-in is required for free apps — if prompted, choose
#   the option to proceed without signing in.
winget install --id AppleInc.iCloud --source msstore `
  --accept-package-agreements --accept-source-agreements

# Verify:
Get-AppxPackage AppleInc.iCloud
# =====================================================================
