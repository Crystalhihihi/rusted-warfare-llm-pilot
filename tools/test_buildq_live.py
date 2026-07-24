# -*- coding: utf-8 -*-
"""buildq 原生拉条 实机验证（PROGRESS.md 未实测清单 #1）。
连 41234 → 找一个空闲 builder → 下发一条 buildq 两个炮塔落点 →
只观察不再发令：两落点先后开工即“连干”成立。
用法: python tools/test_buildq_live.py [观察秒数]
"""
import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 41234
ACTION = "b_c_turret_t1"  # $500 炮塔，任意地可放

# 模式：python tools/test_buildq_live.py            → 双落点连干（默认 150s 观察）
#       python tools/test_buildq_live.py dense [N] [pitch] [观察秒数]
#           → 密集建造：一条 N 个落点的直排长条（pitch 默认 40px=用户实战小建筑节距）
DENSE = len(sys.argv) > 1 and sys.argv[1] == "dense"
DENSE_N = int(sys.argv[2]) if DENSE and len(sys.argv) > 2 else 6
DENSE_PITCH = int(sys.argv[3]) if DENSE and len(sys.argv) > 3 else 40
WATCH_S = int(sys.argv[4]) if DENSE and len(sys.argv) > 4 else (int(sys.argv[1]) if not DENSE and len(sys.argv) > 1 else 150)


def recv_lines(sock, timeout_s):
    end = time.time() + timeout_s
    buf = b""
    while time.time() < end:
        sock.settimeout(max(0.1, end - time.time()))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line


def main():
    sock = socket.create_connection((HOST, PORT), timeout=10)
    snap = None
    for line in recv_lines(sock, 8):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "snapshot":
            snap = msg
    if not snap or not snap.get("own"):
        print("FATAL: 无快照/无己方单位（对局没在跑？）")
        return 1

    team = snap.get("team", {})
    print(f"[state] tick={snap['tick']} funds={team.get('funds')} "
          f"income={team.get('income')} expense={team.get('expense')}", flush=True)
    builders = [u for u in snap["own"] if u["t"] == "builder"]
    if not builders:
        print("FATAL: 没有 builder")
        return 1
    b = next((u for u in builders if u.get("act") == "idle"), builders[0])
    print(f"[builder] #{b['id']} @({b['x']},{b['y']}) act={b.get('act')}", flush=True)

    # 落点：builder 周围空地（与任何己方单位相距 60px 以上）
    def clear(x, y):
        return all(abs(u["x"] - x) > 60 or abs(u["y"] - y) > 60 for u in snap["own"])
    spots = []
    if DENSE:
        # 密集直排：找一条 N×pitch 的全空横/竖条（候选原点网格扫描）
        want = DENSE_N
        found = False
        for axis in ("x", "y"):
            for rad in range(230, 640, 40):
                if found:
                    break
                for sign in (1, -1):
                    for perp in range(-320, 330, 40):
                        ox = b["x"] + (rad * sign if axis == "x" else perp)
                        oy = b["y"] + (perp if axis == "x" else rad * sign)
                        cand = [(ox + i * DENSE_PITCH, oy) if axis == "x"
                                else (ox, oy + i * DENSE_PITCH) for i in range(want)]
                        if all(clear(x, y) for x, y in cand):
                            spots = cand
                            found = True
                            break
                    if found:
                        break
            if found:
                break
        print(f"[dense] {want} 连排 pitch={DENSE_PITCH} axis 起点={spots[0] if spots else None}",
              flush=True)
    else:
        want = 2
        for dx, dy in [(-290, -210), (-290, -110), (-350, -210), (-350, -110),
                       (-230, -260), (230, -210), (-230, 210)]:
            x, y = b["x"] + dx, b["y"] + dy
            if clear(x, y):
                spots.append((x, y))
            if len(spots) == want:
                break
    if len(spots) < want:
        print(f"FATAL: 找不到 {want} 个空地落点")
        return 1
    items = ";".join(f"{ACTION}@{x},{y}" for x, y in spots)
    known_ids = {u["id"] for u in snap["own"]}

    cmd = {"cmd": "buildq", "builder": b["id"], "items": items}
    sock.sendall((json.dumps(cmd) + "\n").encode())
    print(f"[send] buildq builder=#{b['id']} items={items}", flush=True)

    # 观察：回执 + 两落点开工时刻 + builder 行为轨迹
    seen = {}
    t0 = time.time()
    last_trace = -1
    for line in recv_lines(sock, WATCH_S):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        el = int(time.time() - t0)
        if msg.get("type") != "snapshot":
            print(f"[{el:3d}s] reply: {msg}", flush=True)
            continue
        tol = DENSE_PITCH // 2 if DENSE else 40
        for u in msg["own"]:
            if u["id"] in known_ids:
                continue
            for i, (x, y) in enumerate(spots):
                if i not in seen and abs(u["x"] - x) <= tol and abs(u["y"] - y) <= tol:
                    seen[i] = el
                    print(f"[{el:3d}s] ★ 落点{i + 1} 开工: {u['t']}#{u['id']} hp={u['hp']}",
                          flush=True)
        if el // 10 > last_trace:
            last_trace = el // 10
            ub = next((u for u in msg["own"] if u["id"] == b["id"]), None)
            if ub:
                print(f"[{el:3d}s] builder act={ub.get('act')} "
                      f"@({ub['x']},{ub['y']}) 开工={len(seen)}/{want} funds={msg['team']['funds']}",
                      flush=True)
        if len(seen) == want:
            gaps = [seen[i + 1] - seen[i] for i in range(want - 1)]
            print(f"[ok] {want} 个落点全部开工（间隔 {gaps}），连干成立", flush=True)
            return 0
    print(f"[timeout] {WATCH_S}s 内开工落点={len(seen)}/{want} —— 连干未成立", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
