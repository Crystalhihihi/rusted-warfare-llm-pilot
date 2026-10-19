# -*- coding: utf-8 -*-
"""经济模拟器（tools/sim_economy.py）离线验证：回本口径/分段扣款停摆/
建造者并行/升级增量回本/breakpoint/meta 标记。零网络零 LLM。"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import sim_economy as se

# 固定时长表（隔绝 price/10 启发式，单测机制）
T = copy.deepcopy(se.BUILD)
for oid, dur in (("b_extractorT1", 60), ("extractorT2", 140),
                 ("b_fabricatorT1", 100), ("b_landFactory", 80)):
    T[oid]["duration"] = dur


def paybacks(res):
    return [(e["order"], e["eta"]) for e in res["events"] if e["type"] == "payback"]


# 1. 单矿回本口径：eta − 完工 = price/income = 700/8 = 87.5s
r1 = se.simulate(["b_extractorT1"], funds0=10000, builders=1, table=T, horizon=600)
c1 = [e for e in r1["events"] if e["type"] == "completed"][0]
pb1 = [e for e in r1["events"] if e["type"] == "payback"][0]
assert c1["t"] == 60.0, c1
assert abs((pb1["eta"] - c1["t"]) - 87.5) < 1e-6, pb1
print("§1 OK  回本口径 87.5s=700/8")

# 2. 分段扣款停摆→恢复：扣款速率(700/30≈23.3/s) > 收入(18/s)，
#    资金不足时进度停、资金不归负、收入到账后断续续建直至完工
T2 = copy.deepcopy(T)
T2["b_extractorT1"]["duration"] = 30  # rate 23.3/s > CC 18/s → 必然反复停摆
r2 = se.simulate(["b_extractorT1"], funds0=0, builders=1, table=T2, horizon=900)
stalls = [e for e in r2["events"] if e["type"] == "stall"]
done2 = [e for e in r2["events"] if e["type"] == "completed"]
assert stalls, "应出现停摆事件"
assert all(f >= -1e-6 for _, f, _, _ in r2["curve"]), r2["curve"]
assert done2 and done2[0]["t"] > 30, "停摆后应晚于 30s 完工"
print("§2 OK  停摆/恢复/资金非负")

# 3. 建造者并行：三单 60s，3 建造者同刻完工 vs 1 建造者 180s
r3a = se.simulate(["b_extractorT1"] * 3, funds0=10000, builders=3, table=T, horizon=600)
r3b = se.simulate(["b_extractorT1"] * 3, funds0=10000, builders=1, table=T, horizon=600)
t3a = max(e["t"] for e in r3a["events"] if e["type"] == "completed")
t3b = max(e["t"] for e in r3b["events"] if e["type"] == "completed")
assert t3a == 60.0 and t3b == 180.0, (t3a, t3b)
print("§3 OK  并行墙钟 60 vs 180")

# 4. 升级增量回本：T1→T2 完工后 income=18+12=30，eta=完工+1400/4=+350
r4 = se.simulate(["b_extractorT1", "extractorT2"], funds0=10000, builders=2,
                 table=T, horizon=900)
inc4 = r4["final"]["income"]
assert inc4 == 30.0, r4["final"]
pb4 = [e for e in r4["events"] if e["type"] == "payback" and e["order"] == "extractorT2"][0]
c4 = [e for e in r4["events"] if e["type"] == "completed" and e["order"] == "extractorT2"][0]
assert abs((pb4["eta"] - c4["t"]) - 350.0) < 1e-6, (pb4, c4)
print("§4 OK  升级增量回本 350s=1400/(12-8)")

# 5. breakpoint：income≥20 在 fabricator 完工时刻触发
r5 = se.simulate(["b_fabricatorT1"], funds0=10000, builders=1, table=T,
                 horizon=600, breakpoints=(20,))
bps = [e for e in r5["events"] if e["type"] == "breakpoint"]
assert bps and bps[0]["t"] == 100.0, bps
print("§5 OK  breakpoint@100s")

# 6. meta 粗版标记 + 时长表 + 未知 id 进 skipped
r6 = se.simulate(["b_extractorT1", "u_ghost"], funds0=10000, builders=1,
                 table=T, horizon=600)
assert r6["meta"]["coarse"] is True
assert "b_extractorT1" in r6["meta"]["durations"]
assert r6["meta"]["skipped"] == ["u_ghost"], r6["meta"]
print("§6 OK  meta/skipped")

print("ALL PASS (test_sim_economy：回本口径/停摆恢复/并行/升级回本/breakpoint/meta)")
