#!/usr/bin/env python3
"""rw-pilot: LLM 分队指挥官。
连接 rw-agent (localhost:41234)，周期性把战场摘要交给 LLM，
把 LLM 的工具调用翻译成游戏指令。
- 委托：bridge/delegation.json {"groups":[9]} 指定 AI 指挥的编队（热更新）
- 游戏内面板：每周期向 agent 推送 AI 状态行
- 建造：build/train/rally 工具
公平性：只转发本队视野内信息；指令走游戏正常队列；限速+拟人间隔。
"""
import json
import os
import random
import socket
import sys
import textwrap
import threading
import time
import tomllib
from collections import deque

import llm
import summarize
import executor as exec_mod

try:  # Windows GBK 控制台打印 ✓/✗/⚙ 等字符会崩线程，统一改 UTF-8+替换
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST, PORT = "127.0.0.1", 41234
DELEGATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegation.json")
CONTROL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control.json")
ACTIVITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.jsonl")


def log_activity(kind: str, text: str) -> None:
    """供仪表盘窗口消费的活动流（note/cmd/alert/status/error）。"""
    try:
        with open(ACTIVITY_FILE, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "命令小队移动到世界坐标 (x,y)。用于侦察、撤退、转移。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "ids": {"type": "string", "description": "可选：只指挥这些单位（逗号分隔id，来自小队#id）；省略=整个委托小队"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attack_move",
            "description": "攻击移动到 (x,y)：沿途遭遇敌人自动交战。用于进攻、骚扰。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "ids": {"type": "string", "description": "可选：只指挥这些单位（逗号分隔id）；省略=整个委托小队"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attack_unit",
            "description": "集火攻击一个当前可见的敌单位（id 必须来自最新快照的可见敌人列表）。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build",
            "description": "命令建造者建造建筑。action 是建造动作ID（形如 b_extractorT1）。坐标可省略——省略时系统在主基地附近自动选址（推荐：采集器/工厂都省略坐标）。采集器自动选最近的空闲资源点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "建造动作ID，如 b_extractorT1"},
                    "x": {"type": "number", "description": "可选：选址锚点x，省略=主基地"},
                    "y": {"type": "number", "description": "可选：选址锚点y，省略=主基地"},
                    "builder": {"type": "integer", "description": "可选：指定建造者id（来自建造者状态#id）；省略=自动选空闲的"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "train",
            "description": "命令工厂生产单位，或命令建筑自我升级。action 是动作ID（生产形如 u_tank；升级取动作表 升级[...] 里的ID，如 extractorT2_0 或 110），factory 是工厂/建筑单位id（来自快照我方单位列表）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "factory": {"type": "integer"},
                },
                "required": ["action", "factory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rally",
            "description": "设置工厂的集结点到世界坐标 (x,y)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "factory": {"type": "integer"},
                },
                "required": ["x", "y", "factory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop",
            "description": "停止小队当前动作，原地驻防。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {"type": "string", "description": "可选：只停这些单位（逗号分隔id）；省略=整个委托小队"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note",
            "description": "记录你的分析/意图（不执行任何游戏操作，玩家可见）。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]

# 执行层托管时，LLM 只保留小队指挥类工具（build/train/rally 由 executor 自动完成）
LLM_TOOLS = [t for t in TOOLS if t["function"]["name"] not in ("build", "train", "rally")]

# ---------- 战略层工具（执行层托管时启用：只做选择题，不碰微观） ----------
STRAT_TOOLS = [
    {"type": "function", "function": {
        "name": "attack",
        "description": "指挥委托小队攻击移动到坐标 (x,y)，沿途自动交战。用于进攻/压制/抢资源点。",
        "parameters": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "harass",
        "description": "指挥小队骚扰坐标 (x,y)（攻击移动，意图为牵制/打了就走）。",
        "parameters": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "defend_home",
        "description": "把小队撤回主基地防守（被压制、基地告急或需要休整时用）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "expand",
        "description": "提高采集器目标数量（经济扩张）。extractors 为目标总数，省略=当前目标+1。",
        "parameters": {"type": "object", "properties": {"extractors": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "build_army",
        "description": "设定军队构成（动作ID:目标数量，如 {\"u_c_tank\":8,\"u_missileTank\":4}）与可选集结点。生产由执行层自动完成。",
        "parameters": {"type": "object", "properties": {
            "composition": {"type": "object"}, "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["composition"]}}},
    {"type": "function", "function": {
        "name": "note",
        "description": "一句话记录你的战略分析（不执行操作，玩家可见）。",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "query",
        "description": "查参考资料（本地数据即时回答，不占指令名额）。topic 选: roster(全单位花名册,分组+科技) / unit(单位详情,arg=单位名) / building(建筑详情,arg=名称) / prices(生产者价格与产能表) / map_overview(全图地形粗览) / map_zone(区域详图,arg=\"x,y\"世界坐标) / bases(视野内已知主基地位置)。拿不准先查再决策，每轮最多2次。",
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}, "arg": {"type": "string"}}, "required": ["topic"]}}},
]


def answer_query(topic, arg, map_msg, pedia, snap):
    """目录式资料应答器：全部本地数据生成，不调 LLM，答案紧凑。"""
    units = {u["name"]: u for u in (pedia or {}).get("units", [])}
    topic = (topic or "").strip()
    arg = (arg or "").strip()
    if topic in ("unit", "building"):
        u = units.get(arg) or next((v for k, v in units.items() if k.lower() == arg.lower()), None)
        if u is None:
            cands = [k for k in units if arg.lower() in k.lower()][:8]
            return f"未找到'{arg}'。相近: {','.join(cands) or '无'}"
        builds = list(dict.fromkeys(u.get("builds", [])))
        return (f"{u['name']}({u.get('display', '')}): ${u.get('price', '?')} T{u.get('tech', '?')} "
                f"建筑={u.get('building')} 建造者={u.get('builder')} 移动={u.get('move', '?')} "
                f"hp={u.get('hp', '?')} 动作[{','.join(builds[:24])}]")
    if topic == "prices":
        return _capability_brief(pedia)
    if topic == "roster":
        # 全单位花名册（分组+科技等级标注），供 LLM 发现"原来还有这种东西"
        groups = {"建筑": [], "陆军": [], "空军": [], "海军": [], "建造者": [], "其他": []}
        for name, u in units.items():
            if u.get("builder"):
                g = "建造者"
            elif u.get("building"):
                g = "建筑"
            else:
                mv = (u.get("move") or "").upper()
                g = "空军" if "AIR" in mv else ("海军" if ("SEA" in mv or "WATER" in mv) else "陆军")
            groups[g].append(f"{name}(${u.get('price', '?')},T{u.get('tech', '?')})")
        return "\n".join(f"{k}({len(v)}): " + ",".join(v[:40]) for k, v in groups.items() if v)
    if topic == "map_overview":
        rows = map_msg.get("rows", [])
        if not rows:
            return "(无地图)"
        f = max(1, len(rows) // 24)
        return "\n".join(r[::f] for r in rows[::f])[:1600]
    if topic == "map_zone":
        rows = map_msg.get("rows", [])
        if not rows:
            return "(无地图)"
        try:
            x, y = (float(v) for v in arg.replace("(", "").replace(")", "").split(",")[:2])
        except (ValueError, IndexError):
            return "arg 需为 'x,y' 世界坐标"
        tw, th = map_msg["tileW"], map_msg["tileH"]
        tx, ty = int(x / tw), int(y / th)
        rad = 12
        grid = "\n".join(rows[gy][max(0, tx - rad):tx + rad]
                         for gy in range(max(0, ty - rad), min(len(rows), ty + rad)))
        zone = []
        for label, key in (("我", "own"), ("敌", "enemy")):
            for u in snap.get(key, []):
                if abs(u["x"] - x) < rad * tw and abs(u["y"] - y) < rad * th:
                    zone.append(f"{label}:{u['t']}@({u['x']},{u['y']})")
        return grid[:1600] + (("\n单位: " + ",".join(zone[:20])) if zone else "")
    if topic == "bases":
        out = [f"{label}主基地@({u['x']},{u['y']})"
               for key, label in (("own", "我方"), ("allies", "盟友"))
               for u in snap.get(key, []) if u["t"] == "commandCenter"]
        return "；".join(out) or "视野内暂无已知主基地（出生点 tmx 解析待接入）"
    return "未知 topic，可选: unit/building/prices/map_overview/map_zone/bases"


def strategic_grid(map_msg, snap, n=12):
    """nxn 战略格：数字=可见敌数量，o=我方，X=敌我皆有，否则地形字符。"""
    rows = map_msg.get("rows", [])
    th = len(rows)
    tw = len(rows[0]) if th else 0
    if not tw:
        return "(无地图)"
    tile_w = map_msg["tileW"]
    world_w = tw * tile_w
    world_h = th * map_msg["tileH"]
    enemy_cells = {}
    own_cells = set()
    for u in snap.get("enemy", []):
        key = (min(n - 1, int(u["x"] / world_w * n)), min(n - 1, int(u["y"] / world_h * n)))
        enemy_cells[key] = enemy_cells.get(key, 0) + 1
    for u in snap.get("own", []):
        own_cells.add((min(n - 1, int(u["x"] / world_w * n)), min(n - 1, int(u["y"] / world_h * n))))
    out = []
    for gy in range(n):
        line = ""
        for gx in range(n):
            key = (gx, gy)
            if key in enemy_cells and key in own_cells:
                line += "X"
            elif key in enemy_cells:
                c = enemy_cells[key]
                line += "9" if c > 9 else str(c)
            elif key in own_cells:
                line += "o"
            else:
                line += rows[min(th - 1, gy * th // n)][min(tw - 1, gx * tw // n)]
        out.append(line)
    return "\n".join(out)


def _capability_brief(pedia):
    """紧凑的单位能力清单：主要生产者可产兵种（带科技标注）与升级路径。"""
    by_name = {u["name"]: u for u in (pedia or {}).get("units", [])}

    def with_tech(action):
        ut = by_name.get(action[2:]) if action.startswith("u_") else None
        return f"{action}(T{ut['tech']})" if ut else action

    rows = []
    for name in ("commandCenter", "builder", "landFactory", "airFactory", "seaFactory", "mechFactory"):
        u = by_name.get(name)
        if not u:
            continue
        prods = list(dict.fromkeys(b for b in u.get("builds", []) if b.startswith("u_")))[:10]
        ups = list(dict.fromkeys(b for b in u.get("builds", []) if not b.startswith(("b_", "u_", "c_"))))[:3]
        s = f"{name}(${u.get('price', '?')})"
        if prods:
            s += " 产[" + ",".join(with_tech(a) for a in prods) + "]"
        if ups:
            s += " 升[" + ",".join(ups) + "]"
        rows.append(s)
    return "\n".join(rows) or "（百科未载入）"


def build_strategy_prompt(map_msg, pedia, cfg, user_groups):
    """战略层系统提示（静态）：职责、地图基础信息、图例。token 预算 ~1.5K。"""
    tw = map_msg["tilesW"] * map_msg["tileW"]
    th = map_msg["tilesH"] * map_msg["tileH"]
    res = map_msg.get("resources", [])[:40]
    res_txt = ",".join(f"({p['x']},{p['y']})" for p in res)
    groups_txt = ",".join(str(g) for g in sorted(user_groups)) or "9"
    return f"""你是《Rusted Warfare》的战略指挥官。玩家把编队({groups_txt}队)委托给你。经济建设（采集器/工厂建造、工厂生产排队、建筑升级、集结点）由系统执行层自动完成——你只决定战略：扩张、憋兵、进攻还是防守。

【公平】你只看得见本队视野内的信息，迷雾中的敌情未知，不要臆测。
【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。地图上的资源点: {res_txt}
【战略格图例】每轮战场态势里附一张 12x12 战略格：o=我方单位，1-9=可见敌人数量，X=敌我皆有；地形 .=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯(机甲可爬) #=高崖(仅空军)
【规则】
- 每次回复必须调用工具，且**尽量给一揽子动作**：1~2 个军事动作(attack/harass/defend_home) + 1 个经济动作(expand/build_army)，不要一轮只做一件事。工具：attack / harass / defend_home / expand / build_army / note / query(查资料)。
- 单位/建筑/价格/地图详情拿不准就用 query 查（每轮最多2次），严禁编造动作ID和单位名；query(roster) 是全单位花名册。
- 工厂升级后必须 query 该厂看新解锁兵种并换产：T2 单位全面强于 T1，继续造 T1 是浪费。
- expand 采集器目标上限为 12，超过会被系统拒绝；经济够用就把资源转向军力和升级。
- 生产与建造不要插手（没有对应工具），军队构成只通过 build_army 调整；兵种要混合（主战+防空+远程），看敌方构成针对性调整，构成ID从上面能力表选。
- 禁止军队闲置：构成基本成型且视野无敌情时，用 attack/harass 向地图中部或未探索区域推进侦察；连续两轮只发展不行动是不允许的。
- build_army 可带集结点坐标(x,y)：出厂单位会自动向集结点汇聚。把集结点设在主基地靠前/交火方向，别默认让部队蹲在厂里。
- 小队残血或被围就用 defend_home，别硬拼；扩张与进攻二选一，不要同时。
- 你只给战略方向，微观走位/集火由系统和游戏AI完成。"""


class AgentLink:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest_snap = None
        self.map_msg = None
        self.map_version = 0  # 新对局（新地图消息）递增，pilot 据此重置上下文
        self.pedia = None
        self.ai_control = {}
        self.cmd_results = deque(maxlen=20)
        self.sock = None
        self.connected = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="pilot-link").start()

    def _run(self):
        failures = 0
        while True:
            try:
                s = socket.create_connection((HOST, PORT), timeout=5)
                failures = 0
                s.settimeout(None)
                with self.lock:
                    self.sock = s
                self.connected.set()
                print("[link] 已连接 rw-agent")
                buf = b""
                while True:
                    chunk = s.recv(524288)
                    if not chunk:
                        raise ConnectionError("closed")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = msg.get("type")
                        with self.lock:
                            if t == "snapshot":
                                self.latest_snap = msg
                            elif t == "map":
                                rows_hash = hash(tuple(msg.get("rows", [])))
                                if rows_hash != getattr(self, "_map_hash", None):
                                    self._map_hash = rows_hash
                                    self.map_msg = msg
                                    self.map_version += 1
                            elif t == "pedia":
                                self.pedia = msg
                                print(f"[link] 百科已载入 ({len(msg.get('units', []))} 类)")
                            elif t == "aiControl":
                                self.ai_control = msg
                                print(f"[aiControl] enabled={msg.get('enabled')} interval={msg.get('interval')} groups={msg.get('groups')}")
                                log_activity("status", f"AI控制: {'运行' if msg.get('enabled') else '停止'}"
                                                       f" 节拍={msg.get('interval') or '自动'} 委托={msg.get('groups')}")
                            elif t == "cmdResult":
                                self.cmd_results.append(msg)
                                mark = "✓" if msg.get("ok") else "✗"
                                print(f"[cmdResult] {mark} {msg.get('cmd')}: {msg.get('msg')}")
                                log_activity("cmd" if msg.get("ok") else "error",
                                             f"{mark} {msg.get('cmd')}: {msg.get('msg')}")
            except (ConnectionError, socket.timeout, OSError) as e:
                failures += 1
                self.connected.clear()
                with self.lock:
                    self.sock = None
                if failures > 45:
                    print("[link] 游戏已关闭超过90秒，pilot 自动退出")
                    os._exit(0)
                print(f"[link] 断开 ({e})，2s 后重连")
                time.sleep(2)

    def snapshot(self):
        with self.lock:
            return self.latest_snap

    def ai_control_state(self):
        with self.lock:
            return dict(self.ai_control)

    def map_message(self):
        with self.lock:
            return self.map_msg

    def pedia_message(self):
        with self.lock:
            return self.pedia

    def drain_cmd_results(self):
        with self.lock:
            out = list(self.cmd_results)
            self.cmd_results.clear()
            return out

    def send(self, obj: dict):
        with self.lock:
            s = self.sock
            if s is None:
                return False
            try:
                s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                return True
            except OSError:
                return False


def downsample(rows, f):
    if f <= 1:
        return rows
    return [row[::f] for row in rows[::f]]


def pedia_brief(pedia, cap=48):
    """从百科里挑建筑条目做价格表（建造决策用）。"""
    if not pedia:
        return "（百科未载入）"
    rows = []
    for u in pedia.get("units", []):
        if u.get("building"):
            rows.append(f"{u['name']}(${u.get('price', '?')},T{u.get('tech', '?')},{u.get('move', '?')},hp{u.get('hp', '?')})")
        if len(rows) >= cap:
            break
    return "\n".join(rows) if rows else "（无建筑条目）"


PRODUCERS = [
    "commandCenter", "builder", "fabricatorT1", "fabricatorT2", "fabricatorT3",
    "landFactory", "airFactory", "mechFactory", "mechFactoryT2", "seaFactory",
    "experimentalLandFactory", "outpostT1", "outpostT2",
    # 可自我升级的建筑（升级动作在其动作表里，用 train 触发）
    "extractorT1", "extractorT2", "extractorT3",
]


def pedia_production_table(pedia, cap=16):
    """生产者 -> 动作ID，按语义分组：建造[b_] 生产[u_] 升级[含upgrade] 通用[c_]。"""
    if not pedia:
        return "（百科未载入）"
    by_name = {u["name"]: u for u in pedia.get("units", [])}
    rows = []
    for name in PRODUCERS:
        u = by_name.get(name)
        if not u:
            continue
        builds = u.get("builds", [])
        groups = []
        # 分组：建造[b_] 生产[u_] 通用[c_]；其余（数字、目标_0、含upgrade）一律归升级/特殊
        for label, pred in (("建造", lambda b: b.startswith("b_")),
                            ("生产", lambda b: b.startswith("u_")),
                            ("通用", lambda b: b.startswith("c_")),
                            ("升级", lambda b: not b.startswith(("b_", "u_", "c_")))):
            ids = list(dict.fromkeys(b for b in builds if pred(b)))[:cap]  # 去重（科技列表有重复）
            if ids:
                groups.append(f"{label}[{','.join(ids)}]")
        if groups:
            rows.append(f"{name} {' '.join(groups)}")
    return "\n".join(rows) or "（无生产选项）"


def build_system_prompt(map_msg, pedia, cfg, user_groups):
    tw = map_msg["tilesW"] * map_msg["tileW"]
    th = map_msg["tilesH"] * map_msg["tileH"]
    rows = downsample(map_msg.get("rows", []), int(cfg["map_downsample"]))
    f = int(cfg["map_downsample"])
    tile = map_msg["tileW"] * f
    res = map_msg.get("resources", [])[:60]
    res_txt = ",".join(f"({p['x']},{p['y']})" for p in res)
    map_txt = "\n".join(rows)
    groups_txt = ",".join(str(g) for g in sorted(user_groups)) or "9"
    return f"""你在即时战略游戏《Rusted Warfare》中担任指挥官。玩家把若干编队（当前：{groups_txt} 队）的单位委托给你指挥，你通过工具下达指令。未委托给你的单位由玩家自己负责。

【公平规则】你只收到玩家本队视野内的信息（战争迷雾已过滤）。迷雾中的敌情你不知道，不要臆测。敌人离开视野后快照中即消失。

【坐标系】世界坐标为像素，y 向下为正（北=-y）。地图大小 {tw:.0f} x {th:.0f}。单位位置、指令目标都用世界坐标。

【地形图】下面是降采样地图，每个字符 = {tile:.0f} 像素见方：
. = 陆地（陆军可行）   ~ = 水域（仅海军/悬浮/空军）   = = 桥梁（陆军可过）
L = 岩浆（仅空军）     ^ = 悬崖阶梯（仅 OVER_CLIFF 兵种如机甲可攀爬）   # = 高崖（仅空军可飞越）

{map_txt}

【资源点（世界坐标）】{res_txt}

【建筑百科（名称/价格/科技/移动类型/血量）】
{pedia_brief(pedia)}
动作ID规则：建造者施工=b_类型名（如 b_extractorT1）；工厂产兵=u_类型名（如 u_tank、u_builder）；建筑升级=动作表 升级[...] 里列出的ID（如 extractorT2_0=采集器升T2、110=工厂升T2，对该建筑用 train）；c_ 系列=通用动作。注意：s_ 是"选中同类单位"的本地动作，禁止用作生产/建造指令。

【生产/建造动作表（生产者: 合法动作ID）】
{pedia_production_table(pedia)}
用法：建造者用 build(action,x,y) 盖建筑，action 必须用建造[b_]列表里的 ID；工厂用 train(action,factory) 生产，action 必须用生产[s_]列表里的 ID，factory 取"我方工厂"列表里的 id（带 # 的数字）；rally(x,y,factory) 设集结点。ID 区分大小写，不要自己编造。

【规则】
- 指令通过工具下达，进入游戏正常指令队列，与玩家鼠标点击同级（约0.2s排队延迟）。
- 你只指挥快照中"你的小队"（委托单位）。attack_unit 的目标 id 必须来自最新快照的可见敌人列表；train/rally 的 factory 必须是我方单位 id。
- 每个用户消息是最新战场态势。你的回复必须包含至少一个工具调用——分析用 note 工具写（一两句话，不要长篇），content 留空或极短，重点是把决策变成工具调用。
- 动作 ID 必须原样取自动作表（含 b_/u_/数字前缀）。如果某个 ID 被回执拒绝，严禁再次尝试同一个 ID——从回执附带的可用列表中另选一个。回执里的失败信息用于纠正，不要道歉，直接给下一步行动。
- 硬性规则：小队正在承受攻击时，必须当机立断——要么还击要么撤退，禁止连续分析而不行动。
- 建造前先确认资金充足（百科有价格；资金不足时先等钱或造便宜的），采集器必须建在资源点上。
- 发展顺序（重要）：①先让建造者在最近的空闲陆地资源点建采集器（摘要"空闲资源点(陆地)"列表**从第一个开始建，它是离家最近的，不许跳过近的去远的**）；②指挥中心保持生产 builder（总数3~4个即可，不要无限造）；③有了工厂就让工厂持续产战斗单位（生产[u_]列表里的坦克/机甲/飞机），不要闲置；④战斗单位成群后再侦察/骚扰，不要开局就把所有单位派往地图中心。
- 经济纪律（重要）：每个工厂排队最多2~3个任务（看摘要里[队列N]），堆满了就**扩建新工厂**而不是继续堆；有钱不等于要花，留钱用于扩张（更多采集器/工厂/炮塔）；主基地周围适时建炮塔防御；采集器富余后可升级——对该建筑用 train，action 取动作表 升级[...] 里的ID（如 extractorT1 的 extractorT2_0）；工厂同理用其升级ID（如 110）解锁更强单位。
- 兵种搭配（重要）：不要只生产单一兵种——看摘要里"我方其他部队"的构成，保持混合（如主战坦克/机甲 + 防空 + 远程/炮兵），敌方空军多就补防空，敌方龟缩就补远程。
- 分开操作：move/attack_move/stop 都支持 ids 参数（逗号分隔的单位id），需要让不同单位去不同地方时，给每个单位单独下一条带 ids 的指令，不要对整队下一锅端指令。例：move(1000,500,222) 派 #222 去(1000,500)，move(3000,1000,323) 派 #323 去(3000,1000)。
- 建造是有时间的：建筑出现≠完工（快照 prog<1 是建造中），完工前工厂不能生产、建筑没作用。**build 的坐标可以省略——系统会在主基地附近自动选址（采集器自动选最近的空闲资源点），除非你明确知道该建在哪，否则一律省略坐标。**
- 资金纪律：资金<700 时只能建采集器（系统会拦截其他建造）；同类工厂最多 3 座（先满产/升级再扩）；每个工厂排队最多2~3个任务。
- 建造者纪律（重要）：摘要"建造者状态"里显示施工中(build@)的建造者**正在赶路或施工，绝对禁止再派任何新指令**——它会自己走到工地盖好，你要等它变回 idle（空闲）再派下一个活。重复派活会取消它原来的施工，导致什么都盖不成。一次只给一个建造者派一个活。想并行施工就用 builder 参数分别指定：build(b_extractorT1,x1,y1,builder=222) 和 build(b_landFactory,x2,y2,builder=530) 各干各的。
- 建造者数量纪律（重要）：建造者总数没有硬性上限，但**只要有≥2个在闲置（状态行"空闲可派活"），系统就禁止再生产新的**——先把闲置的派去建采集器/工厂/炮塔，全部用起来后才能继续产。建筑选址在主基地一侧，不许派建造者深入敌方区域。
- 小队空闲（没有进行中的任务）时，给出下一个任务目标，不要让它原地待命。
- 典型任务循环：侦察摸清敌情 → 用 build 在资源点建采集器发展经济 → 用 train 补充兵力 → 骚扰 → 劣势时撤退。
- 注意兵种与地形匹配：陆军不能下水，机甲可爬 '^'，只有空军能过 'L' 和 '#'；采集器必须建在资源点上；水上资源点可以让陆军 builder 在岸边造（建造距离有限），太远的水域要用海上建造者(Builder Ship, 海军)。"""


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def executor_worker(link, holder, cmd_times):
    """执行层独立线程：每 2s 跑一拍，不再被 LLM 调用阻塞（指令饿死的根治）。"""
    while True:
        time.sleep(2.0)
        ex = holder.get("ex")
        if ex is None:
            continue
        snap = link.snapshot()
        if snap is None or not snap.get("team") or snap["team"].get("ai"):
            continue
        if not snap.get("delegated"):
            continue
        with ex.lock:
            try:
                cmds = ex.tick(snap)
            except Exception as e:
                print(f"[executor] tick 异常: {e}")
                continue
        for ec in cmds:
            if not link.send(ec):
                break
            cmd_times.append(time.time())
            log_activity("cmd", "⚙ " + json.dumps(ec, ensure_ascii=False))


def group_to_slot(g: int) -> int:
    """用户编队号(1-9,0) → agent 槽位(0-8,9)。"""
    return (g - 1) % 10 if g != 0 else 9


def pick_interval(cfg, alerts, snap, manual_override):
    """自适应节拍：手动覆盖 > 警报/敌近 > 有敌情 > 太平。"""
    if manual_override > 0:
        return manual_override
    if alerts:
        return float(cfg["fast_interval_sec"])
    enemies = snap.get("enemy", [])
    if not enemies:
        return float(cfg["calm_interval_sec"])
    squad_ids = set(snap.get("delegated", []))
    squad = [u for u in snap.get("own", []) if u["id"] in squad_ids]
    if squad:
        c = summarize.centroid(squad)
        if any(summarize.dist(e, c) < 1200 for e in enemies):
            return float(cfg["fast_interval_sec"])
    return float(cfg["mid_interval_sec"])


def main():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml"), "rb") as fp:
        cfg = tomllib.load(fp)
    # LLM_OFF=1：纯执行层模式（离线验证经济/生产调度，不调用任何 LLM）
    llm_off = os.environ.get("LLM_OFF") == "1"
    api_key = os.environ.get(cfg["api_key_env"], "")
    if not api_key:
        secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.toml")
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as fp:
                api_key = tomllib.load(fp).get("api_key", "")
    if not api_key and not llm_off:
        print(f"缺少 API key：请设置环境变量 {cfg['api_key_env']} 或填写 bridge/secret.toml")
        return 1

    link = AgentLink()
    open(ACTIVITY_FILE, "w", encoding="utf-8").close()  # 清空上轮活动流
    print("[pilot] 等待游戏地图数据（请先开一局遭遇战）...")
    while link.map_message() is None:
        time.sleep(1)
    map_msg = link.map_message()
    # 百科与地图几乎同时到，最多等 5s
    t0 = time.time()
    while link.pedia_message() is None and time.time() - t0 < 5:
        time.sleep(0.5)
    pedia = link.pedia_message()
    # 百科动作 ID 全集（含 upgradeT2 等无前缀动作），用于合法性校验
    known_actions = set()
    for pu in (pedia or {}).get("units", []):
        known_actions.update(pu.get("builds", []))
    world_w = map_msg["tilesW"] * map_msg["tileW"]
    world_h = map_msg["tilesH"] * map_msg["tileH"]

    # 委托编队（热更新）
    user_groups = {9}
    deleg_mtime = None
    if os.path.exists(DELEGATION_FILE):
        try:
            with open(DELEGATION_FILE, encoding="utf-8") as fp:
                user_groups = {int(g) for g in json.load(fp).get("groups", [9]) if 0 <= int(g) <= 9}
            deleg_mtime = os.path.getmtime(DELEGATION_FILE)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[delegate] delegation.json 解析失败，用默认[9]: {e}")
            user_groups = {9}
    else:
        with open(DELEGATION_FILE, "w", encoding="utf-8") as fp:
            json.dump({"groups": [9]}, fp)
        print("[delegate] 已创建 delegation.json（默认委托 9 队，可编辑热更新）")

    use_exec = cfg.get("use_executor", True)
    system = {"role": "system", "content": (build_strategy_prompt(map_msg, pedia, cfg, user_groups)
                                            if use_exec else build_system_prompt(map_msg, pedia, cfg, user_groups))}
    print(f"[pilot] 地图 {map_msg['tilesW']}x{map_msg['tilesH']} 已载入，LLM={cfg['model']}，委托编队={sorted(user_groups)}")
    if user_groups != {9}:  # 默认 9 队由 agent 侧兜底，不强行覆盖游戏内 /ai groups 设置
        link.send({"cmd": "delegate", "groups": ",".join(str(group_to_slot(g)) for g in sorted(user_groups))})

    smz = summarize.Summarizer(map_msg.get("resources", []))
    # 执行层：经济/生产/升级自动调度（use_executor=false 可回退到纯 LLM 微操）
    executor = None
    if use_exec:
        executor = exec_mod.Executor(map_msg, pedia, log_activity)
        executor.auto_military = bool(cfg.get("auto_military", False))
    # 反射层：受击撤退/基地回防（纯规则，始终启用）
    import reflex as reflex_mod
    reflex = reflex_mod.Reflex(log_activity)
    history = []
    # 失败封禁双轨：永封=非法/编造ID、死目标（本对局内）；临封=资金/施工/队列/科技等条件性失败，TTL 后可重试
    FAILED_TTL = 90.0
    failed_perm = set()       # call_key，永久封禁
    failed_temp = {}          # call_key -> 解封时间戳

    def is_blocked(key):
        if key in failed_perm:
            return True
        exp = failed_temp.get(key)
        if exp is None:
            return False
        if time.time() >= exp:
            failed_temp.pop(key, None)
            return False
        return True

    def block_perm(key):
        failed_perm.add(key)

    def block_temp(key, ttl=FAILED_TTL):
        failed_temp[key] = time.time() + ttl

    def unblock_cmd(cmd_name):
        """某类指令后来成功了 = 条件已恢复，清掉它的临时封禁。"""
        for k in [k for k in failed_temp if k.startswith(cmd_name + ":")]:
            failed_temp.pop(k, None)

    def classify_agent_fail(cmd_name, msg, key):
        """按 agent 回执内容决定永封还是临封。"""
        if "；可用：" in msg:            # 附可用列表 = 该 ID 对此单位无效（编造/前缀错）
            block_perm(key)
        elif "target not found" in msg or "not found/not ours" in msg:
            block_perm(key)              # 目标死了或单位不存在
        else:                            # funds/正在施工/尚未完工/找不到位置/不可达/no delegated 等
            block_temp(key)

    sent_keys = deque(maxlen=30)  # (cmd名, call_key) 用于把 agent 失败回执归因到具体调用
    pending_notes = []        # 下一轮要塞进上下文的系统提示（拦截/拒绝记录）
    seen_map_version = getattr(link, "map_version", 0)
    cmd_times = deque()
    tok_history = deque()  # (ts, prompt, cache_hit, completion)
    manual_interval = 0.0  # >0 时覆盖自适应节拍（control.json）
    control_mtime = None
    if os.path.exists(CONTROL_FILE):
        try:
            with open(CONTROL_FILE, encoding="utf-8") as fp:
                manual_interval = float(json.load(fp).get("interval_sec", 0))
            control_mtime = os.path.getmtime(CONTROL_FILE)
        except (json.JSONDecodeError, ValueError, OSError):
            manual_interval = 0.0
    else:
        with open(CONTROL_FILE, "w", encoding="utf-8") as fp:
            json.dump({"interval_sec": 0}, fp)  # 0 = 自适应
    last_cycle = 0.0
    last_hint = 0.0
    interval = float(cfg["interval_sec"])
    max_cmds = int(cfg["max_commands_per_cycle"])
    apm_cap = int(cfg["apm_cap"])

    # 执行层独立线程：每 2s 一拍，与 LLM 调用完全解耦
    exec_holder = {"ex": executor}
    if executor is not None:
        threading.Thread(target=executor_worker, args=(link, exec_holder, cmd_times),
                         daemon=True, name="rw-executor").start()

    while True:
        # 新对局检测：地图消息版本变化 → 清空 LLM 上下文与摘要器状态
        mv = getattr(link, "map_version", 0)
        if mv != seen_map_version:
            seen_map_version = mv
            history.clear()
            failed_perm.clear()
            failed_temp.clear()
            new_map = link.map_message()
            smz = summarize.Summarizer((new_map or {}).get("resources", []))
            if cfg.get("use_executor", True):
                executor = exec_mod.Executor(new_map, pedia, log_activity)
                executor.auto_military = bool(cfg.get("auto_military", False))
                exec_holder["ex"] = executor
            print("[pilot] 检测到新对局，上下文已重置")
            log_activity("status", "新对局开始，AI 上下文已重置")
            continue

        # delegation.json 热更新
        try:
            m = os.path.getmtime(DELEGATION_FILE)
            if deleg_mtime is not None and m != deleg_mtime:
                deleg_mtime = m
                with open(DELEGATION_FILE, encoding="utf-8") as fp:
                    new_groups = {int(g) for g in json.load(fp).get("groups", [9]) if 0 <= int(g) <= 9}
                if new_groups != user_groups:
                    user_groups = new_groups
                    print(f"[delegate] 委托编队更新 -> {sorted(user_groups)}")
                    link.send({"cmd": "delegate",
                               "groups": ",".join(str(group_to_slot(g)) for g in sorted(user_groups))})
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        # control.json 热更新（仪表盘滑块）
        try:
            cm = os.path.getmtime(CONTROL_FILE)
            if control_mtime is not None and cm != control_mtime:
                control_mtime = cm
                with open(CONTROL_FILE, encoding="utf-8") as fp:
                    manual_interval = max(0.0, min(60.0, float(json.load(fp).get("interval_sec", 0))))
                print(f"[control] 节拍手动设定 -> {manual_interval}s (0=自动)")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        snap = link.snapshot()
        if snap is None or not snap.get("team") or snap["team"].get("ai"):
            time.sleep(1)
            continue
        ac = link.ai_control_state()
        if ac and not ac.get("enabled", True):
            if time.time() - last_hint > 15:
                print("[pilot] AI 已停止（游戏内 /ai on 或按钮恢复）")
                last_hint = time.time()
            time.sleep(1)
            continue
        if not snap.get("delegated"):
            if time.time() - last_hint > 15:
                print("[pilot] 等待委托：框选单位后点游戏内[委托选中]按钮，或 /ai groups 指定编队")
                last_hint = time.time()
            time.sleep(1)
            continue

        text, alerts = smz.summarize(snap)
        results = link.drain_cmd_results()
        if results:
            text += "\n\n指令回执（你上一条指令的执行结果，失败原因必须据此纠正）:\n" + "\n".join(
                ("✓ " if r.get("ok") else "✗ ") + f"{r.get('cmd')}: {r.get('msg')}" for r in results[-8:])
            for r in results:
                if r.get("ok"):
                    unblock_cmd(r.get("cmd", ""))  # 同类指令已成功 = 条件恢复，解除临时封禁
                    continue
                for sc, skey in reversed(sent_keys):
                    if sc == r.get("cmd"):
                        classify_agent_fail(sc, r.get("msg") or "", skey)
                        break
        if pending_notes:
            text += "\n\n" + "\n".join(pending_notes)
            pending_notes.clear()
        # 失败回执喂给执行层学习（tick 已在独立线程跑，这里只喂回执）
        if executor is not None:
            with executor.lock:
                executor.on_cmd_results(results)
        # 反射层：受击撤退/基地回防（纯规则，毫秒级）
        if reflex is not None and snap.get("delegated"):
            home_xy = (smz.home["x"], smz.home["y"]) if getattr(smz, "home", None) else None
            reflex.tick(snap, home_xy, set(snap.get("delegated", [])), link.send)
        if llm_off:  # 纯执行层模式：跳过 LLM 调用，只报执行层状态
            if time.time() - last_hint > 10:
                last_hint = time.time()
                print("[llm_off] " + (executor.status_line(snap) if executor else "执行层未启用"))
            time.sleep(0.5)
            continue
        if executor is not None:
            text += ("\n\n【战略格】o=我方 数字=敌数 X=混居 其余为地形\n" + strategic_grid(map_msg, snap)
                     + "\n【执行层】 " + executor.status_line(snap)
                     + "\n[系统提示] 本回合必须调用一个战略工具（attack/harass/defend_home/expand/build_army/note/query）。")
            # 委托队无战斗单位时明确提示（attack/harass 会失败）
            squad_ids = set(snap.get("delegated", []))
            combat = [u for u in snap.get("own", [])
                      if u["id"] in squad_ids and not u.get("bld")
                      and "uilder" not in u["t"] and "abricator" not in u["t"]]
            if squad_ids and not combat:
                text += ("\n⚠ 委托小队里没有战斗单位（attack/harass 会失败）——"
                         "请玩家把战斗单位也编入委托编队，或点全队接管。")
        else:
            text += "\n\n[系统提示] 本回合必须至少调用一个工具（指令或 note），不允许只回复文本。[idle] 表示单位已完成上一个指令（空闲待命），不是卡住、也不是指令被忽略。"
        interval_override = (ac.get("interval") or 0) if ac else 0
        if not interval_override:
            interval_override = manual_interval
        if executor is not None and not interval_override:
            target_interval = (float(cfg["fast_interval_sec"]) if alerts
                               else float(cfg.get("strategy_interval_sec", 25)))
        else:
            target_interval = pick_interval(cfg, alerts, snap, interval_override)
        if time.time() - last_cycle < target_interval:
            time.sleep(0.25)
            continue
        last_cycle = time.time()

        active_tools = STRAT_TOOLS if executor is not None else TOOLS
        messages = [system]
        for u, a in history:
            messages.append({"role": "user", "content": u})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": text})

        t_start = time.time()
        try:
            msg = llm.chat(cfg["base_url"], api_key, cfg["model"], messages, active_tools,
                           timeout=int(cfg["timeout_sec"]))
        except llm.LlmError as e:
            print(f"[llm] 调用失败: {e}")
            time.sleep(interval)
            continue
        api_ms = (time.time() - t_start) * 1000

        note = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if not calls:
            # DeepSeek 有时会只写分析不调工具——追问一次，强制工具调用
            print("[llm] 未调用工具，追问重试")
            messages.append({"role": "assistant", "content": note or "（空）"})
            verbs = ("attack/harass/defend_home/expand/build_army" if executor is not None
                     else "move/attack_move/attack_unit/build/train/stop")
            messages.append({"role": "user", "content":
                             f"刚才的回复没有任何工具调用。请直接用一个工具给出下一步指令（{verbs} 之一），或用 note 一句话说明等待原因。无需解释。"})
            try:
                msg = llm.chat(cfg["base_url"], api_key, cfg["model"], messages, active_tools,
                               timeout=int(cfg["timeout_sec"]))
                note = (msg.get("content") or "").strip()
                calls = msg.get("tool_calls") or []
            except llm.LlmError as e:
                print(f"[llm] 追问失败: {e}")
        if not note and not calls:
            print(f"[llm-debug] 空响应 finish={msg.get('_finish_reason')}")
        executed = []
        note_parts = []
        blocked = []
        visible_enemy_ids = {u["id"] for u in snap.get("enemy", [])}
        own_ids = {u["id"] for u in snap.get("own", [])}

        # 资料查询预处理：本地即时回答→回填→追问（最多2轮），随后进入正常指令执行
        for _qr in range(2):
            q_calls = [tc for tc in calls if (tc.get("function") or {}).get("name") == "query"]
            if not q_calls:
                break
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": msg.get("tool_calls")})
            for tc in q_calls:
                fn = tc.get("function") or {}
                try:
                    qa = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    qa = {}
                ans = answer_query(qa.get("topic", ""), qa.get("arg", ""), map_msg, pedia, snap)
                cap = 2600 if qa.get("topic") == "roster" else 1500
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": ans[:cap]})
                log_activity("note", f"🔍 查{qa.get('topic')}:{qa.get('arg', '')}")
            calls = [tc for tc in calls if (tc.get("function") or {}).get("name") != "query"]
            try:
                msg = llm.chat(cfg["base_url"], api_key, cfg["model"], messages, active_tools,
                               timeout=int(cfg["timeout_sec"]))
                note = (msg.get("content") or "").strip() or note
                calls = list(msg.get("tool_calls") or []) + calls
            except llm.LlmError as e:
                print(f"[llm] 查询追问失败: {e}")
                break

        for tc in calls:
            if len(executed) >= max_cmds:
                break
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            if name == "note":
                t = str(args.get("text", "")).strip()
                if t:
                    note_parts.append(t)
                continue
            # 战略动词映射（执行层托管时）
            if executor is not None and name in ("attack", "harass"):
                executor.last_military_order = time.time()
                cmd = {"cmd": "attackmove",
                       "x": clamp(float(args.get("x", 0)), 0, world_w),
                       "y": clamp(float(args.get("y", 0)), 0, world_h)}
            elif executor is not None and name == "defend_home":
                executor.last_military_order = time.time()
                if getattr(smz, "home", None):
                    cmd = {"cmd": "move", "x": float(smz.home["x"]), "y": float(smz.home["y"])}
                else:
                    blocked.append("defend_home(找不到主基地)")
                    continue
            elif executor is not None and name == "expand":
                with executor.lock:
                    cur = executor.econ_goal["extractors"]
                    executor.set_economy_goal(extractors=int(args.get("extractors") or cur + 1))
                    executed.append(f"expand(目标池{executor.econ_goal['extractors']})")
                cmd_times.append(time.time())
                continue
            elif executor is not None and name == "build_army":
                comp = args.get("composition") or {}
                rally = None
                if args.get("x") is not None and args.get("y") is not None:
                    rally = (clamp(float(args["x"]), 0, world_w), clamp(float(args["y"]), 0, world_h))
                with executor.lock:
                    executor.set_army_goal(composition=comp, rally=rally)
                executed.append(f"build_army({comp})")
                cmd_times.append(time.time())
                continue
            # 执行层托管期间拦截幻觉调用的经济指令（工具表已移除，双保险）
            if executor is not None and name in ("build", "train", "rally"):
                blocked.append(f"{name}(经济已由系统托管)")
                continue
            # 重复拦截：永封或临封（TTL 内）的同一指令不再发送
            call_key = name + ":" + json.dumps(args, sort_keys=True)
            if is_blocked(call_key):
                blocked.append(f"{name}({','.join(str(v) for v in args.values())})")
                continue
            now = time.time()
            while cmd_times and now - cmd_times[0] > 60:
                cmd_times.popleft()
            if len(cmd_times) >= apm_cap:
                print("[pilot] APM 限速，跳过本轮剩余指令")
                break
            cmd = None
            if name == "move":
                cmd = {"cmd": "move",
                       "x": clamp(float(args.get("x", 0)), 0, world_w),
                       "y": clamp(float(args.get("y", 0)), 0, world_h)}
            elif name == "attack_move":
                cmd = {"cmd": "attackmove",
                       "x": clamp(float(args.get("x", 0)), 0, world_w),
                       "y": clamp(float(args.get("y", 0)), 0, world_h)}
            elif name == "attack_unit":
                uid = int(args.get("id", -1))
                if uid not in visible_enemy_ids:
                    print(f"[pilot] 拒绝 attack_unit：id {uid} 不在可见敌人中")
                    block_temp(call_key)
                    pending_notes.append(f"✗ attack_unit({uid}): 目标暂不可见，90s内不要再试同一个")
                    continue
                cmd = {"cmd": "attack", "id": uid}
            elif name == "build":
                act = str(args.get("action", ""))
                if not (act in known_actions or act.startswith(("b_", "u_", "c_")) or act[:1].isdigit()):
                    print(f"[pilot] 拒绝 build：非法 action {act}（应为 b_/u_/数字 ID）")
                    block_perm(call_key)
                    pending_notes.append(f"✗ build action={act}: 非法ID（必须原样取自动作表），本局不要再试")
                    continue
                # 系统守卫：资金储备（穷时不许乱花，先留钱建采集器）
                funds_now = (snap.get("team") or {}).get("funds", 0)
                if funds_now < 700 and "extractor" not in act:
                    block_temp(call_key)
                    pending_notes.append(f"✗ build {act}: 资金仅{funds_now:.0f}<700，先留钱建采集器恢复经济，别的不许盖")
                    print(f"[pilot] 守卫：资金 {funds_now:.0f}，拒绝非采集器建造 {act}")
                    continue
                # 系统守卫：同类工厂上限（防工厂刷屏）
                if "actory" in act or "Factory" in act:
                    fac_type = act.split("_", 1)[1] if "_" in act else act
                    same = sum(1 for u in snap.get("own", []) if u.get("bld") and u["t"] == fac_type)
                    if same >= 3:
                        block_temp(call_key)
                        pending_notes.append(f"✗ build {act}: 已有{same}座{fac_type}，先让它们满产/升级，不许再建同类")
                        print(f"[pilot] 守卫：{fac_type} 已有 {same} 座，拒绝刷屏")
                        continue
                cmd = {"cmd": "build", "action": act}
                if args.get("x") is not None and args.get("y") is not None:
                    cmd["x"] = clamp(float(args["x"]), 0, world_w)
                    cmd["y"] = clamp(float(args["y"]), 0, world_h)
                if args.get("builder") is not None:
                    cmd["builder"] = int(args["builder"])
            elif name == "train":
                act = str(args.get("action", ""))
                fid = int(args.get("factory", -1))
                if not (act in known_actions or act.startswith(("b_", "u_", "c_")) or act[:1].isdigit()):
                    print(f"[pilot] 拒绝 train：非法 action {act}（应为 b_/u_/数字 ID）")
                    block_perm(call_key)
                    pending_notes.append(f"✗ train action={act}: 非法ID（必须原样取自动作表），本局不要再试")
                    continue
                if fid not in own_ids:
                    print(f"[pilot] 拒绝 train：factory {fid} 不是我方单位")
                    block_perm(call_key)
                    pending_notes.append(f"✗ train factory={fid}: 不是我方单位id，factory 必须取快照里的#id，本局不要再试")
                    continue
                # 系统守卫：闲置建造者≥2 时禁止再生产（不卡总数，卡闲置）
                if act in ("u_builder", "u_fabricator"):
                    idle_b = sum(
                        1 for u in snap.get("own", [])
                        if ("uilder" in u["t"] or "abricator" in u["t"])
                        and not u.get("bld")
                        and (u.get("act") in (None, "", "idle")))
                    if idle_b >= 2:
                        block_temp(call_key)
                        pending_notes.append(
                            f"✗ train {act}: 有{idle_b}个建造者正在闲置！先给他们派建筑活（采集器/工厂/炮塔），都用上了再产新的")
                        print(f"[pilot] 守卫：{idle_b} 个 builder 闲置，拒绝再产")
                        continue
                # 系统守卫：工厂队列上限（防单厂堆货）
                fac_u = next((u for u in snap.get("own", []) if u["id"] == fid), None)
                if fac_u is not None and (fac_u.get("q") or 0) >= 3:
                    block_temp(call_key)
                    pending_notes.append(
                        f"✗ train {act}: 该工厂队列已满(3)，不要再堆！请扩建新工厂或等队列消化")
                    print(f"[pilot] 守卫：factory {fid} 队列满，拒绝堆货")
                    continue
                cmd = {"cmd": "train", "action": act, "factory": fid}
            elif name == "rally":
                fid = int(args.get("factory", -1))
                if fid not in own_ids:
                    print(f"[pilot] 拒绝 rally：factory {fid} 不是我方单位")
                    block_perm(call_key)
                    pending_notes.append(f"✗ rally factory={fid}: 不是我方单位id，本局不要再试")
                    continue
                cmd = {"cmd": "rally", "factory": fid,
                       "x": clamp(float(args.get("x", 0)), 0, world_w),
                       "y": clamp(float(args.get("y", 0)), 0, world_h)}
            elif name == "stop":
                cmd = {"cmd": "stop"}
            else:
                print(f"[pilot] 未知工具: {name}")
                continue
            ids_txt = str(args.get("ids", "")).strip()
            if ids_txt and name in ("move", "attack_move", "stop", "attack_unit"):
                cmd["units"] = ids_txt
            if link.send(cmd):
                sent_keys.append((cmd["cmd"], call_key))
                cmd_times.append(time.time())
                args_txt = ",".join(str(v) for v in args.values())
                executed.append(f"{name}({args_txt})")
                time.sleep(random.uniform(0.3, 0.5))  # 拟人操作间隔
            else:
                print("[pilot] 发送失败（连接断开）")
                break

        if blocked:
            msg_blocked = "系统拦截了你的重复无效指令（之前已被拒绝，不要再试）: " + ", ".join(blocked)
            print("[pilot] " + msg_blocked)
            pending_notes.append(msg_blocked)

        tick = snap.get("tick", 0)
        full_note = " ".join(p for p in [note] + note_parts if p).strip()
        usage = msg.get("_usage") or {}
        tok = ""
        if usage:
            tok_history.append((time.time(), usage.get("prompt_tokens", 0),
                                usage.get("prompt_cache_hit_tokens", 0),
                                usage.get("completion_tokens", 0)))
            while tok_history and time.time() - tok_history[0][0] > 300:
                tok_history.popleft()
            tok = (f" tok={usage.get('prompt_tokens', '?')}"
                   f"(hit{usage.get('prompt_cache_hit_tokens', '?')})/{usage.get('completion_tokens', '?')}")
        tpm_txt = ""
        if tok_history:
            span = max(1.0, time.time() - tok_history[0][0])
            tpm = sum(t[1] for t in tok_history) / span * 60
            hitm = sum(t[2] for t in tok_history) / span * 60
            tpm_txt = f" {tpm/1000:.0f}k tok/min(hit{hitm/1000:.0f}k)"
        summary_line = full_note[:120] if full_note else "(无备注)"
        ts = time.strftime("%H:%M:%S")
        print(f"[cycle] {ts} tick={tick} api={api_ms:.0f}ms{tok} 节拍={target_interval}s{tpm_txt} 小队={len(snap.get('delegated', []))}"
              f" 敌={len(snap.get('enemy', []))} 警报={len(alerts)} | {summary_line} | 执行: {', '.join(executed) or '无'}")

        # 活动流（仪表盘窗口消费）
        team = snap.get("team") or {}
        log_activity("status", f"AI指挥[{cfg['model']}] 编队{sorted(user_groups)} 小队{len(snap.get('delegated', []))}"
                               f" 资金{team.get('funds', 0):.0f} tick={tick} api={api_ms:.0f}ms{tok}{tpm_txt}")
        if full_note:
            log_activity("note", full_note)
        for ex in executed:
            log_activity("cmd", "→ " + ex)
        for al in alerts:
            log_activity("alert", "⚠ " + al)

        # 游戏内面板
        ov = [f"AI指挥[{cfg['model']}] 编队{sorted(user_groups)} 小队{len(snap.get('delegated', []))} 资金{team.get('funds', 0):.0f}"]
        if full_note:
            ov.extend(textwrap.wrap(full_note.replace("\n", " "), 44)[:3])
        if executed:
            ov.append("指令: " + ", ".join(executed)[:60])
        if alerts:
            ov.append("警报: " + " | ".join(alerts)[:60])
        link.send({"cmd": "overlay", "text": "|||".join(ov)})

        record = full_note or "（无备注）"
        if executed:
            record += "（系统记录：已下发 " + ", ".join(executed) + "）"
        history.append((text, record))
        keep_turns = 2 if executor is not None else int(cfg["history_turns"])
        history = history[-keep_turns:]


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
