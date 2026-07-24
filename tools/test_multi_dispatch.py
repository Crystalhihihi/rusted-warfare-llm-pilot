# -*- coding: utf-8 -*-
"""多建造者并行工单实机测试：一条批量指令指挥全部 builder 分头干活。
任务编组：双池资源 / 工厂区 / 防线弧 / 防空排 / 维修湾+扩厂。
用法: python tools/test_multi_dispatch.py [观察秒数]
"""
import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 41234
WATCH_S = int(sys.argv[1]) if len(sys.argv) > 1 else 300
CLR = 45          # 落点与现有单位的最小间距
EXTRACTOR = "b_extractorT1"
FACTORY = "b_landFactory"
TURRET = "b_c_turret_t1"
AA = "b_c_antiAirTurret"
REPAIR = "b_repairbay"


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
    snap = mapm = None
    for line in recv_lines(sock, 8):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "snapshot":
            snap = m
        elif m.get("type") == "map":
            mapm = m
    if not snap or not snap.get("own") or not mapm:
        print("FATAL: 快照/地图不全")
        return 1

    team = snap["team"]
    print(f"[state] funds={team['funds']} income={team['income']} builders="
          f"{sum(1 for u in snap['own'] if u['t'] == 'builder')}", flush=True)
    occupied = [(u["x"], u["y"]) for u in snap["own"]]
    pools = [(r["x"], r["y"]) for r in mapm["resources"]]
    cc = next(u for u in snap["own"] if u["t"] == "commandCenter")
    home = (cc["x"], cc["y"])
    builders = sorted((u for u in snap["own"] if u["t"] == "builder"),
                      key=lambda u: u["id"])

    def is_clear(x, y, extra):
        return (all(abs(px - x) > CLR or abs(py - y) > CLR for px, py in occupied)
                and all(abs(px - x) > CLR or abs(py - y) > CLR for px, py in extra))

    def find_strip(action, n, pitch, anchor, extra, axis="x"):
        """以 anchor 为期望中心找一条 n×pitch 的全空直排。"""
        for rad in range(0, 720, 40):
            for sign in (1, -1):
                for perp in range(-360, 361, 40):
                    ox = anchor[0] + (rad * sign if axis == "x" else perp)
                    oy = anchor[1] + (perp if axis == "x" else rad * sign)
                    cand = [(ox + i * pitch, oy) if axis == "x"
                            else (ox, oy + i * pitch) for i in range(n)]
                    if all(is_clear(x, y, extra) for x, y in cand):
                        return [(action, x, y) for x, y in cand]
        return None

    # ---- 任务编组（落点全部预计算，组间不撞点）----
    taken = []
    groups = []  # (名称, [(action,x,y),...])

    # 1) 资源：两个空池各一座 extractor（池点即落点，不需净空）
    ext_spots = [p for p in pools
                 if all(abs(u["x"] - p[0]) > 20 or abs(u["y"] - p[1]) > 20
                        for u in snap["own"] if u["t"].startswith("extractor"))]
    ext_spots.sort(key=lambda p: abs(p[0] - home[0]) + abs(p[1] - home[1]))
    res_tasks = []
    for p in ext_spots[:2]:
        res_tasks.append([(EXTRACTOR, p[0], p[1])])
    print(f"[plan] 资源池: {[(g[0][1], g[0][2]) for g in res_tasks]}", flush=True)

    # 2) 工厂区：3 座 landFactory，100px 节距，锚定现有厂区南侧
    fac_anchor = (home[0] - 150, home[1] + 320)
    fac = find_strip(FACTORY, 3, 100, fac_anchor, taken)
    taken += [(x, y) for _, x, y in fac]

    # 3) 防线：北弧 5 塔（40px）+ 西排 4 塔（40px）
    north = find_strip(TURRET, 5, 40, (home[0], home[1] - 380), taken)
    taken += [(x, y) for _, x, y in north]
    west = find_strip(TURRET, 4, 40, (home[0] - 320, home[1] - 60), taken, axis="y")
    taken += [(x, y) for _, x, y in west]

    # 4) 防空排：北弧后排 3 座（压后排不占弧尖）
    aa = find_strip(AA, 3, 40, (north[2][1], north[2][2] + 70), taken)
    taken += [(x, y) for _, x, y in aa]

    # 5) 后勤：维修湾 + 第 4 工厂
    misc = find_strip(REPAIR, 1, 100, (home[0] + 200, home[1] + 260), taken)
    taken += [(x, y) for _, x, y in misc]
    misc += find_strip(FACTORY, 1, 100, (fac[0][1] - 100, fac[0][2]), taken)

    groups = ([("资源A", res_tasks[0]), ("资源B", res_tasks[1])] if len(res_tasks) == 2 else
              [("资源", t) for t in res_tasks])
    groups += [("工厂区", fac), ("北防线", north), ("西防线", west),
               ("防空排", aa), ("后勤", misc)]

    # ---- 分配：每组任务取离首落点最近的空闲 builder ----
    def dist(u, p):
        return abs(u["x"] - p[0]) + abs(u["y"] - p[1])
    assign = []
    pool = list(builders)
    for name, tasks in groups:
        p = (tasks[0][1], tasks[0][2])
        u = min(pool, key=lambda w: dist(w, p))
        pool.remove(u)
        assign.append((name, u, tasks))
    cost = 0
    price = {EXTRACTOR: 700, FACTORY: 700, TURRET: 500, AA: 600, REPAIR: 1000}
    for name, u, tasks in assign:
        items = ";".join(f"{a}@{x},{y}" for a, x, y in tasks)
        cost += sum(price.get(a, 0) for a, _, _ in tasks)
        cmd = {"cmd": "buildq", "builder": u["id"], "items": items}
        sock.sendall((json.dumps(cmd) + "\n").encode())
        print(f"[send] {name}: builder#{u['id']} x{len(tasks)} → {items}", flush=True)
    print(f"[plan] 共 {len(assign)} 组，预计花费 ${cost}（现有 {team['funds']}）", flush=True)

    # ---- 观察：回执跳过 + 各组开工推进 ----
    expect = {}   # builder_id -> [(action,x,y)]
    for name, u, tasks in assign:
        expect[u["id"]] = list(tasks)
    done_pts = set()
    known_ids = {u["id"] for u in snap["own"]}
    t0 = time.time()
    last = -1
    total = sum(len(t) for _, _, t in assign)
    for line in recv_lines(sock, WATCH_S):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        el = int(time.time() - t0)
        if m.get("type") != "snapshot":
            print(f"[{el:3d}s] reply: {m}", flush=True)
            continue
        for u in m["own"]:
            if u["id"] in known_ids:
                continue
            for name, bld, tasks in assign:
                for a, x, y in tasks:
                    key = (a, x, y)
                    if key in done_pts:
                        continue
                    tol = 25 if a == EXTRACTOR else 55
                    if abs(u["x"] - x) <= tol and abs(u["y"] - y) <= tol:
                        done_pts.add(key)
                        print(f"[{el:3d}s] ★ {name} 开工: {u['t']}#{u['id']}@({x},{y}) "
                              f"进度={len(done_pts)}/{total}", flush=True)
        if el // 30 > last:
            last = el // 30
            busy = sum(1 for name, bld, tasks in assign
                       for w in m["own"] if w["id"] == bld["id"] and w.get("act") != "idle")
            print(f"[{el:3d}s] 在干={busy}/{len(assign)} 完成={len(done_pts)}/{total} "
                  f"funds={m['team']['funds']}", flush=True)
        if len(done_pts) == total:
            print(f"[ok] {total} 个落点全部开工，多建造者并行工单成立", flush=True)
            return 0
    print(f"[timeout] {WATCH_S}s 完成 {len(done_pts)}/{total}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
