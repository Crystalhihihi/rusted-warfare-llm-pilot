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
        f"45s 未动应代打: {link.sent}"
    assert st4.squads["S1"]["last_act_ts"] == 1000.0
    assert any("看门狗" in t for t in logs)
    # 宽限内不动
    link.sent.clear()
    multiagent._squad_watchdog(ctx4, st4, snap, 1010.0)
    assert not link.sent, "last_act 刚更新，宽限内不动"
    # 危险让位反射
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

    print("PASS test_combat_phase3：触发器/看门狗/去重放宽 全部符合预期")


if __name__ == "__main__":
    main()
