# -*- coding: utf-8 -*-
"""建造时长标定器：直连 41234 快照流，跟踪施工中单位（prog 0-1）的进度斜率，
按类型聚合等效建造时长（游戏秒）。沙盒+变速环境下使用（PROGRESS §五.1③）。

时长 = (tick_last - tick_first)/60 / (prog_last - prog_first)
采样要求：prog 跨度 ≥0.15 且 tick 跨度 ≥10（防单点噪声）。
用法: python tools/calibrate_durations.py [--seconds 360] [--out tools/durations_calibrated.json]
"""
import argparse
import json
import socket
import statistics
import sys
import time

HOST, PORT = "127.0.0.1", 41234
TICK_PER_SEC = 60
MIN_PROG_SPAN = 0.15
MIN_TICK_SPAN = 10


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=360)
    ap.add_argument("--out", default="tools/durations_calibrated.json")
    args = ap.parse_args()

    s = socket.socket()
    s.settimeout(10)
    try:
        s.connect((HOST, PORT))
    except OSError as e:
        print(f"连不上 {HOST}:{PORT}: {e}")
        return 1
    s.settimeout(5)

    tracking = {}   # id -> {"t":type, "first":(tick,prog), "last":(tick,prog)}
    finished = {}   # type -> [duration_s, ...]
    t0 = time.time()
    buf = b""
    while time.time() - t0 < args.seconds:
        try:
            chunk = s.recv(1 << 20)
        except socket.timeout:
            continue
        if not chunk:
            print("连接断开")
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                m = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if m.get("type") != "snapshot":
                continue
            tick = m.get("tick")
            if tick is None:
                continue
            live = {}
            gms = m.get("timeMs")
            for u in m.get("own", []):
                uid = u.get("id")
                ut = u.get("t")
                if uid is None or not ut:
                    continue
                prog = u.get("prog")
                if prog is not None and 0.0 < prog < 1.0:
                    live[uid] = (tick, prog, ut, gms)
            # 更新跟踪（timeMs 为一等公民：游戏时 ground truth；tick 仅为参照）
            for uid, (tick2, prog2, ut, gms2) in live.items():
                if uid in tracking:
                    tracking[uid]["last"] = (gms2, prog2)
                    tracking[uid]["tick_last"] = tick2
                else:
                    tracking[uid] = {"t": ut, "first": (gms2, prog2),
                                     "last": (gms2, prog2), "tick_last": tick2}
            # 消失（完工或被摧毁）→ 结算
            for uid in list(tracking):
                if uid in live:
                    continue
                tr = tracking.pop(uid)
                (g1, p1), (g2, p2) = tr["first"], tr["last"]
                dp = p2 - p1
                dg = g2 - g1  # 游戏毫秒
                if dp >= MIN_PROG_SPAN and dg >= 200:
                    dur = (dg / 1000.0) / dp
                    finished.setdefault(tr["t"], []).append(round(dur, 1))
    s.close()

    summary = {}
    for ut, ds in sorted(finished.items()):
        summary[ut] = {"n": len(ds), "median": round(statistics.median(ds), 1),
                       "min": min(ds), "max": max(ds)}
    result = {"wall_seconds": round(time.time() - t0, 1),
              "samples": {k: len(v) for k, v in finished.items()},
              "durations": summary, "all": finished}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
