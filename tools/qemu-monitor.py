#!/usr/bin/env python3
"""Drive the Windows guest through QEMU's human monitor socket.

Runs INSIDE the dockur/windows container (see tools/guest-ctl.sh, which copies
this file in and invokes it). dockur starts QEMU with
`-monitor unix:/dev/shm/monitor.sock,server,wait=off`, which gives us two
capabilities that together make the guest scriptable:

  sendkey <key>       -- inject a keystroke
  screendump <file>   -- write a PPM screenshot of the guest display

That is the only general-purpose control channel into the guest: there is no
qemu-guest-agent, no WinRM/SSH, and RDP cannot be used non-interactively because
the auto-logon `icloud` account has a blank password (plan D8) and Windows
refuses blank-password network logons.

Deliberately does NOT press Enter unless --enter is passed, so a typed command
can be verified with a screenshot before it executes.

  python3 qemu-monitor.py --textfile /tmp/cmd.txt
  python3 qemu-monitor.py --enter
  python3 qemu-monitor.py --key ctrl-c
  python3 qemu-monitor.py --shot /tmp/screen.ppm
"""
import argparse, socket, sys, time

SOCK = "/dev/shm/monitor.sock"

# QEMU key names for characters that are not a plain letter/digit.
SYM = {
    ' ': 'spc', '-': 'minus', '.': 'dot', '/': 'slash', '\\': 'backslash',
    ';': 'semicolon', ':': 'shift-semicolon', '_': 'shift-minus',
    '=': 'equal', '+': 'shift-equal', ',': 'comma',
    "'": 'apostrophe', '"': 'shift-apostrophe',
    '[': 'bracket_left', ']': 'bracket_right',
    '{': 'shift-bracket_left', '}': 'shift-bracket_right',
    '`': 'grave_accent', '~': 'shift-grave_accent',
    '!': 'shift-1', '@': 'shift-2', '#': 'shift-3', '$': 'shift-4',
    '%': 'shift-5', '^': 'shift-6', '&': 'shift-7', '*': 'shift-8',
    '(': 'shift-9', ')': 'shift-0',
    '<': 'shift-comma', '>': 'shift-dot', '?': 'shift-slash',
    '|': 'shift-backslash',
}


def key_for(ch):
    if ch.isalpha() and ch.isascii():
        return 'shift-' + ch.lower() if ch.isupper() else ch.lower()
    if ch.isdigit():
        return ch
    if ch in SYM:
        return SYM[ch]
    if ch == '\n':
        return 'ret'
    raise ValueError("unmapped character: %r" % ch)


class Monitor:
    def __init__(self, sock=SOCK):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(sock)
        self.s.settimeout(1.5)
        time.sleep(0.25)
        try:
            self.s.recv(65536)          # drain banner
        except Exception:
            pass

    def cmd(self, c, settle=0.05, read=True):
        # read=False for sendkey. Draining the reply costs a full socket
        # timeout per keystroke, which makes typing ~1.5 s/char.
        self.s.sendall((c + "\n").encode())
        time.sleep(settle)
        if not read:
            return ""
        buf = b""
        try:
            while True:
                d = self.s.recv(65536)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
        return buf.decode(errors="replace")

    def type(self, text, delay=0.035):
        # Validate the whole string first so we never type half a command.
        for k in [key_for(c) for c in text]:
            self.cmd("sendkey " + k, settle=delay, read=False)

    def move(self, x, y, w=1280, h=720):
        """Move the pointer towards a PIXEL coordinate. APPROXIMATE -- see below.

        WARNING: even though `info mice` reports "QEMU HID Tablet (absolute)"
        as the active device, the human-monitor `mouse_move` here delivers
        RELATIVE deltas. Feeding it absolute 0..32767 values parked the cursor
        in the bottom-right corner and clicked "Show desktop", minimising every
        window (verified 2026-07-22). So: home the pointer at (0,0) with a large
        negative delta, then step out by x,y.

        Still only approximate -- Windows "Enhance pointer precision" scales
        relative motion non-linearly. PREFER KEYBOARD NAVIGATION. If you must
        click, --shot first and again after, and never aim near a control whose
        mis-click is destructive (e.g. the iCloud Drive on/off row).
        """
        self.cmd("mouse_move -%d -%d" % (w * 4, h * 4), settle=0.3, read=False)
        self.cmd("mouse_move %d %d" % (int(x), int(y)), settle=0.3, read=False)

    def click(self, x, y, w=1280, h=720):
        self.move(x, y, w, h)
        self.cmd("mouse_button 1", settle=0.15, read=False)
        self.cmd("mouse_button 0", settle=0.35, read=False)

    def close(self):
        self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    # --textfile avoids shell-quoting problems entirely: copy the text in as a
    # file rather than passing it through nested docker exec / sg quoting.
    ap.add_argument("--textfile")
    ap.add_argument("--enter", action="store_true")
    ap.add_argument("--key", action="append", default=[])
    ap.add_argument("--shot")
    ap.add_argument("--info")
    ap.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"))
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--delay", type=float, default=0.035)
    a = ap.parse_args()

    text = a.text
    if a.textfile:
        with open(a.textfile) as fh:
            text = fh.read().rstrip("\n")

    m = Monitor()
    if a.move:
        m.move(a.move[0], a.move[1], a.width, a.height)
    if a.click:
        m.click(a.click[0], a.click[1], a.width, a.height)
    if text:
        m.type(text, a.delay)
    for k in a.key:
        m.cmd("sendkey " + k, settle=0.15, read=False)
    if a.enter:
        m.cmd("sendkey ret", settle=0.15, read=False)
    if a.shot:
        m.cmd("screendump " + a.shot, settle=0.8)
    if a.info:
        print(m.cmd("info " + a.info))
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
