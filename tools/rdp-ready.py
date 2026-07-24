#!/usr/bin/env python3
"""Is the Windows guest actually up? (real RDP handshake, not a TCP connect)

Runs on the Linux host. Exit 0 = guest is serving RDP, i.e. the unattended
install has finished and Windows is running.

WHY THIS EXISTS: a plain TCP connect to 127.0.0.1:3389 proves NOTHING. Docker
publishes the port on the host, so docker-proxy accepts the connection even
while Windows is still downloading. During the first run a naive connect-based
check reported "guest ready" 30 seconds into a 5 GB ISO download. This sends a
real X.224 Connection Request and requires a TPKT response.

  python3 tools/rdp-ready.py && echo up
"""
import socket, sys

HOST, PORT = "127.0.0.1", 3389

# TPKT + X.224 Connection Request with an RDP negotiation request.
CR = bytes([0x03, 0x00, 0x00, 0x13, 0x0E, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00])


def probe(host=HOST, port=PORT, timeout=5):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(CR)
        data = s.recv(19)
        s.close()
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    if len(data) >= 2 and data[0] == 0x03 and data[1] == 0x00:
        return True, "RDP responded, %d bytes" % len(data)
    return False, "no TPKT, got %r" % data


if __name__ == "__main__":
    ok, msg = probe()
    print(("READY (%s)" if ok else "NOT_READY (%s)") % msg)
    sys.exit(0 if ok else 1)
