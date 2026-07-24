# -*- coding: utf-8 -*-
"""多 agent 架构离线验证：mock llm.chat，跑真实三循环，断言指令路由/计划书/构成共享。"""
import json
import sys
import threading
import time
from collections import deque

sys.path.insert(0, "bridge")
import pilot
import multiagent
import executor as exec_mod
import danger as danger_mod
import reflex as reflex_mod

TILE = 20
MAP = {"tilesW": 100, "tilesH": 100, "tileW": TILE, "tileH": TILE,
       "rows": ["." * 100 for _ in range(100)],
       "resources": [{"x": 1300, "y": 1000}, {"x": 1350, "y": 1050}], "path": ""}

PEDIA = {"units": [
    {"name": "commandCenter", "price": 3000, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 4000, "builds": ["u_builder"]},
    {"name": "builder", "price": 250, "tech": 1, "building": False, "builder": True,
     "move": "LAND", "hp": 170, "builds": ["b_extractorT1", "b_landFactory", "b_c_turret_t1"]},
    {"name": "extractorT1", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 800, "builds": []},
    {"name": "landFactory", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 2000, "builds": ["u_c_tank", "u_missileTank", "u_heavyTank"]},
    {"name": "c_tank", "price": 400, "tech": 1, "building": False, "builder": False,
     "move": "LAND", "hp": 210, "builds": []},
    {"name": "missileTank", "price": 700, "tech": 2, "building": False, "builder": False,
     "move": "LAND", "hp": 1100, "builds": []},
]}

_uid = [0]
def U(t, x, y, bld=False, act=None, q=None):
    _uid[0] += 1
    u = {"id": _uid[0], "t": t, "x": x, "y": y, "hp": 100, "maxHp": 100}
    if bld: u["bld"] = True
    if act is not None: u["act"] = act
    if q is not None: u["q"] = q
    return u

CC = U("commandCenter", 1500, 1000, bld=True, q=0)
BLD = U("builder", 1500, 1000, act="idle")
FAC = U("landFactory", 1500, 740, bld=True, q=0)
TANKS = [U("c_tank", 1400 + i * 30, 900) for i in range(10)]
ETANK = U("tank", 300, 300)  # 敌兵压在池边会正确触发工单的危险拦截（曾误当派发失败）
SNAP = {"tick": 100, "own": [CC, BLD, FAC] + TANKS, "allies": [], "enemy": [ETANK],
        "team": {"id": 0, "funds": 5000, "ai": False}, "delegated": [u["id"] for u in [CC, BLD, FAC] + TANKS]}


class FakeLink:
    def __init__(self):
        self.lock = threading.Lock()
        self.map_version = 1
        self.cmd_results = deque(maxlen=20)
        self.user_msgs = deque(maxlen=10)
        self.sent = []

    def snapshot(self):
        return SNAP

    def map_message(self):
        return MAP

    def ai_control_state(self):
        return {"enabled": True}

    def send(self, cmd):
        self.sent.append(cmd)
        return True


chat_log = []
cmdr_calls = [0]
PLAN_TEXT = ("【阶段】开局：3池1厂标准开，经济优先\n【经济】采集器补到4，陆厂保持2座不断产\n"
             "【配兵】坦克为主战，混两成防空\n【攻防】expand 稳开，坦克成群后向东推进\n"
             "【布局】厂区沿主基地侧翼展开\n"
             "```json\n{\"阶段\":\"开局\",\"采集器目标\":4,\"建造者目标\":4,\"姿态\":\"expand\"}\n```")


def fake_chat(base_url, api_key, model, messages, tools=None, timeout=45, max_retries=2):
    names = {t["function"]["name"] for t in (tools or [])}
    chat_log.append(names)
    if "set_missions" in names:  # 战斗指挥：L1 进攻 + L2 生产 + 非法编号 S9（应被忽略）
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "m1", "function": {"name": "set_missions",
                                      "arguments": json.dumps({"squads": [
                                          {"id": "L1", "任务": "attack", "目标": [1000, 900], "编成": "主战坦克"},
                                          {"id": "L2", "任务": "produce", "动作": "u_c_tank", "数量": 12},
                                          {"id": "S9", "任务": "attack", "目标": [0, 0]}]})}}]}
    if "set_work_order" in names:  # 建造指挥：采集器工单 + 建造者目标
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "w1", "function": {"name": "set_work_order",
                                      "arguments": json.dumps(
                                          {"tasks": [{"action": "b_extractorT1", "slot": "pool:0"}],
                                           "builders_target": 4})}}]}
    if "attack" in names:  # squad chat（S1）：执行 attack
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "s1", "function": {"name": "attack",
                                      "arguments": json.dumps({"x": 1000, "y": 900})}}]}
    # 总指挥：首轮只 query（模拟 reasoner 查资料癖），应答后才给计划书——
    # 覆盖"query 后必须再调用拿真正决策"的回归
    cmdr_calls[0] += 1
    if cmdr_calls[0] == 1:
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "q1", "function": {"name": "query", "arguments": json.dumps({"topic": "roster"})}}]}
    return {"content": PLAN_TEXT, "tool_calls": [], "_usage": {}}


multiagent.llm.chat = fake_chat

link = FakeLink()
ex = exec_mod.Executor(MAP, PEDIA, lambda k, t: None)
ex.autopilot = False
ex.routine = True  # 生产环境由 pilot.py 在 multiagent.run 前设置（routine_layer），测试照摆
known_actions = set()
for pu in PEDIA["units"]:
    known_actions.update(pu.get("builds", []))

ctx = {
    "link": link,
    "cfg": {"base_url": "http://x", "model": "reasoner-x", "model_fast": "chat-x",
            "timeout_sec": 10, "commander_interval_sec": 50, "build_interval_sec": 10,
            "combat_interval_sec": 6, "fast_interval_sec": 2.5},
    "api_key": "test", "pedia": PEDIA,
    "map_msg": MAP, "map_msg_holder": {"m": MAP},
    "known_actions": known_actions, "naval_types": set(),
    "exec_holder": {"ex": ex}, "reflex": reflex_mod.Reflex(lambda k, t: None),
    "danger_map": danger_mod.DangerMap(), "conn_comp": None, "conn_n": 0,
    "mine_spawn": (1500, 1000), "enemy_spawns": [(300, 300)],
    "log_activity": lambda k, t: None,
    "answer_query": pilot.answer_query, "strategic_grid": pilot.strategic_grid,
    "ground_combat_ids": pilot.ground_combat_ids, "naval_split": pilot.naval_split,
    "clamp": pilot.clamp, "pedia_production_table": pilot.pedia_production_table,
    "downsample": pilot.downsample,
    "ai_enabled": lambda: True,
    "role_enabled": lambda name: True,
}

st = multiagent.start(ctx)


# 模拟 pilot.executor_worker：驱动 ex.tick（生产环境由 pilot 起 rw-executor 线程）
def _pump():
    while True:
        with ex.lock:
            for ec in ex.tick(SNAP):
                link.sent.append(ec)
        time.sleep(0.2)


threading.Thread(target=_pump, daemon=True).start()
time.sleep(13)  # stagger: commander 1s / build 4s / combat(指挥) 7s + squad chat 起线程

builds = [c for c in link.sent if c["cmd"] in ("build", "buildq")]
atks = [c for c in link.sent if c["cmd"] == "attackmove"]
trains = [c for c in link.sent if c["cmd"] == "train"]
# 建造指挥：工单接收且规则层派发采集器
assert any(t["action"] == "b_extractorT1" for t in ex.work_order), \
    f"建造指挥工单应有采集器: {ex.work_order}"
assert builds, f"规则层应派发建造指令: {link.sent}"
# 战斗指挥：任务书落盘 + 非法编号忽略
assert (st.squads["L1"]["mission"] or {}).get("任务") == "attack", f"L1 任务: {st.squads}"
assert (st.squads["L2"]["mission"] or {}).get("任务") == "produce", f"L2 任务: {st.squads}"
assert all((sq["mission"] or {}).get("目标") != [0.0, 0.0] for sq in st.squads.values()), \
    f"S9 非法编号不应落盘: {st.squads}"
# 小队归队（军种三层）：坦克全进陆军队（L1/L2 拆分后并集全覆盖）
land_members = set().union(*(sq["members"] for sid, sq in st.squads.items()
                             if sid.startswith("L"))) if st.squads else set()
assert set(u["id"] for u in TANKS) <= land_members, \
    f"坦克应全归陆军队: {[(k, v['members']) for k, v in st.squads.items()]}"
# squad chat：attack 且 units 限定本队
assert atks and "units" in atks[0], f"squad chat 应带 units 限定: {link.sent}"
# 生产条目 → train
assert any(t["action"] == "u_c_tank" for t in trains), f"produce 应产坦克: {link.sent}"
# 总指挥：计划书 + 参数块消费
assert "【阶段】" in st.plan, f"总指挥应产出计划书: {st.plan!r}"
assert st.plan_params.get("采集器目标") == 4, f"参数块应解析: {st.plan_params}"
assert ex.econ_goal["extractors"] == 4, f"参数块应被规则层消费: {ex.econ_goal}"
assert ex.econ_goal["builders"] == 4, f"建造者目标应生效: {ex.econ_goal}"
roles_seen = {("combat" if "set_missions" in n else "build" if "set_work_order" in n else "squad"
               if "attack" in n else "commander") for n in chat_log}
assert {"commander", "build", "combat", "squad"} <= roles_seen, f"四路都应跑过: {roles_seen}"
print("sent:", link.sent)
print("plan:", st.plan[:60])
print("squads:", {k: (v["mission"], sorted(v["members"])) for k, v in st.squads.items()})

# ---- 闭环规则 1：点位黑名单（反射召回点 10 分钟禁建） ----
st.site_blocks[(int(1500 // 400), int(1400 // 400))] = time.time() + 600
g1 = {"fail_perm": set(), "fail_temp": {}, "recent_builds": {}}
cmd, desc = multiagent._translate_build(
    ctx, st, "build", {"action": "b_extractorT1", "x": 1510, "y": 1410}, SNAP, g1)
assert cmd is None and "禁建" in (desc or ""), f"黑名单点应拦截: {cmd} {desc}"

# ---- 闭环规则 2：失败回执自学习（300s 封禁 + 跨域需求板） ----
g2 = {"fail_perm": set(), "fail_temp": {}, "recent_builds": {}}
multiagent._learn_receipts({"name": "combat"},
                           ["✗ train: 该工厂没有此动作: u_heavyTank；可用: u_c_tank"], g2, st)
assert g2["action_bad"].get("u_heavyTank", 0) > time.time(), "失败动作应封 300s"
cmd, desc = multiagent._translate_combat(ctx, st, "train", {"action": "u_heavyTank", "factory": 3}, SNAP, g2)
assert cmd is None and "封禁" in (desc or ""), f"封禁动作应拦截: {cmd} {desc}"
assert any("u_heavyTank" in r for r in st.requests), "需求板应挂 T2 升级需求"

# ---- 闭环规则 3：同值去重（rally 坐标未变跳过） ----
r1 = multiagent._translate_combat(ctx, st, "rally", {"x": 1700, "y": 1200}, SNAP, g2)
r2 = multiagent._translate_combat(ctx, st, "rally", {"x": 1705, "y": 1205}, SNAP, g2)
assert "跳过" in (r2[1] or ""), f"同值 rally 应跳过: {r1} {r2}"

# ---- u_ 前缀归一化（LLM 省略前缀不再被误判拦截） ----
g3 = {"fail_perm": set(), "fail_temp": {}, "recent_builds": {}}
cmd, desc = multiagent._translate_combat(ctx, st, "train", {"action": "heavyTank", "factory": 3}, SNAP, g3)
assert cmd and cmd["action"] == "u_heavyTank", f"u_ 前缀应归一化: {cmd} {desc}"

# ---- train 回执分域（失败回执的可用列表不得污染判域） ----
assert multiagent._train_domain("该工厂没有此动作: u_heavyTank；可用: c_1,u_builder,u_scout") == "combat"
assert multiagent._train_domain("landFactory train u_c_tank") == "combat"
assert multiagent._train_domain("commandCenter train u_builder") == "build"
assert multiagent._train_domain("seaFactory train u_builderShip") == "build"
assert multiagent._train_domain("landFactory train 110") == "build"
assert multiagent._train_domain("动作当前不可用（科技/条件不满足）: extractorT2_0") == "build"

print("闭环规则 ALL PASS")
print("ALL PASS")
