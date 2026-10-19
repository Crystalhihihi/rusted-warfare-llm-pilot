# -*- coding: utf-8 -*-
"""经济前向模拟器（BOSS 式数值模拟，AIIDE'11 范式）：给定建造/升级序列，
前向推演资金/收入曲线。零依赖零网络，供 LLM 层当"信息"消费（decisions.md §十三.9）。

机制按反编译实证（agent-0 调查）：
- 资金按施工进度分段扣（每秒 price/duration），付不起则进度停摆、资金不归负
  （建造 y.java:2313-2339 粒度 0.1%，工厂 k.java:267-300 粒度 1%）
- 建造者并行：每建造者同时只接一单，订单 FIFO 领取
- 升级单须等基建筑完工后才可领取（requires）

数据口径（docs/decisions.md §十二.4，已交叉验证）：
- 收入 /s：extractor T1/T2/T3 = 8/12/20，超频 30；fabricator T1/T2/T3 = 2/7/14；CC = 18
- 升级价 = 目标型 full 价 − 当前型 full 价（pedia-dump.json）：T2 +1400、T3 +4000、
  超频 +8000、强化 +4000；回本账核对：1400/4=350s ✓、4000/8=500s ✓、8000/10=800s ✓、
  制造仪 2200/2=1100s ✓（与 §十二.4 全中）

时长表（2026-10-19 沙盒 10x/16x 变速局标定，timeMs 为 ground truth；
原始数据 tools/durations_calibrated.json；未标定项保留启发式并在 meta 注明）：
- b_landFactory 16.6s / b_fabricatorT1 16.7s（实测定）/ b_c_turret_t1 20.8s（与 7 月 1x 实测
  "单塔施工 20s" 锚点吻合）/ b_c_antiAirTurret 20.8s / experimentalLandFactory ~96s(1 样本)
- 采集器未采到样本（池饱和）：同价 700 类比厂 16.6s，meta 标未标定
- 升级单未采到样本（升级不吃 prog 字段）：保留启发式
"""
import copy

# 收入基数（§十二.4 用户口径，待游戏内复核项已注明）
INCOME = {"extractorT1": 8, "extractorT2": 12, "extractorT3": 20,
          "extractorT3_overclocked": 30, "fabricatorT1": 2, "fabricatorT2": 7,
          "fabricatorT3": 14, "commandCenter": 18}

# 建造表：price/duration_s（duration=None → 启发式 price/10；带 * 的=2026-10-19 实测定）
BUILD = {
    "b_extractorT1":   {"price": 700,  "duration": 16.6, "income": 8,   # 同价类比厂（未实测）
                        "src": "price-analogy"},
    "b_landFactory":   {"price": 700,  "duration": 16.6, "src": "calibrated"},
    "b_fabricatorT1":  {"price": 2200, "duration": 16.7, "income": 2, "src": "calibrated"},
    "b_c_turret_t1":   {"price": 500,  "duration": 20.8, "src": "calibrated"},
    "b_c_antiAirTurret": {"price": 800, "duration": 20.8, "src": "calibrated"},
    "b_laserDefence":  {"price": 2700, "duration": 8.4,  "src": "calibrated"},
    "b_antiNukeLauncherC": {"price": 7000, "duration": 8.4, "src": "calibrated(1样本)"},
    "b_experimentalLandFactory": {"price": 11000, "duration": 96.0, "src": "calibrated(1样本)"},
    # 升级单：price=差价，requires=基建筑，replaces=被升级资产收入键；时长未标定（升级不吃 prog）
    "extractorT2":     {"price": 1400, "duration": None, "upgrade": True,
                        "requires": "b_extractorT1", "income": 12, "src": "heuristic"},
    "extractorT3":     {"price": 4000, "duration": None, "upgrade": True,
                        "requires": "extractorT2", "income": 20, "src": "heuristic"},
    "extractorT3_overclocked": {"price": 8000, "duration": None, "upgrade": True,
                               "requires": "extractorT3", "income": 30, "src": "heuristic"},
    "fabricatorT2":    {"price": 4400, "duration": None, "upgrade": True,
                        "requires": "b_fabricatorT1", "income": 7, "src": "heuristic"},
    "fabricatorT3":    {"price": 7500, "duration": None, "upgrade": True,
                        "requires": "fabricatorT2", "income": 14, "src": "heuristic"},
}

HEURISTIC_RATE = 10.0  # 未标定项的粗版时长启发式：price/10 秒


def duration_of(order_id, table=None):
    e = (table or BUILD)[order_id]
    d = e.get("duration")
    return d if d is not None else e["price"] / HEURISTIC_RATE


def simulate(orders, funds0=5000, builders=3, cc_count=1, horizon=1800,
             dt=1.0, table=None, breakpoints=(), sample_every=30):
    """前向推演建造序列。

    orders: 建造/升级 id 列表（FIFO 领取；升级单等基建筑完工）
    返回 {"curve": [(t, funds, income, build_spend)], "events": [...],
         "final": {...}, "meta": {...}}；事件含 completed/stall/payback/breakpoint。
    payback 事件在完工时刻按 price/收入增量预测（信息用途，非精确值）。
    """
    table = table or BUILD
    funds = float(funds0)
    income = INCOME["commandCenter"] * cc_count
    queue = [o for o in orders if o in table]
    skipped = [o for o in orders if o not in table]
    slots = [None] * max(1, builders)      # 每建造者当前任务
    done = []                              # 已完成资产（income 计数用）
    events, curve = [], []
    stalled_flag = {}                      # 每任务只报一次停摆
    t = 0.0
    bp_done = set()
    spend_this_sample = 0.0

    def emit(kind, **kw):
        e = {"t": round(t, 1), "type": kind}
        e.update(kw)
        events.append(e)

    while t <= horizon + 1e-9:
        t += dt  # 先走时序：第 N 个施工 tick 发生在 t=N，完工时刻即 N 秒
        # 收入累积（完工建筑的产出）
        funds += income * dt
        # 领取：空闲建造者按序找第一个就绪任务
        for i, cur in enumerate(slots):
            if cur is not None:
                continue
            nxt = None
            for j, oid in enumerate(queue):
                req = table[oid].get("requires")
                if req and not any(d["id"] == req for d in done):
                    continue
                nxt = j
                break
            if nxt is not None:
                oid = queue.pop(nxt)
                slots[i] = {"oid": oid, "prog": 0.0,
                            "dur": duration_of(oid, table),
                            "price": table[oid]["price"]}
        # 施工：分段扣款，付不起停摆
        for cur in slots:
            if cur is None:
                continue
            rate = cur["price"] / cur["dur"] * dt
            if funds >= rate - 1e-9:
                funds -= rate
                spend_this_sample += rate
                cur["prog"] += dt / cur["dur"]
            else:
                key = id(cur)
                if key not in stalled_flag:
                    stalled_flag[key] = True
                    emit("stall", order=cur["oid"], need_per_s=round(rate / dt, 2))
        # 完工
        for i, cur in enumerate(slots):
            if cur and cur["prog"] >= 1.0 - 1e-9:
                e = table[cur["oid"]]
                inc = e.get("income")
                asset = {"id": cur["oid"], "income": inc or 0}
                if e.get("upgrade") and inc:
                    # 替换最旧一个基建筑的收入；回本按增量算（ upgrade 差价 / Δ收入）
                    base_inc = 0
                    for d in done:
                        if d["id"] == e["requires"]:
                            base_inc = d["income"]
                            income -= base_inc
                            d["income"] = 0
                            break
                    income += inc
                    delta = inc - base_inc
                else:
                    income += inc or 0
                    delta = inc or 0
                done.append(asset)
                emit("completed", order=cur["oid"], income=round(income, 1))
                if cur["price"] > 0 and delta:
                    emit("payback", order=cur["oid"],
                         eta=round(t + cur["price"] / delta, 1))
                slots[i] = None
        # 曲线采样与 breakpoint
        if int(t / dt + 0.5) % max(1, int(sample_every / dt)) == 0:
            curve.append((round(t, 1), round(funds, 1), round(income, 1),
                          round(spend_this_sample, 1)))
            spend_this_sample = 0.0
        for bp in breakpoints:
            if income >= bp and bp not in bp_done:
                bp_done.add(bp)
                emit("breakpoint", income=bp)
        # 全部完成且队列空 → 提前收工
        if not queue and all(c is None for c in slots):
            curve.append((round(t, 1), round(funds, 1), round(income, 1),
                          round(spend_this_sample, 1)))
            break

    return {
        "curve": curve,
        "events": events,
        "final": {"t": round(t, 1), "funds": round(funds, 1),
                  "income": round(income, 1), "assets": len(done)},
        "meta": {"coarse": any(e.get("duration") is None for e in table.values()),
                 "src": {oid: table[oid].get("src", "?") for oid in table},
                 "skipped": skipped,
                 "durations": {oid: duration_of(oid, table) for oid in table}},
    }


if __name__ == "__main__":
    import json
    demo = simulate(
        ["b_extractorT1", "b_extractorT1", "b_extractorT1",
         "extractorT2", "b_landFactory", "b_fabricatorT1"],
        funds0=5000, builders=3, horizon=900, breakpoints=(15, 30))
    print(json.dumps(demo["events"], ensure_ascii=False, indent=1))
    print("final:", demo["final"])
