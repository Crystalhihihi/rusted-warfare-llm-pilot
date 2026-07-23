# -*- coding: utf-8 -*-
"""对局度量器（纯数据通道，不碰鼠标）：连 agent 快照流，记录整局时间线与结果。

用法: python tools/match_metrics.py [--label 备注] [--timeout 3600]
产出: 打印摘要 + 追加 tools/metrics_history.jsonl（每局一行）
"""
import argparse
import json
import math
import socket
import time

HOST, PORT = "127.0.0.1", 41234
BUILDING_TYPES = ("commandCenter", "landFactory", "airFactory", "seaFactory", "mechFactory")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.settimeout(10)
    f = sock.makefile("rb")

    t0 = None
    first_ext = first_fac = first_turret = None
    funds_hist = []          # (t, funds)
    own_bld_hist = []        # (t, count)
    enemy_bld_hist = []
    army_peak = 0
    verdict = None
    last_own_bld = last_enemy_bld = None
    menu_streak = 0

    print("[metrics] 等待对局开始（快照流）...")
    while True:
        if t0 and time.time() - t0 > args.timeout:
            verdict = verdict or "timeout"
            break
        try:
            line = f.readline()
        except (socket.timeout, TimeoutError):
            continue
        if not line:
            print("[metrics] 连接断开（游戏关闭？）")
            verdict = verdict or "aborted"
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "snapshot":
            continue
        own = msg.get("own", [])
        if not own:
            menu_streak += 1
            if t0 and menu_streak > 6:
                verdict = verdict or "menu"
                break
            continue
        menu_streak = 0
        if t0 is None:
            t0 = time.time()
            print(f"[metrics] 对局开始 tick={msg.get('tick')}")
        el = time.time() - t0
        blds = [u for u in own if u.get("bld") and u.get("prog", 1) >= 1]
        enemy_blds = [u for u in msg.get("enemy", []) if u.get("bld")]
        army = [u for u in own if not u.get("bld")]
        funds = (msg.get("team") or {}).get("funds", 0)

        if first_ext is None and any(u["t"].startswith("extractor") for u in blds):
            first_ext = el
        if first_fac is None and any(u["t"] in BUILDING_TYPES[1:] for u in blds):
            first_fac = el
        if first_turret is None and any("turret" in u["t"].lower() for u in blds):
            first_turret = el
        army_peak = max(army_peak, len(army))
        if len(funds_hist) == 0 or el - funds_hist[-1][0] > 15:
            funds_hist.append((el, funds))
            own_bld_hist.append((el, len(blds)))
            enemy_bld_hist.append((el, len(enemy_blds)))
        last_own_bld, last_enemy_bld = len(blds), len(enemy_blds)

        # 胜负判定：任一方建筑清零（可见范围内）且持续 3 个快照
        if t0 and el > 120:
            if last_enemy_bld == 0 and last_own_bld > 0:
                verdict = "win"
            elif last_own_bld == 0:
                verdict = "loss"

    dur = time.time() - t0 if t0 else 0
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": args.label,
        "verdict": verdict,
        "duration_s": int(dur),
        "first_extractor_s": int(first_ext) if first_ext else None,
        "first_factory_s": int(first_fac) if first_fac else None,
        "first_turret_s": int(first_turret) if first_turret else None,
        "army_peak": army_peak,
        "funds_end": funds,
        "own_bld_end": last_own_bld,
        "enemy_bld_end": last_enemy_bld,
    }
    with open("tools/metrics_history.jsonl", "a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("[metrics] 结果:", json.dumps(rec, ensure_ascii=False))


main()
