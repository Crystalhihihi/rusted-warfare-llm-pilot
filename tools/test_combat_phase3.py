# -*- coding: utf-8 -*-
"""阶段 3 离线验证：进攻触发器（蓄势/兵力已备）、服从看门狗（代执行/让位反射/宽限）、
去重器放宽（无敌才拦/有敌放行/过窗放行）。"""
import sys
import threading
import time
from collections import deque

sys.path.insert(0, "bridge")
import connmap
import executor as exec_mod
import multiagent


class Link:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


def mk_ctx(ex=None):
    return {"exec_holder": {"ex": ex}, "log_activity": lambda k, t: None,
            "clamp": lambda v, a, b: max(a, min(b, v))}


def mk_st(**kw):
    base = {"pending": {"combat": deque(), "build": deque()}, "plan_ts": 0.0,
            "plan_params": {}, "lock": threading.Lock(), "defeated": False,
            "squads": {sid: {"mission": None, "members": set()} for sid in ("S1", "S2", "S3", "S4")},
            "world_w": 4000.0, "world_h": 4000.0, "danger_map": None}
    base.update(kw)
    return type("S", (), base)()


class ExMock:
    def __init__(self, comp):
        self.army_goal = {"composition": comp, "rally": None}


def tanks(n, start=1):
    return [{"id": i, "t": "c_tank", "hp": 100, "maxHp": 100, "x": 100, "y": 100}
            for i in range(start, start + n)]


def main():
    # 1. 进攻触发器：蓄势七成即召唤
    ex = ExMock({"u_c_tank": 10})
    ctx, st = mk_ctx(ex), mk_st()
    g = {"plan_ts_fired": 0.0, "last_fire": 0.0}
    r = multiagent._combat_event(ctx, st, {"own": tanks(8)}, [], 1000.0, g)
    assert r and "蓄势已成" in r, f"8/10 应触发蓄势: {r}"
    # 四成兵力 + 无姿态 → 不触发
    g2 = {"plan_ts_fired": 0.0, "last_fire": 1000.0}
    assert multiagent._combat_event(ctx, st, {"own": tanks(4)}, [], 1000.0, g2) is None
    # 兵力≥8 + 姿态 attack（构成未设）→ 触发"该动了"
    st2 = mk_st(plan_params={"姿态": "attack"})
    ctx2 = mk_ctx(ExMock(None))
    g3 = {"plan_ts_fired": 0.0, "last_fire": 0.0}
    r = multiagent._combat_event(ctx2, st2, {"own": tanks(8)}, [], 1000.0, g3)
    assert r and "该动了" in r, f"姿态进攻+8 兵应触发: {r}"
    # 姿态 defend → 不触发
    st3 = mk_st(plan_params={"姿态": "defend"})
    assert multiagent._combat_event(ctx2, st3, {"own": tanks(8)}, [], 1000.0,
                                    {"plan_ts_fired": 0.0, "last_fire": 1000.0}) is None

    # 2. 服从看门狗
    class DM:
        def __init__(self, d):
            self.d = d

        def danger(self, x, y, snap=None):
            return self.d

    logs = []
    link = Link()
    st4 = mk_st(danger_map=DM(False))
    st4.squads["S1"] = {"mission": {"任务": "attack", "目标": [2000, 2000]}, "members": {1, 2, 3},
                        "last_act_ts": 0.0, "mission_ts": 0.0}
    ctx4 = {"link": link, "log_activity": lambda k, t: logs.append(t)}
    snap = {"own": tanks(3)}
    multiagent._squad_watchdog(ctx4, st4, snap, 1000.0)
    assert any(m.get("cmd") == "attackmove" and m.get("x") == 2000.0 for m in link.sent), \
        f"超时未动应代打: {link.sent}"
    assert st4.squads["S1"]["last_act_ts"] == 1000.0
    assert any("看门狗" in t for t in logs)
    # 宽限内不动
    link.sent.clear()
    multiagent._squad_watchdog(ctx4, st4, snap, 1010.0)
    assert not link.sent, "last_act 刚更新，宽限内不动"
    # 46s 未动仍在 90s 宽限内（曾 45s 即代打——与 LLM 往返撞车，抢在决策返回前开火）
    multiagent._squad_watchdog(ctx4, st4, snap, 1046.0)
    assert not link.sent, "90s 宽限内（46s）不应代打"
    # 91s → 代打
    multiagent._squad_watchdog(ctx4, st4, snap, 1091.0)
    assert any(m.get("cmd") == "attackmove" for m in link.sent), "90s 宽限过后应代打"
    assert st4.squads["S1"]["last_act_ts"] == 1091.0
    # config 覆盖：squad_watchdog_sec=45 → 46s 即代打（阈值可调）
    link.sent.clear()
    ctx4["cfg"] = {"squad_watchdog_sec": 45}
    multiagent._squad_watchdog(ctx4, st4, snap, 1137.0)
    assert any(m.get("cmd") == "attackmove" for m in link.sent), "config 覆盖 45s 应生效"
    del ctx4["cfg"]
    # 危险让位反射
    link.sent.clear()
    st4.danger_map = DM(True)
    multiagent._squad_watchdog(ctx4, st4, snap, 2000.0)
    assert not link.sent, "质心危险应让位反射层"
    # defend 任务不代打
    st4.danger_map = DM(False)
    st4.squads["S1"]["mission"] = {"任务": "defend"}
    st4.squads["S1"]["last_act_ts"] = 0.0
    st4.squads["S1"]["mission_ts"] = 0.0
    multiagent._squad_watchdog(ctx4, st4, snap, 3000.0)
    assert not link.sent, "defend 不代打"
    # 阵亡不动作
    st4.squads["S1"]["mission"] = {"任务": "attack", "目标": [2000, 2000]}
    st4.defeated = True
    multiagent._squad_watchdog(ctx4, st4, snap, 4000.0)
    assert not link.sent, "阵亡待机不动作"

    # 3. 去重器放宽
    now = time.time()
    st5 = mk_st()
    st5.squads["S1"] = {"mission": None, "members": {1, 2}, "last_atk": (1000.0, 1000.0, now - 10)}
    ctx5 = mk_ctx()
    g5 = {}
    # 3a. 25s 内同点+无敌 → 拦
    cmd, desc = multiagent._translate_squad(ctx5, st5, "S1", "attack",
                                            {"x": 1010, "y": 1010}, {"enemy": []}, g5)
    assert cmd is None and "到点无敌" in desc, f"无敌重复点应拦: {desc}"
    # 3b. 25s 内同点+有敌 → 放行
    foe = {"id": 9, "t": "tank", "x": 1100, "y": 1000}
    cmd, desc = multiagent._translate_squad(ctx5, st5, "S1", "attack",
                                            {"x": 1010, "y": 1010}, {"enemy": [foe]}, g5)
    assert cmd and cmd["cmd"] == "attackmove", f"有敌应放行: {desc}"
    # 3c. 过 25s 窗 → 放行
    st5.squads["S1"]["last_atk"] = (1000.0, 1000.0, now - 30)
    cmd, desc = multiagent._translate_squad(ctx5, st5, "S1", "attack",
                                            {"x": 1010, "y": 1010}, {"enemy": []}, g5)
    assert cmd and cmd["cmd"] == "attackmove", f"过窗应放行: {desc}"

    # 4. 清场完成→属主军种 pending 上报+召唤（2026-07-26 问题2①）
    st6 = mk_st(pending={"commander": deque(), "build": deque(), "combat": deque(),
                         "land": deque(), "air": deque(), "navy": deque()},
                mission_stale=False, enemy_spawns=[], move_map={})
    st6.squads = {"L1": {"mission": {"任务": "attack", "目标": [2000, 2000]}, "members": {1, 2}}}
    ctx6 = mk_ctx()
    snap6 = {"own": [{"id": 1, "t": "c_tank", "x": 2010, "y": 2000},
                     {"id": 2, "t": "c_tank", "x": 1990, "y": 2010}], "enemy": []}
    multiagent._mission_clear_tick(ctx6, st6, snap6)
    assert st6.squads["L1"]["mission"] is None and st6.mission_stale, "清场应完成任务并置失效信号"
    assert len(st6.pending["land"]) == 1 and "清场" in st6.pending["land"][0], \
        f"完成上报必须落到属主军种 pending: {list(st6.pending['land'])}"
    # 任务完成事件触发军种召唤（_branch_event 最先查本军种 pending）
    r6 = multiagent._branch_event(ctx6, st6, snap6, [], time.time(),
                                  {"plan_ts_fired": 0.0, "last_fire": time.time()}, "land")
    assert r6 == "待读消息", f"属主军种应被完成事件召唤: {r6}"
    st6.mission_stale = False  # 旧全局信号仍会漏给别军种（legacy 保留项），此断言只验 pending 必达属主
    r6b = multiagent._branch_event(ctx6, st6, snap6, [], time.time(),
                                   {"plan_ts_fired": 0.0, "last_fire": time.time()}, "air")
    assert r6b is None and not st6.pending["air"], "空军无兵无队无消息不应被误召唤"
    # harass 同款完成
    st6.squads["L2"] = {"mission": {"任务": "harass", "目标": [2000, 2000]}, "members": {1, 2}}
    multiagent._mission_clear_tick(ctx6, st6, snap6)
    assert st6.squads["L2"]["mission"] is None, "harass 清场同样完成"
    # 目标点有敌 → 不完成
    st6.squads["L3"] = {"mission": {"任务": "attack", "目标": [2000, 2000]}, "members": {1, 2}}
    multiagent._mission_clear_tick(ctx6, st6, dict(snap6, enemy=[{"id": 9, "t": "tank",
                                                                 "x": 2100, "y": 2000}]))
    assert st6.squads["L3"]["mission"] is not None, "目标点有敌不许完成"

    # 4b. 实控前沿推进≥400px→总指挥战报，10min 冷却（2026-07-26 问题2②）
    class ExFA:
        def __init__(self):
            self.pt = (1000.0, 1000.0, 40.0)

        def frontier_anchor(self, cc, axis, now):
            return self.pt

    ex6 = ExFA()
    ctx6b = mk_ctx(ex6)
    st6b = mk_st(pending={"commander": deque()}, enemy_spawns=[(3000, 1000)],
                 frontier_proj=None, frontier_report_ts=0.0)
    snap6b = {"own": [{"id": 1, "t": "commandCenter", "bld": True, "x": 0, "y": 1000}], "enemy": []}
    multiagent._frontier_report_tick(ctx6b, st6b, snap6b)
    assert not st6b.pending["commander"], "首见只建档不战报"
    ex6.pt = (1500.0, 1000.0, 40.0)  # 沿敌向(+x)推进 500px
    multiagent._frontier_report_tick(ctx6b, st6b, snap6b)
    assert len(st6b.pending["commander"]) == 1 and "推进" in st6b.pending["commander"][0], \
        f"推进≥400px 应呈战报: {list(st6b.pending['commander'])}"
    ex6.pt = (2000.0, 1000.0, 40.0)  # 又推进 500px 但 10min 冷却内
    multiagent._frontier_report_tick(ctx6b, st6b, snap6b)
    assert len(st6b.pending["commander"]) == 1, "10min 冷却内不重复呈报"

    # 5. attackmove 目标连通域预检（②）：陆队隔水拦/换点；同域照常；空军不受限；海队判水域
    # 图：70x50 格，x∈[600,800) 为贯通水带（左右两块陆域隔断）
    rows5 = ["." * 30 + "~" * 10 + "." * 30 for _ in range(50)]
    comp5, _n5 = connmap.build_components(rows5)
    map5 = {"tileW": 20, "tileH": 20, "tilesW": 70, "tilesH": 50, "rows": rows5}
    logs5 = []

    def ctx5b():
        return {"exec_holder": {"ex": None}, "clamp": lambda v, a, b: max(a, min(b, v)),
                "log_activity": lambda k, t: logs5.append(t)}

    def st5b(squads, **kw):
        base = {"squads": squads, "world_w": 1400.0, "world_h": 1000.0, "map_msg": map5,
                "conn_comp": comp5, "conn_wcomp": exec_mod.water_components(rows5),
                "defeated": False, "mission_stale": False,
                "pending": {"land": deque(), "air": deque(), "navy": deque()}}
        base.update(kw)
        return type("S", (), base)()

    U5 = lambda i, x, y: {"id": i, "t": "c_tank", "hp": 100, "maxHp": 100, "x": x, "y": y}
    # 5a. 小队指令：陆队（质心 300,500）打对岸 (1200,500) → 换可达近点（近岸格心 590,510）
    stA = st5b({"L1": {"mission": None, "members": {1, 2}}})
    cmd, desc = multiagent._translate_squad(ctx5b(), stA, "L1", "attack",
                                            {"x": 1200, "y": 500}, {"own": [U5(1, 300, 500), U5(2, 300, 500)], "enemy": []}, {})
    assert cmd and cmd["cmd"] == "attackmove" and (cmd["x"], cmd["y"]) == (590.0, 490.0), \
        f"隔水目标应换可达近点: {cmd} {desc}"
    assert any("换可达近点" in t for t in logs5), f"换点应记活动流: {logs5}"
    # 5b. 同 comp 目标照常
    logs5.clear()
    cmd, desc = multiagent._translate_squad(ctx5b(), stA, "L1", "attack",
                                            {"x": 400, "y": 500}, {"own": [U5(1, 300, 500), U5(2, 300, 500)], "enemy": []}, {})
    assert cmd and (cmd["x"], cmd["y"]) == (400.0, 500.0), f"同域目标不许动: {cmd}"
    assert not logs5, "同域不记活动流"
    # 5c. 已在最近岸（590,510）仍打对岸 → 换不了：拦截+回执劝换目标（不进游戏）
    stA2 = st5b({"L1": {"mission": None, "members": {1, 2}}})
    cmd, desc = multiagent._translate_squad(ctx5b(), stA2, "L1", "attack",
                                            {"x": 1200, "y": 500}, {"own": [U5(1, 590, 490), U5(2, 590, 490)], "enemy": []}, {})
    assert cmd is None and "不可达" in desc and "换目标" in desc, f"换不了应拦截回执: {cmd} {desc}"
    # 5d. 空军不受限（同坐标直接过）
    stA3 = st5b({"A1": {"mission": None, "members": {3}}})
    cmd, desc = multiagent._translate_squad(ctx5b(), stA3, "A1", "attack",
                                            {"x": 1200, "y": 500}, {"own": [U5(3, 300, 500)], "enemy": []}, {})
    assert cmd and (cmd["x"], cmd["y"]) == (1200.0, 500.0), f"空军恒真: {cmd}"
    # 5e. 看门狗：不可达换近点代打；换不了任务作废+军种 pending 通报
    class DM5:
        def danger(self, x, y, snap=None):
            return False
    link5 = Link()
    ctxW = {"link": link5, "log_activity": lambda k, t: logs5.append(t)}
    stW = st5b({"L1": {"mission": {"任务": "attack", "目标": [1200.0, 500.0]},
                       "members": {1, 2}, "last_act_ts": 0.0, "mission_ts": 0.0}},
               danger_map=DM5())
    multiagent._squad_watchdog(ctxW, stW, {"own": [U5(1, 300, 500), U5(2, 300, 500)]}, 1000.0)
    assert any(m.get("cmd") == "attackmove" and (m["x"], m["y"]) == (590.0, 490.0)
               for m in link5.sent), f"看门狗应代打可达近点: {link5.sent}"
    assert stW.squads["L1"]["mission"] is not None, "换点代打不许作废任务"
    link5.sent.clear()
    stW2 = st5b({"L1": {"mission": {"任务": "attack", "目标": [1200.0, 500.0]},
                        "members": {1, 2}, "last_act_ts": 0.0, "mission_ts": 0.0}},
                danger_map=DM5())
    multiagent._squad_watchdog(ctxW, stW2, {"own": [U5(1, 590, 490), U5(2, 590, 490)]}, 1000.0)
    assert not link5.sent, "换不了不许代打进游戏"
    assert stW2.squads["L1"]["mission"] is None and stW2.mission_stale, "换不了任务应作废待改派"
    assert len(stW2.pending["land"]) == 1 and "不可达" in stW2.pending["land"][0], \
        f"作废须通报军种中层: {list(stW2.pending['land'])}"
    # 5f. 任务书目标校验（军种中层派单路径）：不可达换近点落盘；换不了该项拒绝
    stM = st5b({"L1": {"mission": None, "members": {1, 2}},
                "A1": {"mission": None, "members": {3}}})
    snapM = {"own": [U5(1, 300, 500), U5(2, 300, 500), U5(3, 300, 500)]}
    _c, desc = multiagent._translate_missions(
        ctx5b(), stM, {"squads": [{"id": "L1", "任务": "attack", "目标": [1200, 500]},
                                  {"id": "A1", "任务": "attack", "目标": [1200, 500]}]}, {}, snapM)
    assert stM.squads["L1"]["mission"]["目标"] == [590.0, 490.0], f"任务书目标应换近点: {stM.squads['L1']['mission']}"
    assert "换可达近点" in desc, f"desc 应告知换点: {desc}"
    assert stM.squads["A1"]["mission"]["目标"] == [1200.0, 500.0], "空军任务书目标不动"
    stM2 = st5b({"L1": {"mission": None, "members": {1, 2}}})
    _c, desc = multiagent._translate_missions(
        ctx5b(), stM2, {"squads": [{"id": "L1", "任务": "attack", "目标": [1200, 500]}]}, {},
        {"own": [U5(1, 590, 490), U5(2, 590, 490)]})
    assert stM2.squads["L1"]["mission"] is None and "不可达" in desc, f"换不了应拒绝并回执: {desc}"
    # 5g. 海队判水域 comp：两湖隔断，N1 在湖1 打湖2 → 换湖1 近岸点；同湖目标照常
    rows7 = [list("." * 70) for _ in range(50)]
    for gy in range(10, 20):
        for gx in range(10, 20):
            rows7[gy][gx] = "~"  # 湖1
    for gy in range(30, 40):
        for gx in range(30, 40):
            rows7[gy][gx] = "~"  # 湖2
    rows7 = ["".join(r) for r in rows7]
    map7 = {"tileW": 20, "tileH": 20, "tilesW": 70, "tilesH": 50, "rows": rows7}
    stN = st5b({"N1": {"mission": None, "members": {5}}})
    stN.map_msg = map7
    stN.conn_wcomp = exec_mod.water_components(rows7)
    boat = {"id": 5, "t": "gunBoat", "hp": 100, "maxHp": 100, "x": 300, "y": 300}  # 湖1 内
    cmd, desc = multiagent._translate_squad(ctx5b(), stN, "N1", "attack",
                                            {"x": 700, "y": 700}, {"own": [boat], "enemy": []}, {})
    assert cmd and (cmd["x"], cmd["y"]) == (390.0, 390.0), f"跨湖目标应换本湖近岸点: {cmd} {desc}"
    cmd, desc = multiagent._translate_squad(ctx5b(), stN, "N1", "attack",
                                            {"x": 250, "y": 250}, {"own": [boat], "enemy": []}, {})
    assert cmd and (cmd["x"], cmd["y"]) == (250.0, 250.0), f"同湖目标照常: {cmd}"

    print("PASS test_combat_phase3：触发器/看门狗/去重放宽/清场上报/前沿战报/目标连通域预检 全部符合预期")


if __name__ == "__main__":
    main()
