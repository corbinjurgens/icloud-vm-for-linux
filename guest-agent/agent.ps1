# ============================================================================
#  agent.ps1 - iCloud bridge guest agent (v2 plan section 3)
#
#  copied by 04-bridge-agent.ps1; source of truth: guest-agent/agent.ps1
#
#  Runs INSIDE the Windows guest, as the limited user "icloud", started by the
#  "icloud-bridge-agent" Task Scheduler logon task:
#
#     powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden `
#       -File C:\ProgramData\icloud-bridge\agent.ps1
#
#  Responsibilities:
#    - enforce the exclusion list from the bridge share (deny ACE on each
#      excluded item + DELETE_CHILD guard on its parent, then request
#      online-only content state) -- v2 plan D15/D20/D22
#    - reclaim guest disk by requesting online-only for cold, in-sync, fully
#      cached files when free space drops below the floor -- v2 plan D26
#    - publish status.json / tree.json and answer per-folder list requests
#      over the bridge share -- v2 plan section 2
#
#  It NEVER writes file content and NEVER deletes, moves or renames anything
#  inside the sync root (v2 plan D22a). The only deletes it performs are of
#  consumed or expired bridge request/response files.
#
#  Idempotent: every pass recomputes desired state from exclusions.json and
#  converges. Safe to stop and restart at any point.
#
#  PowerShell 5.1 syntax only (that is what ships with Windows 11).
#
#  Mechanism choices that originally diverged from the plan's pseudocode
#  (v2 plan section 3 now records them), and why:
#    * Directory enumeration uses FindFirstFileW/FindNextFileW through the
#      interop helper instead of [IO.Directory]::EnumerateFileSystemEntries.
#      Both are attribute-only with no per-file handle opens, but FindFirstFileW
#      also returns the reparse tag, which is what keeps the routine scan's
#      CfGetPlaceholderInfo count near zero (v2 plan A5): every Cloud Files
#      placeholder directory carries FILE_ATTRIBUTE_REPARSE_POINT, so without
#      the tag the containment check of section 2.1 would open a handle per
#      directory. Per-path SyncRootFileId validation still runs for configured
#      paths (exclusion roots and list requests) -- the bounded set.
#    * JSON is emitted by a small serializer instead of ConvertTo-Json because
#      Windows PowerShell 5.1's ConvertTo-Json does not reliably render empty
#      arrays as [] and Set-Content -Encoding UTF8 emits a BOM (v2 plan
#      section 2). Output is otherwise identical to ConvertTo-Json -Depth 20.
#    * A directory's online-only request runs as `attrib +U -P <dir>` followed
#      by `attrib +U -P <dir>\* /S /D`; attrib needs the wildcard form for /S
#      to reach descendants.
#    * Full-tree ACL reconciliation carries a per-pass time budget and a
#      persisted cursor, so a very large library resumes instead of overrunning
#      the ten-minute interval.
#    * Steady-state work elision (v2 plan D34): the per-entry DACL reads of that
#      reconciliation are skipped while an orphan deny is provably impossible;
#      the re-verification walk of an already-applied exclusion is decimated; and
#      a low-disk episode reuses its candidate list for a few passes. Every one of
#      those is reporting or discovery work. Nothing that enforces access is
#      elided: the deny and parent-guard ACEs are still asserted on every 60 s
#      pass, and every dehydration candidate is still re-checked with
#      CfGetPlaceholderInfo immediately before its request.
# ============================================================================

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- constants --
$SyncRoot  = "$env:USERPROFILE\iCloudDrive"
$BaseDir   = "C:\ProgramData\icloud-bridge"
$BridgeDir = Join-Path $BaseDir "io"
$StateDir  = Join-Path $BaseDir "state"
$ShareUser = "syncshare"

# Bridge protocol identity (v2 plan D35). $ProtocolVersion is the "version"
# field of every document this agent writes and reads; there is exactly one
# supported value, and a mismatch is an error the GUI reports rather than
# something either end works around. $AgentBuild is a non-negative, monotonically
# increasing integer -- bump it in any commit that changes this script's
# behavior, so a GUI shipped alongside a newer agent can say so. The GUI carries
# the same number in bridge.py and a test compares the two literals.
$ProtocolVersion = 1
$AgentBuild      = 3

# Cloud Files / FILE_ATTRIBUTE values.
# DIRECTORY and UNPINNED are listed for reference and are deliberately unused:
# the native enumerator already reports IsDirectory, and UNPINNED is only user
# *intent*, not proof that content is gone -- the iCloud placeholders measured
# in v2 plan section 0.5 carried RECALL without UNPINNED. Completion is decided
# by CfGetPlaceholderInfo.OnDiskDataSize, and pin state by its PinState field.
$ATTR_DIRECTORY = 0x00000010
$ATTR_REPARSE   = 0x00000400
$ATTR_PINNED    = 0x00080000   # FILE_ATTRIBUTE_PINNED
$ATTR_UNPINNED  = 0x00100000   # FILE_ATTRIBUTE_UNPINNED
$ATTR_RECALL    = 0x00400000   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (not fully local)

# CF_PIN_STATE / CF_IN_SYNC_STATE
$CF_PIN_PINNED   = 1
$CF_IN_SYNC      = 1

# Disk reclamation (v2 plan D26)
$SweepFloorBytes  = 20GB      # sweep when free disk drops below this
$SweepTargetBytes = 30GB      # request coldest-first until this target is covered

# Bounds on untrusted bridge input (v2 plan section 2.1)
$MaxConfigBytes    = 1MB
$MaxConfigEntries  = 10000
$MaxRequestBytes   = 64KB
$MaxRequestsPerTick = 10
$RequestTtlSeconds = 600

# Internal work bounds so one pass cannot overrun its cadence
$MaxPlaceholderQueriesPerPass = 5000    # exclusion measurement
$MaxStage2QueriesPerPass      = 2000    # reclamation stage 2 batch
$AclReconcileBudgetSeconds    = 120     # full-tree ACL reconciliation slice
$SweepCooldownSeconds         = 600     # after an episode ends with nothing eligible

# Steady-state work elision (v2 plan D34). These only decimate *reporting* work:
# the deny/parent-guard ACEs are still asserted on every 60 s enforcement pass,
# and every dehydration candidate is still re-checked immediately before its
# request, so no safety property depends on any of these intervals.
$AppliedVerifyFastPasses  = 3     # re-verify a newly applied root every pass this often
$AppliedVerifyEveryPasses = 10    # ...and every Nth pass after that
$SweepRewalkEveryPasses   = 5     # re-walk sweep candidates this rarely inside an episode

# Cadence (seconds); the loop ticks every 2 s
$TickSeconds        = 2
$StatusEverySeconds = 15
$EnforceEverySeconds = 60
$TreeEverySeconds   = 600

# ------------------------------------------------------------------ interop --
$nativeSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public sealed class NativeEntry {
    public string Name;
    public uint   Attributes;
    public long   Length;
    public uint   ReparseTag;
    public long   LastWriteTicks;
    public long   LastAccessTicks;
    public bool   IsDirectory;
}

public sealed class PlaceholderInfo {
    public bool   Queried;        // the file could be opened for READ_ATTRIBUTES
    public bool   IsPlaceholder;  // CfGetPlaceholderInfo returned data
    public long   OnDiskDataSize;
    public long   ModifiedDataSize;
    public int    PinState;
    public int    InSyncState;
    public long   SyncRootFileId;
    public long   AllocatedSize;  // GetCompressedFileSizeW, for non-placeholders
    public int    ErrorCode;
    public string Error;
}

public static class IcloudBridgeNative {
    const uint FILE_READ_ATTRIBUTES = 0x0080;
    const uint READ_CONTROL         = 0x00020000;
    const uint WRITE_DAC            = 0x00040000;
    const uint FILE_SHARE_ALL       = 0x00000007;   // read | write | delete
    const uint OPEN_EXISTING        = 3;
    const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
    const int  CF_PLACEHOLDER_INFO_STANDARD = 1;
    static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct WIN32_FIND_DATA {
        public uint dwFileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME ftCreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME ftLastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME ftLastWriteTime;
        public uint nFileSizeHigh;
        public uint nFileSizeLow;
        public uint dwReserved0;      // reparse tag when FILE_ATTRIBUTE_REPARSE_POINT is set
        public uint dwReserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string cFileName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]  public string cAlternateFileName;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr FindFirstFileW(string lpFileName, out WIN32_FIND_DATA lpFindFileData);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool FindNextFileW(IntPtr hFindFile, out WIN32_FIND_DATA lpFindFileData);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool FindClose(IntPtr hFindFile);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes,
        IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetCompressedFileSizeW(string lpFileName, out uint lpFileSizeHigh);

    [DllImport("cfapi.dll", CharSet = CharSet.Unicode)]
    static extern int CfGetPlaceholderInfo(IntPtr FileHandle, int InfoClass, IntPtr InfoBuffer,
        uint InfoBufferLength, out uint ReturnedLength);

    // \\?\ form so deep trees enumerate; components are still <= 255 chars.
    static string Long(string path) {
        if (path.StartsWith(@"\\?\")) return path;
        if (path.StartsWith(@"\\"))   return @"\\?\UNC\" + path.Substring(2);
        return @"\\?\" + path;
    }

    static long Ticks(System.Runtime.InteropServices.ComTypes.FILETIME ft) {
        long v = ((long)ft.dwHighDateTime << 32) | (long)(uint)ft.dwLowDateTime;
        if (v <= 0) return 0;
        try { return DateTime.FromFileTimeUtc(v).Ticks; } catch { return 0; }
    }

    /// Attribute-only directory listing. No handles opened per entry, so this
    /// cannot hydrate a placeholder.
    public static NativeEntry[] Enumerate(string directory) {
        var list = new List<NativeEntry>();
        WIN32_FIND_DATA fd;
        IntPtr h = FindFirstFileW(Long(directory.TrimEnd('\\')) + @"\*", out fd);
        if (h == INVALID_HANDLE_VALUE) {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(),
                "cannot enumerate " + directory);
        }
        try {
            do {
                if (fd.cFileName == "." || fd.cFileName == "..") continue;
                var e = new NativeEntry();
                e.Name        = fd.cFileName;
                e.Attributes  = fd.dwFileAttributes;
                e.IsDirectory = (fd.dwFileAttributes & 0x10) != 0;
                e.Length      = ((long)fd.nFileSizeHigh << 32) | (long)(uint)fd.nFileSizeLow;
                e.ReparseTag  = ((fd.dwFileAttributes & 0x400) != 0) ? fd.dwReserved0 : 0;
                e.LastWriteTicks  = Ticks(fd.ftLastWriteTime);
                e.LastAccessTicks = Ticks(fd.ftLastAccessTime);
                list.Add(e);
            } while (FindNextFileW(h, out fd));
        } finally { FindClose(h); }
        return list.ToArray();
    }

    /// Sorts entries into the walk's emission order: OrdinalIgnoreCase with an
    /// Ordinal tiebreak. Compiled, because the PowerShell walks previously
    /// re-created a scriptblock comparator per directory and paid a delegate
    /// dispatch per comparison. Every ordered walk must use exactly this order
    /// or Compare-RelPathDfs disagrees with it and the ACL resume cursor can
    /// skip never-visited subtrees; tools/test-agent-walk.ps1 is the fixture.
    ///
    /// Returns its argument so PowerShell callers can rebind: assignment from
    /// a function call has already collected Enumerate's array into Object[]
    /// (or a bare scalar, or null for an empty directory), and the parameter
    /// conversion here re-materializes the NativeEntry[] the caller must keep.
    public static NativeEntry[] SortByName(NativeEntry[] entries) {
        if (entries != null && entries.Length > 1) Array.Sort(entries, CompareByName);
        return entries;
    }

    static int CompareByName(NativeEntry a, NativeEntry b) {
        int r = string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase);
        if (r != 0) return r;
        return string.CompareOrdinal(a.Name, b.Name);
    }

    /// True when the object can be opened for READ_CONTROL | WRITE_DAC, i.e.
    /// the agent may rewrite its DACL (v2 plan D28 preflight).
    public static bool CanEditDacl(string path, bool isDirectory) {
        IntPtr h = CreateFileW(Long(path), READ_CONTROL | WRITE_DAC, FILE_SHARE_ALL, IntPtr.Zero,
            OPEN_EXISTING, isDirectory ? FILE_FLAG_BACKUP_SEMANTICS : 0, IntPtr.Zero);
        if (h == INVALID_HANDLE_VALUE) return false;
        CloseHandle(h);
        return true;
    }

    /// Cloud Files placeholder metadata. Opens with FILE_READ_ATTRIBUTES only;
    /// CfGetPlaceholderInfo is documented not to modify the file or fetch data.
    public static PlaceholderInfo GetPlaceholderInfo(string path, bool isDirectory) {
        var r = new PlaceholderInfo();
        IntPtr h = CreateFileW(Long(path), FILE_READ_ATTRIBUTES, FILE_SHARE_ALL, IntPtr.Zero,
            OPEN_EXISTING, isDirectory ? FILE_FLAG_BACKUP_SEMANTICS : 0, IntPtr.Zero);
        if (h == INVALID_HANDLE_VALUE) {
            r.ErrorCode = Marshal.GetLastWin32Error();
            r.Error = "open failed (" + r.ErrorCode + ")";
            return r;
        }
        r.Queried = true;
        IntPtr buf = Marshal.AllocHGlobal(8192);
        try {
            uint returned;
            int hr = CfGetPlaceholderInfo(h, CF_PLACEHOLDER_INFO_STANDARD, buf, 8192, out returned);
            if (hr != 0) {
                r.ErrorCode = hr;
                r.Error = "not a cloud placeholder (0x" + hr.ToString("x8") + ")";
            } else {
                r.IsPlaceholder    = true;
                r.OnDiskDataSize   = Marshal.ReadInt64(buf, 0);
                r.ModifiedDataSize = Marshal.ReadInt64(buf, 16);
                r.PinState         = Marshal.ReadInt32(buf, 32);
                r.InSyncState      = Marshal.ReadInt32(buf, 36);
                r.SyncRootFileId   = Marshal.ReadInt64(buf, 48);
            }
        } finally {
            Marshal.FreeHGlobal(buf);
            CloseHandle(h);
        }
        if (!r.IsPlaceholder && !isDirectory) {
            uint high;
            uint low = GetCompressedFileSizeW(Long(path), out high);
            if (low != 0xFFFFFFFF || Marshal.GetLastWin32Error() == 0) {
                r.AllocatedSize = ((long)high << 32) | (long)low;
            } else {
                r.AllocatedSize = -1;
            }
        }
        return r;
    }

    // JSON string escaping (v2 plan section 2). Compiled, because the PowerShell
    // original walked every string one character at a time through an eight-branch
    // comparison chain and allocated a StringBuilder per call -- and it runs for
    // every key and every value of every node of tree.json every ten minutes,
    // plus status.json every fifteen seconds.
    //
    // The fast path is the point. Almost every real key and path needs no
    // escaping at all, so the common case scans once and concatenates without
    // touching a StringBuilder. Output is byte-identical to the version this
    // replaced, including lowercase \uXXXX for the control characters that have
    // no short escape, and raw pass-through of DEL, non-ASCII and surrogate
    // pairs. tools/test-bridge-json.ps1 is the proof.
    public static string JsonString(string value) {
        if (string.IsNullOrEmpty(value)) return "\"\"";
        int i = 0;
        for (; i < value.Length; i++) {
            char c = value[i];
            if (c == '"' || c == '\\' || c < 32) break;
        }
        if (i == value.Length) return "\"" + value + "\"";

        var sb = new System.Text.StringBuilder(value.Length + 16);
        sb.Append('"');
        sb.Append(value, 0, i);
        for (; i < value.Length; i++) {
            char c = value[i];
            switch (c) {
                case '"':  sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b");  break;
                case '\t': sb.Append("\\t");  break;
                case '\n': sb.Append("\\n");  break;
                case '\f': sb.Append("\\f");  break;
                case '\r': sb.Append("\\r");  break;
                default:
                    if (c < 32) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }
}
'@

try {
    Add-Type -TypeDefinition $nativeSource -Language CSharp -ErrorAction Stop
} catch {
    Write-Error "fatal: cannot compile the native helper: $($_.Exception.Message)"
    exit 2
}

# ------------------------------------------------------------ script state --
$script:SyncRootFull   = $null
$script:SyncRootFileId = $null
$script:ShareSid       = $null
$script:AgentStartedAt = [DateTime]::UtcNow
$script:Errors         = @{}        # subtask -> @{ msg; at }
$script:ErrorSeq       = 0
$script:State          = $null      # private applied.json
$script:Exclusions     = @()        # status records, most recent evaluation
$script:WantedRoots    = @()        # last validated exclusion set (user intent)
$script:AppliedRevision = $null
$script:LastEnforcementAt = $null
$script:FullyLocalLogicalBytes = [Int64]0
$script:ScanInfo = [ordered]@{ lastCompletedAt = $null; durationMs = 0; entries = 0; cloudInfoQueries = 0 }
$script:SweepInfo = [ordered]@{ lastRunAt = $null; requestedBytes = [Int64]0; freedBytes = [Int64]0;
                                blockedBytes = [Int64]0; blockedCount = 0; inProgress = $false; belowFloor = $false }
$script:CloudInfoQueries = 0        # counter for the routine scan window
$script:LastAccessUsable = $false
$script:TreeDirty        = $true
$script:EnforcePassNo    = 0
$script:DehydrateAttempts = @{}     # rel path -> @{ count; lastPass } (in memory only)
$script:AppliedVerify    = @{}      # rel path -> @{ pass; verifications; logicalBytes } (D34)
$script:SweepStage1      = $null    # cached sweep candidates for the active episode (D34)
$script:SweepStage1Pass  = -999
$script:SweepStage2      = $null
$script:SweepStage2Pass  = -999

# =========================================================== small utilities =

function Get-UtcStamp {
    param([DateTime]$When = [DateTime]::UtcNow)
    return $When.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Set-SubtaskError {
    param([string]$Key, [string]$Message)
    $script:ErrorSeq++
    $script:Errors[$Key] = @{ msg = $Message; seq = $script:ErrorSeq }
}

function Clear-SubtaskError {
    param([string]$Key)
    if ($script:Errors.ContainsKey($Key)) { $script:Errors.Remove($Key) }
}

function Get-LastError {
    $best = $null
    foreach ($k in $script:Errors.Keys) {
        $e = $script:Errors[$k]
        if ($null -eq $best -or $e.seq -gt $best.seq) { $best = $e }
    }
    if ($null -eq $best) { return $null }
    return $best.msg
}

function Get-Sha256Hex {
    param([string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join '')
    } finally { $sha.Dispose() }
}

# --- JSON output -------------------------------------------------------------
# Equivalent to ConvertTo-Json -Depth 20 for the shapes this agent emits, but
# renders empty collections as [] and never emits a BOM (v2 plan section 2).

# The escape loop lives in the compiled helper, and a whole document is built
# into one StringBuilder. The version this replaced had every level materialize
# each child as a complete string in a List[string] and then -join them, which
# recopied every byte once per level of nesting: O(size x depth), where the depth
# is the operator's folder depth. Measured under PowerShell 7 on a synthetic
# 3906-node / 4.1 MB / depth-6 tree, the pair of changes took one serialization
# from 11.9 s to well under half that, and the recursion was the larger share.
# The output is unchanged -- tools/test-bridge-json.ps1 is the byte-identity proof.

function ConvertTo-BridgeJsonString {
    param([string]$Value)
    return [IcloudBridgeNative]::JsonString($Value)
}

function Add-BridgeJson {
    param([System.Text.StringBuilder]$Sb, $Value, [int]$Depth = 0)
    if ($Depth -gt 64) { throw "JSON nesting too deep" }
    if ($null -eq $Value) { [void]$Sb.Append('null'); return }
    if ($Value -is [bool]) {
        if ($Value) { [void]$Sb.Append('true') } else { [void]$Sb.Append('false') }
        return
    }
    if ($Value -is [string]) {
        [void]$Sb.Append([IcloudBridgeNative]::JsonString($Value)); return
    }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or
        $Value -is [uint32] -or $Value -is [uint64] -or $Value -is [byte]) {
        [void]$Sb.Append(([Int64]$Value).ToString([Globalization.CultureInfo]::InvariantCulture))
        return
    }
    if ($Value -is [double] -or $Value -is [decimal] -or $Value -is [single]) {
        [void]$Sb.Append(([double]$Value).ToString('R', [Globalization.CultureInfo]::InvariantCulture))
        return
    }
    if ($Value -is [System.Collections.IDictionary]) {
        [void]$Sb.Append('{')
        $first = $true
        foreach ($k in $Value.Keys) {
            if (-not $first) { [void]$Sb.Append(',') }
            $first = $false
            [void]$Sb.Append([IcloudBridgeNative]::JsonString([string]$k))
            [void]$Sb.Append(':')
            Add-BridgeJson $Sb $Value[$k] ($Depth + 1)
        }
        [void]$Sb.Append('}')
        return
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        [void]$Sb.Append('[')
        $first = $true
        foreach ($item in $Value) {
            if (-not $first) { [void]$Sb.Append(',') }
            $first = $false
            Add-BridgeJson $Sb $item ($Depth + 1)
        }
        [void]$Sb.Append(']')
        return
    }
    [void]$Sb.Append([IcloudBridgeNative]::JsonString([string]$Value))
}

function ConvertTo-BridgeJson {
    param($Value, [int]$Depth = 0)
    $sb = New-Object System.Text.StringBuilder
    Add-BridgeJson $sb $Value $Depth
    return $sb.ToString()
}

function Write-JsonAtomic {
    param([string]$Path, $Object)
    $json = ConvertTo-BridgeJson $Object
    $enc  = New-Object System.Text.UTF8Encoding($false)
    $dir  = Split-Path -Parent $Path
    $tmp  = Join-Path $dir ('.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tmp, $json, $enc)
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($tmp, $Path, $null)
        } else {
            [IO.File]::Move($tmp, $Path)
        }
    } finally {
        if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) }
    }
}

function Read-JsonFile {
    param([string]$Path, [int]$MaxBytes)
    $fi = New-Object System.IO.FileInfo($Path)
    if (-not $fi.Exists) { throw "missing file: $Path" }
    if ($fi.Length -gt $MaxBytes) { throw "file exceeds $MaxBytes bytes: $Path" }
    $bytes = [IO.File]::ReadAllBytes($Path)
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $offset = 3 }
    $text = [Text.Encoding]::UTF8.GetString($bytes, $offset, $bytes.Length - $offset)
    return ($text | ConvertFrom-Json)
}

function Test-HasProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $false }
    return (@($Object.PSObject.Properties.Name) -contains $Name)
}

function Get-IntegerOrNull {
    param($Value)
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or $Value -is [byte]) { return [Int64]$Value }
    if ($Value -is [double] -and [Math]::Floor($Value) -eq $Value) { return [Int64]$Value }
    return $null
}

# ================================================================ path rules =

function Get-CanonicalRelative {
    # Validate one untrusted relative path (v2 plan section 2.1 / D19 / D22d).
    param([string]$Rel, [switch]$AllowRoot)
    $res = [ordered]@{ ok = $false; rel = ''; full = ''; error = '' }
    if ($null -eq $Rel)          { $res.error = 'path is null'; return $res }
    if ($Rel.Length -gt 4096)    { $res.error = 'path too long'; return $res }
    if ($Rel.IndexOf([char]0) -ge 0) { $res.error = 'path contains NUL'; return $res }

    $t = $Rel.Replace('\', '/')
    if ($t -eq '') {
        if ($AllowRoot) { $res.ok = $true; $res.rel = ''; $res.full = $script:SyncRootFull; return $res }
        $res.error = 'the sync root itself cannot be used here'
        return $res
    }
    if ($t.StartsWith('/')) { $res.error = 'rooted or UNC path'; return $res }
    foreach ($s in $t.Split('/')) {
        if ($s -eq '')   { $res.error = 'empty path segment'; return $res }
        if ($s -eq '.' -or $s -eq '..') { $res.error = 'relative path segment'; return $res }
        if ($s -match '[<>:"|?*]')      { $res.error = 'invalid character in path segment'; return $res }
        if ($s.EndsWith(' ') -or $s.EndsWith('.')) { $res.error = 'path segment ends with a space or dot'; return $res }
    }
    $rel = [string]::Join('/', $t.Split('/'))
    try {
        $full = [IO.Path]::GetFullPath((Join-Path $script:SyncRootFull ($rel.Replace('/', '\'))))
    } catch {
        $res.error = 'path cannot be resolved'
        return $res
    }
    if (-not $full.StartsWith($script:SyncRootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $res.error = 'path escapes the sync root'
        return $res
    }
    $res.ok = $true; $res.rel = $rel; $res.full = $full
    return $res
}

function Test-CloudReparseTag {
    # IO_REPARSE_TAG_CLOUD .. IO_REPARSE_TAG_CLOUD_F  (0x9000_X01A).
    # $Tag is 0 for entries that are not reparse points at all.
    #
    # The constants are declared as [uint32] via the L suffix on purpose: a bare
    # `0x9000001A` literal is Int32 in PowerShell, i.e. NEGATIVE, so comparing a
    # widened uint32 against it can never match and every cloud placeholder
    # directory would look like a foreign junction.
    param([uint32]$Tag)
    if ($Tag -eq 0) { return $true }
    return (($Tag -band ([uint32]0xFFFF0FFFL)) -eq ([uint32]0x9000001AL))
}

function Test-PathContainment {
    # Walk each intermediate directory of a configured path and refuse to follow
    # a reparse point that is not a Cloud Files placeholder of this sync root
    # (v2 plan section 2.1). Bounded: only configured paths reach here.
    param([string]$Rel)
    if ($Rel -eq '') { return @{ ok = $true; error = '' } }
    $segs = $Rel.Split('/')
    $cur = $script:SyncRootFull
    for ($i = 0; $i -lt $segs.Length - 1; $i++) {
        $cur = Join-Path $cur $segs[$i]
        if (-not [IO.Directory]::Exists($cur)) { return @{ ok = $true; error = '' } }
        $attrs = 0
        try { $attrs = [int][IO.File]::GetAttributes($cur) } catch { return @{ ok = $false; error = "cannot read attributes of $cur" } }
        if (($attrs -band $ATTR_REPARSE) -eq 0) { continue }
        $script:CloudInfoQueries++
        $info = [IcloudBridgeNative]::GetPlaceholderInfo($cur, $true)
        if (-not $info.IsPlaceholder) {
            return @{ ok = $false; error = "refusing to follow a non-cloud reparse point: $cur" }
        }
        if ($null -ne $script:SyncRootFileId -and $info.SyncRootFileId -ne $script:SyncRootFileId) {
            return @{ ok = $false; error = "reparse point belongs to a different sync root: $cur" }
        }
    }
    return @{ ok = $true; error = '' }
}

function Get-AntichainPaths {
    # De-duplicate case-insensitively and drop every descendant of another entry.
    param([string[]]$Paths)
    $unique = New-Object System.Collections.Generic.List[string]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($p in $Paths) { if ($seen.Add($p)) { $unique.Add($p) } }
    $sorted = New-Object System.Collections.Generic.List[string]
    $sorted.AddRange($unique)
    $sorted.Sort([System.Comparison[string]]{
        param($a, $b)
        $r = [string]::Compare($a, $b, [StringComparison]::OrdinalIgnoreCase)
        if ($r -ne 0) { return $r }
        return [string]::CompareOrdinal($a, $b)
    })
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in $sorted) {
        $covered = $false
        foreach ($k in $out) {
            if ($p.StartsWith($k + '/', [StringComparison]::OrdinalIgnoreCase)) { $covered = $true; break }
        }
        if (-not $covered) { $out.Add($p) }
    }
    return , $out.ToArray()
}

function Get-ParentRelative {
    param([string]$Rel)
    $i = $Rel.LastIndexOf('/')
    if ($i -lt 0) { return '' }
    return $Rel.Substring(0, $i)
}

function Get-FullFromRelative {
    param([string]$Rel)
    if ($Rel -eq '') { return $script:SyncRootFull }
    return (Join-Path $script:SyncRootFull ($Rel.Replace('/', '\')))
}

# ===================================================================== ACLs ==

function Get-DaclSecurity {
    param([string]$Full, [bool]$IsDirectory)
    $sections = [Security.AccessControl.AccessControlSections]::Access
    if ($IsDirectory) { return [IO.Directory]::GetAccessControl($Full, $sections) }
    return [IO.File]::GetAccessControl($Full, $sections)
}

function Set-DaclSecurity {
    param([string]$Full, [bool]$IsDirectory, $Security)
    if ($IsDirectory) { [IO.Directory]::SetAccessControl($Full, $Security) }
    else              { [IO.File]::SetAccessControl($Full, $Security) }
}

function New-TargetDenyRule {
    param([bool]$IsDirectory)
    $inherit = [Security.AccessControl.InheritanceFlags]::None
    if ($IsDirectory) {
        $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
                   [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    return (New-Object Security.AccessControl.FileSystemAccessRule(
        $script:ShareSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $inherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Deny))
}

function New-ParentGuardRule {
    return (New-Object Security.AccessControl.FileSystemAccessRule(
        $script:ShareSid,
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles,
        [Security.AccessControl.InheritanceFlags]::None,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Deny))
}

function Test-RuleEquivalent {
    param($Rule, $Template)
    if ($Rule.IsInherited) { return $false }
    if ($Rule.AccessControlType -ne $Template.AccessControlType) { return $false }
    if ($Rule.FileSystemRights -ne $Template.FileSystemRights) { return $false }
    if ($Rule.InheritanceFlags -ne $Template.InheritanceFlags) { return $false }
    if ($Rule.PropagationFlags -ne $Template.PropagationFlags) { return $false }
    $sid = $Rule.IdentityReference
    if ($sid -isnot [Security.Principal.SecurityIdentifier]) {
        try { $sid = $sid.Translate([Security.Principal.SecurityIdentifier]) } catch { return $false }
    }
    return ($sid.Value -eq $script:ShareSid.Value)
}

function Test-ExactRulePresent {
    param([string]$Full, [bool]$IsDirectory, $Template)
    $sec = Get-DaclSecurity $Full $IsDirectory
    foreach ($r in $sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        if (Test-RuleEquivalent $r $Template) { return $true }
    }
    return $false
}

function Add-ExactRule {
    # Idempotent: returns $true only when the DACL was actually rewritten.
    param([string]$Full, [bool]$IsDirectory, $Template)
    $sec = Get-DaclSecurity $Full $IsDirectory
    foreach ($r in $sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        if (Test-RuleEquivalent $r $Template) { return $false }
    }
    $sec.AddAccessRule($Template)
    Set-DaclSecurity $Full $IsDirectory $sec
    return $true
}

function Remove-ExactRule {
    # RemoveAccessRuleSpecific, never icacls /remove:d -- an exact match only,
    # so a parent/child transition cannot strip the wrong deny (v2 plan section 3).
    param([string]$Full, [bool]$IsDirectory, $Template)
    $sec = Get-DaclSecurity $Full $IsDirectory
    $found = $false
    foreach ($r in $sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        if (Test-RuleEquivalent $r $Template) { $found = $true; break }
    }
    if (-not $found) { return $false }
    $sec.RemoveAccessRuleSpecific($Template)
    Set-DaclSecurity $Full $IsDirectory $sec
    return $true
}

function Test-AclAuthority {
    # D28 preflight: READ_CONTROL | WRITE_DAC on the object about to be changed.
    param([string]$Full, [bool]$IsDirectory)
    return [IcloudBridgeNative]::CanEditDacl($Full, $IsDirectory)
}

# ======================================================== placeholder helpers =

function Get-PlaceholderState {
    param([string]$Full, [bool]$IsDirectory)
    $script:CloudInfoQueries++
    return [IcloudBridgeNative]::GetPlaceholderInfo($Full, $IsDirectory)
}

function Invoke-Native {
    # Run a console tool and return its exit code. $ErrorActionPreference is
    # relaxed for the call because PowerShell 5.1 turns native stderr into
    # terminating errors under 'Stop'.
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        & $Exe @Arguments 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Set-OnlineOnlyRequest {
    # attrib +U -P: request the online-only state. Dehydration is asynchronous
    # and Cloud Files refuses it for content that is not in sync (v2 plan D14).
    # attrib needs the wildcard form before /S reaches descendants, so a
    # directory gets both the object itself and its subtree.
    param([string]$Full, [bool]$IsDirectory)
    $rc = Invoke-Native 'attrib.exe' @('+U', '-P', $Full)
    if ($rc -ne 0) { throw "attrib +U -P failed (exit $rc) on $Full" }
    if ($IsDirectory) {
        $rc = Invoke-Native 'attrib.exe' @('+U', '-P', (Join-Path $Full '*'), '/S', '/D')
        if ($rc -ne 0) { throw "attrib +U -P /S /D failed (exit $rc) on $Full" }
    }
}

function Get-VolumeSpace {
    $drive = New-Object System.IO.DriveInfo([IO.Path]::GetPathRoot($script:SyncRootFull))
    return @{ free = [Int64]$drive.AvailableFreeSpace; total = [Int64]$drive.TotalSize }
}

function Test-LastAccessUsable {
    # Only trust LastAccessTime as the LRU key when NTFS actually updates it.
    try {
        $v = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
                -Name 'NtfsDisableLastAccessUpdate' -ErrorAction Stop
        # Low bit set => updates disabled. 0x80000000 marks "system-managed".
        return (([int]$v.NtfsDisableLastAccessUpdate -band 1) -eq 0)
    } catch {
        return $false
    }
}

# =================================================== private state (D22f/D27) =

function Get-DefaultState {
    return @{
        version          = 1
        roots            = @()
        guardedParents   = @()
        appliedRevision  = $null
        wantedHash       = $null
        sweep            = @{ episodeActive = $false; episodeStartFreeBytes = 0;
                              stage2Cursor = $null; stage2Exhausted = $false; cooldownUntilTicks = 0 }
        aclCursor        = $null
        # D34: set only by a reconciliation pass that proved there is nothing to
        # reconcile. Defaulting to $false is what makes a missing or corrupt state
        # file force a real full-tree pass before any reads may be skipped again.
        aclCleanEmpty    = $false
    }
}

function Read-PrivateState {
    $path = Join-Path $StateDir 'applied.json'
    if (-not [IO.File]::Exists($path)) { return (Get-DefaultState) }
    try {
        $doc = Read-JsonFile $path (1MB)
        $s = Get-DefaultState
        if (Test-HasProperty $doc 'roots')           { $s.roots = @($doc.roots) }
        if (Test-HasProperty $doc 'guardedParents')  { $s.guardedParents = @($doc.guardedParents) }
        if (Test-HasProperty $doc 'appliedRevision') { $s.appliedRevision = Get-IntegerOrNull $doc.appliedRevision }
        if (Test-HasProperty $doc 'wantedHash')      { $s.wantedHash = [string]$doc.wantedHash }
        if (Test-HasProperty $doc 'aclCursor')       { $s.aclCursor = $doc.aclCursor }
        if (Test-HasProperty $doc 'aclCleanEmpty')   { $s.aclCleanEmpty = [bool]$doc.aclCleanEmpty }
        if (Test-HasProperty $doc 'sweep') {
            foreach ($k in @('episodeActive','episodeStartFreeBytes','stage2Cursor','stage2Exhausted','cooldownUntilTicks')) {
                if (Test-HasProperty $doc.sweep $k) { $s.sweep[$k] = $doc.sweep.$k }
            }
        }
        return $s
    } catch {
        # A corrupt private state file must not re-include everything: keep the
        # defaults, let full-tree reconciliation rebuild from the live ACLs.
        Set-SubtaskError 'state' "private state unreadable, rebuilding from ACLs: $($_.Exception.Message)"
        return (Get-DefaultState)
    }
}

function Write-PrivateState {
    $s = $script:State
    $obj = [ordered]@{
        version         = 1
        roots           = @($s.roots)
        guardedParents  = @($s.guardedParents)
        appliedRevision = $s.appliedRevision
        wantedHash      = $s.wantedHash
        aclCursor       = $s.aclCursor
        aclCleanEmpty   = [bool]$s.aclCleanEmpty
        sweep           = [ordered]@{
            episodeActive         = [bool]$s.sweep.episodeActive
            episodeStartFreeBytes = [Int64]$s.sweep.episodeStartFreeBytes
            stage2Cursor          = $s.sweep.stage2Cursor
            stage2Exhausted       = [bool]$s.sweep.stage2Exhausted
            cooldownUntilTicks    = [Int64]$s.sweep.cooldownUntilTicks
        }
    }
    Write-JsonAtomic (Join-Path $StateDir 'applied.json') $obj
    Clear-SubtaskError 'state'
}

# ==================================================== exclusions.json parsing =

function Read-WantedConfig {
    # Returns @{ ok; revision; wanted; hash; error }. Fails closed: any problem
    # leaves every current deny/guard in place and does not move appliedRevision.
    $path = Join-Path $BridgeDir 'exclusions.json'
    $res = @{ ok = $false; revision = $null; wanted = @(); hash = $null; error = '' }
    try {
        $doc = Read-JsonFile $path $MaxConfigBytes
    } catch {
        $res.error = "exclusions.json unreadable: $($_.Exception.Message)"
        return $res
    }
    if (-not (Test-HasProperty $doc 'version') -or (Get-IntegerOrNull $doc.version) -ne 1) {
        $res.error = 'exclusions.json has an unsupported "version"'
        return $res
    }
    $rev = $null
    if (Test-HasProperty $doc 'revision') { $rev = Get-IntegerOrNull $doc.revision }
    if ($null -eq $rev -or $rev -lt 0) {
        $res.error = 'exclusions.json "revision" is not a non-negative integer'
        return $res
    }
    if (-not (Test-HasProperty $doc 'exclusions')) {
        $res.error = 'exclusions.json has no "exclusions" list'
        return $res
    }
    $raw = @($doc.exclusions)
    if ($raw.Count -eq 1 -and $null -eq $raw[0]) { $raw = @() }
    if ($raw.Count -gt $MaxConfigEntries) {
        $res.error = "exclusions.json has more than $MaxConfigEntries entries"
        return $res
    }
    $canon = New-Object System.Collections.Generic.List[string]
    foreach ($item in $raw) {
        if ($item -isnot [string]) {
            $res.error = 'exclusions.json contains a non-string entry'
            return $res
        }
        $c = Get-CanonicalRelative -Rel $item
        if (-not $c.ok) {
            $res.error = "invalid exclusion path '$item': $($c.error)"
            return $res
        }
        $contain = Test-PathContainment $c.rel
        if (-not $contain.ok) {
            $res.error = "invalid exclusion path '$item': $($contain.error)"
            return $res
        }
        $canon.Add($c.rel)
    }
    $wanted = Get-AntichainPaths $canon.ToArray()
    $hashInput = (($wanted | ForEach-Object { $_.ToLowerInvariant() }) -join "`n")
    $res.ok = $true
    $res.revision = $rev
    $res.wanted = $wanted
    $res.hash = Get-Sha256Hex $hashInput
    return $res
}

# ============================================================ tree walking ===

function Get-Entries {
    # PowerShell collects this function's output, so callers receive Object[]
    # for two or more entries, a bare NativeEntry for one, and $null for an
    # empty directory. foreach tolerates all three; anything else (AddRange,
    # .Count, indexing) does not. Ordered walks must pass the result through
    # [IcloudBridgeNative]::SortByName and keep its return value, which also
    # re-materializes a real NativeEntry[].
    param([string]$Full)
    return [IcloudBridgeNative]::Enumerate($Full)
}

function Measure-SubtreeCheap {
    # Attribute-only recursive measurement. No handles are opened, so this never
    # hydrates. Used for tree.json, fullyLocalLogicalBytes and applied roots.
    param([string]$Full, [hashtable]$Acc)
    $entries = @()
    try { $entries = Get-Entries $Full } catch { return }
    foreach ($e in $entries) {
        $Acc.entries++
        if ($e.IsDirectory) {
            if (-not (Test-CloudReparseTag $e.ReparseTag)) { continue }
            $Acc.dirCount++
            Measure-SubtreeCheap ($Full + '\' + $e.Name) $Acc
        } else {
            $Acc.fileCount++
            $Acc.logicalBytes += $e.Length
            if (($e.Attributes -band $ATTR_RECALL) -eq 0) {
                $Acc.fullyLocalLogicalBytes += $e.Length
                if ($e.Length -gt 0) { $Acc.nonRecallFiles++ }
            }
        }
    }
}

function New-CheapAccumulator {
    return @{ entries = 0; dirCount = 0; fileCount = 0; logicalBytes = [Int64]0
              fullyLocalLogicalBytes = [Int64]0; nonRecallFiles = 0 }
}

function Measure-ExclusionAllocation {
    # Exact local allocation under a pending exclusion. This is one of the three
    # places allowed to open handles (v2 plan section 3).
    param([string]$Full, [bool]$IsDirectory, [hashtable]$Acc)
    if (-not $IsDirectory) {
        $Acc.fileCount++
        try { $Acc.logicalBytes += (New-Object System.IO.FileInfo($Full)).Length } catch { }
        if ($Acc.queries -ge $MaxPlaceholderQueriesPerPass) { $Acc.capped = $true; return }
        $Acc.queries++
        $info = Get-PlaceholderState $Full $false
        if ($info.IsPlaceholder) {
            $Acc.allocatedBytes += [Math]::Max([Int64]0, $info.OnDiskDataSize)
            if ($info.OnDiskDataSize -gt 0) { $Acc.localFiles++ }
            if ($info.InSyncState -ne $CF_IN_SYNC -or $info.ModifiedDataSize -gt 0) { $Acc.notInSync++ }
        } elseif ($info.Queried) {
            if ($info.AllocatedSize -gt 0) { $Acc.allocatedBytes += $info.AllocatedSize; $Acc.localFiles++ }
            $Acc.notPlaceholder++
        } else {
            $Acc.unreadable++
        }
        return
    }
    $entries = @()
    try { $entries = Get-Entries $Full } catch { $Acc.unreadable++; return }
    foreach ($e in $entries) {
        $child = $Full + '\' + $e.Name
        if ($e.IsDirectory) {
            if (-not (Test-CloudReparseTag $e.ReparseTag)) { continue }
            Measure-ExclusionAllocation $child $true $Acc
        } else {
            $Acc.fileCount++
            $Acc.logicalBytes += $e.Length
            if ($e.Length -eq 0) { continue }
            if ($Acc.queries -ge $MaxPlaceholderQueriesPerPass) { $Acc.capped = $true; continue }
            $Acc.queries++
            $info = Get-PlaceholderState $child $false
            if ($info.IsPlaceholder) {
                $Acc.allocatedBytes += [Math]::Max([Int64]0, $info.OnDiskDataSize)
                if ($info.OnDiskDataSize -gt 0) { $Acc.localFiles++ }
                if ($info.InSyncState -ne $CF_IN_SYNC -or $info.ModifiedDataSize -gt 0) { $Acc.notInSync++ }
            } elseif ($info.Queried) {
                if ($info.AllocatedSize -gt 0) { $Acc.allocatedBytes += $info.AllocatedSize; $Acc.localFiles++ }
                $Acc.notPlaceholder++
            } else {
                $Acc.unreadable++
            }
        }
    }
}

function New-AllocationAccumulator {
    return @{ fileCount = 0; logicalBytes = [Int64]0; allocatedBytes = [Int64]0; localFiles = 0
              notInSync = 0; notPlaceholder = 0; unreadable = 0; queries = 0; capped = $false }
}

# ============================================================== enforcement ==

function Test-IsUnderAny {
    param([string]$Rel, [string[]]$Roots)
    foreach ($r in $Roots) {
        if ($Rel -eq $r) { return $true }
        if ($Rel.StartsWith($r + '/', [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Invoke-EnforcementPass {
    $script:EnforcePassNo++
    $cfg = Read-WantedConfig
    if (-not $cfg.ok) {
        Set-SubtaskError 'config' $cfg.error
        return $false
    }

    # Monotonic revision guard: a lower revision, or the same revision with
    # different content, is an error (v2 plan section 2.1).
    $prevRev  = $script:State.appliedRevision
    $prevHash = $script:State.wantedHash
    if ($null -ne $prevRev) {
        if ($cfg.revision -lt $prevRev) {
            Set-SubtaskError 'config' "exclusions.json revision $($cfg.revision) is lower than the applied revision $prevRev"
            return $false
        }
        if ($cfg.revision -eq $prevRev -and $null -ne $prevHash -and $cfg.hash -ne $prevHash) {
            Set-SubtaskError 'config' "exclusions.json revision $($cfg.revision) was reused with different contents"
            return $false
        }
    }
    Clear-SubtaskError 'config'

    $wanted = @($cfg.wanted)

    # A changed configuration re-arms every decimated re-verification below: a new
    # exclusion elsewhere can move content around an already-applied root (D34).
    if ($cfg.hash -ne $script:State.wantedHash) { $script:AppliedVerify = @{} }

    # Disarm the D34 ACL-reconciliation fast path *before* the first ACL write,
    # not after. The flag licenses the ten-minute pass to skip per-entry DACL
    # reads, so a crash between adding a deny and persisting state must not be
    # able to leave an orphan ACE that no later pass ever looks for. Nothing below
    # mutates an ACL when the wanted set is empty and private state is already
    # empty -- and the flag is only ever set true in exactly that situation.
    if ($script:State.aclCleanEmpty -and $wanted.Count -gt 0) {
        $script:State.aclCleanEmpty = $false
        Write-PrivateState
    }

    $records = New-Object System.Collections.Generic.List[object]
    $skipped = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $changed = $false
    $enforcementError = $null

    $newRoots = New-Object System.Collections.Generic.List[string]
    $newGuards = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $pending = New-Object System.Collections.Generic.List[object]

    # --- preflight ACL authority on every target and parent before mutating ---
    $existing = New-Object System.Collections.Generic.List[string]
    foreach ($rel in $wanted) {
        $full = Get-FullFromRelative $rel
        if (-not ([IO.File]::Exists($full) -or [IO.Directory]::Exists($full))) { continue }
        $existing.Add($rel)
    }
    foreach ($rel in $existing) {
        $full = Get-FullFromRelative $rel
        $isDir = [IO.Directory]::Exists($full)
        $parentRel = Get-ParentRelative $rel
        $parentFull = Get-FullFromRelative $parentRel
        $bad = $null
        if (-not (Test-AclAuthority $full $isDir))       { $bad = $rel }
        elseif (-not (Test-AclAuthority $parentFull $true)) { $bad = $parentRel }
        if ($null -ne $bad) {
            [void]$skipped.Add($rel)
            # Still claim the parent guard: the item is skipped, not re-included,
            # so the obsolete-removal pass below must not strip protection that
            # is already in place around it.
            [void]$newGuards.Add($parentRel)
            $records.Add([ordered]@{
                path = $rel; state = 'error'
                detail = "acl-write-denied: $bad"
                logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
            })
        }
    }

    # --- establish every newly desired protection before removing any old one ---
    foreach ($rel in $wanted) {
        if ($skipped.Contains($rel)) { continue }
        $full = Get-FullFromRelative $rel
        $isDir = [IO.Directory]::Exists($full)
        $isFile = [IO.File]::Exists($full)
        if (-not ($isDir -or $isFile)) {
            $records.Add([ordered]@{
                path = $rel; state = 'not-found'
                detail = 'no such item under the sync root yet; it will be hidden as soon as it appears'
                logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
            })
            continue
        }
        $parentRel = Get-ParentRelative $rel
        $parentFull = Get-FullFromRelative $parentRel
        try {
            if (Add-ExactRule $parentFull $true (New-ParentGuardRule)) { $changed = $true }
            if (Add-ExactRule $full $isDir (New-TargetDenyRule $isDir)) { $changed = $true }
            [void]$newGuards.Add($parentRel)
            $newRoots.Add($rel)
            $pending.Add(@{ rel = $rel; full = $full; isDir = $isDir })
        } catch {
            # Partial application is possible here (the guard may have landed
            # before the target deny threw), so keep the guard claimed and retry
            # next pass rather than unwinding.
            [void]$newGuards.Add($parentRel)
            $enforcementError = "cannot protect '$rel': $($_.Exception.Message)"
            $records.Add([ordered]@{
                path = $rel; state = 'error'; detail = $enforcementError
                logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
            })
        }
    }

    # --- remove obsolete protections only after all desired ones exist ---
    foreach ($oldRel in @($script:State.roots)) {
        if ($wanted -contains $oldRel) { continue }
        $full = Get-FullFromRelative $oldRel
        $isDir = [IO.Directory]::Exists($full)
        if (-not ($isDir -or [IO.File]::Exists($full))) { continue }
        try {
            if (Remove-ExactRule $full $isDir (New-TargetDenyRule $isDir)) { $changed = $true }
        } catch {
            $enforcementError = "cannot re-include '$oldRel': $($_.Exception.Message)"
        }
    }
    foreach ($oldParent in @($script:State.guardedParents)) {
        if ($newGuards.Contains($oldParent)) { continue }
        $full = Get-FullFromRelative $oldParent
        if (-not [IO.Directory]::Exists($full)) { continue }
        try {
            if (Remove-ExactRule $full $true (New-ParentGuardRule)) { $changed = $true }
        } catch {
            $enforcementError = "cannot remove the parent guard on '$oldParent': $($_.Exception.Message)"
        }
    }

    # --- request dehydration only after access has been denied ---
    foreach ($p in $pending) {
        $rel = $p.rel; $full = $p.full; $isDir = $p.isDir
        $wasApplied = $false
        foreach ($old in $script:Exclusions) {
            if ($old.path -eq $rel -and $old.state -eq 'applied') { $wasApplied = $true; break }
        }
        $denyOk = $false
        try {
            $denyOk = (Test-ExactRulePresent $full $isDir (New-TargetDenyRule $isDir)) -and
                      (Test-ExactRulePresent (Get-FullFromRelative (Get-ParentRelative $rel)) $true (New-ParentGuardRule))
        } catch {
            $records.Add([ordered]@{
                path = $rel; state = 'error'
                detail = "cannot verify the deny ACEs: $($_.Exception.Message)"
                logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
            })
            continue
        }
        if (-not $denyOk) {
            $records.Add([ordered]@{
                path = $rel; state = 'applying'; detail = 'establishing the deny and parent-guard ACEs'
                logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
            })
            continue
        }

        if ($wasApplied) {
            # Cheap re-verification: stay applied unless content came back. The
            # walk opens no handles, but it is still recursive, and running it over
            # every applied root every 60 s is the agent's largest steady-state
            # cost once a few large folders are excluded. It is purely reporting:
            # the target deny and parent guard above are re-asserted every pass
            # regardless, so `syncshare` access never depends on this measurement
            # (D15/D34). Verify every pass for the first few after a root becomes
            # applied -- when content is most likely still settling -- then every
            # tenth, and immediately whenever the configuration changes. Content
            # can only come back here through the iCloud client, so a label that
            # lags by up to ten minutes matches the tree.json cadence users
            # already see.
            $seen = $script:AppliedVerify[$rel]
            $due = $true
            if ($null -ne $seen) {
                $due = ([int]$seen.verifications -lt $AppliedVerifyFastPasses) -or
                       (($script:EnforcePassNo - [int]$seen.pass) -ge $AppliedVerifyEveryPasses)
            }
            if (-not $due) {
                $records.Add([ordered]@{
                    path = $rel; state = 'applied'; detail = ''
                    logicalBytes = [Int64]$seen.logicalBytes; localAllocatedBytes = [Int64]0
                })
                continue
            }
            $acc = New-CheapAccumulator
            if ($isDir) { Measure-SubtreeCheap $full $acc }
            else {
                $acc.fileCount = 1
                try { $acc.logicalBytes = (New-Object System.IO.FileInfo($full)).Length } catch { }
                $attrs = 0
                try { $attrs = [int][IO.File]::GetAttributes($full) } catch { }
                if (($attrs -band $ATTR_RECALL) -eq 0 -and $acc.logicalBytes -gt 0) { $acc.nonRecallFiles = 1 }
            }
            if ($acc.nonRecallFiles -eq 0) {
                $verifications = 1
                if ($null -ne $seen) { $verifications = [int]$seen.verifications + 1 }
                $script:AppliedVerify[$rel] = @{ pass = $script:EnforcePassNo
                                                 verifications = $verifications
                                                 logicalBytes = [Int64]$acc.logicalBytes }
                $records.Add([ordered]@{
                    path = $rel; state = 'applied'; detail = ''
                    logicalBytes = [Int64]$acc.logicalBytes; localAllocatedBytes = [Int64]0
                })
                continue
            }
            # Content is back under an applied root: fall through to re-request it,
            # and measure every pass again until it settles.
            $script:AppliedVerify.Remove($rel)
        }

        # On first protection, and a bounded retry while still pending: the
        # request is idempotent but `attrib /S /D` over a large folder is not
        # free, and dehydration is asynchronous, so re-asking every 60 s buys
        # nothing (v2 plan section 3.1).
        $attempt = $script:DehydrateAttempts[$rel]
        if ($null -eq $attempt) { $attempt = @{ count = 0; lastPass = -999 } }
        if ($attempt.count -lt 3 -or ($script:EnforcePassNo - $attempt.lastPass) -ge 10) {
            try { Set-OnlineOnlyRequest $full $isDir }
            catch {
                $records.Add([ordered]@{
                    path = $rel; state = 'error'; detail = $_.Exception.Message
                    logicalBytes = [Int64]0; localAllocatedBytes = [Int64]0
                })
                continue
            }
            $attempt.count++
            $attempt.lastPass = $script:EnforcePassNo
            $script:DehydrateAttempts[$rel] = $attempt
        }

        $acc = New-AllocationAccumulator
        Measure-ExclusionAllocation $full $isDir $acc
        if ($acc.allocatedBytes -eq 0 -and -not $acc.capped -and $acc.unreadable -eq 0) {
            $script:DehydrateAttempts.Remove($rel)
            # Seed the D34 re-verification schedule: zero verifications so far, so
            # the next few passes measure this root on every cycle.
            $script:AppliedVerify[$rel] = @{ pass = $script:EnforcePassNo; verifications = 0
                                             logicalBytes = [Int64]$acc.logicalBytes }
            $records.Add([ordered]@{
                path = $rel; state = 'applied'; detail = ''
                logicalBytes = [Int64]$acc.logicalBytes; localAllocatedBytes = [Int64]0
            })
        } else {
            $script:AppliedVerify.Remove($rel)
            $bits = New-Object System.Collections.Generic.List[string]
            $bits.Add('online-only requested; content is still allocated locally')
            if ($acc.notInSync -gt 0)      { $bits.Add("$($acc.notInSync) file(s) modified or not yet in sync") }
            if ($acc.notPlaceholder -gt 0) { $bits.Add("$($acc.notPlaceholder) file(s) are not cloud placeholders") }
            if ($acc.unreadable -gt 0)     { $bits.Add("$($acc.unreadable) item(s) could not be inspected (open or locked)") }
            if ($acc.capped)               { $bits.Add("measurement capped at $MaxPlaceholderQueriesPerPass files this pass") }
            $records.Add([ordered]@{
                path = $rel; state = 'pending-dehydrate'; detail = ($bits -join '; ')
                logicalBytes = [Int64]$acc.logicalBytes; localAllocatedBytes = [Int64]$acc.allocatedBytes
            })
        }
    }

    # --- persist ---
    foreach ($stale in @($script:DehydrateAttempts.Keys)) {
        if ($wanted -notcontains $stale) { $script:DehydrateAttempts.Remove($stale) }
    }
    foreach ($stale in @($script:AppliedVerify.Keys)) {
        if ($wanted -notcontains $stale) { $script:AppliedVerify.Remove($stale) }
    }
    $script:State.roots = @($newRoots.ToArray())
    $script:State.guardedParents = @($newGuards)
    $script:State.appliedRevision = $cfg.revision
    $script:State.wantedHash = $cfg.hash
    Write-PrivateState
    $script:AppliedRevision = $cfg.revision
    # User intent, not "where a deny actually landed": tree/list marking, sweep
    # pruning and ACL reconciliation all use this, so a root that could not be
    # protected this pass keeps any protection it already has.
    $script:WantedRoots = $wanted

    $sorted = $records | Sort-Object -Property @{ Expression = { $_.path } }
    $script:Exclusions = @($sorted)

    if ($null -ne $enforcementError) { Set-SubtaskError 'enforcement' $enforcementError }
    else { Clear-SubtaskError 'enforcement' }

    if ($changed) {
        $script:TreeDirty = $true
        # The pruned set the sweep walks is derived from the exclusion set, so a
        # protection change invalidates any cached candidate list (D34).
        Reset-SweepCandidateCache
    }
    return $changed
}

# ======================================================= reclamation (D26) ===

function Get-SweepCandidates {
    # Attribute-only walk of INCLUDED areas, pruned at every excluded root.
    param([string]$Full, [string]$Rel, [string[]]$ExcludedRoots, [hashtable]$Acc, [bool]$WantRecall)
    $entries = @()
    try { $entries = Get-Entries $Full } catch { return }
    $entries = [IcloudBridgeNative]::SortByName($entries)
    foreach ($e in $entries) {
        $childRel = if ($Rel -eq '') { $e.Name } else { "$Rel/$($e.Name)" }
        if (Test-IsUnderAny $childRel $ExcludedRoots) { continue }
        $child = $Full + '\' + $e.Name
        if ($e.IsDirectory) {
            if (-not (Test-CloudReparseTag $e.ReparseTag)) { continue }
            Get-SweepCandidates $child $childRel $ExcludedRoots $Acc $WantRecall
            if ($Acc.stopped) { return }
        } else {
            if ($e.Length -le 0) { continue }
            if (($e.Attributes -band $ATTR_PINNED) -ne 0) { continue }
            $hasRecall = (($e.Attributes -band $ATTR_RECALL) -ne 0)
            if ($hasRecall -ne $WantRecall) { continue }
            $age = if ($script:LastAccessUsable -and $e.LastAccessTicks -gt 0) { $e.LastAccessTicks } else { $e.LastWriteTicks }
            $Acc.items.Add(@{ rel = $childRel; full = $child; age = $age; length = $e.Length })
        }
    }
}

function Reset-SweepCandidateCache {
    $script:SweepStage1 = $null
    $script:SweepStage1Pass = -999
    $script:SweepStage2 = $null
    $script:SweepStage2Pass = -999
}

# Rebuilding a candidate list means a full attribute-only walk of every included
# area, and a low-disk episode spans many 60 s passes -- exactly when the guest is
# also busy uploading. Reuse the list for a few passes instead (v2 plan D34).
# Freshness is not what makes this safe: every candidate's placeholder state is
# re-read immediately before its dehydration request, so pinned, modified,
# not-in-sync, already-dehydrated and vanished files are all re-detected there
# (D26). A file hydrated mid-episode simply waits for the next walk, and the
# episode may not *end* on a stale list -- see Invoke-ReclamationSweep.

function Get-SweepStage1 {
    # Fully local files (no RECALL), coldest first.
    param([string[]]$ExcludedRoots)
    if ($null -ne $script:SweepStage1 -and
        ($script:EnforcePassNo - $script:SweepStage1Pass) -lt $SweepRewalkEveryPasses) {
        return $script:SweepStage1
    }
    $acc = @{ items = (New-Object System.Collections.Generic.List[object]); stopped = $false }
    Get-SweepCandidates $script:SyncRootFull '' $ExcludedRoots $acc $false
    $script:SweepStage1 = @($acc.items | Sort-Object -Property @{ Expression = { $_.age } })
    $script:SweepStage1Pass = $script:EnforcePassNo
    return $script:SweepStage1
}

function Get-SweepStage2 {
    # Partially hydrated placeholders (RECALL), ordered for the persisted cursor.
    param([string[]]$ExcludedRoots)
    if ($null -ne $script:SweepStage2 -and
        ($script:EnforcePassNo - $script:SweepStage2Pass) -lt $SweepRewalkEveryPasses) {
        return $script:SweepStage2
    }
    $acc = @{ items = (New-Object System.Collections.Generic.List[object]); stopped = $false }
    Get-SweepCandidates $script:SyncRootFull '' $ExcludedRoots $acc $true
    # Ordinal-ignore-case so the persisted cursor comparison below cannot
    # skip or repeat entries the way a culture-sensitive sort would.
    $acc.items.Sort([System.Comparison[object]]{
        param($a, $b)
        $r = [string]::Compare($a.rel, $b.rel, [StringComparison]::OrdinalIgnoreCase)
        if ($r -ne 0) { return $r }
        return [string]::CompareOrdinal($a.rel, $b.rel)
    })
    $script:SweepStage2 = $acc.items
    $script:SweepStage2Pass = $script:EnforcePassNo
    return $script:SweepStage2
}

function Invoke-ReclamationSweep {
    param([string[]]$ExcludedRoots)

    $space = Get-VolumeSpace
    $sw = $script:State.sweep
    $nowTicks = [DateTime]::UtcNow.Ticks
    $belowFloor = ($space.free -lt $SweepFloorBytes)

    $script:SweepInfo.lastRunAt = Get-UtcStamp
    $script:SweepInfo.belowFloor = $belowFloor
    $script:SweepInfo.requestedBytes = [Int64]0
    $script:SweepInfo.blockedBytes = [Int64]0
    $script:SweepInfo.blockedCount = 0

    if (-not $sw.episodeActive) {
        if (-not $belowFloor) { $script:SweepInfo.inProgress = $false; $script:SweepInfo.freedBytes = [Int64]0; return }
        if ([Int64]$sw.cooldownUntilTicks -gt $nowTicks) {
            # Nothing was eligible recently; do not re-walk the tree every minute.
            $script:SweepInfo.inProgress = $false
            $script:SweepInfo.freedBytes = [Int64]0
            return
        }
        $sw.episodeActive = $true
        $sw.episodeStartFreeBytes = [Int64]$space.free
        $sw.stage2Cursor = $null
        $sw.stage2Exhausted = $false
        Reset-SweepCandidateCache
    }

    $script:SweepInfo.freedBytes = [Int64][Math]::Max(0, $space.free - [Int64]$sw.episodeStartFreeBytes)

    if ($space.free -ge $SweepTargetBytes) {
        $sw.episodeActive = $false
        $sw.stage2Cursor = $null
        $script:SweepInfo.inProgress = $false
        Reset-SweepCandidateCache
        Write-PrivateState
        return
    }

    $deficit = [Int64]($SweepTargetBytes - $space.free)
    $requested = [Int64]0
    $blocked = [Int64]0
    $blockedCount = 0
    $eligible = 0

    # --- stage 1: fully local files (no RECALL) -- the hydrated working set ---
    # Iteration deliberately restarts from the coldest entry every pass rather than
    # consuming the list: dehydration is asynchronous, so a file requested last
    # pass still reports OnDiskDataSize > 0 and counts towards the deficit again.
    # That is what keeps the sweep from over-requesting while Windows catches up.
    $stage1 = Get-SweepStage1 $ExcludedRoots
    foreach ($c in $stage1) {
        if ($requested -ge $deficit) { break }
        $info = Get-PlaceholderState $c.full $false
        if (-not $info.IsPlaceholder) {
            if ($info.AllocatedSize -gt 0) { $blocked += $info.AllocatedSize; $blockedCount++ }
            continue
        }
        if ($info.PinState -eq $CF_PIN_PINNED -or $info.InSyncState -ne $CF_IN_SYNC -or
            $info.ModifiedDataSize -gt 0 -or $info.OnDiskDataSize -le 0) {
            if ($info.OnDiskDataSize -gt 0) { $blocked += $info.OnDiskDataSize; $blockedCount++ }
            continue
        }
        try {
            Set-OnlineOnlyRequest $c.full $false
            $requested += $info.OnDiskDataSize
            $eligible++
        } catch {
            $blocked += $info.OnDiskDataSize; $blockedCount++
        }
    }

    # --- stage 2: partially hydrated placeholders (RECALL and OnDiskDataSize>0) ---
    if ($requested -lt $deficit -and -not $sw.stage2Exhausted) {
        $ordered = Get-SweepStage2 $ExcludedRoots
        $cursor = $sw.stage2Cursor
        $examined = 0
        $last = $null
        # Exhaustion means "reached the end of the candidate list", not "left
        # the loop": breaking early because the deficit was covered must leave
        # the cursor live, or a pass whose dehydration requests stall upstream
        # would end the episode with unexamined candidates (section 3.1).
        $reachedEnd = $true
        foreach ($c in $ordered) {
            if ($null -ne $cursor -and [string]::Compare($c.rel, [string]$cursor, [StringComparison]::OrdinalIgnoreCase) -le 0) { continue }
            if ($examined -ge $MaxStage2QueriesPerPass) { $reachedEnd = $false; break }
            $examined++
            $last = $c.rel
            $info = Get-PlaceholderState $c.full $false
            if (-not $info.IsPlaceholder -or $info.OnDiskDataSize -le 0) { continue }
            if ($info.PinState -eq $CF_PIN_PINNED -or $info.InSyncState -ne $CF_IN_SYNC -or $info.ModifiedDataSize -gt 0) {
                $blocked += $info.OnDiskDataSize; $blockedCount++
                continue
            }
            try {
                Set-OnlineOnlyRequest $c.full $false
                $requested += $info.OnDiskDataSize
                $eligible++
            } catch {
                $blocked += $info.OnDiskDataSize; $blockedCount++
            }
            if ($requested -ge $deficit) { $reachedEnd = $false; break }
        }
        if ($null -ne $last) { $sw.stage2Cursor = $last }
        if ($reachedEnd) {
            if ($script:SweepStage2Pass -eq $script:EnforcePassNo) {
                $sw.stage2Exhausted = $true; $sw.stage2Cursor = $null
            } else {
                # Reached the end of a *cached* list. Exhaustion ends the episode,
                # so it may only be declared from a walk taken this pass (D34).
                Reset-SweepCandidateCache
            }
        }
    }

    $script:SweepInfo.requestedBytes = $requested
    $script:SweepInfo.blockedBytes = $blocked
    $script:SweepInfo.blockedCount = $blockedCount

    # Same rule for stage 1: never conclude "nothing eligible" from a cached list.
    # Drop it, keep the episode open, and let the next pass decide on a fresh walk.
    if ($eligible -eq 0 -and $script:SweepStage1Pass -ne $script:EnforcePassNo) {
        Reset-SweepCandidateCache
        $script:SweepInfo.inProgress = $true
        Write-PrivateState
        return
    }

    # Dehydration is asynchronous: keep the episode open and re-measure next pass.
    if ($eligible -eq 0 -and $sw.stage2Exhausted) {
        $sw.episodeActive = $false
        $sw.cooldownUntilTicks = [Int64]([DateTime]::UtcNow.AddSeconds($SweepCooldownSeconds).Ticks)
        $script:SweepInfo.inProgress = $false
        Reset-SweepCandidateCache
    } else {
        $script:SweepInfo.inProgress = $true
    }
    Write-PrivateState
}

# ================================================== full scan + reconciliation

function Get-AgentDenyKind {
    # Classify this object's explicit syncshare deny ACEs, if any.
    param([string]$Full, [bool]$IsDirectory)
    $kind = @{ target = $false; guard = $false }
    $sec = Get-DaclSecurity $Full $IsDirectory
    $targetTemplate = New-TargetDenyRule $IsDirectory
    $guardTemplate = New-ParentGuardRule
    foreach ($r in $sec.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        if (Test-RuleEquivalent $r $targetTemplate) { $kind.target = $true }
        elseif (Test-RuleEquivalent $r $guardTemplate) { $kind.guard = $true }
    }
    return $kind
}

function Compare-RelPathDfs {
    # Compare two relative paths in DFS-preorder emission order: segment-wise
    # with the walk's own comparator, ancestors before descendants. A flat
    # string compare would disagree with the walk for sibling names containing
    # characters below '/' (space sorts "a b" before "a/z", the walk emits
    # "a/z" first), which would let a resume cursor skip never-visited subtrees.
    param([string]$A, [string]$B)
    $sa = $A -split '/'
    $sb = $B -split '/'
    $n = [Math]::Min($sa.Length, $sb.Length)
    for ($i = 0; $i -lt $n; $i++) {
        $r = [string]::Compare($sa[$i], $sb[$i], [StringComparison]::OrdinalIgnoreCase)
        if ($r -eq 0) { $r = [string]::CompareOrdinal($sa[$i], $sb[$i]) }
        if ($r -ne 0) { return $r }
    }
    return $sa.Length.CompareTo($sb.Length)
}

function Invoke-FullScan {
    # Ten-minute pass (v2 plan sections 2.3 / 3): builds tree.json, reports
    # fullyLocalLogicalBytes, and reconciles orphan agent-owned deny ACEs.
    # Attribute-only except for the bounded ACL reads.
    param([string[]]$ExcludedRoots, [bool]$ConfigValid)

    $started = [Diagnostics.Stopwatch]::StartNew()
    $script:CloudInfoQueries = 0
    $totals = @{ entries = 0; fullyLocal = [Int64]0 }
    $aclDeadline = [DateTime]::UtcNow.AddSeconds($AclReconcileBudgetSeconds)
    $aclCursor = [string]$script:State.aclCursor
    $aclState = @{ cursor = $aclCursor; resume = ($null -ne $aclCursor -and $aclCursor -ne ''); done = $true
                   last = $null; errored = $false; removed = $false }
    $wantedGuards = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($r in $ExcludedRoots) { [void]$wantedGuards.Add((Get-ParentRelative $r)) }

    # D34 fast path. Reconciliation reads a DACL per library entry -- a handle open
    # plus a security-descriptor read for every file and folder, budgeted at
    # $AclReconcileBudgetSeconds every ten minutes, forever. When nothing is
    # excluded, private state holds no roots or guarded parents, and the previous
    # pass completed with nothing to remove and no error, an orphan agent-owned
    # deny cannot exist, so the reads are pure cost. Skip them and keep only the
    # attribute-only walk that builds tree.json. Any exclusion, any ACL mutation
    # (the enforcement pass disarms the flag before its first write), a corrupt
    # private state file, or a reconciliation error all bring the full pass back.
    $emptyState = (@($ExcludedRoots).Count -eq 0 -and @($script:State.roots).Count -eq 0 -and
                   @($script:State.guardedParents).Count -eq 0)
    $aclSkip = ($ConfigValid -and $emptyState -and [bool]$script:State.aclCleanEmpty -and
                [string]::IsNullOrEmpty($aclCursor))
    $aclActive = ($ConfigValid -and -not $aclSkip)

    function Build-Node {
        param([string]$Full, [string]$Rel, [string]$Name)

        $node = [ordered]@{
            name = $Name; path = $Rel; logicalBytes = [Int64]0; fileCount = 0; dirCount = 0
            excluded = (Test-IsUnderAny $Rel $ExcludedRoots); dirs = @()
        }
        $entries = @()
        try { $entries = Get-Entries $Full } catch { return $node }
        $entries = [IcloudBridgeNative]::SortByName($entries)
        $childDirs = New-Object System.Collections.Generic.List[object]
        foreach ($e in $entries) {
            $totals.entries++
            $childRel = if ($Rel -eq '') { $e.Name } else { "$Rel/$($e.Name)" }
            $childFull = $Full + '\' + $e.Name

            # --- bounded, resumable ACL reconciliation -----------------------
            if ($aclActive -and [DateTime]::UtcNow -lt $aclDeadline) {
                $skip = $false
                if ($aclState.resume) {
                    if ((Compare-RelPathDfs $childRel $aclState.cursor) -le 0) { $skip = $true }
                    else { $aclState.resume = $false }
                }
                if (-not $skip) {
                    $aclState.last = $childRel
                    try {
                        $kind = Get-AgentDenyKind $childFull $e.IsDirectory
                        if ($kind.target -and -not (Test-IsUnderAny $childRel $ExcludedRoots)) {
                            [void](Remove-ExactRule $childFull $e.IsDirectory (New-TargetDenyRule $e.IsDirectory))
                            $aclState.removed = $true
                            $script:TreeDirty = $true
                        }
                        if ($kind.guard -and -not $wantedGuards.Contains($childRel)) {
                            [void](Remove-ExactRule $childFull $e.IsDirectory (New-ParentGuardRule))
                            $aclState.removed = $true
                            $script:TreeDirty = $true
                        }
                    } catch {
                        $aclState.errored = $true
                        Set-SubtaskError 'acl' "ACL reconciliation failed on '$childRel': $($_.Exception.Message)"
                    }
                }
            } elseif ($aclActive) {
                $aclState.done = $false
            }

            if ($e.IsDirectory) {
                if (-not (Test-CloudReparseTag $e.ReparseTag)) {
                    Set-SubtaskError 'scan' "skipping a non-cloud reparse point: $childRel"
                    continue
                }
                $child = Build-Node $childFull $childRel $e.Name
                $node.logicalBytes += $child.logicalBytes
                $node.fileCount += $child.fileCount
                $node.dirCount += 1 + $child.dirCount
                $childDirs.Add($child)
            } else {
                $node.fileCount++
                $node.logicalBytes += $e.Length
                if (($e.Attributes -band $ATTR_RECALL) -eq 0) { $totals.fullyLocal += $e.Length }
            }
        }
        if (-not $node.excluded) { $node.dirs = @($childDirs.ToArray()) }
        return $node
    }

    $root = Build-Node $script:SyncRootFull '' ''
    $started.Stop()

    if ($ConfigValid) {
        if ($aclState.done) {
            $script:State.aclCursor = $null
            # A complete, error-free reconciliation pass is the success that
            # clears a previously reported 'acl' failure (section 2.2).
            if (-not $aclState.errored) { Clear-SubtaskError 'acl' }
        } else {
            $script:State.aclCursor = $aclState.last
        }
        # Licence the next pass to skip the DACL reads only when this one proved
        # there is nothing to reconcile: an empty wanted set and empty private
        # state, plus a complete pass that removed nothing and hit no error (D34).
        $clean = $aclSkip -or ($aclState.done -and -not $aclState.errored -and -not $aclState.removed)
        $script:State.aclCleanEmpty = ($clean -and $emptyState)
        Write-PrivateState
    }

    $script:FullyLocalLogicalBytes = $totals.fullyLocal
    $script:ScanInfo.lastCompletedAt = Get-UtcStamp
    $script:ScanInfo.durationMs = [int]$started.ElapsedMilliseconds
    $script:ScanInfo.entries = $totals.entries
    $script:ScanInfo.cloudInfoQueries = $script:CloudInfoQueries

    $tree = [ordered]@{
        version = $ProtocolVersion
        generatedAt = Get-UtcStamp
        root = [ordered]@{ dirs = $root.dirs }
    }
    Write-JsonAtomic (Join-Path $BridgeDir 'tree.json') $tree
    $script:TreeDirty = $false
}

# ============================================================ list requests ==

function Invoke-ListRequests {
    param([string[]]$ExcludedRoots)
    $reqDir = Join-Path $BridgeDir 'requests'
    $respDir = Join-Path $BridgeDir 'responses'
    if (-not [IO.Directory]::Exists($reqDir)) { return }

    $cutoff = [DateTime]::UtcNow.AddSeconds(-$RequestTtlSeconds)
    $handled = 0
    foreach ($fi in ([IO.DirectoryInfo]::new($reqDir)).GetFiles()) {
        if ($fi.LastWriteTimeUtc -lt $cutoff) {
            try { $fi.Delete() } catch { }
            continue
        }
        if ($fi.Name -notmatch '^list-[0-9a-f]{32}\.json$') { continue }
        if ($handled -ge $MaxRequestsPerTick) { continue }
        $handled++
        $id = $fi.BaseName
        # The version goes on the response too (v2 plan section 2.4/D35), so all
        # three document kinds are checked the same way. It is set before the
        # try block so an error response carries it as well -- a failure the GUI
        # can read is more useful than one it rejects as unversioned.
        $response = [ordered]@{ version = $ProtocolVersion; path = ''; error = $null; offset = 0; nextOffset = $null; files = @() }
        try {
            if ($fi.Length -gt $MaxRequestBytes) { throw "request exceeds $MaxRequestBytes bytes" }
            $doc = Read-JsonFile $fi.FullName $MaxRequestBytes
            $reqPath = ''
            if (Test-HasProperty $doc 'path') { $reqPath = [string]$doc.path }
            $offset = 0
            if (Test-HasProperty $doc 'offset') {
                $o = Get-IntegerOrNull $doc.offset
                if ($null -eq $o -or $o -lt 0) { throw 'invalid "offset"' }
                $offset = [int]$o
            }
            $limit = 1000
            if (Test-HasProperty $doc 'limit') {
                $l = Get-IntegerOrNull $doc.limit
                if ($null -eq $l -or $l -lt 1 -or $l -gt 1000) { throw 'invalid "limit" (must be 1-1000)' }
                $limit = [int]$l
            }
            $c = Get-CanonicalRelative -Rel $reqPath -AllowRoot
            if (-not $c.ok) { throw $c.error }
            $contain = Test-PathContainment $c.rel
            if (-not $contain.ok) { throw $contain.error }
            $response.path = $c.rel
            $response.offset = $offset
            if (-not [IO.Directory]::Exists($c.full)) { throw 'no such folder under the sync root' }

            $entries = Get-Entries $c.full
            $files = New-Object System.Collections.Generic.List[object]
            foreach ($e in $entries) {
                if ($e.IsDirectory) { continue }
                $childRel = if ($c.rel -eq '') { $e.Name } else { "$($c.rel)/$($e.Name)" }
                $files.Add([ordered]@{
                    name = $e.Name
                    path = $childRel
                    logicalBytes = [Int64]$e.Length
                    excluded = (Test-IsUnderAny $childRel $ExcludedRoots)
                    dataless = ((($e.Attributes -band $ATTR_RECALL) -ne 0))
                })
            }
            $files.Sort([System.Comparison[object]]{
                param($a, $b)
                $r = [string]::Compare($a.name, $b.name, [StringComparison]::OrdinalIgnoreCase)
                if ($r -ne 0) { return $r }
                return [string]::CompareOrdinal($a.name, $b.name)
            })
            $page = New-Object System.Collections.Generic.List[object]
            for ($i = $offset; $i -lt $files.Count -and $page.Count -lt $limit; $i++) { $page.Add($files[$i]) }
            $response.files = @($page.ToArray())
            $next = $offset + $page.Count
            if ($next -lt $files.Count) { $response.nextOffset = $next }
        } catch {
            $response.error = $_.Exception.Message
            $response.files = @()
        }
        try {
            Write-JsonAtomic (Join-Path $respDir "$id.json") $response
        } catch {
            Set-SubtaskError 'requests' "cannot write a list response: $($_.Exception.Message)"
        }
        try { $fi.Delete() } catch { }
    }

    if ([IO.Directory]::Exists($respDir)) {
        foreach ($fi in ([IO.DirectoryInfo]::new($respDir)).GetFiles()) {
            if ($fi.LastWriteTimeUtc -lt $cutoff) { try { $fi.Delete() } catch { } }
        }
    }
}

# ================================================================== status ===

function Write-Status {
    $space = Get-VolumeSpace
    $running = $false
    try {
        $p = @(Get-Process -Name 'iCloudServices', 'iCloudDrive' -ErrorAction SilentlyContinue)
        $running = ($p.Count -gt 0)
    } catch { }

    $status = [ordered]@{
        version = $ProtocolVersion
        agentBuild = $AgentBuild
        generatedAt = Get-UtcStamp
        agentStartedAt = Get-UtcStamp $script:AgentStartedAt
        syncRoot = $script:SyncRootFull.Replace('\', '/')
        icloudClientRunning = $running
        diskFreeBytes = [Int64]$space.free
        diskTotalBytes = [Int64]$space.total
        appliedRevision = $script:AppliedRevision
        lastEnforcementAt = $script:LastEnforcementAt
        lastError = Get-LastError
        exclusions = @($script:Exclusions)
        fullyLocalLogicalBytes = [Int64]$script:FullyLocalLogicalBytes
        scan = $script:ScanInfo
        sweep = $script:SweepInfo
    }
    Write-JsonAtomic (Join-Path $BridgeDir 'status.json') $status
}

# ================================================================= startup ===

function Initialize-Agent {
    if (-not [IO.Directory]::Exists($SyncRoot)) { throw "sync root not found: $SyncRoot" }
    # No trailing separator: the recursive walks build every child path by plain
    # concatenation ($Full + '\' + $e.Name), which is only equivalent to
    # Join-Path while this invariant holds. It cannot make a drive root bare
    # ("C:") because the sync root is always a directory below one.
    $script:SyncRootFull = ([IO.Path]::GetFullPath($SyncRoot)).TrimEnd('\')
    foreach ($d in @($BridgeDir, $StateDir, (Join-Path $BridgeDir 'requests'), (Join-Path $BridgeDir 'responses'))) {
        if (-not [IO.Directory]::Exists($d)) { throw "bridge directory missing (run 04-bridge-agent.ps1): $d" }
    }

    try {
        $script:ShareSid = (New-Object Security.Principal.NTAccount($env:COMPUTERNAME, $ShareUser)).Translate([Security.Principal.SecurityIdentifier])
    } catch {
        throw "cannot resolve the '$ShareUser' account to a SID: $($_.Exception.Message)"
    }

    $rootInfo = [IcloudBridgeNative]::GetPlaceholderInfo($script:SyncRootFull, $true)
    if ($rootInfo.IsPlaceholder) { $script:SyncRootFileId = $rootInfo.SyncRootFileId }

    $script:LastAccessUsable = Test-LastAccessUsable
    $script:State = Read-PrivateState
    # D34: whatever the previous run concluded, every agent start does one real
    # full-tree reconciliation. The fast path may only be re-armed by a pass this
    # process ran itself, so anything that changed while the agent was stopped is
    # still caught -- which keeps section 3's startup-reconciliation rule intact.
    $script:State.aclCleanEmpty = $false
    $script:AppliedRevision = $script:State.appliedRevision
    # Best knowledge before the first enforcement pass reads exclusions.json.
    $script:WantedRoots = @($script:State.roots)

    # First heartbeat before the potentially slow one-time migration below, so
    # 04-bridge-agent.ps1's verify step (and the GUI) sees the agent alive
    # within seconds of task start even on a large library.
    try { Write-Status } catch { Set-SubtaskError 'status' "status write failed: $($_.Exception.Message)" }

    # D25 one-time migration: clear v1's always-available intent without asking
    # for eviction. Never run a global +U.
    $marker = Join-Path $StateDir 'v1-pin-cleared.marker'
    if (-not [IO.File]::Exists($marker)) {
        $rc = Invoke-Native 'attrib.exe' @('-P', (Join-Path $script:SyncRootFull '*'), '/S', '/D')
        if ($rc -ne 0) {
            Set-SubtaskError 'migration' "clearing the legacy pin intent failed (exit $rc); will retry next start"
        } else {
            Clear-SubtaskError 'migration'
            Write-JsonAtomic $marker ([ordered]@{ version = 1; clearedAt = (Get-UtcStamp) })
        }
    }
}

# =================================================================== main ====

try {
    Initialize-Agent
} catch {
    Write-Error "fatal: $($_.Exception.Message)"
    exit 1
}

$sinceStatus = [int]$StatusEverySeconds
$sinceEnforce = [int]$EnforceEverySeconds
$sinceTree = [int]$TreeEverySeconds

while ($true) {
    try {
        try {
            Invoke-ListRequests @($script:WantedRoots)
            Clear-SubtaskError 'requests'
        } catch {
            Set-SubtaskError 'requests' "list request handling failed: $($_.Exception.Message)"
        }

        if ($sinceEnforce -ge $EnforceEverySeconds) {
            $sinceEnforce = 0
            try {
                [void](Invoke-EnforcementPass)
                $script:LastEnforcementAt = Get-UtcStamp
            } catch {
                Set-SubtaskError 'enforcement' "enforcement pass failed: $($_.Exception.Message)"
            }
            try {
                Invoke-ReclamationSweep @($script:WantedRoots)
                Clear-SubtaskError 'sweep'
            } catch {
                Set-SubtaskError 'sweep' "reclamation sweep failed: $($_.Exception.Message)"
            }
        }

        if ($sinceTree -ge $TreeEverySeconds -or $script:TreeDirty) {
            $sinceTree = 0
            try {
                Clear-SubtaskError 'scan'
                Invoke-FullScan @($script:WantedRoots) (-not $script:Errors.ContainsKey('config'))
                Clear-SubtaskError 'tree'
            } catch {
                Set-SubtaskError 'tree' "tree scan failed: $($_.Exception.Message)"
                $script:TreeDirty = $false
            }
        }

        if ($sinceStatus -ge $StatusEverySeconds) {
            $sinceStatus = 0
            try { Write-Status; Clear-SubtaskError 'status' }
            catch { Set-SubtaskError 'status' "status write failed: $($_.Exception.Message)" }
        }
        Clear-SubtaskError 'loop'
    } catch {
        Set-SubtaskError 'loop' "unexpected error: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $TickSeconds
    $sinceStatus += $TickSeconds
    $sinceEnforce += $TickSeconds
    $sinceTree += $TickSeconds
}
