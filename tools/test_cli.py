# -*- coding: utf-8 -*-
"""架构二期（cli_mode）离线验证：开局任务单生成 / 谓词词表 / 任务单状态机 /
Jev 封装（mock）/ executor cli 裁剪段 / cliseat 解析 / 开局物化闭环。
与既有套件同款：mock 快照+pedia，零网络零 LLM。"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

sys.path.insert(0, "bridge")
import danger as danger_mod
import executor as exec_mod
import predicates
import jev as jev_mod
import opening
import cliseat
import cliloop

TILE = 20
MAP = {"tilesW": 100, "tilesH": 100, "tileW": TILE, "tileH": TILE,
       "rows": ["." * 100 for _ in range(100)],
       "resources": [{"x": 1300, "y": 1000}, {"x": 1350, "y": 1050}, {"x": 700, "y": 700}],
       "path": ""}

PEDIA = {"units": [
    {"name": "commandCenter", "price": 3000, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 4000, "builds": ["u_builder"]},
    {"name": "builder", "price": 250, "tech": 1, "building": False, "builder": True,
     "move": "LAND", "hp": 170, "builds": ["b_extractorT1", "b_landFactory", "b_c_turret_t1"]},
    {"name": "extractorT1", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 800, "builds": ["extractorT2_0"]},
    {"name": "landFactory", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 2000, "builds": ["u_c_tank", "u_missileTank", "u_heavyTank", "110"]},
    {"name": "c_tank", "price": 400, "tech": 1, "building": False, "builder": False,
     "move": "LAND", "hp": 210, "builds": []},
    {"name": "missileTank", "price": 700, "tech": 2, "building": False, "builder": False,
     "move": "LAND", "hp": 1100, "builds": []},
    {"name": "heavyTank", "price": 900, "tech": 2, "building": False, "builder": False,
     "move": "LAND", "hp": 1500, "builds": []},
]}

_uid = [0]


def U(t, x, y, bld=False, act=None, q=None, hp=100, maxhp=100, prog=None):
    _uid[0] += 1
    u = {"id": _uid[0], "t": t, "x": x, "y": y, "hp": hp, "maxHp": maxhp}
    if bld:
        u["bld"] = True
    if act is not None:
        u["act"] = act
    if q is not None:
        u["q"] = q
    if prog is not None:
        u["prog"] = prog
    return u


def mk_snap(own, funds=5000, enemy=()):
    return {"tick": 100, "own": own, "allies": [], "enemy": list(enemy),
            "team": {"id": 0, "funds": funds, "ai": False},
            "delegated": [u["id"] for u in own]}


def mk_ex(cli):
    ex = exec_mod.Executor(MAP, PEDIA, lambda k, t: None)
    ex.autopilot = False
    ex.routine = True
    ex.cli_mode = cli
    return ex


# ==================== §1 开局任务单生成 ====================
print("== §1 opening 生成 ==")
cc0 = U("commandCenter", 1500, 1000, bld=True, q=0)
op = opening.generate_opening({"own": [cc0]}, PEDIA, ((1500, 1000), [(300, 300)]),
                              False, "中图", MAP)
assert op is not None
wo = op["work_order"]
assert [t["slot"] for t in wo[:3]] == ["pool:0", "pool:1", "pool:2"], wo
assert wo[3]["action"] == "b_landFactory" and wo[3]["slot"] == "factory:0", wo[3]
assert len(wo) == 4, wo  # 只给基础任务：无防线阵地单
assert op["composition"] == {}, op["composition"]      # 不代 AI 决定造兵
assert op["mission_templates"] == []                   # 不代 AI 决定小队去哪
pp = op["plan_params"]
assert pp["采集器目标"] == 3 and pp["建造者目标"] == 3 and pp["姿态"] == "expand", pp
assert pp["厂型清单"] == ["landFactory"] and "主攻" not in pp and "防线" not in pp, pp
assert "【阶段】" in op["plan_text"] and "未定" in op["plan_text"]
# 水图/小图同样只出基础任务；无家=None
op_n = opening.generate_opening({"own": [cc0]}, PEDIA, ((1500, 1000), [(300, 300)]),
                                True, "小图", MAP)
assert op_n["mission_templates"] == [] and op_n["composition"] == {}
assert len(op_n["work_order"]) == 4
assert opening.generate_opening({"own": []}, PEDIA, (None, []), False, "中图", MAP) is None
print("§1 OK")

# ==================== §2 谓词词表 ====================
print("== §2 谓词 ==")
tks = [U("c_tank", 1400 + i * 30, 900) for i in range(6)]
exts = [U("extractorT1", 1300, 1000, bld=True, prog=1),
        U("extractorT1", 1350, 1050, bld=True, prog=1)]
en1 = U("tank", 1510, 1090)
snap2 = mk_snap([cc0] + exts + tks, enemy=[en1])
sqs = {"L1": {"members": {u["id"] for u in tks}, "mission": None}}
now2 = time.time()
tasks2 = {"t1": {"_status": "active", "_act_ts": now2 - 100, "_prog_ts": now2 - 100},
          "t2": {"_status": "pending"}}
w2 = SimpleNamespace(snap=snap2, squads=sqs, wiped={"L9"}, tasks=tasks2,
                     home=(1500, 1000), now=now2)
assert predicates.evaluate(w2, "at_point(L1,1400,900,200)")
assert not predicates.evaluate(w2, "at_point(L1,100,100,100)")
assert not predicates.evaluate(w2, "at_point(L7,1400,900,200)")  # 未知小队
assert predicates.evaluate(w2, "built(b_extractorT1,2)")
assert not predicates.evaluate(w2, "built(extractorT1,3)")
assert predicates.evaluate(w2, "unit_count(c_tank,>=,6)")
assert predicates.evaluate(w2, "unit_count(u_c_tank,==,6)")
assert not predicates.evaluate(w2, "unit_count(c_tank,>,6)")
assert predicates.evaluate(w2, "enemy_visible(any)")
assert predicates.evaluate(w2, "enemy_visible(home)")      # 敌入家圈 1200px
assert predicates.evaluate(w2, "enemy_visible(1510,1090,100)")
assert not predicates.evaluate(w2, "enemy_visible(300,300,100)")
assert predicates.evaluate(w2, "squad_wiped(L9)")
assert not predicates.evaluate(w2, "squad_wiped(L1)")
assert predicates.evaluate(w2, "task_stuck(t1,90)")
assert not predicates.evaluate(w2, "task_stuck(t1,200)")
assert not predicates.evaluate(w2, "task_stuck(t2,999)")   # 非 active
errs = []
assert not predicates.evaluate(w2, "fly(home)", errs.append)          # 词表外
assert not predicates.evaluate(w2, "at_point(L1)", errs.append)       # 参数不够
assert errs and "未知谓词" in errs[0], errs
snap2no = mk_snap([cc0], enemy=[])
w2.snap = snap2no
assert not predicates.evaluate(w2, "enemy_visible(any)")
assert not predicates.evaluate(w2, "enemy_visible(home)")
print("§2 OK")

# ==================== §3 任务单状态机 ====================
print("== §3 任务单状态机 ==")
tmp3 = tempfile.mkdtemp()
td3 = os.path.join(tmp3, "tasks")
os.makedirs(td3)
events3 = []
fired3 = []
eng3 = predicates.TaskEngine(td3, lambda t: fired3.append(t["id"]) or True,
                             events3.append)
with open(os.path.join(td3, "a.json"), "w", encoding="utf-8") as fp:
    json.dump({"tasks": [
        {"id": "t1", "branch": "build", "task": "移交型", "do": {}},
        {"id": "t2", "branch": "land", "task": "闸门+链",
         "do": {}, "trigger": {"when": ["unit_count(c_tank,>=,6)"], "then": "t3"}},
        {"id": "t3", "branch": "land", "task": "接力（自身 when 永不真，只等链式强触）",
         "do": {}, "trigger": {"when": ["unit_count(c_tank,>,99)"], "then": None}},
        {"id": "t4", "branch": "land", "task": "中止",
         "do": {}, "trigger": {"when": ["enemy_visible(any)"], "then": "abort"}},
    ]}, fp, ensure_ascii=False)
w3 = SimpleNamespace(snap=snap2, squads={}, wiped=set(), tasks=None,
                     home=(1500, 1000), now=time.time())
w3.snap = mk_snap([cc0] + tks)  # 6 坦克无敌
eng3.tick(w3, time.time())
assert eng3.tasks["t1"]["_status"] == "done", eng3.tasks["t1"]     # 无触发器=移交即完成
assert eng3.tasks["t2"]["_status"] == "active"                     # when 真=激活
assert eng3.tasks["t3"]["_status"] == "pending"                    # 自身 when 不真=等链
assert eng3.tasks["t4"]["_status"] == "pending"                    # 无敌=不激活
assert fired3 == ["t1", "t2"], fired3
eng3.tick(w3, time.time())   # t2 when 仍真 → done + 链式强触 t3（绕过其 when）
assert eng3.tasks["t2"]["_status"] == "done"
assert eng3.tasks["t3"]["_status"] == "active", eng3.tasks["t3"]
assert any(e.get("ev") == "task_chain" and e.get("to") == "t3" for e in events3), events3
eng3.tick(w3, time.time())   # t3 自身 when 不真 → 保持 active 不完成
assert eng3.tasks["t3"]["_status"] == "active"
w3.snap = mk_snap([cc0] + tks, enemy=[en1])  # 敌现 → t4 激活，下拍 when 触发 abort
eng3.tick(w3, time.time())
assert eng3.tasks["t4"]["_status"] == "active"
assert "t4" in fired3
eng3.tick(w3, time.time())
assert eng3.tasks["t4"]["_status"] == "aborted"
# do_fn 条件不足留 pending 重试 + task_wait 事件
with open(os.path.join(td3, "b.json"), "w", encoding="utf-8") as fp:
    json.dump({"tasks": [{"id": "t5", "branch": "land", "task": "等条件", "do": {}}]}, fp)
eng3.do_fn = lambda t: False if t["id"] == "t5" else True
eng3.tick(w3, time.time())
assert eng3.tasks["t5"]["_status"] == "pending"
assert any(e.get("ev") == "task_wait" and e.get("id") == "t5" for e in events3), events3
# 热更新：改文件内容覆盖、运行态保留
t4_before = eng3.tasks["t4"]["_status"]
with open(os.path.join(td3, "a.json"), "w", encoding="utf-8") as fp:
    json.dump({"tasks": [
        {"id": "t4", "branch": "land", "task": "中止（改名）", "do": {},
         "trigger": {"when": ["enemy_visible(any)"], "then": "abort"}},
        {"id": "t6", "branch": "build", "task": "新增", "do": {}}]}, fp)
eng3.do_fn = lambda t: True
time.sleep(0.02)
os.utime(os.path.join(td3, "a.json"))
eng3.tick(w3, time.time())
assert eng3.tasks["t4"]["task"].endswith("（改名）") and eng3.tasks["t4"]["_status"] == t4_before
assert eng3.tasks["t6"]["_status"] == "done"
print("§3 OK")

# ==================== §4 Jev 封装（mock urlopen） ====================
print("== §4 Jev 封装 ==")
_orig_urlopen = urllib.request.urlopen
try:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"answers": {"retreat": {"type": "noul", "noul": 0.9},
                                           "focus": {"type": "choice", "choice": "坦克群#1",
                                                     "probabilities": {"坦克群#1": 0.8}}}}
                              ).encode("utf-8")

    captured = {}

    def fake_ok(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Resp()

    urllib.request.urlopen = fake_ok
    jc = jev_mod.Jev("http://127.0.0.1:8009")
    ans = jc.ask("态势", {"retreat": {"type": "noul", "instructions": "该撤吗"}})
    assert ans["retreat"]["noul"] == 0.9, ans
    assert captured["payload"]["state"] == "态势"
    assert "retreat" in captured["payload"]["questions"]
    assert captured["timeout"] == 1.5, captured  # 快层 1.5s 超时

    def fake_down(req, timeout=None):
        raise urllib.error.URLError("refused")

    urllib.request.urlopen = fake_down
    assert jc.ask("x", {"q": {"type": "noul", "instructions": "y"}}) is None
    assert jc.down_since is not None
    urllib.request.urlopen = fake_ok
    assert jc.ask("x", {"q": {"type": "noul", "instructions": "y"}}) is not None
    assert jc.down_since is None  # 恢复清零
finally:
    urllib.request.urlopen = _orig_urlopen
# 置信带：noul∈[0.35,0.65]=拿不准；choice 顶端<0.6=拿不准
assert jev_mod.unsure_noul({"noul": 0.5}) and jev_mod.unsure_noul({"noul": 0.35})
assert not jev_mod.unsure_noul({"noul": 0.9}) and not jev_mod.unsure_noul({"noul": 0.2})
assert jev_mod.unsure_noul({})
assert not jev_mod.unsure_choice({"probabilities": {"a": 0.7}})
assert jev_mod.unsure_choice({"probabilities": {"a": 0.5}})
assert jev_mod.unsure_choice({"choice": "a"})
print("§4 OK")

# ==================== §5 executor cli 裁剪段 ====================
print("== §5 executor cli_mode 裁剪 ==")
# A. 自动防线③跳过（旧模式对照会发 b_c_turret_t1）
bldA = U("builder", 1500, 1000, act="idle")
facA = U("landFactory", 1500, 760, bld=True, q=0)
snapA = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0), bldA, facA], funds=50000)
ex_old = mk_ex(False)
ex_old.set_economy_goal(extractors=0, builders=1)  # 静默①②，隔离③
ex_old.routine_defense = True
cmds_old = ex_old.tick(snapA)
assert any(c.get("action") == "b_c_turret_t1" for c in cmds_old), f"旧模式应自动防线: {cmds_old}"
ex_cli = mk_ex(True)
ex_cli.set_economy_goal(extractors=0, builders=1)
ex_cli.routine_defense = True
cmds_cli = ex_cli.tick(snapA)
assert cmds_cli == [], f"cli 模式自动防线/扩厂/升级/补水应全跳: {cmds_cli}"

# B. 自动升级⑤跳过（旧模式对照发 train extractorT2_0）
ext_b = U("extractorT1", 1300, 1000, bld=True, prog=1)
snapB = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                 U("builder", 1500, 1000, act="idle"),
                 U("landFactory", 1500, 760, bld=True, q=0), ext_b], funds=5000)
ex_old = mk_ex(False)
ex_old.set_economy_goal(extractors=0, builders=1)
ex_old.routine_defense = False
cmds_old = ex_old.tick(snapB)
assert any(c["cmd"] == "train" and c["action"] == "extractorT2_0" for c in cmds_old), cmds_old
ex_cli = mk_ex(True)
ex_cli.set_economy_goal(extractors=0, builders=1)
ex_cli.routine_defense = False
cmds_cli = ex_cli.tick(snapB)
assert not any(c["cmd"] == "train" and c["action"] == "extractorT2_0" for c in cmds_cli), cmds_cli
assert not any(c.get("action") == "b_landFactory" for c in cmds_cli), cmds_cli  # ④扩厂同跳

# C. ⑦生产：cli 只认显式构成（默认构成补水取消）；显式构成后正常产
snapC = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                 U("builder", 1500, 1000, act="idle"),
                 U("landFactory", 1500, 760, bld=True, q=0)], funds=5000)
ex_cli = mk_ex(True)
ex_cli.set_economy_goal(extractors=0, builders=1)
ex_cli.routine_defense = False
cmds_c0 = ex_cli.tick(snapC)
assert not any(c["cmd"] == "train" and str(c.get("action", "")).startswith("u_")
               for c in cmds_c0), f"无显式构成 cli 不应产兵: {cmds_c0}"
ex_cli.set_army_goal(composition={"u_c_tank": 4})  # T1 主战（T2 兵在未知档厂被 §11 过滤，另测）
cmds_c1 = ex_cli.tick(snapC)
assert any(c["cmd"] == "train" and c["action"] == "u_c_tank" for c in cmds_c1), cmds_c1
ex_old = mk_ex(False)  # 旧模式默认构成兜底照产（行为不变的旁证）；
# 厂标已 T2 避开⑤升级窗口（110 一拍占厂会挤掉⑦，那是既有让路语义非本测目标）
_facA = next(u for u in snapC["own"] if u["t"] == "landFactory")
ex_old.fac_upgraded.add(exec_mod.fac_key(_facA["t"], _facA["x"], _facA["y"]))
ex_old.set_economy_goal(extractors=0, builders=1)
ex_old.routine_defense = False
ex_old.army_goal["composition"] = None  # 真空→默认构成兜底
cmds_c2 = ex_old.tick(snapC)
assert any(c["cmd"] == "train" and str(c.get("action", "")).startswith("u_")
           for c in cmds_c2), f"旧模式默认构成应补水: {cmds_c2}"

# D. 保留段：①建造者 ②采集器 工单调度 抢修 在 cli 模式照跑
snapD = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                 U("builder", 1500, 1000, act="idle")], funds=2000)
ex_cli = mk_ex(True)
ex_cli.set_economy_goal(extractors=1, builders=3)
cmds_d = ex_cli.tick(snapD)
assert any(c["cmd"] == "train" and c["action"] == "u_builder" for c in cmds_d), cmds_d  # ①
assert any(c["cmd"] == "build" and c["action"] == "b_extractorT1" for c in cmds_d), cmds_d  # ②
ex_cli2 = mk_ex(True)
ex_cli2.set_economy_goal(extractors=0, builders=1)
ex_cli2.set_work_order([{"action": "b_extractorT1", "slot": "pool:0"}], snapD)
cmds_d2 = ex_cli2.tick(snapD)
assert any(c["cmd"] == "build" and c["action"] == "b_extractorT1" for c in cmds_d2), cmds_d2
assert ex_cli2.work_order[0]["status"] == "assigned", ex_cli2.work_order
dmg = U("landFactory", 1500, 760, bld=True, q=0, hp=50, maxhp=100)  # 受损 <85%
snapD2 = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                  U("builder", 1500, 1000, act="idle"), dmg], funds=100)
ex_cli3 = mk_ex(True)
ex_cli3.set_economy_goal(extractors=0, builders=1)
cmds_d3 = ex_cli3.tick(snapD2)
assert any(c["cmd"] == "resume" and c.get("target") == dmg["id"] for c in cmds_d3), cmds_d3

# E. ⑧主动侦察跳过（旧模式对照发 move）
tkE = U("c_tank", 1400, 900, act="idle")
snapE = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                 U("builder", 1500, 1000, act="idle"), tkE], funds=100)


def _fog_stub():
    return SimpleNamespace(fresh=lambda: True, enabled=lambda: True,
                           rows=[[2] * 100 for _ in range(100)], w=100, h=100, tile=20,
                           state_at=lambda x, y: "black")


def _mk_scout_ex(cli):
    ex = mk_ex(cli)
    ex.set_economy_goal(extractors=0, builders=1)
    ex.routine_defense = False
    ex.scout_enabled = True
    ex.dmap = danger_mod.DangerMap()
    ex.dmap.seed_bases([(300, 300)])
    ex.control.fog = _fog_stub()
    return ex


cmds_e_old = _mk_scout_ex(False).tick(snapE)
assert any(c["cmd"] == "move" and c.get("units") for c in cmds_e_old), \
    f"旧模式平静期应派侦察: {cmds_e_old}"
cmds_e_cli = _mk_scout_ex(True).tick(snapE)
assert not any(c["cmd"] == "move" for c in cmds_e_cli), f"cli 模式不应自动侦察: {cmds_e_cli}"
print("§5 OK")

# ==================== §6 cliseat 解析 ====================
print("== §6 cliseat stream-json 解析 ==")
sample = "\n".join([
    '{"type":"assistant","message":{"role":"assistant","content":"","tool_calls":[{"id":"1"}]}}',
    '{"type":"tool","message":{"role":"tool","content":"ok"}}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"计划书v2"}]}}',
])
assert cliseat.parse_stream_json(sample) == "计划书v2"
assert cliseat.parse_stream_json("not json\nplain") == "not json\nplain"  # 兜底原文
assert cliseat.parse_stream_json("") == ""
pb = cliseat.extract_params_block('布局……\n```json\n{"阶段":"扩张","采集器目标":5}\n```')
assert pb == {"阶段": "扩张", "采集器目标": 5}, pb
assert cliseat.extract_params_block("没有代码块") is None
assert cliseat.extract_params_block("```json\n{bad}\n```") is None
seat_c = cliseat.Seat("commander", tmp3, {}, lambda ev: None, lambda w, t: None)
seat_l = cliseat.Seat("land", tmp3, {}, lambda ev: None, lambda w, t: None)
assert seat_c.enabled and seat_c.interval == 60          # 总指挥默认开，60s 兜底
assert not seat_l.enabled and seat_l.interval == 25      # 中层 stub 默认关
seat_l2 = cliseat.Seat("land", tmp3, {"cli_middle_enabled": True, "middle_interval_sec": 20},
                       lambda ev: None, lambda w, t: None)
assert seat_l2.enabled and seat_l2.interval == 20        # 配置驱动可开
print("（手动验证：真 spawn 需 kimi 在 PATH——起局后看局文件夹 plan.md/logs/events.jsonl）")
print("§6 OK")

# ==================== §7 开局物化闭环（任务单→executor） ====================
print("== §7 物化闭环 ==")
tmp7 = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp7, "tasks"))
os.makedirs(os.path.join(tmp7, "logs"))
known7 = set()
for pu in PEDIA["units"]:
    known7.update(pu.get("builds", []))
tks7 = [U("c_tank", 1400 + i * 30, 900, act="idle") for i in range(6)]
snap7 = mk_snap([cc0, U("builder", 1500, 1000, act="idle"),
                 U("landFactory", 1500, 760, bld=True, q=0)] + tks7, funds=5000)
ex7 = mk_ex(True)
events7 = []
sent7 = []
link7 = SimpleNamespace(send=lambda c: sent7.append(c) or True)
ctx7 = {"exec_holder": {"ex": ex7}, "known_actions": known7, "link": link7,
        "log_activity": lambda k, t: None, "cfg": {}}
match7 = SimpleNamespace(dir=tmp7, log=lambda k, t: None,
                         squad_st=SimpleNamespace(squads={}, move_map={}, mission_stale=False),
                         home_xy=(1500, 1000), world=None, ex=ex7, engine=None)
match7.engine = predicates.TaskEngine(
    os.path.join(tmp7, "tasks"),
    do_fn=lambda t: cliloop._do_fn(ctx7, match7, t),
    event_fn=lambda ev: (events7.append(ev), cliloop._write_event(match7, ev))[1])
op7 = opening.generate_opening(snap7, PEDIA, ((1500, 1000), [(300, 300)]), False, "中图", MAP)
cliloop._write_opening(ctx7, match7, op7, "中图")
assert os.path.exists(os.path.join(tmp7, "tasks", "opening.json"))
assert os.path.exists(os.path.join(tmp7, "plan.md"))
assert os.path.exists(os.path.join(tmp7, "board.md"))
w7 = SimpleNamespace(snap=snap7, squads=match7.squad_st.squads, wiped=set(),
                     tasks=None, home=(1500, 1000), now=time.time())
match7.world = w7
match7.engine.tick(w7, time.time())
# 移交型任务：op-econ 激活即完成，executor 目标已消费（v2：op-army 不再挂——构成归 AI 层）
assert set(match7.engine.tasks) == {"op-econ"}, list(match7.engine.tasks)
assert match7.engine.tasks["op-econ"]["_status"] == "done", events7
assert ex7.econ_goal["extractors"] == 3 and ex7.econ_goal["builders"] == 3
assert not ex7.army_goal.get("composition"), ex7.army_goal  # 脚本不预设构成
acts7 = [t["action"] for t in ex7.work_order]
assert acts7.count("b_extractorT1") == 3 and "b_landFactory" in acts7, acts7
assert not any(t.startswith("b_c_turret") or t.startswith("b_c_antiAir") for t in acts7), acts7
# mission 认领路径保活：中层任务单热加载一个 mission 任务 → 小队未成型留 pending；编成后认领+一次性派发
with open(os.path.join(tmp7, "tasks", "land.json"), "w", encoding="utf-8") as fp:
    json.dump({"kind": "middle", "tasks": [
        {"id": "m-test", "branch": "land", "squad": "auto", "task": "测试驻守",
         "do": {"mission": {"任务": "hold", "目标": [1800.0, 900.0], "编成": "主战", "备注": "测试"}},
         "trigger": {"when": [], "then": None}, "status": "pending"}]}, fp, ensure_ascii=False)
match7.engine.tick(w7, time.time())
m1 = match7.engine.tasks["m-test"]
assert m1["_status"] == "pending", m1
match7.squad_st.squads["L1"] = {"mission": None, "members": {u["id"] for u in tks7}}
match7.engine.tick(w7, time.time())
assert m1["_status"] == "active", (m1, events7)
sq1 = match7.squad_st.squads["L1"]
assert (sq1["mission"] or {}).get("任务") == "hold", sq1
mv7 = [c for c in sent7 if c["cmd"] == "move" and c.get("units")]
assert mv7 and str(tks7[0]["id"]) in mv7[0]["units"], sent7
assert os.path.exists(os.path.join(tmp7, "logs", "events.jsonl"))
ev_lines = open(os.path.join(tmp7, "logs", "events.jsonl"), encoding="utf-8").read()
assert '"task_loaded"' in ev_lines and '"to": "done"' in ev_lines, ev_lines
print("§7 OK")

# ==================== §8 Jev 事件流（受击撤退/拿不准上报/降级不动） ====================
print("== §8 Jev 事件流 ==")


def _mk_match8(jev_ans):
    """伪 Jev 事件环境：L1 六坦克，受击窗口血量跌 20%（>15% 阈值）。"""
    d8 = tempfile.mkdtemp()
    os.makedirs(os.path.join(d8, "tasks"))
    os.makedirs(os.path.join(d8, "logs"))
    logs8 = []
    m8 = SimpleNamespace(
        dir=d8, log=lambda k, t: logs8.append((k, t)), home_xy=(1500, 1000),
        squad_st=SimpleNamespace(squads={}, move_map={}, mission_stale=False),
        wiped=set(), hp_hist={}, seen_enemies=set(), jev_cd={}, cmd_dedup={},
        unsure_ts={}, task_chat_ts={}, defeated=False,
        jev=SimpleNamespace(enabled=True, ask=lambda s, q: jev_ans),
        ex=None, world=None, seats=[])
    m8.engine = predicates.TaskEngine(os.path.join(d8, "tasks"),
                                      lambda t: True, lambda ev: None)
    return m8, logs8


tks8 = [U("c_tank", 1400 + i * 30, 900, act="idle") for i in range(6)]
ctx8 = {"link": SimpleNamespace(send=lambda c: sent8.append(c) or True),
        "log_activity": lambda k, t: None, "cfg": {}}

# a. 撤退：noul=0.9 > 0.65 → move 回主基地
m8, logs8 = _mk_match8({"retreat": {"type": "noul", "noul": 0.9}})
m8.squad_st.squads["L1"] = {"mission": {"任务": "attack", "目标": [300, 300]},
                            "members": {u["id"] for u in tks8}}
sent8 = []
t0 = time.time()
cliloop._jev_tick(ctx8, m8, mk_snap(tks8), t0)          # 建档
hurt = [dict(u, hp=80) for u in tks8]                   # 总血 600→480（-20%）
cliloop._jev_tick(ctx8, m8, mk_snap(hurt), t0 + 5)
mv8 = [c for c in sent8 if c["cmd"] == "move"]
assert mv8 and mv8[0]["x"] == 1500.0 and mv8[0]["y"] == 1000.0, sent8
assert str(tks8[0]["id"]) in mv8[0]["units"], mv8
assert any(k == "alert" and "Jev 撤退" in t for k, t in logs8), logs8

# b. 拿不准：noul=0.5 ∈ 置信带 → 不动 + board.md 上报中层
m8, logs8 = _mk_match8({"retreat": {"type": "noul", "noul": 0.5}})
m8.squad_st.squads["L1"] = {"mission": None, "members": {u["id"] for u in tks8}}
sent8 = []
cliloop._jev_tick(ctx8, m8, mk_snap(tks8), t0)
cliloop._jev_tick(ctx8, m8, mk_snap(hurt), t0 + 5)
assert not [c for c in sent8 if c["cmd"] != "overlay"], sent8  # overlay=警戒推送（新可观测），非指令
bd = open(os.path.join(m8.dir, "board.md"), encoding="utf-8").read()
assert "拿不准" in bd and "L1" in bd, bd

# c. 服务不可用降级：ask→None → 不动 + jev_down 事件
m8, logs8 = _mk_match8(None)
m8.squad_st.squads["L1"] = {"mission": None, "members": {u["id"] for u in tks8}}
sent8 = []
cliloop._jev_tick(ctx8, m8, mk_snap(tks8), t0)
cliloop._jev_tick(ctx8, m8, mk_snap(hurt), t0 + 5)
assert not [c for c in sent8 if c["cmd"] != "overlay"], sent8
assert any("jev_down" in t for k, t in logs8), logs8
print("§8 OK")

# ==================== §9 pool 解析宽限 + 物化等 CC + 认领放弃时限 ====================
print("== §9 热身期宽限/物化闸/放弃时限 ==")
from collections import deque as _deque
import threading as _threading

# 1. pool: 槽位解析失败=热身期瞬态 → 宽限重试（pending 不判死）；非 pool 维持立即 failed
ex9 = mk_ex(True)
ex9.set_economy_goal(extractors=0, builders=1)
snap90 = mk_snap([U("builder", 1500, 1000, act="idle")], funds=5000)  # 无 CC：safe_pools=0
ex9.set_work_order([{"action": "b_extractorT1", "slot": "pool:0"},
                    {"action": "b_landFactory", "slot": "factory:0"}], snap90)
ex9.tick(snap90)
tp9, tf9 = ex9.work_order
assert tp9["status"] == "pending" and "_pool_fail0" in tp9, tp9     # pool 宽限重试
assert "|槽位解析失败" not in tp9["note"], tp9
assert tf9["status"] == "failed" and "|槽位解析失败" in tf9["note"], tf9  # factory 维持立即判死
ex9.tick(snap90)                                                    # 再拍仍 pending（重试）
assert tp9["status"] == "pending"
tp9["_pool_fail0"] = time.time() - 46                               # 连续失败满 45s
ex9.tick(snap90)
assert tp9["status"] == "failed" and "宽限45s已尽" in tp9["note"], tp9
# 热身成功后照常派发：首拍无 CC pending，CC 进快照后 assigned
ex9b = mk_ex(True)
ex9b.set_economy_goal(extractors=0, builders=1)
ex9b.set_work_order([{"action": "b_extractorT1", "slot": "pool:0"}], snap90)
ex9b.tick(snap90)
assert ex9b.work_order[0]["status"] == "pending"
snap91 = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                  U("builder", 1500, 1000, act="idle")], funds=5000)
cmds91 = ex9b.tick(snap91)
assert ex9b.work_order[0]["status"] == "assigned", ex9b.work_order
assert any(c["cmd"] == "build" and c["action"] == "b_extractorT1" for c in cmds91), cmds91

# 2. 开局物化等快照有效性：无我方建筑 → 不建局文件夹不物化；CC 进快照 → 物化
tmp9 = tempfile.mkdtemp()
_orig_mdir = cliloop.MATCHES_DIR
cliloop.MATCHES_DIR = tmp9
try:
    snap9v = [mk_snap([U("builder", 1500, 1000, act="idle")])]  # 有单位无建筑（热身期）
    link9 = SimpleNamespace(map_message=lambda: MAP, snapshot=lambda: snap9v[0],
                            map_version=1, fog_snap=None, user_msgs=_deque(),
                            lock=_threading.Lock())
    ctx9 = {"link": link9, "cfg": {}, "pedia": PEDIA, "log_activity": lambda k, t: None,
            "exec_holder": {}, "ai_enabled": lambda: True, "known_actions": known7}
    assert cliloop._setup_match(ctx9, 1) is None
    assert os.listdir(tmp9) == [], os.listdir(tmp9)  # 局文件夹都没建=未物化
    snap9v[0] = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                         U("builder", 1500, 1000, act="idle")])
    m9 = cliloop._setup_match(ctx9, 1)
    assert m9 is not None and os.path.exists(os.path.join(m9.dir, "tasks", "opening.json"))
    ex9c = ctx9["exec_holder"].get("ex")
    assert ex9c is not None and ex9c.cli_mode and not ex9c.autopilot, "cli 裁剪标志"
finally:
    cliloop.MATCHES_DIR = _orig_mdir

# 3. mission 认领放弃时限：do 条件不足超 600s → aborted + task_give_up（治海军模板在无海队的图空转）
td9 = os.path.join(tempfile.mkdtemp(), "tasks")
os.makedirs(td9)
events9 = []
eng9 = predicates.TaskEngine(td9, lambda t: False, events9.append)
with open(os.path.join(td9, "c.json"), "w", encoding="utf-8") as fp:
    json.dump({"tasks": [{"id": "op-mission-navy-1", "branch": "navy", "task": "海军待命",
                          "do": {"mission": {"任务": "hold", "目标": [900, 700]}},
                          "trigger": {"when": [], "then": None}}]}, fp, ensure_ascii=False)
w9 = SimpleNamespace(snap=snap90, squads={}, wiped=set(), tasks=None,
                     home=(1500, 1000), now=time.time())
eng9.tick(w9, time.time())
assert eng9.tasks["op-mission-navy-1"]["_status"] == "pending"
assert "_fail0" in eng9.tasks["op-mission-navy-1"]
eng9.tasks["op-mission-navy-1"]["_fail0"] -= 601  # 模拟认不到小队 10 分钟
eng9.tick(w9, time.time())
assert eng9.tasks["op-mission-navy-1"]["_status"] == "aborted"
assert any(e.get("ev") == "task_give_up" for e in events9), events9
print("§9 OK")

# ==================== §10 Jev 直驾循环 ====================
print("== §10 Jev 直驾 ==")


class FakeJev10:
    def __init__(self, ans):
        self.enabled = True
        self.ans = ans
        self.calls = []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return self.ans


def _mk_match10(ans):
    d10 = tempfile.mkdtemp()
    os.makedirs(os.path.join(d10, "tasks"))
    os.makedirs(os.path.join(d10, "logs"))
    m10 = SimpleNamespace(
        dir=d10, log=lambda k, t: None, home_xy=(1500, 1000),
        squad_st=SimpleNamespace(squads={}, move_map={}, mission_stale=False),
        wiped=set(), hp_hist={}, seen_enemies=set(), jev_cd={},
        drive_last=0.0, drive_mem={}, cmd_dedup={}, drive_down=0.0,
        unsure_ts={}, task_chat_ts={},
        defeated=False, jev=FakeJev10(ans), ex=None, world=None, seats=[])
    m10.engine = predicates.TaskEngine(os.path.join(d10, "tasks"),
                                       lambda t: True, lambda ev: None)
    return m10


tks10 = [U("c_tank", 1400 + i * 30, 900, act="idle") for i in range(6)]
foe10 = U("tank", 1700, 950)   # 距队质心 ~280px（有现场）
foe10b = U("tank", 1720, 960)
snap10 = mk_snap(tks10, enemy=[foe10, foe10b])
sent10 = []
ctx10 = {"link": SimpleNamespace(send=lambda c: sent10.append(c) or True),
         "log_activity": lambda k, t: None,
         "cfg": {"jev_drive": True, "jev_drive_interval_sec": 3.0}}


def _mk_squad10(m, mission=None):
    m.squad_st.squads["L1"] = {"mission": mission or {"任务": "attack", "目标": [2000, 500]},
                               "members": {u["id"] for u in tks10}}


def _evs10(m):
    return open(os.path.join(m.dir, "logs", "events.jsonl"), encoding="utf-8").read()


# a. engage+focus 双置信 → attack 该目标（focus 键尾码 #id）
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.9},
                   "retreat": {"type": "noul", "noul": 0.1},
                   "focus": {"type": "choice", "choice": f"tank#{foe10['id']}",
                             "probabilities": {f"tank#{foe10['id']}": 0.8, "原地待命": 0.2}}})
_mk_squad10(m10)
sent10 = []
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time())
atk10 = [c for c in sent10 if c["cmd"] == "attack"]
assert atk10 and atk10[0]["id"] == foe10["id"] and str(tks10[0]["id"]) in atk10[0]["units"], sent10
qs = m10.jev.calls[0][1]
assert set(qs) == {"engage", "retreat", "focus"}, qs          # 三题齐问
assert "原地待命" in qs["focus"]["criteria"], qs               # 待命选项在
assert "jev_drive" in _evs10(m10) and "attack" in _evs10(m10)  # 可观测落账
# a2. state 未变 → 不重复发问（计数断言）；去重也不重复发指令
n0 = len(m10.jev.calls)
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time() + 4)   # 过节拍但 state 同
assert len(m10.jev.calls) == n0, "state 未变应复用答案不重复发问"
assert len([c for c in sent10 if c["cmd"] == "attack"]) == 1, sent10
# b. retreat 置信 → move 回主基地
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.5},
                   "retreat": {"type": "noul", "noul": 0.9},
                   "focus": {"type": "choice", "choice": "原地待命",
                             "probabilities": {"原地待命": 0.9}}})
_mk_squad10(m10)
sent10 = []
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time())
mv10 = [c for c in sent10 if c["cmd"] == "move"]
assert mv10 and mv10[0]["x"] == 1500.0 and mv10[0]["y"] == 1000.0, sent10
# c. engage 拒战(≤0.35) 且 retreat 不置信 → 原地 stop
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.2},
                   "retreat": {"type": "noul", "noul": 0.1},
                   "focus": {"type": "choice", "choice": "原地待命",
                             "probabilities": {"原地待命": 0.9}}})
_mk_squad10(m10)
sent10 = []
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time())
assert [c["cmd"] for c in sent10] == ["stop"], sent10
# d. 关键题 unsure → 维持上一动作（不发指令）+ 记事件
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.5},
                   "retreat": {"type": "noul", "noul": 0.1},
                   "focus": {"type": "choice", "choice": f"tank#{foe10['id']}",
                             "probabilities": {f"tank#{foe10['id']}": 0.9}}})
_mk_squad10(m10)
sent10 = []
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time())
assert not sent10, sent10
assert "维持上一动作" in _evs10(m10), _evs10(m10)
# e. engage 置信但 focus 选待命 → attackmove 任务目标点
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.9},
                   "retreat": {"type": "noul", "noul": 0.1},
                   "focus": {"type": "choice", "choice": "原地待命",
                             "probabilities": {"原地待命": 0.85}}})
_mk_squad10(m10)
sent10 = []
cliloop._jev_drive_tick(ctx10, m10, snap10, time.time())
am10 = [c for c in sent10 if c["cmd"] == "attackmove"]
assert am10 and am10[0]["x"] == 2000.0 and am10[0]["y"] == 500.0, sent10
# f. jev down → 本轮跳过不炸 + jev_down 节流事件
m10 = _mk_match10(None)
_mk_squad10(m10)
sent10 = []
t10 = time.time()
cliloop._jev_drive_tick(ctx10, m10, snap10, t10)
cliloop._jev_drive_tick(ctx10, m10, snap10, t10 + 4)   # state 未变（mem 未写）但 down 仍再问；
assert not sent10, sent10                              # 没发任何指令
assert _evs10(m10).count("jev_down") == 1, _evs10(m10)  # 30s 节流只记一条
# g. 完全平静的队不问（无近敌+mission 非 attack/harass）
m10 = _mk_match10({"engage": {"type": "noul", "noul": 0.9}})
_mk_squad10(m10, mission={"任务": "hold", "目标": [1500, 1000]})
cliloop._jev_drive_tick(ctx10, m10, mk_snap(tks10), time.time())  # 无敌快照
assert not m10.jev.calls, "平静队不应发问"
print("§10 OK")

# ==================== §11 cli 构成消费的厂科技档过滤 ====================
print("== §11 构成消费厂科技档过滤 ==")
fac11 = U("landFactory", 1500, 760, bld=True, q=0)
snap11 = mk_snap([U("commandCenter", 1500, 1000, bld=True, q=0),
                  U("builder", 1500, 1000, act="idle"), fac11], funds=5000)


def _mk_ex11():
    ex = mk_ex(True)
    ex.set_economy_goal(extractors=0, builders=1)
    ex.routine_defense = False
    return ex


# 厂真实动作表未知（未学到/未实证 T2）：T2 兵跳过不试，只产 T1
ex11 = _mk_ex11()
ex11.set_army_goal(composition={"u_missileTank": 4, "u_c_tank": 8})
cmds11 = ex11.tick(snap11)
trains11 = [c.get("action") for c in cmds11 if c["cmd"] == "train"]
assert "u_missileTank" not in trains11, f"未知档厂不应试 T2 兵（实机刷屏病根）: {trains11}"
assert "u_c_tank" in trains11, f"T1 主战应照产: {trains11}"
# 学到真实表（含 missileTank=实证该厂可产）→ 放行
ex11b = _mk_ex11()
ex11b.set_army_goal(composition={"u_missileTank": 2})
ex11b.fac_avail[exec_mod.fac_key("landFactory", 1500, 760)] = \
    ({"u_missileTank", "u_c_tank"}, time.time())
cmds11b = ex11b.tick(snap11)
assert any(c.get("action") == "u_missileTank" for c in cmds11b if c["cmd"] == "train"), cmds11b
# 换代继承实证 T2（fac_upgraded）→ pedia 全集可用
ex11c = _mk_ex11()
ex11c.set_army_goal(composition={"u_missileTank": 2})
ex11c.fac_upgraded.add(exec_mod.fac_key("landFactory", 1500, 760))
cmds11c = ex11c.tick(snap11)
assert any(c.get("action") == "u_missileTank" for c in cmds11c if c["cmd"] == "train"), cmds11c
# 旧路径（cli_mode=False）语义不变：未知档照 pedia 合并表试 missileTank
# （厂标 T2 避开⑤升级窗口占厂——110 一拍挤掉⑦是既有让路语义，非本测目标）
ex11d = mk_ex(False)
ex11d.set_economy_goal(extractors=0, builders=1)
ex11d.routine_defense = False
ex11d.fac_upgraded.add(exec_mod.fac_key("landFactory", 1500, 760))
ex11d.set_army_goal(composition={"u_missileTank": 2})
cmds11d = ex11d.tick(snap11)
assert any(c.get("action") == "u_missileTank" for c in cmds11d if c["cmd"] == "train"), cmds11d
print("§11 OK")

# ==================== §12 overlay 推送 + margin 规则 + unsure 节流 ====================
print("== §12 overlay/置信标定/节流 ==")
# 1. push_chat：who/文本/1s 限速/emoji 剥离
sent12 = []
ctx12 = {"link": SimpleNamespace(send=lambda c: sent12.append(c) or True),
         "log_activity": lambda k, t: None,
         "cfg": {"jev_drive": True, "jev_drive_interval_sec": 3.0}}
cliloop._CHAT_LAST.clear()
cliloop.push_chat(ctx12, "指挥", "【开局】姿态expand🚀 主攻[300,300]")
ov = [c for c in sent12 if c["cmd"] == "overlay"]
assert ov and ov[0]["who"] == "指挥" and "🚀" not in ov[0]["chat"], ov
cliloop.push_chat(ctx12, "指挥", "第二行")
assert len([c for c in sent12 if c["cmd"] == "overlay" and c["who"] == "指挥"]) == 1, "1s 限速"
cliloop.push_chat(ctx12, "Jev", "L1 打 tank#55")  # 另一路不受限
assert any(c.get("who") == "Jev" for c in sent12)

# 2. 直驾实际执行动作 → who="Jev" 推聊天台；unsure → 不推
cliloop._CHAT_LAST.clear()
m12 = _mk_match10({"engage": {"type": "noul", "noul": 0.9},
                   "retreat": {"type": "noul", "noul": 0.1},
                   "focus": {"type": "choice", "choice": f"tank#{foe10['id']}",
                             "probabilities": {f"tank#{foe10['id']}": 0.8, "原地待命": 0.2}}})
_mk_squad10(m12)
sent10 = []
cliloop._jev_drive_tick(ctx10, m12, snap10, time.time())
ovj = [c for c in sent10 if c["cmd"] == "overlay" and c.get("who") == "Jev"]
assert ovj and "打" in ovj[0]["chat"] and f"#{foe10['id']}" in ovj[0]["chat"], sent10
m12u = _mk_match10({"engage": {"type": "noul", "noul": 0.5},
                    "retreat": {"type": "noul", "noul": 0.1},
                    "focus": {"type": "choice", "choice": "原地待命",
                              "probabilities": {"原地待命": 0.9}}})
_mk_squad10(m12u)
sent10 = []
cliloop._jev_drive_tick(ctx10, m12u, snap10, time.time())
assert not [c for c in sent10 if c["cmd"] == "overlay"], "unsure 不推聊天台"

# 3. 任务迁移推送：who="任务" + 同任务 10s 节流
cliloop._CHAT_LAST.clear()
sent12 = []
m12t = SimpleNamespace(task_chat_ts={})
evt = {"ev": "task", "id": "op-econ", "from": "active", "to": "done"}
cliloop._push_task(ctx12, m12t, evt)
ovt = [c for c in sent12 if c["cmd"] == "overlay" and c.get("who") == "任务"]
assert ovt and "op-econ" in ovt[0]["chat"] and "done" in ovt[0]["chat"], sent12
cliloop._CHAT_LAST.pop("任务", None)
cliloop._push_task(ctx12, m12t, dict(evt, **{"from": "done", "to": "aborted"}))
assert len([c for c in sent12 if c.get("who") == "任务"]) == 1, "同任务 10s 节流"
m12t.task_chat_ts["op-econ"] -= 11
cliloop._CHAT_LAST.pop("任务", None)
cliloop._push_task(ctx12, m12t, dict(evt, **{"from": "done", "to": "aborted"}))
assert len([c for c in sent12 if c.get("who") == "任务"]) == 2, "节流到期再放行"

# 4. margin 规则：顶概率 ≥ 地板 且 ≥ margin×次高
assert not jev_mod.unsure_choice({"probabilities": {"a": 0.4, "b": 0.2}}, top=0.35, margin=1.5)
assert jev_mod.unsure_choice({"probabilities": {"a": 0.4, "b": 0.3}}, top=0.35, margin=1.5)
# 旧默认值行为不变（top=0.6，无 margin）
assert not jev_mod.unsure_choice({"probabilities": {"a": 0.7}})
assert jev_mod.unsure_choice({"probabilities": {"a": 0.5}})
# noul 阈值参数化：0.60/0.40 带
assert not jev_mod.unsure_noul({"noul": 0.7}, lo=0.40, hi=0.60)
assert jev_mod.unsure_noul({"noul": 0.55}, lo=0.40, hi=0.60)
assert jev_mod.unsure_noul({"noul": 0.5})  # 旧默认 0.35/0.65 带

# 5. unsure 节流：事件流同队同结论 15s 只记一条
m12e, _logs12e = _mk_match8({"retreat": {"type": "noul", "noul": 0.5}})
m12e.squad_st.squads["L1"] = {"mission": None, "members": {u["id"] for u in tks8}}
t12 = time.time()
cliloop._jev_tick(ctx8, m12e, mk_snap(tks8), t12)            # 建档
cliloop._jev_tick(ctx8, m12e, mk_snap(hurt), t12 + 5)        # 受击触发 → unsure 记一条
m12e.jev_cd["L1"] = 0                                        # 绕开事件流 20s 冷却（只测 unsure 节流）
cliloop._jev_tick(ctx8, m12e, mk_snap([dict(u, hp=66) for u in tks8]), t12 + 10)  # 再跌再触发
ev12 = open(os.path.join(m12e.dir, "logs", "events.jsonl"), encoding="utf-8").read()
assert ev12.count("jev_unsure") == 1, ev12  # 第二次 unsure 被 15s 节流
# 直驾 unsure 节流：state 微变（敌移位）再触发，同结论 15s 内不重复记/不刷屏
m12d = _mk_match10({"engage": {"type": "noul", "noul": 0.5},
                    "retreat": {"type": "noul", "noul": 0.1},
                    "focus": {"type": "choice", "choice": "原地待命",
                              "probabilities": {"原地待命": 0.9}}})
_mk_squad10(m12d)
cliloop._CHAT_LAST.clear()
sent10 = []
cliloop._jev_drive_tick(ctx10, m12d, snap10, t12)
snap10b = mk_snap(tks10, enemy=[U("tank", 1750, 950), U("tank", 1720, 960)])  # 敌移位=state 变
cliloop._jev_drive_tick(ctx10, m12d, snap10b, t12 + 4)
assert len(m12d.jev.calls) == 2, "state 变了应再问"
evd = open(os.path.join(m12d.dir, "logs", "events.jsonl"), encoding="utf-8").read()
assert evd.count("jev_drive") == 1, evd  # unsure 同结论第二次被节流（不记不刷）
print("§12 OK")

# ==================== §13 分席会话 + 中层名单 + ref/units.md + 中层 prompt ====================
print("== §13 分席会话/中层名单/units.md/prompt ==")
tmp13 = tempfile.mkdtemp()
# 1. 分席会话：首拍全新 spawn（无 -c/-S）→ 抓 sid → 次拍 -S <sid>；两席各拍各的互不串
runs13 = []
out13 = '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"布局全文"}]}}'
_orig_run = cliseat.subprocess.run


def fake_run13(cmd, **kw):
    runs13.append(list(cmd))
    sidline = json.dumps({"type": "session.resume_hint",
                          "session_id": f"session_{len(runs13):03d}"})
    return SimpleNamespace(stdout=sidline + "\n" + out13, stderr="", returncode=0)


cliseat.subprocess.run = fake_run13
try:
    seatA = cliseat.Seat("commander", tmp13, {}, lambda ev: None, lambda w, t: None)
    seatA._run()
    assert "-c" not in runs13[0] and "-S" not in runs13[0], runs13[0]  # 首拍全新 spawn
    assert seatA.sid == "session_001", seatA.sid
    seatA._run()
    assert "-S" in runs13[1] and "session_001" in runs13[1], runs13[1]  # 续拍按 id
    seatB = cliseat.Seat("land", tmp13, {"cli_middle_enabled": True},
                         lambda ev: None, lambda w, t: None)
    seatB._run()
    assert "-c" not in runs13[2] and "-S" not in runs13[2], runs13[2]
    assert seatB.sid == "session_003", seatB.sid  # 第三次 spawn（seatA 占了 001/002）
    seatB._run()
    assert "session_003" in runs13[3] and "session_001" not in runs13[3], runs13[3]  # 互不串
finally:
    cliseat.subprocess.run = _orig_run
# session_id 解析：meta 事件优先 / 文本尾部 kimi -r 兜底 / 都没有=None
assert cliseat.extract_session_id('{"type":"session.resume_hint","session_id":"session_m1"}') == "session_m1"
assert cliseat.extract_session_id("……\nTo resume this session: kimi -r session_xyz789") == "session_xyz789"
assert cliseat.extract_session_id("nothing here") is None

# 2. cli_middle_seats 名单生效（总闸 × 名单）
c13 = {"cli_middle_enabled": True, "cli_middle_seats": "land,build"}
assert cliseat.Seat("land", tmp13, c13, lambda ev: None, lambda w, t: None).enabled
assert cliseat.Seat("build", tmp13, c13, lambda ev: None, lambda w, t: None).enabled
assert not cliseat.Seat("navy", tmp13, c13, lambda ev: None, lambda w, t: None).enabled
assert not cliseat.Seat("air", tmp13, c13, lambda ev: None, lambda w, t: None).enabled
assert not cliseat.Seat("land", tmp13, {"cli_middle_enabled": False, "cli_middle_seats": "land"},
                        lambda ev: None, lambda w, t: None).enabled  # 总闸关全关

# 3. ref/units.md：动作 ID + 价格都在
tmp13u = tempfile.mkdtemp()
cliloop._write_units_ref(tmp13u, PEDIA)
utxt = open(os.path.join(tmp13u, "ref", "units.md"), encoding="utf-8").read()
assert "u_c_tank" in utxt and "b_extractorT1" in utxt, utxt
assert "$400" in utxt and "$700" in utxt, utxt  # c_tank/extractorT1 价格
assert "别发明" in utxt

# 4. 中层 prompt 冒烟：通用段纪律 + 分域段
pl13 = cliseat._middle_prompt("land")
assert "谓词词表" in pl13 and "不许微操" in pl13 and "ref/units.md" in pl13, pl13[:200]
pb13 = cliseat._middle_prompt("build")
assert "position" in pb13 and "阵地单" in pb13 and "plan_params" in pb13, pb13[:200]
assert "待 seaFactory" in cliseat._middle_prompt("navy")
assert "ref/units.md" in cliseat.COMMANDER_PROMPT
print("§13 OK")

print("ALL PASS (test_cli：opening/谓词/状态机/jev/executor裁剪/cliseat/物化闭环/jev事件流/热身期修复/jev直驾/构成科技档过滤/overlay与标定/分席会话与中层)")
