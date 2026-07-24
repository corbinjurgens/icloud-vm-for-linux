# icloud-folders.ps1 — per-folder size and hydration breakdown of iCloud Drive.
#
# Run in the guest via the container Samba share:
#   powershell -ExecutionPolicy Bypass -File \\host.lan\Data\icloud-folders.ps1
#
# Purpose: decide WHICH subtrees to pin. Pinning is per-path, so you can keep
# the bulk of the Drive online-only (dataless placeholders, ~0 bytes on disk)
# and hydrate just the folders the Linux host needs to read reliably.
#
# "logicalGB" is what the folder WOULD occupy if fully pinned.
# "local" is how many files are already real bytes on disk.

$root = "$env:USERPROFILE\iCloudDrive"
$RECALL = 0x00400000
$OFFLINE = 0x00001000

"{0,-34} {1,10} {2,7} {3,7}" -f "FOLDER", "logicalGB", "files", "local"
"{0,-34} {1,10} {2,7} {3,7}" -f ("-" * 34), "---------", "-----", "-----"

$rows = foreach ($d in Get-ChildItem $root -Force -Directory -ErrorAction SilentlyContinue) {
  $f = @(Get-ChildItem $d.FullName -Recurse -Force -File -ErrorAction SilentlyContinue)
  $sum = ($f | Measure-Object -Property Length -Sum).Sum
  $local = @($f | Where-Object {
    (([int]$_.Attributes -band $RECALL) -eq 0) -and (([int]$_.Attributes -band $OFFLINE) -eq 0)
  }).Count
  [pscustomobject]@{ Name = $d.Name; GB = [math]::Round($sum / 1GB, 2); Files = $f.Count; Local = $local }
}

foreach ($r in ($rows | Sort-Object GB -Descending)) {
  "{0,-34} {1,10:N2} {2,7} {3,7}" -f $r.Name, $r.GB, $r.Files, $r.Local
}

$loose = @(Get-ChildItem $root -Force -File -ErrorAction SilentlyContinue)
if ($loose.Count) {
  $lsum = ($loose | Measure-Object -Property Length -Sum).Sum
  "{0,-34} {1,10:N2} {2,7} {3,7}" -f "(files at root)", ($lsum / 1GB), $loose.Count, ""
}
