# ============ 02-install-icloud.ps1 — run as Administrator ============
# Installs Apple's official iCloud for Windows client.
#
# Fallback (if winget/msstore errors, e.g. region or Store-source issues):
#   open Microsoft Store from Start, search "iCloud", install manually. No
#   Microsoft account sign-in is required for free apps — if prompted, choose
#   the option to proceed without signing in.
winget install --id AppleInc.iCloud --source msstore `
  --accept-package-agreements --accept-source-agreements

# Some winget builds resolve msstore entries only by Store product id, not by
# the AppleInc.iCloud moniker. Retry with iCloud's Store id before falling back
# to a manual Store install.
if ($LASTEXITCODE -ne 0) {
  winget install --id 9PKTQ5699M62 --source msstore `
    --accept-package-agreements --accept-source-agreements
}

# Verify:
Get-AppxPackage AppleInc.iCloud
# =====================================================================
