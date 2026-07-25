# -*- coding: utf-8 -*-
"""建造 chat 层离线验证（mock llm.chat）：
①域让位（defense 有员则不自动派，econ 无员照自动派）②合法 build 输出→指令+落账
③非法锚点→拒绝 ④hold→不动 ⑤45s 回退期→规则层恢复自动派。
用法: python tools/test_buildchat.py
"""
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
     "move": "LAND", "hp": 170,
     "builds": ["b_extractorT1", "b_landFactory", "b_c_turret_t1", "b_c_antiAirTurret"]},
    {"name": "extractorT1", "price": 700, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 800, "builds": []},
    {"name": "c_turret_t1", "price": 750, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 900, "builds": []},
    {"name": "c_antiAirTurret", "price": 800, "tech": 1, "building": True, "builder": False,
     "move": "NONE", "hp": 900, "builds": []},
    {"name": "c_tank", "price": 400, "tech": 1, "building": False, "builder": False,
     "move": "LAND", "hp": 210, "builds": []},
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
BUILDERS = [U("builder", 1500 + i * 20, 1000, act="idle") for i in range(4)]
TANKS = [U("c_tank", 1400 + i * 30, 900) for i in range(6)]
ETANK = U("tank", 300, 300)  # 远敌：近敌排序/射程判定路径的回归（曾因 dict 比较崩循环）
SNAP = {"tick": 100, "own": [CC] + BUILDERS + TANKS, "allies": [], "enemy": [ETANK],
        "team": {"id": 0, "funds": 9000, "ai": False},
        "delegated": [u["id"] for u in [CC] + BUILDERS + TANKS]}


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
    def send(self, cmd):
        self.sent.append(cmd)
        return True


RESP = []  # 预置的 LLM 应答（build JSON 文本）


def fake_chat(base_url, api_key, model, messages, tools=None, timeout=45, max_retries=2):
    if RESP:
        return {"content": RESP.pop(0), "_usage": {}}
    return {"content": '{"action":"hold","reason":"没排应答"}', "_usage": {}}


multiagent.llm.chat = fake_chat

link = FakeLink()
ex = exec_mod.Executor(MAP, PEDIA, lambda k, t: None)
ex.autopilot = False
ex.routine = True
known = set()
for pu in PEDIA["units"]:
    known.update(pu.get("builds", []))
ctx = {
    "link": link, "cfg": {"base_url": "http://x", "model": "m", "model_fast": "mf",
                          "timeout_sec": 5, "combat_interval_sec": 1,
                          "builder_chat_enabled": True, "builder_chat_cap": 4,
                          "builder_chat_cadence": 1},
    "api_key": "k", "pedia": PEDIA, "map_msg": MAP, "map_msg_holder": {"m": MAP},
    "known_actions": known, "naval_types": set(),
    "exec_holder": {"ex": ex}, "reflex": reflex_mod.Reflex(lambda k, t: None),
    "danger_map": danger_mod.DangerMap(), "conn_comp": None, "conn_n": 0,
    "mine_spawn": (1500, 1000), "enemy_spawns": [],
    "log_activity": lambda k, t: None,
    "answer_query": pilot.answer_query, "strategic_grid": pilot.strategic_grid,
    "clamp": pilot.clamp, "downsample": pilot.downsample,
    "ai_enabled": lambda: True, "role_enabled": lambda name: True,
}
st = multiagent.SharedState(ctx)


def _yield(domain):
    if not ctx["cfg"].get("builder_chat_enabled"):
        return False
    bg = st.bgroups.get(domain)
    return bool(bg and bg["members"] and time.time() > bg["fallback_until"])


ctx["chat_yield_fn"] = _yield
ex.chat_yield_fn = _yield

# 初始化：一拍让 front_dir/安全池/占位图就位
with ex.lock:
    ex.tick(SNAP)

# 工单：防御(turret) + 经济(extractor)
ex.set_work_order([{"action": "b_c_turret_t1", "slot": "defense:0", "note": "防线"},
                   {"action": "b_extractorT1", "slot": "pool:0", "note": "经济"}], SNAP)
tur_idx = ex.work_order[0]["idx"]
ext_idx = ex.work_order[1]["idx"]

# ① 域让位：defense 组有员 → turret 不自动派；econ 无员 → extractor 自动派
st.bgroups["defense"] = multiagent._new_bgroup()
st.bgroups["defense"]["members"] = {BUILDERS[0]["id"], BUILDERS[1]["id"]}
st.bgroups["econ"] = multiagent._new_bgroup()
with ex.lock:
    cmds = ex.tick(SNAP)
auto_build = [c for c in cmds if c["cmd"] in ("build", "buildq")]
assert any("extractor" in str(c) for c in auto_build), f"econ 无员应自动派采集器: {cmds}"
assert not any("turret" in str(c) for c in auto_build), f"defense 有员应让位不派炮塔: {cmds}"
# 清掉 econ 自动派的任务，免得干扰后面断言
for t in ex.work_order:
    if t["idx"] == ext_idx:
        t["status"] = "done"
print("① 域让位 OK")

# ② 合法 build：mock 返回 build turret@弧位D → link.sent 有 build + 任务落账
RESP.append('{"action":"build","idx":%d,"anchor":"弧位D","reason":"先起防线"}' % tur_idx)
th = threading.Thread(target=multiagent._bchat_loop, args=(ctx, st, "defense"), daemon=True)
th.start()
time.sleep(3.5)
chat_build = [c for c in link.sent if c["cmd"] == "build" and c["action"] == "b_c_turret_t1"]
assert chat_build, f"chat 合法 build 应发出: {link.sent}"
task = next(t for t in ex.work_order if t["idx"] == tur_idx)
assert task["status"] == "assigned" and "chat派" in task["note"], f"任务应落账: {task}"
assert st.bgroups["defense"]["last_act"] > 0, "last_act 应更新"
print("② 合法 build 执行+落账 OK")

# ③ 非法锚点：mock 返回 火星 → 无新增 build
ex.work_order.append({"idx": 90, "action": "b_c_antiAirTurret", "slot": "defense:1",
                      "note": "", "status": "pending"})
n0 = len([c for c in link.sent if c["cmd"] == "build"])
RESP.append('{"action":"build","idx":90,"anchor":"火星","reason":"瞎编"}')
time.sleep(2.5)
n1 = len([c for c in link.sent if c["cmd"] == "build"])
assert n1 == n0, "非法锚点不应发出 build"
assert any("非法" in a for a in st.bgroups["defense"]["actions"]), \
    f"非法应记入最近行动: {list(st.bgroups['defense']['actions'])}"
print("③ 非法锚点拒绝 OK")

# ④ hold：无动作
RESP.append('{"action":"hold","reason":"先观望"}')
time.sleep(2.0)
n2 = len([c for c in link.sent if c["cmd"] in ("build", "move", "resume")])
assert n2 == n1, "hold 不应发任何指令"
print("④ hold 不动 OK")

# ⑤ 回退：组 45s 静默（fallback_until 内）→ 规则层恢复自动派该域
th._dummy = None  # 线程仍在跑，无害
bg = st.bgroups["defense"]
bg["fallback_until"] = time.time() + 45
with ex.lock:
    cmds = ex.tick(SNAP)
aa_build = [c for c in cmds if c["cmd"] in ("build", "buildq") and "antiAir" in str(c)]
assert aa_build, f"回退期规则层应恢复自动派防空: {cmds}"
print("⑤ 45s 回退规则层接管 OK")

print("ALL PASS")
