# -*- coding: utf-8 -*-
"""阶段 2 离线验证：构成驱动产兵补水（produce_focus 防撞车）、工单低水位催单、
工单在干保留、阵地单展开、produce_tick focus 维护。"""
import sys
import time
from collections import deque

sys.path.insert(0, "bridge")
import executor as exec_mod
import fog as fog_mod
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
    # 密集阵几何：两两不撞（≥60px）；炮塔排沿敌向垂直线展开；防空压炮塔排后 ~120px
    import math as _m
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = _m.hypot(pts[i]["slot"][0] - pts[j]["slot"][0],
                         pts[i]["slot"][1] - pts[j]["slot"][1])
            assert d >= 60, f"阵型撞点 {pts[i]['slot']} vs {pts[j]['slot']} 距 {d:.0f}"
    ax_, ay_ = 1.0, 0.0  # anchor(1000,1000)→toward(2000,1000) 的敌向
    for t in aa:
        # 防空相对锚点的敌向投影应约为 -120（压后排）
        proj = (t["slot"][0] - 1000) * ax_ + (t["slot"][1] - 1000) * ay_
        assert abs(proj - (-120)) < 60, f"防空未压后排: 投影 {proj:.0f}"
    for t in ground:
        proj = (t["slot"][0] - 1000) * ax_ + (t["slot"][1] - 1000) * ay_
        assert abs(proj) < 60, f"炮塔未顶第一排: 投影 {proj:.0f}"
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

    # 6. 槽位安全值择优（地板 SAFE_PICK_FLOOR=10）：威胁覆盖首选位 → 沿排走向第一个过地板的槽位
    MAP_BIG = {"tilesW": 300, "tilesH": 300, "tileW": TILE, "tileH": TILE,
               "rows": ["." * 300 for _ in range(300)],
               "resources": [{"x": 1300, "y": 1000}], "path": ""}
    ex = exec_mod.Executor(MAP_BIG, PEDIA, lambda k, t: None)
    cc = {"x": 2000, "y": 1000}
    s0 = ex._row_slot_safe(set(), "landFactory", cc, 0, 1.0, 0.0, 0)
    assert s0, "首选位应存在"
    assert ex.control.safety(s0[0], s0[1]) == 0.0, "无数据时安全值为 0（全冷兜底退回首选，不空手）"
    k0 = (int(s0[0] // 400), int(s0[1] // 400))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            ex.control.cells[(k0[0] + dx, k0[1] + dy)] = \
                {"army": [0, 0], "struct": [0, 0], "threat": 5.0}
    assert ex.control.safety(s0[0], s0[1]) < exec_mod.SAFE_PICK_FLOOR, "首选位已被压到地板下"
    # 地板=10 下沿途空格 safety=0 也不过地板：给远排脱开威胁邻域的槽位放友军军力顶过地板
    # （safety 威胁取 3x3 邻域 max，邻格仍被 5.0 压制，须隔 ≥3 格）
    slot1 = k1 = None
    for n in range(1, 30):
        cand = ex._row_slot(set(), "landFactory", cc, n, 1.0, 0.0, 0)
        if cand is None:
            break
        ck = (int(cand[0] // 400), int(cand[1] // 400))
        if abs(ck[0] - k0[0]) > 2 or abs(ck[1] - k0[1]) > 2:
            slot1, k1 = cand, ck
            break
    assert slot1, "排内应找到脱开威胁邻域的槽位"
    ex.control.cells[k1] = {"army": [12.0, 0.0], "struct": [0, 0], "threat": 0.0}
    s1 = ex._row_slot_safe(set(), "landFactory", cc, 0, 1.0, 0.0, 0)
    assert s1 and s1 != s0, f"威胁覆盖后应移位: {s0} -> {s1}"
    assert ex.control.safety(s1[0], s1[1]) >= exec_mod.SAFE_PICK_FLOOR, \
        f"新位应过地板: {ex.control.safety(s1[0], s1[1])}"
    # 全热兜底：威胁铺满整排时退回已见最安全候选（不空手，由派发闸安全值地板终判）
    for gx in range(k0[0] - 2, k0[0] + 13):
        for gy in range(k0[1] - 1, k0[1] + 2):
            ex.control.cells[(gx, gy)] = {"army": [0, 0], "struct": [0, 0], "threat": 5.0}
    s2 = ex._row_slot_safe(set(), "landFactory", cc, 0, 1.0, 0.0, 0)
    assert s2 == s0, f"全热应退回已见最安全候选（不空手）: {s2}"

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
    # 2026-07-26 问题1 语义变更：无 LLM 构成=默认构成兜底（缺口全满）→先补兵不攒升级
    # （旧语义"无构成=兵力不设防直接攒 4500"正是平静期⑦断产→蓄势死锁的根，见 PROGRESS §二十一）
    assert ex._military_reserve([fac_u2]) == 700 + 1400, "默认构成缺口全满→不攒升级先补兵"
    ex.fac_upgraded.add(exec_mod.fac_key("landFactory", 1, 1))  # ① 位点键（fac_u2 id=9 @1,1）
    ext_u = {"id": 10, "t": "extractorT1", "bld": True, "hp": 1, "maxHp": 1, "x": 1, "y": 1}
    army8 = ([{"id": i, "t": "c_tank", "hp": 1, "maxHp": 1, "x": 1, "y": 1} for i in range(30, 38)]
             + [{"id": i, "t": "missileTank", "hp": 1, "maxHp": 1, "x": 1, "y": 1} for i in range(40, 42)])
    assert ex._military_reserve([fac_u2, ext_u] + army8) == 700 + 1400 + 3200, \
        "默认构成达标+厂已 T2→退而攒采集器 3200"
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
    ex2.fac_mem[11] = ("landFactory", 1.0, 1.0)  # ① 回执/窗口判定走 fac_mem 定位
    ex2.fac_upgraded.add(exec_mod.fac_key("landFactory", 1, 1))
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

    # 11. 隘口检测：开阔图无隘口；走廊图逮到隘口且宽度合理
    cc0 = {"x": 200, "y": 1000}
    ex11 = make_ex()
    assert ex11.chokes(cc0, (1.0, 0.0)) == [], "开阔图不应有隘口"
    rowsC = (["." * 100] * 45 + ["~" * 100] * 10 + ["." * 100] * 10
             + ["~" * 100] * 10 + ["." * 100] * 25)
    MAPC = {"tilesW": 100, "tilesH": 100, "tileW": TILE, "tileH": TILE,
            "rows": rowsC, "resources": [], "path": ""}
    exC = exec_mod.Executor(MAPC, PEDIA, lambda k, t: None)
    ch = exC.chokes({"x": 200, "y": 1200}, (1.0, 0.0))
    assert ch, "走廊图应检出隘口"
    cx, cy, w = ch[0]
    assert 1100 <= cy <= 1300 and w <= 400, f"隘口应在走廊带内且窄: ({cx},{cy}) 宽{w}"

    # 12. 实控前沿锚点：安全值≥30 格中沿敌向投影最远者
    ex12 = make_ex()
    ex12.control.cells = {
        (5, 3): {"army": [40.0, 0.0], "struct": [0.0, 0.0], "threat": 0.0},   # (2200,1400) proj 1200
        (3, 2): {"army": [50.0, 0.0], "struct": [0.0, 0.0], "threat": 0.0},   # (1400,1000) proj 400
        (2, 2): {"army": [99.0, 0.0], "struct": [0.0, 0.0], "threat": 0.0},   # (1000,1000) proj 0 家圈内排除
    }
    cc12 = {"x": 1000, "y": 1000}
    fa = ex12.frontier_anchor(cc12, (1.0, 0.0), time.time())
    assert fa and abs(fa[0] - 2200) < 1 and abs(fa[1] - 1400) < 1, f"应取投影最远的实控格: {fa}"
    ex12.control.cells = {(2, 2): {"army": [99.0, 0.0], "struct": [0.0, 0.0], "threat": 0.0}}
    assert ex12.frontier_anchor(cc12, (1.0, 0.0), time.time()) is None, "只家圈内实控=无前沿"

    # 13. 撞点统一占位：阵地展开坐标进占位图，defense:k 解析避让；自身豁免不误挪
    ex13 = make_ex()
    ex13.set_work_order([{"action": "position", "kind": "defense_mixed",
                          "anchor": [1000, 1000], "toward": [2000, 1000], "count": 4}],
                        snap={"own": [], "allies": []})
    fpts = [t["slot"] for t in ex13.work_order if isinstance(t.get("slot"), list)]
    assert len(fpts) == 4
    occ13 = ex13._occupancy({"own": [], "allies": []})
    for x, y in fpts:
        assert not ex13._cell_free(occ13, "c_turret_t1", int(x // TILE), int(y // TILE)), \
            f"展开坐标必须进占位图: ({x},{y})"
    # 自身豁免：排除本任务 wo_id 后，自己的点应恢复 free（不被当成"撞占位"）
    wo0 = ex13.work_order[0]["wo_id"]
    occ_self = ex13._occupancy({"own": [], "allies": []}, exclude_wo=wo0)
    assert ex13._cell_free(occ_self, "c_turret_t1",
                           int(fpts[0][0] // TILE), int(fpts[0][1] // TILE)), \
        "豁免本任务后自身坐标应恢复 free"

    # 14. 新池安稳召唤：新坐标进安全池集 → 召唤一次；同集/10 分钟内回锅/有采集工单在干 → 不重复
    class ExPools:
        def __init__(self, wo, pools):
            self.work_order = wo
            self.last_safe_pools = pools

    def evp(wo, pools, g):
        ctx = {"exec_holder": {"ex": ExPools(wo, pools)}}
        st = type("S", (), {"pending": {"build": deque()}, "plan_ts": 0.0,
                            "requests": deque(), "lock": __import__("threading").Lock()})()
        return multiagent._build_event(ctx, st, {}, [], time.time(), g)

    wo3 = [{"status": "pending", "action": "b_landFactory", "slot": "factory:0"},
           {"status": "pending", "action": "b_landFactory", "slot": "factory:1"},
           {"status": "pending", "action": "b_c_turret_t1", "slot": "defense:0"}]
    p_home = [{"x": 100, "y": 100}]
    p_new = [{"x": 100, "y": 100}, {"x": 500, "y": 500}]
    g14 = {"last_fire": time.time()}  # 压住心跳，单测新池链路
    assert evp(wo3, p_home, g14) is None, "首拍只建档不召唤"
    r = evp(wo3, p_new, g14)
    assert r and "新池安稳" in r and "(500,500)" in r, f"新池进安全集应召唤: {r}"
    assert evp(wo3, p_new, g14) is None, "同一池集不重复召唤"
    assert evp(wo3, p_home, g14) is None
    assert evp(wo3, p_new, g14) is None, "池丢了再回来 10 分钟内也不重复召唤"
    # 该池已有 pending 采集器工单在干 → 不召唤（防重复；坐标 slot 与 pool:k 两种写法都算）
    g15 = {"last_fire": time.time()}
    assert evp(wo3, p_home, g15) is None
    wo_ext = wo3 + [{"status": "pending", "action": "b_extractorT1", "slot": [500, 500]}]
    assert evp(wo_ext, p_new, g15) is None, "已有采集器坐标单盖住该池不应召唤"
    g16 = {"last_fire": time.time()}
    assert evp(wo3, p_home, g16) is None
    wo_pool = wo3 + [{"status": "pending", "action": "b_extractorT1", "slot": "pool:1"}]
    assert evp(wo_pool, p_new, g16) is None, "pool:k 在干工单盖住该池不应召唤"

    # 15. 默认构成兜底（2026-07-26 问题1：无 LLM 构成时补水层也要按默认构成产兵）
    #     根因：构成真空→reserve 恒挂升级款→⑦断产→兵力到不了蓄势门槛→死锁
    ex15 = make_ex()  # 无任何 set_army_goal（零 LLM 输入）
    assert ex15._default_composition() == {"u_c_tank": 8, "u_missileTank": 2}, \
        f"默认构成=陆T1主战×8+防空~20%: {ex15._default_composition()}"
    fac15 = {"id": 3, "t": "landFactory"}
    act15 = ex15._next_production(fac15, [])
    assert act15 == "u_missileTank", \
        f"无 LLM 输入也应按默认构成产兵（c_tank 被 T2 过滤，missileTank 缺口2）: {act15}"
    # LLM 构成优先级更高：一到即覆盖默认值
    ex15.set_army_goal(composition={"u_plasmaTank": 5})
    assert ex15._next_production(fac15, []) == "u_plasmaTank", "LLM 构成必须覆盖默认兜底"
    # 储备线：默认构成未达七成→不挂升级款（修复前 ready 恒真→reserve=700+4500 压死平静期生产）
    ex15b = make_ex()
    own15 = [{"id": 9, "t": "landFactory", "bld": True, "x": 0, "y": 0, "hp": 1, "maxHp": 1}]
    assert ex15b._military_reserve(own15) == exec_mod.FUNDS_RESERVE, \
        f"默认构成缺口大时储备线不许挂升级款: {ex15b._military_reserve(own15)}"

    # 16. 规则层主动侦察（2026-07-26 问题3：平静期+全黑→move 探路；冷却不重复；警报不派）
    def mk_scout_ex():
        ex = make_ex()
        ex.autopilot = False
        ex.routine = True
        ex.routine_defense = False
        fm = fog_mod.FogMap(MAP)
        fm.update({"fogGrid": {"e": True, "f": False, "g": False,
                               "rows": ["2x100"] * 100}})  # 全图全黑
        ex.control.fog = fm
        return ex

    def scout_snap(enemy=None):
        own = [{"id": 1, "t": "commandCenter", "bld": True, "q": 0,
                "x": 1500, "y": 1000, "hp": 1, "maxHp": 1},
               {"id": 5, "t": "c_tank", "x": 1500, "y": 1000, "hp": 1, "maxHp": 1}]
        return {"tick": 100, "own": own, "allies": [], "enemy": enemy or [],
                "team": {"id": 0, "funds": 0, "ai": False}, "delegated": [1, 5]}

    ex16 = mk_scout_ex()
    cmds16 = ex16.tick(scout_snap())
    mv = [c for c in cmds16 if c["cmd"] == "move"]
    assert len(mv) == 1 and mv[0]["units"] == "5" and not mv[0].get("action"), \
        f"平静期+全黑应发 1 条纯 move 侦察（非 attackmove）: {cmds16}"
    assert mv[0]["x"] < 1450, f"应朝敌向（front_dir=图心方向，-x）探: {mv[0]}"
    assert not [c for c in ex16.tick(scout_snap()) if c["cmd"] == "move"], \
        "45s 冷却内不得重复派侦察"
    ex16b = mk_scout_ex()
    snap_alert = scout_snap(enemy=[{"id": 9, "t": "tank", "x": 1800, "y": 1000}])
    assert not [c for c in ex16b.tick(snap_alert) if c["cmd"] == "move"], \
        "警报期（可见敌/danger）不得派侦察"

    # 17. 按厂实际科技档（2026-07-26 §二十二：max_tech 不再被 pedia T1/T2 合并条目恒推 2）
    # 17a. 回执学到 T1 表（可用列表无 T2 动作）→ T1 主战 c_tank 解锁可产
    #      （修复前：pedia max 恒 2，c_tank 被永久滤死=实局纯防空军队的根）
    #      ① 起：回执按 fac_mem 长记忆定位落 (类型,格化坐标) 位点键，先种映射
    ex17 = make_ex()
    ex17.fac_mem[3] = ("landFactory", 500.0, 500.0)
    ex17.on_cmd_results([{"ok": False, "msg": "工厂#3 没有此动作: u_plasmaTank；可用: "
                                              "u_builder,u_scout,u_c_tank,u_hoverTank,u_c_artillery"}])
    assert 3 not in ex17.fac_upgraded, "可用列表无 T2 不得进 fac_upgraded"
    act17 = ex17._next_production({"id": 3, "t": "landFactory"}, [])
    assert act17 == "u_c_tank", f"学习表=T1 档：默认构成 c_tank 主战必须可产: {act17}"
    # 17b. 升级单在途（回执 ok 不落地悬案）不得提前按 T2 产——仍是 c_tank
    ex17.up_pending[3] = time.time()
    act17b = ex17._next_production({"id": 3, "t": "landFactory"}, [])
    assert act17b == "u_c_tank", f"升级在途不提前按 T2 产: {act17b}"
    ex17.up_pending.clear()
    # 17c. 回执实证 T2（可用列表含 T2 动作）→ fac_upgraded 永久免探，T1 主战滤掉只产 T2
    ex17.on_cmd_results([{"ok": False, "msg": "工厂#3 没有此动作: u_heavyTank；可用: "
                                              "u_c_tank,u_missileTank,u_heavyTank"}])
    assert exec_mod.fac_key("landFactory", 500, 500) in ex17.fac_upgraded, \
        "可用列表含 T2 动作=实证已 T2，应进 fac_upgraded（① 位点键）"
    ex17.set_army_goal(composition={"u_c_tank": 8, "u_missileTank": 2})
    act17c = ex17._next_production({"id": 3, "t": "landFactory"}, [])
    assert act17c == "u_missileTank", f"实证 T2 后 T1 主战滤掉、只产 T2: {act17c}"

    # 18. 110 升级知识换代继承（①：厂 T1→T2 游戏换发新 id，升级知识按 (类型,格化坐标) 继承）
    # 18a. _fac_track 换代检测：旧 id 消失+同位(≤40px)同类型新厂 → 新位点直接标 upgraded
    ex18 = make_ex()
    fac_old = {"id": 100, "t": "landFactory", "bld": True, "x": 1500.0, "y": 740.0}
    ex18._fac_track([fac_old])
    ex18.fac_avail[exec_mod.fac_key("landFactory", 1500, 740)] = ({"u_c_tank"}, time.time())  # 旧 T1 档学习表
    fac_new = {"id": 200, "t": "landFactory", "bld": True, "x": 1510.0, "y": 745.0}
    ex18._fac_track([fac_new])
    k_new = exec_mod.fac_key("landFactory", 1510, 745)
    assert k_new in ex18.fac_upgraded, "换代新位点应直接标 T2（零学费）"
    assert not ex18.fac_avail, "换代后旧位学习表（T1 档）应作废等回执重学"
    assert ex18.fac_mem[100] == ("landFactory", 1500.0, 740.0), "旧 id 机型/位点入长记忆（④b 用）"
    # 旧厂未消失（另起炉灶的远处新厂）/旧厂消失但太远 → 都不许误判换代
    ex18b = make_ex()
    ex18b._fac_track([fac_old])
    far_new = {"id": 201, "t": "landFactory", "bld": True, "x": 800.0, "y": 740.0}
    ex18b._fac_track([fac_old, far_new])
    assert not ex18b.fac_upgraded, "旧厂还在+远处新厂不许误判换代"
    ex18b._fac_track([far_new])
    assert not ex18b.fac_upgraded, "旧 id 消失但新厂不同位不许误判换代"
    # 18b. fac_avail 120s 过期仍生效（新鲜 T1 表解锁 c_tank；过期回退 pedia 合并档滤 T1）
    ex18c = make_ex()
    ex18c.fac_mem[3] = ("landFactory", 500.0, 500.0)
    ex18c.on_cmd_results([{"ok": False, "msg": "工厂#3 没有此动作: u_plasmaTank；可用: "
                                               "u_builder,u_scout,u_c_tank,u_hoverTank,u_c_artillery"}])
    k18c = exec_mod.fac_key("landFactory", 500, 500)
    assert ex18c._next_production({"id": 3, "t": "landFactory"}, []) == "u_c_tank", "新鲜 T1 学习表解锁主战"
    av, ts = ex18c.fac_avail[k18c]
    ex18c.fac_avail[k18c] = (av, ts - 121)
    assert ex18c._next_production({"id": 3, "t": "landFactory"}, []) == "u_missileTank", \
        "120s 过期回退 pedia 型号表（T1 主战被合并档滤掉）"
    # 18c. 回执厂 id 无任何位置记忆 → 跳过学习不炸（无法归位宁缺，别再按 id 瞎学）
    ex18d = make_ex()
    ex18d.on_cmd_results([{"ok": False, "msg": "工厂#999 没有此动作: 110；可用: u_c_tank"}])
    assert not ex18d.fac_avail and not ex18d.fac_upgraded

    # 19. train 厂引用重解析（④b）：旧 id 不存在/不归己 → 同类型+距旧位最近重绑定；绑不了退单记活动流
    ex19 = make_ex()
    logs19 = []
    ctx19 = {"exec_holder": {"ex": ex19}, "known_actions": {"u_c_tank"}, "link": None,
             "log_activity": lambda k, t: logs19.append(t)}
    st19 = type("S", (), {})()
    g19 = {"fail_perm": set(), "fail_temp": {}}
    ex19.fac_mem[50] = ("landFactory", 1500.0, 740.0)  # 旧 id（换代/被毁前）的机型与位点
    facB = {"id": 60, "t": "landFactory", "bld": True, "x": 1510.0, "y": 745.0, "q": 0}  # 旧位附近
    facC = {"id": 70, "t": "landFactory", "bld": True, "x": 300.0, "y": 300.0, "q": 0}   # 远处
    snap19 = {"own": [facB, facC], "team": {"funds": 5000}}
    cmd, desc = multiagent._translate_combat(ctx19, st19, "train",
                                             {"action": "u_c_tank", "factory": 50}, snap19, g19)
    assert cmd and cmd["factory"] == 60, f"旧 id 应重绑定同类型最近厂#60: {cmd} {desc}"
    assert any("重绑定" in t for t in logs19), f"重绑定应记活动流: {logs19}"
    # id 存在 → 直用不重绑
    cmd, desc = multiagent._translate_combat(ctx19, st19, "train",
                                             {"action": "u_c_tank", "factory": 70}, snap19, g19)
    assert cmd and cmd["factory"] == 70, f"存在的厂 id 必须直用: {cmd}"
    # 无同类型厂 → 退单 + 活动流
    ex19.fac_mem[88] = ("airFactory", 100.0, 100.0)
    cmd, desc = multiagent._translate_combat(ctx19, st19, "train",
                                             {"action": "u_c_tank", "factory": 88}, snap19, g19)
    assert cmd is None and "退单" in desc, f"绑不了应退单: {desc}"
    assert any("退单" in t for t in logs19), f"退单应记活动流: {logs19}"

    print("PASS test_build_phase2：补水/防撞车/在干保留/低水位/阵地展开/focus/槽位择优/阵亡判定/预算仲裁/海厂锚点/隘口/实控前沿/占位统一/新池召唤/默认构成兜底/主动侦察/按厂科技档/升级换代继承/train厂重解析 全部符合预期")


if __name__ == "__main__":
    main()
