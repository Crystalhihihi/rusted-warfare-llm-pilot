# -*- coding: utf-8 -*-
"""军种三层小队层离线验证：分类/归队不越界/上限/拆分/注销/反射拉最近队/任务书动态校验。"""
import sys
import threading
import time
from collections import deque

sys.path.insert(0, "bridge")
import multiagent
import reflex as reflex_mod


class Link9:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


class ExMock9:
    fac_avail = {}
    factory_actions = {"landFactory": {"u_c_tank"}, "airFactory": {"u_c_tank"}}
    price = {"u_c_tank": 400}
    produce_focus = {}

    def _fac_bad(self, fid, action, now):
        return False

    def _bad(self, action, now):
        return False


def U(uid, t, x=0, y=0):
    return {"id": uid, "t": t, "x": x, "y": y, "hp": 100, "maxHp": 100}


MOVE = {"tank": "LAND", "heavyTank": "LAND", "hoverTank": "HOVER", "gunShip": "AIR",
        "interceptor": "AIR", "gunBoat": "WATER", "missileShip": "WATER",
        "mechBunker": "OVER_CLIFF", "modularSpider_antinuke": "OVER_CLIFF_WATER"}


def mk_st(squads=None):
    return type("S", (), {"squads": squads or {}, "move_map": MOVE,
                          "lock": threading.Lock()})()


def mk_ctx(caps=None):
    multiagent._CAPS_OVERRIDE.clear()
    if caps:
        multiagent._CAPS_OVERRIDE.update(caps)
    return {"cfg": {}}


def main():
    # 1. 军种分类
    assert multiagent._branch_of("tank", MOVE) == "land"
    assert multiagent._branch_of("hoverTank", MOVE) == "land", "悬浮归陆军"
    assert multiagent._branch_of("gunShip", MOVE) == "air"
    assert multiagent._branch_of("gunBoat", MOVE) == "navy"
    assert multiagent._branch_of("mechBunker", MOVE) == "land"
    assert multiagent._branch_of("???", MOVE) == "land", "未知按陆"

    # 2. 归队不越界：空军进 A 队、陆军进 L 队、海军进 N 队
    st, ctx = mk_st(), mk_ctx()
    snap = {"own": [U(1, "tank"), U(2, "gunShip"), U(3, "gunBoat"), U(4, "heavyTank")]}
    multiagent.squad_assign(ctx, st, snap)
    branches = {sid: sorted(sq["members"]) for sid, sq in st.squads.items()}
    assert branches.get("L1") == [1, 4] and branches.get("A1") == [2] and branches.get("N1") == [3], \
        f"军种分队错: {branches}"

    # 3. 上限：cap land=1 → 新陆兵不开 L2，塞 L1；无空位军种等下拍
    multiagent._CAPS_OVERRIDE.update({"land": 1, "air": 1, "navy": 1})
    multiagent.squad_assign(ctx, st, {"own": [U(1, "tank"), U(4, "heavyTank"), U(5, "tank"),
                                              U(2, "gunShip"), U(3, "gunBoat")]})
    assert "L2" not in st.squads and st.squads["L1"]["members"] == {1, 4, 5}, st.squads
    multiagent._CAPS_OVERRIDE.clear()

    # 4. 拆分：单队 10 人 → 拆成两队 5/5
    st2, ctx2 = mk_st(), mk_ctx()
    multiagent.squad_assign(ctx2, st2, {"own": [U(i, "tank") for i in range(1, 11)]})
    multiagent.squad_assign(ctx2, st2, {"own": [U(i, "tank") for i in range(1, 11)]})
    sizes = sorted(len(sq["members"]) for sq in st2.squads.values())
    assert sizes == [5, 5], f"10 人单队应拆 5/5: {sizes}"
    assert {frozenset(sq["members"]) for sq in st2.squads.values()} == \
        {frozenset({1, 2, 3, 4, 5}), frozenset({6, 7, 8, 9, 10})}

    # 5. 注销：成员全灭且无任务 → 小队删除
    st2.squads["L1"]["mission"] = None
    st2.squads["L2"]["mission"] = {"任务": "hold"}
    multiagent.squad_assign(ctx2, st2, {"own": []})
    assert "L1" not in st2.squads and "L2" in st2.squads, \
        f"无任务空队注销、有任务空队保留: {list(st2.squads)}"

    # 6. 反射回防拉最近队（守家队已取消）
    sent = []
    rx = reflex_mod.Reflex(lambda k, t: None)
    own = [U(1, "tank", 1200, 0), U(2, "tank", 1250, 0), U(3, "tank", 3000, 0)]
    enemies = [U(91, "tank", 100, 0), U(92, "tank", 120, 0), U(93, "tank", 140, 0)]
    snap6 = {"own": own, "enemy": enemies}
    view = {"L1": {"members": {1, 2}}, "L2": {"members": {3}}}
    rx._tick_squads(snap6, (0, 0), enemies, lambda c: sent.append(c) or True, view, time.time())
    pulls = [c for c in sent if c.get("cmd") == "move"]
    assert len(pulls) == 1 and pulls[0]["units"] in ("1,2", "2,1"), f"应只拉最近的 L1: {sent}"

    # 7. 任务书动态校验
    st7 = mk_st({"L1": {"mission": None, "members": {1}},
                 "A2": {"mission": None, "members": {2}}})
    _cmd, desc = multiagent._translate_missions(mk_ctx(), st7,
                                                {"squads": [{"id": "L1", "任务": "attack", "目标": [500, 500]},
                                                            {"id": "S9", "任务": "attack"}]}, {})
    assert "L1:attack" in desc and "S9" in desc and "不存在" in desc, desc
    assert st7.squads["L1"]["mission"]["目标"] == [500.0, 500.0]
    assert st7.squads["L1"]["mission_ts"] > 0, "mission_ts 应落盘（看门狗宽限起算）"

    # 8. 上限覆盖层优先于 config
    multiagent._CAPS_OVERRIDE.update({"land": 9})
    assert multiagent._current_caps({"cfg": {"squad_cap_land": 2}})["land"] == 9
    assert multiagent._current_caps({"cfg": {"squad_cap_land": 2}})["air"] == 2
    multiagent._CAPS_OVERRIDE.clear()

    # 9. 军种中层（B 步）
    # 9a. 厂型→军种
    assert multiagent._fac_branch("airFactory") == "air"
    assert multiagent._fac_branch("seaFactory") == "navy"
    assert multiagent._fac_branch("landFactory") == "land"
    # 9b. _roles 含三军种中层、无旧 combat
    cfg9 = {"model": "m", "combat_thinker_model": "f", "combat_squad_model": "s",
            "build_model": "b", "commander_model": "c", "build_chat_enabled": False,
            "timeout_sec": 10, "commander_interval_sec": 50, "build_interval_sec": 10,
            "combat_interval_sec": 6}
    names = [r["name"] for r in multiagent._roles({"cfg": cfg9})]
    assert "land" in names and "air" in names and "navy" in names and "combat" not in names, names
    # 9c. 军种蓄势触发 + 无兵零召唤
    st9 = mk_st()
    st9.plan_ts = 0.0
    st9.plan_params = {"姿态": "attack"}
    st9.pending = {"land": deque(), "air": deque(), "navy": deque()}
    st9.mission_stale = False
    g9 = {"plan_ts_fired": 0.0, "last_fire": 0.0}
    own_air = [U(i, "gunShip") for i in range(1, 9)]
    r = multiagent._branch_event(mk_ctx(), st9, {"own": own_air}, [], 1000.0, dict(g9), "air")
    assert r and "空军兵力已备" in r, f"空 8 兵+姿态 attack 应触发: {r}"
    assert multiagent._branch_event(mk_ctx(), st9, {"own": []}, [], 1000.0, dict(g9), "navy") is None, \
        "无兵无队零召唤"
    r = multiagent._branch_event(mk_ctx(), st9, {"own": own_air}, [], 1000.0,
                                 {"plan_ts_fired": 0.0, "last_fire": 1000.0}, "navy")
    assert r is None, "海军无兵不心跳"
    # 9d. 跨军种任务书拒绝
    st9d = mk_st({"L1": {"mission": None, "members": {1}},
                  "A1": {"mission": None, "members": {2}}})
    _c, desc = multiagent._translate_branch_missions(
        mk_ctx(), st9d, {"squads": [{"id": "L1", "任务": "attack", "目标": [500, 500]},
                                    {"id": "A1", "任务": "attack"}]}, {}, "land")
    assert "L1:attack" in desc and "拒绝跨军种" in desc and "A1" in desc, desc
    assert st9d.squads["A1"]["mission"] is None, "跨军种不落盘"
    # 9e. produce 军种分流：L 队 produce 只在陆厂 train
    logs9 = []
    link9 = Link9()
    ex9 = ExMock9()
    st9e = mk_st({"L1": {"mission": {"任务": "produce", "动作": "u_c_tank", "数量": 4, "_done": False},
                         "members": set()}})
    ctx9 = {"exec_holder": {"ex": ex9}, "known_actions": {"u_c_tank"}, "link": link9,
            "log_activity": lambda k, t: logs9.append(t)}
    facs9 = [{"id": 1, "t": "landFactory", "bld": True, "q": 0, "x": 1, "y": 1},
             {"id": 2, "t": "airFactory", "bld": True, "q": 0, "x": 2, "y": 2}]
    multiagent._produce_tick(ctx9, st9e, {"own": facs9, "team": {"funds": 5000}})
    assert [m["factory"] for m in link9.sent if m.get("cmd") == "train"] == [1], \
        f"陆队只在陆厂产: {link9.sent}"

    print("PASS test_squads_branch：分类/归队/上限/拆分/注销/反射最近队/动态校验/覆盖层/军种中层 全部符合预期")


if __name__ == "__main__":
    main()
