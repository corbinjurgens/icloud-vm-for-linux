# profile-windows-idle.ps1 -- attribute the guest's idle CPU to named processes.
#
# What:    takes two per-process counter samples around an idle interval and
#          reports the DELTAS between them -- CPU, working set, private bytes and
#          disk read/write. A lifetime total says nothing about a current cost,
#          which is why nothing here prints one. Read-only: it starts, stops,
#          disables and configures nothing.
# Where:   inside the Windows guest. Delivered through the container's Samba
#          share the same way as tools/icloud-status.ps1 (see
#          docs/automation-notes.md) and run from a PowerShell window:
#            powershell -ExecutionPolicy Bypass -NoProfile `
#              -File \\host.lan\Data\profile-windows-idle.ps1 -Seconds 300
#          Reading the WMI performance classes may require an elevated window;
#          the script says so plainly rather than reporting partial data.
# Invoke:  -Seconds <10-3600>  the idle interval (default 300, matching the host
#                              side's `tools/vcpu-profile.py --seconds 300`)
#          -Top <5-200>        how many process rows to print (default 40)
#          -OutDir <path>      where the report is written (default \\host.lan\Data)
#          -OutFile <path>     an exact destination instead of OutDir/<stamp>
# Idempotent: yes -- it reads counters and writes one new timestamped report.
#
# Privacy. The report holds process names, PIDs, Windows service names and
# counter numbers, and nothing else. It never records command lines, environment
# variables, file paths, window titles, user names, Apple account data or file
# contents -- the same boundary the host-side diagnostic report keeps (v2 plan
# D37).
#
# Reading the result. This measures *what the guest is doing*, not what should
# be switched off: CHANGELOG rows R-012 and R-019 to R-022 already close
# Defender, WNS, memory compression, ScheduledDefrag, the input services and the
# Store/servicing stack. Naming one of those here records an accepted recurring
# cost. A row is actionable only when its delta repeats across runs and is large
# enough to explain a useful share of the guest-mode CPU the host profiler sees.
# Run it three times after the desktop has settled, alongside three matching
# host samples, and note that this script's own PowerShell process appears in
# its own report.

[CmdletBinding()]
param(
    [ValidateRange(10, 3600)][int]$Seconds = 300,
    [ValidateRange(5, 200)][int]$Top = 40,
    [string]$OutDir = '\\host.lan\Data',
    [string]$OutFile = ''
)

$ErrorActionPreference = 'Stop'

$PerfClass = 'Win32_PerfRawData_PerfProc_Process'

# The processor-time counters are cumulative 100 ns ticks.
$TicksPerSecond = 1e7

function Read-Sample {
    # `_Total` and `Idle` both report IDProcess 0, so they are pulled out by
    # name and every real process is keyed by PID: the instance name carries a
    # "#n" suffix that can move between samples, a PID cannot.
    $procs = @{}
    $total = $null
    $idle = $null
    foreach ($row in Get-CimInstance -ClassName $PerfClass) {
        $entry = [pscustomobject]@{
            Name       = ($row.Name -replace '#\d+$', '')
            Cpu100ns   = [uint64]$row.PercentProcessorTime
            WorkingSet = [uint64]$row.WorkingSetPrivate
            Private    = [uint64]$row.PrivateBytes
            ReadBytes  = [uint64]$row.IOReadBytesPersec
            WriteBytes = [uint64]$row.IOWriteBytesPersec
        }
        if ($row.Name -eq '_Total') { $total = $entry }
        elseif ($row.Name -eq 'Idle') { $idle = $entry }
        else { $procs[[int]$row.IDProcess] = $entry }
    }
    return [pscustomobject]@{
        At    = (Get-Date).ToUniversalTime()
        Procs = $procs
        Total = $total
        Idle  = $idle
    }
}

function Get-Rise([uint64]$before, [uint64]$after) {
    # A counter that went backwards means the instance was replaced under a
    # reused PID; nothing can be attributed to it, so it contributes zero.
    if ($after -lt $before) { return [double]0 }
    return [double]($after - $before)
}

Write-Host "==> Taking the first sample"
try {
    $first = Read-Sample
} catch {
    Write-Host "FAIL: cannot read $PerfClass ($($_.Exception.Message))"
    Write-Host '      Re-run this script from an elevated PowerShell window.'
    exit 1
}

Write-Host "==> Leave the guest idle for $Seconds s -- do not touch the desktop"
Start-Sleep -Seconds $Seconds
$second = Read-Sample

$elapsed = ($second.At - $first.At).TotalSeconds
if ($elapsed -le 0) { Write-Host 'FAIL: the clock did not advance'; exit 1 }

# PID -> hosted Windows service names, so a Service Host row says which services
# it holds. Service names only; nothing else about them is read.
$services = @{}
try {
    foreach ($svc in Get-CimInstance -ClassName Win32_Service -Property Name, ProcessId) {
        $owner = [int]$svc.ProcessId
        if ($owner -le 0) { continue }
        if (-not $services.ContainsKey($owner)) { $services[$owner] = @() }
        $services[$owner] += [string]$svc.Name
    }
} catch {
    Write-Host 'NOTE: service names are unavailable in this session; PIDs only.'
}

$rows = @()
$startedCount = 0
foreach ($key in $second.Procs.Keys) {
    $now = $second.Procs[$key]
    $was = $first.Procs[$key]
    $fresh = ($null -eq $was)
    if ($fresh) {
        # Started during the interval: its whole counter *is* the delta.
        $startedCount++
        $cpu = [double]$now.Cpu100ns / $TicksPerSecond
        $read = [double]$now.ReadBytes
        $write = [double]$now.WriteBytes
        $privateDelta = [double]$now.Private
    } else {
        $cpu = (Get-Rise $was.Cpu100ns $now.Cpu100ns) / $TicksPerSecond
        $read = Get-Rise $was.ReadBytes $now.ReadBytes
        $write = Get-Rise $was.WriteBytes $now.WriteBytes
        $privateDelta = [double]$now.Private - [double]$was.Private
    }
    $svc = ''
    if ($services.ContainsKey([int]$key)) {
        $names = @($services[[int]$key] | Sort-Object)
        $svc = ($names | Select-Object -First 6) -join ','
        if ($names.Count -gt 6) { $svc += ",+$($names.Count - 6)" }
    }
    $rows += [pscustomobject]@{
        Cpu      = $cpu
        Percent  = 100.0 * $cpu / $elapsed
        Name     = $now.Name
        Pid      = [int]$key
        New      = $fresh
        WorkMB   = [double]$now.WorkingSet / 1MB
        PrivMB   = $privateDelta / 1MB
        ReadKiBs = $read / 1KB / $elapsed
        WritKiBs = $write / 1KB / $elapsed
        Services = $svc
    }
}

$exitedCount = 0
foreach ($key in $first.Procs.Keys) {
    if (-not $second.Procs.ContainsKey($key)) { $exitedCount++ }
}

$busy = 0.0
$idleCpu = 0.0
if ($first.Total -and $second.Total) {
    $busy = (Get-Rise $first.Total.Cpu100ns $second.Total.Cpu100ns) / $TicksPerSecond
}
if ($first.Idle -and $second.Idle) {
    $idleCpu = (Get-Rise $first.Idle.Cpu100ns $second.Idle.Cpu100ns) / $TicksPerSecond
}
$busy = $busy - $idleCpu
$attributed = ($rows | Measure-Object -Property Cpu -Sum).Sum
if ($null -eq $attributed) { $attributed = 0.0 }

$ranked = @($rows | Where-Object { $_.Cpu -gt 0 -or $_.ReadKiBs -gt 0 -or $_.WritKiBs -gt 0 } |
    Sort-Object -Property Cpu -Descending)
$shown = @($ranked | Select-Object -First $Top)
$cutCpu = 0.0
if ($ranked.Count -gt $shown.Count) {
    $cutCpu = ($ranked | Select-Object -Skip $Top | Measure-Object -Property Cpu -Sum).Sum
    if ($null -eq $cutCpu) { $cutCpu = 0.0 }
}

$lines = @()
$lines += 'iCloud bridge - Windows guest idle profile (deltas only)'
$lines += ('generated : {0}Z' -f $second.At.ToString('yyyy-MM-ddTHH:mm:ss'))
$lines += ('interval  : {0:N1} s, two samples of {1}' -f $elapsed, $PerfClass)
$lines += ('processes : {0} at the end, {1} started and {2} exited during it' -f
           $second.Procs.Count, $startedCount, $exitedCount)
$lines += ''
$lines += 'aggregate cross-check (core-seconds over the interval)'
$lines += ('  all processes but Idle : {0,9:N2}   ({1,5:N1}% of one core)' -f
           $busy, (100.0 * $busy / $elapsed))
$lines += ('  attributed below       : {0,9:N2}   ({1,5:N1}% of one core)' -f
           $attributed, (100.0 * $attributed / $elapsed))
$lines += ('  unattributed remainder : {0,9:N2}   (exited processes and counter resets)' -f
           ($busy - $attributed))
$lines += ''
$lines += ('by CPU delta, {0} of {1} moving processes shown' -f $shown.Count, $ranked.Count)
$lines += ('{0,9} {1,7}  {2,-22} {3,7} {4,9} {5,9} {6,9} {7,9}  {8}' -f
           'core-s', '%1core', 'process', 'pid', 'ws MB', 'dpriv MB', 'rd KiB/s', 'wr KiB/s', 'services')
foreach ($row in $shown) {
    $name = $row.Name
    if ($row.New) { $name = "$name (new)" }
    $lines += ('{0,9:N2} {1,7:N2}  {2,-22} {3,7} {4,9:N1} {5,9:N1} {6,9:N1} {7,9:N1}  {8}' -f
               $row.Cpu, $row.Percent, $name, $row.Pid, $row.WorkMB, $row.PrivMB,
               $row.ReadKiBs, $row.WritKiBs, $row.Services).TrimEnd()
}
if ($cutCpu -gt 0) {
    $lines += ('{0,9:N2} {1,7:N2}  {2}' -f $cutCpu, (100.0 * $cutCpu / $elapsed),
               'everything below the cut, combined')
}
$lines += ''
$lines += 'A row is a measurement, not a verdict. Compare three runs against three'
$lines += 'host-side tools/vcpu-profile.py --seconds 300 samples before acting, and'
$lines += 'read the accepted-cost rows in CHANGELOG.md before proposing a change.'

foreach ($line in $lines) { Write-Host $line }

if (-not $OutFile) {
    $OutFile = Join-Path $OutDir ('idle-profile-{0}.txt' -f $second.At.ToString('yyyyMMdd-HHmmss'))
}
try {
    $lines | Set-Content -LiteralPath $OutFile -Encoding UTF8
    Write-Host "==> wrote $OutFile"
} catch {
    Write-Host "WARN: could not write $OutFile ($($_.Exception.Message))"
    Write-Host '      The report above is complete; copy it from this window.'
}
