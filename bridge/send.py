#!/usr/bin/env python3
"""Send a one-shot command to rw-agent.
usage: python send.py move X Y | attackmove X Y | stop | attack UNIT_ID
       python send.py build s_extractorT1 X Y
       python send.py train s_tank FACTORY_ID
       python send.py rally X Y FACTORY_ID
       python send.py delegate 0,3,9"""
import json
import socket
import sys

HOST, PORT = "127.0.0.1", 41234


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    c = args[0]
    if c in ("move", "attackmove") and len(args) == 3:
        msg = {"cmd": c, "x": float(args[1]), "y": float(args[2])}
    elif c == "stop" and len(args) == 1:
        msg = {"cmd": "stop"}
    elif c == "attack" and len(args) == 2:
        msg = {"cmd": "attack", "id": int(args[1])}
    elif c == "build" and len(args) == 4:
        msg = {"cmd": "build", "action": args[1], "x": float(args[2]), "y": float(args[3])}
    elif c == "train" and len(args) == 3:
        msg = {"cmd": "train", "action": args[1], "factory": int(args[2])}
    elif c == "rally" and len(args) == 4:
        msg = {"cmd": "rally", "x": float(args[1]), "y": float(args[2]), "factory": int(args[3])}
    elif c == "delegate" and len(args) == 2:
        msg = {"cmd": "delegate", "groups": args[1]}
    else:
        print(__doc__)
        return 1
    with socket.create_connection((HOST, PORT), timeout=5) as s:
        s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    print("sent:", msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
