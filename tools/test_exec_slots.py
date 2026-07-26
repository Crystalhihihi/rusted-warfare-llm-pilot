# -*- coding: utf-8 -*-
"""executor 槽位/水图/续建 离线验证（临时脚本，测完即删）。"""
import sys, time, json
sys.path.insert(0, "bridge")
import executor as ex

TILE = 20
def mk_map(water_cols=0, w=100, h=100):
    rows = []
    for y in range(h):
        rows.append("~" * water_cols + "." * (w - water_cols))
    return {"tilesW": w, "tilesH": h, "tileW": TILE, "tileH": TILE, "rows": rows,
            "resources": [{"x": 1300, "y": 1000}, {"x": 200, "y": 1000, "w": True}]}

_uid = [0]
def U(t, x, y, bld=False, prog=None, act=None, q=None, hp=100, maxHp=100):
    _uid[0] += 1
    u = {"id": _uid[0], "t": t, "x": x, "y": y, "hp": hp, "maxHp": maxHp}
    if bld: u["bld"] = True
    if prog is not None: u["prog"] = prog
    if act is not None: u["act"] = act
    if q is not None: u["q"] = q
    return u

def snap(own, funds):
    return {"own": own, "enemy": [], "allies": [], "team": {"funds": funds}, "tick": 1}

logs = []
def log(kind, text): logs.append(text)

# ---- S1：水图（左侧20列水），开局三建造者+一海上建造者 ----
e1 = ex.Executor(mk_map(water_cols=20), {}, log)
cc = U("commandCenter", 1500, 1000, bld=True)
b1, b2, b3 = U("builder", 1500, 1000), U("builder", 1510, 1000), U("builder", 1520, 1000)
bs = U("builderShip", 400, 1000)
ext1 = U("extractorT1", 1400, 1000, bld=True)
ext2 = U("extractorT1", 1450, 1050, bld=True)
c1 = e1.tick(snap([cc, b1, b2, b3, bs, ext1, ext2], 5000))
assert e1.is_naval_map() is True, "S1 应判为水图"
fac = [c for c in c1 if c.get("action") == "b_landFactory"]
assert len(fac) == 2, f"S1 应盖两座厂: {c1}"
# 网格槽位：front=(-1,0)，flank=(0,-1)，13 格起、fp 默认 2x2 贴死排
assert (fac[0]["x"], fac[0]["y"]) == (1510.0, 750.0), f"S1 首厂槽位错: {fac[0]}"
assert (fac[1]["x"], fac[1]["y"]) == (1510.0, 670.0), f"S1 次厂槽位错: {fac[1]}"
wext = [c for c in c1 if c.get("action") == "b_extractorT1" and c["x"] == 200.0]
assert wext and wext[0]["builder"] == bs["id"], f"S1 水池采集应派 builderShip: {c1}"
land_ext = [c for c in c1 if c.get("action") == "b_extractorT1" and c["x"] == 1300.0]
assert land_ext and land_ext[0]["builder"] != bs["id"], f"S1 陆地池不许派 builderShip: {c1}"
# tick2：建造者腾出，海厂应落在最近水域锚点 tile(19,49) 中心 (390,990)
c2 = e1.tick(snap([cc, b1, b2, b3, bs, ext1, ext2], 5000))
sea = [c for c in c2 if c.get("action") == "b_seaFactory"]
assert sea and (sea[0]["x"], sea[0]["y"]) == (390.0, 990.0), f"S1 海厂锚点错: {c2}"
print("S1 OK:", c1)
print("S1 tick2 OK:", sea)

# ---- S2：陆地图，烂尾续建（低资金隔离其他步骤） ----
e2 = ex.Executor(mk_map(water_cols=0), {}, log)
cc2 = U("commandCenter", 1500, 1000, bld=True)
builders = [U("builder", 1500, 1000), U("builder", 1510, 1000), U("builder", 1520, 1000)]
exts = [U("extractorT1", 1400 + i * 50, 1000, bld=True) for i in range(4)]
facs = [U("landFactory", 1500, 740 - i * 170, bld=True, q=2) for i in range(2)]
tur = U("turret", 1300, 900, bld=True, prog=0.5)
dmg = U("extractorT1", 1460, 1100, bld=True, hp=400, maxHp=800)  # 受损 50%，应触发修理
e2.prog_seen[tur["id"]] = (0.5, time.time() - 61)  # 61s 无进展 = 烂尾
ces = [U("combatEngineer", 1600, 1000, act="idle"), U("combatEngineer", 1610, 1000, act="idle")]
c3 = e2.tick(snap([cc2] + builders + exts + facs + [tur, dmg] + ces, 1800))
assert e2.is_naval_map() is False, "S2 不应判为水图"
res = [c for c in c3 if c["cmd"] == "resume"]
res_targets = {c["target"] for c in res}
assert tur["id"] in res_targets, f"S2 应派烂尾续建: {c3}"
assert dmg["id"] in res_targets, f"S2 应派受损修理: {c3}"
print("S2 OK:", c3)

# ---- S3：高资金，后勤槽位连排（fabricator 已建占0号，新fab占1号，实验厂应占2号） ----
e3 = ex.Executor(mk_map(water_cols=0), {}, log)
cc3 = U("commandCenter", 1500, 1000, bld=True)
b3s = [U("builder", 1500 + i * 10, 1000) for i in range(4)]
exts3 = [U("extractorT1", 1400 + i * 50, 1000, bld=True) for i in range(4)]
facs3 = [U("landFactory", 1500, 740 - i * 170, bld=True, q=2) for i in range(2)]
fab = U("fabricatorT1", 1740, 1000, bld=True)
ces3 = [U("combatEngineer", 1600, 1000), U("combatEngineer", 1610, 1000)]
c4 = e3.tick(snap([cc3] + b3s + exts3 + facs3 + [fab] + ces3, 25000))
exp = [c for c in c4 if c.get("action") == "b_experimentalLandFactory"]
# 几何兜底槽位 (2080,1000) 在本图出界（图宽 2000px）——正确行为=跳过不发非法坐标（旧断言认的就是出界点）
assert not exp, f"出界槽位应跳过而不是照发: {c4}"
for c in c4:
    if "x" in c:
        assert 0 <= c["x"] and 0 <= c["y"], f"负坐标指令: {c}"
# ---- S4：规则快层（多 agent 模式：autopilot=False + routine=True） ----
e4 = ex.Executor(mk_map(water_cols=0), {}, log)
e4.autopilot = False
e4.routine = True
e4.econ_goal["builders"] = 4  # 让①补建造者在本场景也触发
cc4 = U("commandCenter", 1500, 1000, bld=True, q=0)
bs4 = [U("builder", 1500 + i * 10, 1000) for i in range(3)]
ext4 = U("extractorT1", 1400, 1000, bld=True)
ext4b = U("extractorT1", 1450, 1050, bld=True)
fac4 = U("landFactory", 1500, 740, bld=True, q=2)
own4 = [cc4] + bs4 + [ext4, ext4b, fac4]
snap4 = snap(own4, 5000)
snap4["delegated"] = [u["id"] for u in own4]
e4.set_army_goal(rally=(2000.0, 1000.0))
c5 = e4.tick(snap4)
kinds = [(c["cmd"], c.get("action")) for c in c5]
assert ("train", "u_builder") in kinds, f"规则层应补建造者: {c5}"
assert ("build", "b_extractorT1") in kinds, f"规则层应补采集器: {c5}"
tur = [c for c in c5 if c.get("action") == "b_c_turret_t1"]
assert tur and (tur[0]["x"], tur[0]["y"]) == (1810.0, 1110.0), f"防线弧网格位: {c5}"
fac5 = [c for c in c5 if c.get("action") == "b_landFactory"]
assert fac5 and (fac5[0]["x"], fac5[0]["y"]) == (1510.0, 670.0), f"规则层应网格扩厂: {c5}"
# 旁观铁律：无委托时规则层一拍都不动
snap4_nd = snap(own4, 5000)
snap4_nd["delegated"] = []
assert e4.tick(snap4_nd) == [], "无委托时规则层必须旁观"
# ---- S5：建造工单（BuilderManager）调度 ----
e5 = ex.Executor(mk_map(water_cols=0), {}, log)
e5.autopilot = False
e5.routine = True
cc5 = U("commandCenter", 1500, 1000, bld=True, q=0)
bs5 = [U("builder", 1500 + i * 10, 1000) for i in range(3)]
ext5 = U("extractorT1", 1400, 1000, bld=True)
own5 = [cc5] + bs5 + [ext5]
snap5 = snap(own5, 5000)
snap5["delegated"] = [u["id"] for u in own5]
e5.set_work_order([
    {"action": "b_extractorT1", "slot": "pool:0"},
    {"action": "b_landFactory", "slot": "factory:0"},
    {"action": "b_c_turret_t1", "slot": "defense:0"},
])
c6 = e5.tick(snap5)
bq = [c for c in c6 if c["cmd"] == "buildq"]
assert bq and bq[0]["items"].count(";") == 2, f"3 项任务应打包成 1 条 buildq: {c6}"
# 完成推进：建筑出现 → 任务变 done，队列继续
ext_new = U("extractorT1", 1300, 1000, bld=True, prog=0.4)
fac_new = U("landFactory", 1510, 750, bld=True, prog=1.0)
snap5b = snap(own5 + [ext_new, fac_new], 5000)
snap5b["delegated"] = snap5["delegated"]
e5.tick(snap5b)
st = {t["action"]: t["status"] for t in e5.work_order}
assert st["b_extractorT1"] == "done", f"采集任务应完成: {st}"
# 资金闸门：资金 0 时停派（有序攒钱）
e5.price["u_airFactory"] = 1000
e5.set_work_order([{"action": "b_airFactory", "slot": "factory:1"}])
c7 = e5.tick(snap(own5 + [ext_new], 0) | {"delegated": snap5["delegated"]})
assert not any(c.get("action") == "b_airFactory" for c in c7), f"资金0不应派单: {c7}"
print("S5 OK:", c6, st)

# ---- S6：同拍原地消耗——抢修派走后，routine 各段拿到的空闲表不再含它 ----
# （此前 _dispatch_rescue/_wo_tick 只局部重绑，同一建造者一拍内被抢修+工单派两单）
e6 = ex.Executor(mk_map(water_cols=0), {}, log)
e6.autopilot = False
e6.routine = True
cc6 = U("commandCenter", 1500, 1000, bld=True, q=0)
ba6, bb6 = U("builder", 1500, 1000), U("builder", 1510, 1000)
ext6 = U("extractorT1", 1400, 1000, bld=True)
tur6 = U("turret", 1300, 900, bld=True, prog=0.5)
own6 = [cc6, ba6, bb6, ext6, tur6]
snap6 = snap(own6, 5000)
snap6["delegated"] = [u["id"] for u in own6]
e6.prog_seen[tur6["id"]] = (0.5, time.time() - 61)  # 61s 无进展 = 烂尾，抢修必派
c8 = e6.tick(snap6)
res6 = [c for c in c8 if c["cmd"] == "resume"]
bld6 = [c for c in c8 if c["cmd"] in ("build", "buildq")]
assert res6 and res6[0]["target"] == tur6["id"], f"S6 应派烂尾抢修: {c8}"
assert bld6, f"S6 抢修后 routine 应继续派采集器: {c8}"
assert all(c.get("builder") != res6[0]["builder"] for c in bld6), \
    f"S6 同一建造者不许一拍两单（抢修{res6[0]['builder']}）: {c8}"
print("S6 OK:", c8)

# ---- S7：wo_id 稳定（续单在干保留/新单新号/重下新号）+ 资金回执回退 ----
e7 = ex.Executor(mk_map(water_cols=0), {}, log)
e7.set_work_order([{"action": "b_extractorT1", "slot": "pool:0"},
                   {"action": "b_landFactory", "slot": "factory:0"}])
wo1 = [t["wo_id"] for t in e7.work_order]
assert len(set(wo1)) == 2 and wo1 == sorted(wo1), f"首单应发连续 wo_id: {wo1}"
# 首单被接走（chat 派单落账）；已 assigned 不许重复落账（mark-first 契约）
assert e7.wo_mark_assigned(wo1[0], 77, 1300, 1000) is True
assert e7.wo_mark_assigned(wo1[0], 78, 1300, 1000) is False, "已 assigned 重复落账应返 False"
e7.set_work_order([{"action": "b_c_turret_t1", "slot": "defense:0"}])
ids = {t["action"]: t["wo_id"] for t in e7.work_order}
assert ids["b_extractorT1"] == wo1[0], f"续单后在干任务 wo_id 不许变: {ids} vs {wo1}"
assert ids["b_c_turret_t1"] > max(wo1), f"新单应发新号: {ids}"
e7.set_work_order([{"action": "b_landFactory", "slot": "factory:0"}])
ids2 = {t["action"]: t["wo_id"] for t in e7.work_order}
assert ids2["b_landFactory"] != wo1[1], f"被冲掉再下的单是新任务，应发新号: {ids2}"
# 资金不足回执（agent "funds X < price Y"）：任务回 pending 重派，不判死不毒化点位
t7 = next(t for t in e7.work_order if t["action"] == "b_landFactory")
t7["status"] = "assigned"; t7["builder"] = 77; t7["x"], t7["y"] = 1510.0, 750.0
t7["ts"] = time.time()
e7.on_cmd_results([{"ok": False,
                    "cmd": json.dumps({"cmd": "build", "action": "b_landFactory",
                                       "builder": 77, "x": 1510.0, "y": 750.0}),
                    "msg": "funds 120 < price 700"}])
assert t7["status"] == "pending" and t7["builder"] is None, f"资金不足应回退 pending: {t7}"
assert not e7.site_bad, f"资金不足不许 site_bad 毒化点位: {e7.site_bad}"
print("S7 OK:", ids2)

# ---- S8：坐标单实控闸——非防守建筑未实控被拒+文案；防守类豁免；安稳点放行 ----
e8 = ex.Executor(mk_map(water_cols=0, w=200, h=200), {}, log)  # 4000px 图：远点 (2400,*) 在界内
e8.autopilot = False
e8.routine = True
cc8 = U("commandCenter", 1500, 1000, bld=True, q=0)
b8 = U("builder", 1500, 1000)
ext8 = U("extractorT1", 1400, 1000, bld=True)
own8 = [cc8, b8, ext8]
snap8 = snap(own8, 5000)
snap8["delegated"] = [u["id"] for u in own8]
e8.set_work_order([
    {"action": "b_landFactory", "slot": [2400, 1000]},   # 900px 家圈外、未实控：应拒
    {"action": "b_c_turret_t1", "slot": [2400, 800]},    # 防守类豁免实控闸：应派
])
e8.tick(snap8)
st8 = {t["action"]: t for t in e8.work_order}
assert st8["b_landFactory"]["status"] == "failed", f"非防守坐标单未实控应拒: {st8['b_landFactory']}"
assert "超出实控区（支持建筑请先稳固防线）" in st8["b_landFactory"]["note"], \
    f"失败文案应为支持建筑版: {st8['b_landFactory']['note']}"
assert st8["b_c_turret_t1"]["status"] == "assigned", f"防守类坐标单应豁免实控闸: {st8['b_c_turret_t1']}"
# 安稳点放行：家圈 600px 外但安全值≥30 已持续 20s（直接种 secure_since 免等实钟）
e8.control.secure_since[(int(2200 // 200), int(1000 // 200))] = time.time() - 25
e8.set_work_order([{"action": "b_landFactory", "slot": [2200, 1000]}])
e8.tick(snap8)
fac8 = [t for t in e8.work_order if t["action"] == "b_landFactory" and t["slot"] == [2200, 1000]]
assert fac8 and fac8[0]["status"] == "assigned", f"安稳过的坐标单应放行: {fac8}"
print("S8 OK:", {a: t["status"] for a, t in st8.items()}, fac8[0]["status"])
print("ALL PASS")
