#!/usr/bin/env python3
"""rw-agent probe: connect 41234, read snapshots, send one-shot verbs, wait receipts.

usage:
  python tools/probe.py units                       # dump own units (id/type/pos/act/hp/prog)
  python tools/probe.py send '<json>'               # send raw json line, print cmdResult
  python tools/probe.py train <action> <unit_id>    # shorthand send
  python tools/probe.py build <action> <x> <y>
  python tools/probe.py waitborn <type> [timeout_s] # watch snapshots until a NEW unit of type appears
  python tools/probe.py watch [seconds]             # stream snapshot summary lines (tick + own count)

Run with PYTHONIOENCODING=utf-8 on Windows.
"""
import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 41234


class Probe:
    def __init__(self, timeout=10):
        self.s = socket.create_connection((HOST, PORT), timeout=timeout)
        self.f = self.s.makefile("rw", encoding="utf-8", newline="\n")

    def _next(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("agent closed connection")
        line = line.strip()
        if not line:
            return {}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"_raw": line[:200]}

    def send(self, msg):
        self.s.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def wait_type(self, want, timeout=10):
        """Read messages until one with type==want arrives; return it."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = self._next()
            if m.get("type") == want:
                return m
        raise TimeoutError(f"no {want} within {timeout}s")

    def snapshot(self, timeout=15):
        """Return the next full snapshot (agent pushes one on connect)."""
        return self.wait_type("snapshot", timeout)

    def cmd_result(self, msg, timeout=15):
        self.send(msg)
        return self.wait_type("cmdResult", timeout)


def fmt_units(snap, type_filter=None):
    rows = []
    for u in snap.get("own", []):
        t = u.get("t", "")
        if type_filter and type_filter not in t:
            continue
        rows.append(
            "#{id} {t} @({x},{y}) act={act} hp={hp}/{mhp}{extra}".format(
                id=u.get("id"), t=t, x=u.get("x"), y=u.get("y"),
                act=u.get("act", "?"), hp=u.get("hp"), mhp=u.get("mhp"),
                extra=(" prog=%s" % u.get("prog")) if u.get("prog") is not None else "",
            )
        )
    return rows


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]
    p = Probe()

    if cmd == "units":
        snap = p.snapshot()
        print("tick=%s own=%d" % (snap.get("tick"), len(snap.get("own", []))))
        tf = args[1] if len(args) > 1 else None
        for r in fmt_units(snap, tf):
            print(r)
    elif cmd == "send":
        msg = json.loads(args[1])
        r = p.cmd_result(msg)
        print(json.dumps(r, ensure_ascii=False))
    elif cmd == "train":
        r = p.cmd_result({"cmd": "train", "action": args[1], "factory": int(args[2])})
        print(json.dumps(r, ensure_ascii=False))
    elif cmd == "build":
        r = p.cmd_result({"cmd": "build", "action": args[1],
                          "x": float(args[2]), "y": float(args[3])})
        print(json.dumps(r, ensure_ascii=False))
    elif cmd == "waitborn":
        want = args[1]
        timeout = float(args[2]) if len(args) > 2 else 60
        snap = p.snapshot()
        before = {u.get("id") for u in snap.get("own", []) if want in u.get("t", "")}
        print("baseline: %d x %s" % (len(before), want))
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = p._next()
            if m.get("type") != "snapshot":
                continue
            for u in m.get("own", []):
                if want in u.get("t", "") and u.get("id") not in before:
                    print("BORN #%s %s @(%s,%s) after %.1fs" % (
                        u.get("id"), u.get("t"), u.get("x"), u.get("y"),
                        time.time() - t0))
                    return 0
        print("TIMEOUT: no new %s within %.0fs" % (want, timeout))
        return 2
    elif cmd == "watch":
        secs = float(args[1]) if len(args) > 1 else 10
        t0 = time.time()
        while time.time() - t0 < secs:
            m = p._next()
            if m.get("type") == "snapshot":
                print("tick=%s own=%d funds=%s" % (
                    m.get("tick"), len(m.get("own", [])), m.get("funds")))
        return 0
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
