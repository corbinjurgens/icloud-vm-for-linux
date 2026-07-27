# ============ 02-install-icloud.ps1 - run as Administrator ============
# Installs Apple's official iCloud for Windows client.
#
# Fallback (if winget/msstore errors, e.g. region or Store-source issues):
#   open Microsoft Store from Start, search "iCloud", install manually. No
#   Microsoft account sign-in is required for free apps - if prompted, choose
#   the option to proceed without signing in.
# NOTE: with --source msstore the --id is the *Store product ID*, not the AppX
# package name. Passing "AppleInc.iCloud" here fails with "No package found
# matching input criteria" (verified 2026-07-22). 9PKTQ5699M62 is iCloud.
# Confirm with: winget search iCloud
winget install --id 9PKTQ5699M62 --source msstore `
  --accept-package-agreements --accept-source-agreements

# Verify (here AppleInc.iCloud *is* correct -- this is the AppX package name):
Get-AppxPackage AppleInc.iCloud
# =====================================================================
