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
     "move": "NONE", "hp": 2000, "builds": ["u_c_tank", "u_missileTank"]},
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
TANKS = [U("c_tank", 1400 + i * 30, 900) for i in range(3)]
ETANK = U("tank", 1000, 900)
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
def fake_chat(base_url, api_key, model, messages, tools=None, timeout=45, max_retries=2):
    names = {t["function"]["name"] for t in (tools or [])}
    chat_log.append(names)
    if "attack" in names:  # 战斗 agent：定构成 + 军产 + 进攻
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "c1", "function": {"name": "set_composition",
                                      "arguments": json.dumps({"composition": {"u_c_tank": 6}})}},
            {"id": "c3", "function": {"name": "train",
                                      "arguments": json.dumps({"action": "u_c_tank", "factory": 3})}},
            {"id": "c2", "function": {"name": "attack_nearest", "arguments": "{}"}}]}
    if "build" in names:  # 建造 agent：盖采集器 + 试图产坦克（应被拦截）
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "b1", "function": {"name": "build",
                                      "arguments": json.dumps({"action": "b_extractorT1"})}},
            {"id": "b2", "function": {"name": "train",
                                      "arguments": json.dumps({"action": "u_heavyTank", "factory": 3})}}]}
    # 总指挥：首轮只 query（模拟 reasoner 查资料癖），应答后才给计划书——
    # 覆盖"query 后必须再调用拿真正决策"的回归
    cmdr_calls[0] += 1
    if cmdr_calls[0] == 1:
        return {"content": "", "_usage": {}, "tool_calls": [
            {"id": "q1", "function": {"name": "query", "arguments": json.dumps({"topic": "roster"})}}]}
    return {"content": "【阶段】开局：3池1厂标准开，经济优先\n【经济】采集器补到4，陆厂保持2座不断产\n"
                       "【配兵】坦克为主战，混两成防空\n【攻防】expand 稳开，坦克成群后向东推进\n"
                       "【布局】厂区沿主基地侧翼展开", "tool_calls": [], "_usage": {}}


multiagent.llm.chat = fake_chat

link = FakeLink()
ex = exec_mod.Executor(MAP, PEDIA, lambda k, t: None)
ex.autopilot = False
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
    "aa_follow": pilot.aa_follow, "nearest_water": pilot.nearest_water,
    "clamp": pilot.clamp, "pedia_production_table": pilot.pedia_production_table,
    "downsample": pilot.downsample,
    "ai_enabled": lambda: True,
}

st = multiagent.start(ctx)
time.sleep(9.5)  # stagger: commander 1s / build 4s / combat 7s

builds = [c for c in link.sent if c["cmd"] == "build"]
atks = [c for c in link.sent if c["cmd"] == "attackmove"]
trains = [c for c in link.sent if c["cmd"] == "train"]
assert builds and builds[0]["action"] == "b_extractorT1", f"建造 agent 应下采集器: {link.sent}"
assert atks, f"战斗 agent 应下进攻: {link.sent}"
assert any(t["action"] == "u_c_tank" for t in trains), f"战斗 agent 应产坦克: {link.sent}"
assert not any(t["action"] == "u_heavyTank" for t in trains), f"建造 agent 产军兵应被拦截: {link.sent}"
assert "【阶段】" in st.plan, f"总指挥应产出计划书: {st.plan!r}"
assert ex.army_goal.get("composition") == {"u_c_tank": 6}, \
    f"战斗 agent 的构成应共享给建造侧: {ex.army_goal}"
assert "attack" in chat_log[0] or True  # 三角色都被调用过
roles_seen = {("combat" if "attack" in n else "build" if "build" in n else "commander")
              for n in chat_log}
assert roles_seen == {"commander", "build", "combat"}, f"三循环都应跑过: {roles_seen}"
print("sent:", link.sent)
print("plan:", st.plan[:60])
print("composition:", ex.army_goal)
print("ALL PASS")
