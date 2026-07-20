#!/usr/bin/env python3
"""rw-bridge: connects to rw-agent (TCP localhost:41234, NDJSON) and prints
a human-readable summary of each snapshot. M1 verification tool; the LLM loop
plugs in later."""
import json
import socket
import sys
import time
from collections import Counter

try:  # Windows GBK 控制台兼容
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST, PORT = "127.0.0.1", 41234


def summarize(snap: dict) -> str:
    own = Counter(u["t"] for u in snap.get("own", []))
    ally = Counter(u["t"] for u in snap.get("allies", []))
    enemy = Counter(u["t"] for u in snap.get("enemy", []))
    team = snap.get("team") or {}
    fmt = lambda c: ",".join(f"{k}x{v}" for k, v in sorted(c.items())) or "-"
    return (
        f"tick={snap.get('tick'):<7} funds={team.get('funds', 0):>9.0f} "
        f"own[{snap.get('ownCount', 0)}]: {fmt(own)} | "
        f"ally[{snap.get('allyCount', 0)}]: {fmt(ally)} | "
        f"enemyVisible[{snap.get('enemyCount', 0)}]: {fmt(enemy)}"
    )


def main() -> None:
    while True:
        try:
            print(f"connecting to {HOST}:{PORT} ...")
            sock = socket.create_connection((HOST, PORT), timeout=5)
            sock.settimeout(None)
            print("connected")
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("agent closed")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        print("!! bad json:", line[:120])
                        continue
                    if msg.get("type") == "snapshot":
                        print(summarize(msg))
                    elif msg.get("type") == "map":
                        rows = msg.get("rows", [])
                        with open("map.txt", "w", encoding="utf-8") as f:
                            f.write("\n".join(rows))
                        print(
                            f"MAP {msg.get('tilesW')}x{msg.get('tilesH')} "
                            f"tile={msg.get('tileW'):.0f} res={len(msg.get('resources', []))} -> map.txt"
                        )
                    else:
                        print("msg:", msg)
        except (ConnectionError, socket.timeout, OSError) as e:
            print(f"disconnected ({e}), retry in 2s")
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
