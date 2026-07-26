# ============ 03-create-share.ps1 — run as Administrator ============
# Run this AFTER Apple ID sign-in and the initial iCloud Drive sync, so that the
# sync root C:\Users\icloud\iCloudDrive already exists.
#
# Set $pass below to the SHARE_PASS value from your host .env — must match the
# host credentials file exactly. This script is idempotent; safe to re-run after
# a Windows feature update (plan section 10).

# --- SET THIS: must equal SHARE_PASS in the host .env ---
$pass = ConvertTo-SecureString "STRONG_PASSWORD_HERE" -AsPlainText -Force

# D8: dedicated password-protected account, SMB use only, hidden from logon
if (-not (Get-LocalUser -Name "syncshare" -ErrorAction SilentlyContinue)) {
  New-LocalUser -Name "syncshare" -Password $pass -PasswordNeverExpires `
    -AccountNeverExpires -Description "SMB access for Linux host"
} else {
  Set-LocalUser -Name "syncshare" -Password $pass
}
# Hide from the Windows login screen
$wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
New-Item -Path $wl -Force | Out-Null
New-ItemProperty -Path $wl -Name "syncshare" -Value 0 -PropertyType DWord -Force | Out-Null

# Filesystem permission for syncshare on the sync root.
# One inheritable grant at the root only -- deliberately NOT /T. A recursive
# grant stamps explicit allow ACEs on every descendant, and an explicit allow
# outranks an inherited folder deny, which would let a known child path stay
# readable through a v2 exclusion (v2 plan D15). Script 04 cleans up the
# explicit descendant grants left by earlier runs of this script.
$root = "C:\Users\icloud\iCloudDrive"
icacls $root /grant "syncshare:(OI)(CI)M" /Q

# The SMB share itself
if (-not (Get-SmbShare -Name "icloud" -ErrorAction SilentlyContinue)) {
  New-SmbShare -Name "icloud" -Path $root -FullAccess "syncshare"
}

# SMB service + firewall (guest firewall only sees the container network; keep scope tight anyway)
Set-Service -Name LanmanServer -StartupType Automatic
Start-Service LanmanServer
Enable-NetFirewallRule -DisplayGroup "File and Printer Sharing"

# Wire protection off on this transport (v2 plan D32). The whole path is
# host loopback -> docker-proxy -> container NAT -> QEMU tap: anyone positioned on
# it already has root on the host, so signing and sealing buy nothing and cost a
# per-byte HMAC/GMAC (and AES-GCM) pass on both ends of every hydration read.
# Since 24H2 a stock Windows 11 Pro *requires* signing by default, so this must be
# turned off explicitly; cifs.ko then negotiates an unsigned session on its own and
# the host mount needs no `sign`/`seal` option. Authentication (D8) and the
# exclusion model (D15, ACLs + ABE) are untouched, and SMB 3.1.1 pre-auth integrity
# still protects negotiation. The encryption line is an assertion, not a change:
# it is already the default, and re-running this script after a feature update
# (plan section 10) is what corrects a future Microsoft default-flip.
Set-SmbServerConfiguration -RequireSecuritySignature $false -Force
Set-SmbServerConfiguration -EncryptData $false -RejectUnencryptedAccess $false -Force

Write-Host "Share ready: \\<guest>\icloud as user syncshare" -ForegroundColor Green
# ===============================================
