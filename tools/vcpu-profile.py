#!/usr/bin/env python3
"""vcpu-profile.py — split the guest's CPU cost by execution mode.

Runs on the Linux host, unprivileged, read-only. Samples the dockur QEMU
process's per-thread counters in `/proc` over a fixed window and reports, per
thread and in total:

  guest      -- `gtime`, cycles executed inside the guest by KVM. This is
                Windows itself. No host-side knob touches it.
  qemu       -- `utime - gtime`, QEMU's own userspace: device emulation, the
                VNC server, the block layer's userspace half.
  host krnl  -- `stime`, the kernel on the guest's behalf: vmexit handling, EPT
                faults, halt polling, tap/virtio-net work. This is the bucket a
                host tuning knob can plausibly move.

`docker stats` reports one aggregate percentage, which cannot distinguish "the
guest is busy" from "we are burning host CPU emulating it" — and those have
completely different fixes. The split is what says whether host-side tuning is
worth attempting at all. On 2026-07-26 the author's idle guest measured ~73-75%
guest / ~29% host-kernel / ~0% QEMU userspace, which bounded every host-side
knob at roughly 5% of one core and pointed the investigation into Windows.

  ./tools/vcpu-profile.py                # 60 s window
  ./tools/vcpu-profile.py --seconds 300
  ./tools/vcpu-profile.py --json

Alongside the CPU split it reports the container's block I/O rate from the
cgroup's `io.stat` — plan section 11.3's idle criterion covers write churn,
because an "idle" guest was measured writing 5-8 GB/day of host SSD. The
cgroup covers the whole container (QEMU plus its supervisor), which is the
right scope for a what-does-this-cost-the-host criterion.

Idempotent and side-effect free: it reads /proc, /sys/fs/cgroup and runs
`docker inspect`.

Requires only that the container is running and that this user can read its
/proc entries (the same user that can run `docker inspect`).
"""
import argparse
import json
import os
import subprocess
import sys
import time

CONTAINER = os.environ.get("ICLOUD_CONTAINER", "icloud-windows")
CLK_TCK = os.sysconf("SC_CLK_TCK")

# A window shorter than this is not worth reporting: utime and gtime are
# tick-sampled independently, so `utime - gtime` goes slightly negative over
# short windows (-0.48 s observed at 5 s). Sixty seconds keeps that noise well
# under a percent.
MIN_WINDOW = 60


def container_pid() -> int:
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def find_qemu(root_pid: int) -> int:
    """Locate the qemu-system process beneath the container's init.

    It is a *grandchild*, not a child: dockur runs tini as PID 1, which spawns a
    bash supervisor, which execs QEMU. Walking only one level down finds nothing,
    so descend the whole subtree.
    """
    seen, stack = set(), [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().split(b"\0")
        except OSError:
            continue
        if cmdline and b"qemu-system" in os.path.basename(cmdline[0] or b""):
            return pid
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as fh:
                stack.extend(int(x) for x in fh.read().split())
        except OSError:
            continue
    raise SystemExit(f"no qemu-system process found beneath pid {root_pid}")


def read_io(pid: int):
    """Return (read_bytes, write_bytes) from the pid's cgroup-v2 io.stat.

    /proc/<pid>/io would need ptrace rights over a root-owned QEMU; the cgroup
    file is world-readable. Returns None where the unified hierarchy is absent
    or the file is missing (e.g. no io controller), rather than guessing.
    """
    try:
        with open(f"/proc/{pid}/cgroup") as fh:
            path = next((line.split("::", 1)[1].strip()
                         for line in fh if line.startswith("0::")), None)
        if not path:
            return None
        rbytes = wbytes = 0
        with open(f"/sys/fs/cgroup{path}/io.stat") as fh:
            for line in fh:
                for field in line.split()[1:]:
                    key, _, value = field.partition("=")
                    if key == "rbytes":
                        rbytes += int(value)
                    elif key == "wbytes":
                        wbytes += int(value)
        return rbytes, wbytes
    except (OSError, ValueError):
        return None


def read_thread(pid: int, tid: int):
    """Return (name, utime, stime, gtime) in seconds, plus context switches.

    The `comm` field is parenthesised and may itself contain spaces and
    parentheses -- QEMU names its vCPU threads `CPU 0/KVM`. Splitting on the
    *last* ``)`` is the only correct way to find the field boundary; naive
    whitespace splitting silently reads the wrong columns and produces
    plausible-looking nonsense.
    """
    with open(f"/proc/{pid}/task/{tid}/stat") as fh:
        raw = fh.read()
    lparen, rparen = raw.index("("), raw.rindex(")")
    name = raw[lparen + 1:rparen]
    rest = raw[rparen + 2:].split()

    # `rest[0]` is field 3 (state), so field N is rest[N-3].
    utime = int(rest[11]) / CLK_TCK
    stime = int(rest[12]) / CLK_TCK
    gtime = int(rest[40]) / CLK_TCK

    vol = nonvol = 0
    try:
        with open(f"/proc/{pid}/task/{tid}/status") as fh:
            for line in fh:
                if line.startswith("voluntary_ctxt_switches:"):
                    vol = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    nonvol = int(line.split()[1])
    except OSError:
        pass
    return name, utime, stime, gtime, vol, nonvol


def snapshot(pid: int) -> dict:
    out = {}
    for entry in os.listdir(f"/proc/{pid}/task"):
        try:
            out[int(entry)] = read_thread(pid, int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=60.0,
                    help=f"sampling window (minimum {MIN_WINDOW}s for a meaningful split)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lifetime", action="store_true",
                    help="report cumulative totals since QEMU started instead of a window")
    args = ap.parse_args()

    try:
        root_pid = container_pid()
        qemu = find_qemu(root_pid)
    except subprocess.CalledProcessError:
        print(f"FAIL: container '{CONTAINER}' not running (or docker unreachable)",
              file=sys.stderr)
        return 2

    if args.lifetime:
        end, start, elapsed = snapshot(qemu), None, None
        io_delta = read_io(root_pid)
    else:
        if args.seconds < MIN_WINDOW:
            print(f"NOTE: a {args.seconds:g}s window is below the {MIN_WINDOW}s minimum; "
                  "the qemu-userspace figure will be noise.", file=sys.stderr)
        start = snapshot(qemu)
        io0 = read_io(root_pid)
        t0 = time.monotonic()
        time.sleep(args.seconds)
        end = snapshot(qemu)
        io1 = read_io(root_pid)
        elapsed = time.monotonic() - t0
        io_delta = None
        if io0 is not None and io1 is not None:
            io_delta = (io1[0] - io0[0], io1[1] - io0[1])

    rows, totals = [], {"guest": 0.0, "qemu": 0.0, "kernel": 0.0, "vol": 0, "nonvol": 0}
    for tid, cur in sorted(end.items()):
        name, utime, stime, gtime, vol, nonvol = cur
        if start is not None:
            if tid not in start:
                continue
            _, u0, s0, g0, v0, n0 = start[tid]
            utime, stime, gtime = utime - u0, stime - s0, gtime - g0
            vol, nonvol = vol - v0, nonvol - n0
        # utime includes gtime; clamp because they are sampled independently.
        qemu_user = max(0.0, utime - gtime)
        if gtime + qemu_user + stime < 0.005 and not args.lifetime:
            continue
        rows.append({"tid": tid, "name": name, "guest": gtime,
                     "qemu": qemu_user, "kernel": stime,
                     "vol_switches": vol, "nonvol_switches": nonvol})
        totals["guest"] += gtime
        totals["qemu"] += qemu_user
        totals["kernel"] += stime
        totals["vol"] += vol
        totals["nonvol"] += nonvol

    total_cpu = totals["guest"] + totals["qemu"] + totals["kernel"]
    report = {"container": CONTAINER, "qemu_pid": qemu, "window_seconds": elapsed,
              "lifetime": args.lifetime, "threads": rows, "totals": totals,
              "total_cpu_seconds": total_cpu}
    if elapsed:
        report["cores_used"] = total_cpu / elapsed
    if io_delta is not None:
        report["io_read_bytes"] = io_delta[0]
        report["io_write_bytes"] = io_delta[1]

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    scope = "lifetime" if args.lifetime else f"{elapsed:.1f}s window"
    print(f"container {CONTAINER}, qemu pid {qemu} — {scope}")
    print()
    print(f"{'thread':<14}{'guest':>10}{'qemu':>10}{'kernel':>10}{'vol/s':>9}{'nonvol/s':>10}")
    for r in rows:
        per = elapsed or 1.0
        print(f"{r['name']:<14}{r['guest']:>10.2f}{r['qemu']:>10.2f}{r['kernel']:>10.2f}"
              f"{r['vol_switches'] / per:>9.0f}{r['nonvol_switches'] / per:>10.0f}")
    print()
    print(f"{'TOTAL':<14}{totals['guest']:>10.2f}{totals['qemu']:>10.2f}"
          f"{totals['kernel']:>10.2f}   (core-seconds)")
    if total_cpu > 0:
        print(f"{'share':<14}{totals['guest'] / total_cpu:>9.1%}"
              f"{totals['qemu'] / total_cpu:>10.1%}{totals['kernel'] / total_cpu:>10.1%}")
    if elapsed:
        print()
        print(f"total {total_cpu:.2f} core-seconds over {elapsed:.1f}s "
              f"= {total_cpu / elapsed:.1%} of one core")
        print(f"host-side tuning can only ever address the kernel column: "
              f"{totals['kernel'] / elapsed:.1%} of one core.")
        if io_delta is not None:
            print(f"container block I/O: read {io_delta[0] / elapsed / 1024:.1f} KiB/s, "
                  f"write {io_delta[1] / elapsed / 1024:.1f} KiB/s (cgroup io.stat)")
    elif io_delta is not None:
        print(f"container block I/O since start: read {io_delta[0] / 2**30:.2f} GiB, "
              f"write {io_delta[1] / 2**30:.2f} GiB (cgroup io.stat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
