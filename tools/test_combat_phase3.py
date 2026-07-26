# -*- coding: utf-8 -*-
"""阶段 3 离线验证：进攻触发器（蓄势/兵力已备）、服从看门狗（代执行/让位反射/宽限）、
去重器放宽（无敌才拦/有敌放行/过窗放行）。"""
import sys
import threading
import time
from collections import deque

sys.path.insert(0, "bridge")
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

    print("PASS test_combat_phase3：触发器/看门狗/去重放宽/清场上报/前沿战报 全部符合预期")


if __name__ == "__main__":
    main()
