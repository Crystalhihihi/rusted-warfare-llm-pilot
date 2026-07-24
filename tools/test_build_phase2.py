# -*- coding: utf-8 -*-
"""阶段 2 离线验证：构成驱动产兵补水（produce_focus 防撞车）、工单低水位催单、
工单在干保留、阵地单展开、produce_tick focus 维护。"""
import sys
import time
from collections import deque

sys.path.insert(0, "bridge")
import executor as exec_mod
import multiagent

TILE = 20
MAP = {"tilesW": 100, "tilesH": 100, "tileW": TILE, "tileH": TILE,
       "rows": ["." * 100 for _ in range(100)],
       "resources": [{"x": 1300, "y": 1000}, {"x": 1350, "y": 1050}], "path": ""}

PEDIA = {"units": [
    {"name": "commandCenter", "price": 3000, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 4000, "builds": ["u_builder"]},
    {"name": "builder", "price": 250, "tech": 1, "building": False, "builder": True,
     "move": "LAND", "hp": 170, "builds": ["b_extractorT1", "b_landFactory", "b_c_turret_t1",
                                           "b_c_antiAirTurret"]},
    {"name": "extractorT1", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 800, "builds": ["extractorT2_0"]},
    {"name": "landFactory", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 2000, "builds": ["u_c_tank", "u_missileTank", "u_heavyTank",
                                           "u_plasmaTank", "110"]},
    {"name": "c_tank", "price": 400, "tech": 1, "building": False, "builder": False,
     "move": "LAND", "hp": 210, "builds": []},
    {"name": "missileTank", "price": 700, "tech": 2, "building": False, "builder": False,
     "move": "LAND", "hp": 1100, "builds": []},
    {"name": "plasmaTank", "price": 900, "tech": 2, "building": False, "builder": False,
     "move": "LAND", "hp": 900, "builds": []},
    {"name": "heavyTank", "price": 800, "tech": 1, "building": False, "builder": False,
     "move": "LAND", "hp": 800, "builds": []},
    {"name": "c_turret_t1", "price": 600, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 900, "builds": []},
    {"name": "c_antiAirTurret", "price": 800, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 900, "builds": []},
]}


def make_ex():
    return exec_mod.Executor(MAP, PEDIA, lambda k, t: None)


def main():
    # 1. 构成驱动补水：缺口最大者优先；produce_focus 让路（两个 tech2 兵种避开 T2 过滤干扰）
    ex = make_ex()
    ex.set_army_goal(composition={"u_missileTank": 2, "u_plasmaTank": 4})
    fac = {"id": 3, "t": "landFactory"}
    assert ex._next_production(fac, []) == "u_plasmaTank", "缺口最大 plasmaTank×4 优先"
    ex.produce_focus = {"u_plasmaTank": 3}
    assert ex._next_production(fac, []) == "u_missileTank", "focus 让路后选 missileTank"
    ex.produce_focus = {}

    # 2. 工单在干保留：assigned 并回 + idx 重排
    ex = make_ex()
    ex.work_order = [{"idx": 0, "action": "b_extractorT1", "slot": "pool:0", "note": "旧单",
                      "status": "assigned", "builder": 7, "ts": 1.0, "x": 100, "y": 200},
                     {"idx": 1, "action": "b_landFactory", "slot": "factory:0", "note": "",
                      "status": "pending", "builder": None, "ts": 0.0, "x": None, "y": None}]
    ex.set_work_order([{"action": "b_c_turret_t1", "slot": "defense:0", "note": "新单"}])
    assert len(ex.work_order) == 2
    assert ex.work_order[0]["status"] == "assigned" and ex.work_order[0]["builder"] == 7
    assert ex.work_order[1]["note"] == "新单" and ex.work_order[1]["idx"] == 1

    # 3. 阵地单展开：mixed 6 座 → 4 对地 + 2 防空，坐标落在合法地形
    ex = make_ex()
    ex.set_work_order([{"action": "position", "kind": "defense_mixed",
                        "anchor": [1000, 1000], "toward": [2000, 1000], "count": 6}],
                      snap={"own": [], "allies": []})
    pts = [t for t in ex.work_order if t["status"] == "pending"]
    assert len(pts) == 6, f"应展开 6 座: {len(pts)}"
    aa = [t for t in pts if t["action"] == "b_c_antiAirTurret"]
    ground = [t for t in pts if t["action"] == "b_c_turret_t1"]
    assert len(aa) == 2 and len(ground) == 4, f"mixed 配比 4+2: {len(ground)}+{len(aa)}"
    for t in pts:
        assert isinstance(t["slot"], list) and len(t["slot"]) == 2
        gx, gy = int(t["slot"][0] // TILE), int(t["slot"][1] // TILE)
        assert MAP["rows"][gy][gx] == ".", "落点必须在可建地形"
        assert "阵地" in t["note"]
    # defense_ground / defense_aa 纯类型
    ex = make_ex()
    ex.set_work_order([{"action": "position", "kind": "defense_aa",
                        "anchor": [800, 800], "count": 3}], snap={"own": [], "allies": []})
    assert all(t["action"] == "b_c_antiAirTurret" for t in ex.work_order), "纯防空阵地"

    # 4. 低水位催单
    class ExMock:
        def __init__(self, wo):
            self.work_order = wo

    def ev(wo, g=None):
        ctx = {"exec_holder": {"ex": ExMock(wo)}}
        st = type("S", (), {"pending": {"build": deque()}, "plan_ts": 0.0,
                            "requests": deque(), "lock": __import__("threading").Lock()})()
        return multiagent._build_event(ctx, st, {}, [], time.time(), g or {})

    assert ev([]) == "无工单待规划"
    assert ev([{"status": "done"}]) == "工单执行完毕"
    r = ev([{"status": "pending"}, {"status": "assigned"}])
    assert r and "低水位" in r, f"剩2项应催单: {r}"
    assert ev([{"status": "pending"}] * 3, {"last_fire": time.time()}) is None, "3项不催"

    # 5. produce_tick focus 维护 + 完成检测
    class Link:
        def __init__(self):
            self.sent = []

        def send(self, m):
            self.sent.append(m)

    ex = make_ex()
    link = Link()
    ctx = {"exec_holder": {"ex": ex}, "known_actions": {"u_c_tank"}, "link": link,
           "log_activity": lambda k, t: None}
    st = type("S", (), {"squads": {"S2": {"mission": {"任务": "produce", "动作": "u_c_tank",
                                                     "数量": 4, "_done": False},
                                          "members": set()}},
                        "mission_stale": False})()
    fac_u = {"id": 5, "t": "landFactory", "bld": True, "q": 0, "x": 500, "y": 500, "hp": 1, "maxHp": 1}
    snap = {"own": [fac_u], "team": {"funds": 5000}}
    multiagent._produce_tick(ctx, st, snap)
    assert any(m.get("cmd") == "train" and m.get("action") == "u_c_tank" for m in link.sent), \
        f"produce 应发 train: {link.sent}"
    assert ex.produce_focus == {"u_c_tank": 4}, f"focus 应记剩余 4: {ex.produce_focus}"
    # 在役达标 → 完成 + focus 清空
    tanks = [{"id": i, "t": "c_tank", "hp": 1, "maxHp": 1} for i in range(10, 14)]
    multiagent._produce_tick(ctx, st, {"own": [fac_u] + tanks, "team": {"funds": 5000}})
    m = st.squads["S2"]["mission"]
    assert m["_done"] is True and st.mission_stale is True
    assert ex.produce_focus == {}, f"完成后 focus 应清空: {ex.produce_focus}"

    # 6. 槽位安全值择优：威胁覆盖首选位 → 沿排走向第一个 safety ≥ floor 的槽位
    MAP_BIG = {"tilesW": 300, "tilesH": 300, "tileW": TILE, "tileH": TILE,
               "rows": ["." * 300 for _ in range(300)],
               "resources": [{"x": 1300, "y": 1000}], "path": ""}
    ex = exec_mod.Executor(MAP_BIG, PEDIA, lambda k, t: None)
    cc = {"x": 2000, "y": 1000}
    s0 = ex._row_slot_safe(set(), "landFactory", cc, 0, 1.0, 0.0, 0)
    assert s0, "首选位应存在"
    assert ex.control.safety(s0[0], s0[1]) == 0.0, "无数据时安全值为 0（退化为顺排）"
    k0 = (int(s0[0] // 400), int(s0[1] // 400))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            ex.control.cells[(k0[0] + dx, k0[1] + dy)] = \
                {"army": [0, 0], "struct": [0, 0], "threat": 5.0}
    assert ex.control.safety(s0[0], s0[1]) < exec_mod.SAFE_PICK_FLOOR, "首选位已被压到地板下"
    s1 = ex._row_slot_safe(set(), "landFactory", cc, 0, 1.0, 0.0, 0)
    assert s1 and s1 != s0, f"威胁覆盖后应移位: {s0} -> {s1}"
    assert ex.control.safety(s1[0], s1[1]) >= exec_mod.SAFE_PICK_FLOOR, \
        f"新位应过地板: {ex.control.safety(s1[0], s1[1])}"

    # 7. 阵亡判定：own 全空 15s → 待机；有单位立刻复活；不误判
    logs = []
    ctx7 = {"log_activity": lambda k, t: logs.append((k, t))}
    st7 = type("S", (), {"defeated": False, "defeated_since": None})()
    alive = {"own": [{"id": 1}]}
    dead = {"own": []}
    multiagent._update_defeated(ctx7, st7, alive, 1000.0)
    assert not st7.defeated
    multiagent._update_defeated(ctx7, st7, dead, 1000.0)
    assert not st7.defeated, "刚开始全空不判"
    multiagent._update_defeated(ctx7, st7, dead, 1010.0)
    assert not st7.defeated, "10s 宽限内不判"
    multiagent._update_defeated(ctx7, st7, alive, 1012.0)
    assert st7.defeated_since is None, "来单位立即复位计时"
    multiagent._update_defeated(ctx7, st7, dead, 1012.0)
    multiagent._update_defeated(ctx7, st7, dead, 1028.0)
    assert st7.defeated, "全空 15s 应判阵亡"
    assert any("阵亡" in t for _, t in logs), "应记活动流"
    n_logs = len(logs)
    multiagent._update_defeated(ctx7, st7, dead, 1030.0)
    assert len(logs) == n_logs, "阵亡日志只记一次"

    # 8. 预算仲裁储备线 + 升级让路窗口
    ex = make_ex()
    assert ex._military_reserve([]) == 700, "空局=裸底线"
    ex.work_order = [
        {"idx": 0, "action": "b_extractorT1", "slot": "pool:0", "note": "", "status": "pending",
         "builder": None, "ts": 0.0, "x": None, "y": None},
        {"idx": 1, "action": "b_landFactory", "slot": "factory:0", "note": "", "status": "pending",
         "builder": None, "ts": 0.0, "x": None, "y": None},
    ]
    assert ex._military_reserve([]) == 700 + 700 + 700, "工单两项估价进线"
    fac_u2 = {"id": 9, "t": "landFactory", "bld": True, "hp": 1, "maxHp": 1, "x": 1, "y": 1}
    assert ex._military_reserve([fac_u2]) == 700 + 1400 + 4500, "兵力不设防(无构成)+T1厂→攒 4500"
    ex.fac_upgraded.add(9)
    ext_u = {"id": 10, "t": "extractorT1", "bld": True, "hp": 1, "maxHp": 1, "x": 1, "y": 1}
    assert ex._military_reserve([fac_u2, ext_u]) == 700 + 1400 + 3200, "厂已 T2→退而攒采集器 3200"
    # 构成缺口大 → 先造兵不攒升级
    ex2 = make_ex()
    ex2.set_army_goal(composition={"u_c_tank": 10})
    fac_u3 = {"id": 11, "t": "landFactory", "bld": True, "hp": 1, "maxHp": 1, "x": 1, "y": 1}
    assert ex2._military_reserve([fac_u3]) == 700, "缺口大→不攒升级"
    own8 = [fac_u3] + [{"id": i, "t": "c_tank", "hp": 1, "maxHp": 1, "x": 1, "y": 1}
                       for i in range(20, 28)]
    assert ex2._military_reserve(own8) == 700 + 4500, "兵力七成→开始攒 4500"
    # 升级让路窗口
    assert ex2._upgrade_pending(11, 1000.0) is False
    ex2.up_pending[11] = 990.0
    assert ex2._upgrade_pending(11, 1000.0) is True
    assert ex2._upgrade_pending(11, 1100.0) is False, "60s 超时解除"
    ex2.up_pending[11] = 990.0
    ex2.fac_upgraded.add(11)
    assert ex2._upgrade_pending(11, 1000.0) is False, "学成 T2 解除"

    # 9. 海厂锚点：岸边可达才锚定；对岸孤岛水域不锚；_in_bounds
    rows9 = []
    for y in range(40):
        row = ""
        for x in range(40):
            if y < 20:
                row += "."
            elif y < 24:
                row += "~"          # 近岸水带（可达）
            elif y < 30:
                row += "L"          # 岩浆隔离带
            else:
                row += "~"          # 岩浆对岸水域（不可达）
        rows9.append(row)
    MAP9 = {"tilesW": 40, "tilesH": 40, "tileW": TILE, "tileH": TILE,
            "rows": rows9, "resources": [], "path": ""}
    ex9 = exec_mod.Executor(MAP9, PEDIA, lambda k, t: None)
    cc9 = {"x": 400, "y": 200}
    wa = ex9.water_anchor(cc9)
    assert wa and wa[1] < 24 * TILE and wa[2] is True, f"应锚近岸水带且岸边可达: {wa}"
    # 全图只有对岸水 → 给悬浮专场（shore=False），不再返回 None（CE 能坐水里盖）
    rows10 = ["." * 40 for _ in range(20)] + ["L" * 40 for _ in range(6)] + ["~" * 40 for _ in range(14)]
    MAP10 = {"tilesW": 40, "tilesH": 40, "tileW": TILE, "tileH": TILE,
             "rows": rows10, "resources": [], "path": ""}
    ex10 = exec_mod.Executor(MAP10, PEDIA, lambda k, t: None)
    wa10 = ex10.water_anchor(cc9)
    assert wa10 is not None and wa10[2] is False, f"对岸水域应给悬浮专场: {wa10}"
    assert ex10._in_bounds(0, 0) and not ex10._in_bounds(-70, 120) and not ex10._in_bounds(99999, 10)

    print("PASS test_build_phase2：补水/防撞车/在干保留/低水位/阵地展开/focus/槽位择优/阵亡判定/预算仲裁/海厂锚点 全部符合预期")


if __name__ == "__main__":
    main()
