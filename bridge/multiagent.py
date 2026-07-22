#!/usr/bin/env python3
"""rw-multiagent: 多 agent 架构——总指挥(慢) + 建造/战斗(快) 三循环并行。

- 总指挥（commander_interval≈50s）：全图+档案长 prompt，只产出持久《作战计划书》，不下指令
- 建造 agent（build_interval≈10s）：窄 prompt（经济摘要+计划书），只出 build/train 指令
- 战斗 agent（combat_interval≈6s，警报 2.5s）：窄 prompt（战斗摘要+计划书），只出军事指令与目标构成

协调纪律（不做 agent 互聊）：
- 共享状态 = 快照摘要 + 计划书 + 目标构成；冲突裁决 reflex > 战斗 > 建造（APM 配额隔离：战斗 25/min、建造 15/min）
- executor 退为执行内核（autopilot=False）：只做进度跟踪/抢修兜底/选址槽位服务
- 注意：本模块严禁 import pilot（pilot 以 __main__ 运行，import 会重执行 main）；pilot 的函数经 ctx 传入
"""
import json
import re
import threading
import time
from collections import deque

import connmap
import danger as danger_mod
import executor as exec_mod
import llm
import spawns
import summarize

HOST, PORT = "127.0.0.1", 41234

# 指令域划分：回执按此归属到各循环
BUILD_KINDS = {"build", "resume"}
COMBAT_KINDS = {"move", "attackmove", "attack", "stop", "rally"}
# 支援单位生产（建造域专属）：战斗工程师/机械师是工程师阶级，与建造者同域
SUPPORT_PROD = {"u_builder", "u_combatEngineer", "u_mechEngineer", "u_fabricator", "u_builderShip"}
APM_CAP = {"combat": 25, "build": 15}  # 每分钟（reflex 不计，与 pilot 一致）


def _train_domain(text):
    """train 回执归属：按"被 train 的动作"判域——失败回执的可用列表含 u_builder 等
    干扰词（曾把战斗域的 u_heavyTank 回执误判给建造域，学习封禁全落空）。"""
    m = re.search(r"没有此动作[:：]\s*([A-Za-z0-9_]+)", text)
    if not m:
        m = re.search(r"train\s+([A-Za-z0-9_]+)", text)
    if m:
        a = m.group(1)
        if a in ("u_builderShip", "u_builder", "u_combatEngineer", "u_mechEngineer", "u_fabricator"):
            return "build"
        if a.startswith("u_"):
            return "combat"
    return "build"  # 升级/无法识别默认建造域

_QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "query",
        "description": "查本地资料库（零成本即时应答）。topic: roster=全单位花名册 | unit/building=单位详情(带武器数据) | prices=生产者能力表 | guide=战术手册 | map_overview=全图 | map_zone=区域放大(arg填'x,y') | bases=已知主基地",
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string"}, "arg": {"type": "string"}},
                       "required": ["topic"]},
    },
}
_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "note",
        "description": "写一句决策备注（说明意图/等待理由，不进游戏队列）",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
}

BUILD_TOOLS = [
    {"type": "function", "function": {
        "name": "build",
        "description": "命令建造者建造建筑。action 是建造动作ID（形如 b_extractorT1，必须取自动作表）。坐标尽量省略——系统按布局槽位落位（工厂侧翼成行/后勤主基地背后/海厂近水/采集器最近空闲池）。builder 可指定建造者id，省略时系统自动派空闲的。",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"},
                                      "x": {"type": "number"}, "y": {"type": "number"},
                                      "builder": {"type": "integer"}},
                       "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "train",
        "description": "支援单位生产或建筑升级。只允许：u_builder(建造者)/u_combatEngineer(战斗工程师)/u_mechEngineer(机械师)/u_fabricator(制造者)/u_builderShip(海上建造者)，以及升级动作ID（如 110、extractorT2_0）。战斗单位（坦克/飞机/舰船）归战斗 agent，你生产会被系统拦截。",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"}, "factory": {"type": "integer"}},
                       "required": ["action", "factory"]}}},
    _QUERY_TOOL, _NOTE_TOOL,
]

COMBAT_TOOLS = [
    {"type": "function", "function": {
        "name": "attack",
        "description": "全军主攻 (x,y)（纯水上单位自动拆分走水路，纯防空自动殿后）。",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                       "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "harass",
        "description": "骚扰 (x,y)（同 attack，用于佯攻/牵制/摸资源点）。",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                       "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "attack_nearest",
        "description": "全军压上攻击最近的可见敌人（无需坐标）。犹豫不知道打哪时就先打最近这个。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "defend_home",
        "description": "撤回主基地防守（小队残血/被围/基地遇袭时用）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "rally",
        "description": "设全军集结点 (x,y)：出厂单位自动汇聚。设在主基地靠前/交火方向，别蹲厂里。",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                       "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "set_composition",
        "description": "设定目标军队构成（建造 agent 据此生产）。composition 是 {生产动作ID: 目标数量}，如 {\"u_c_tank\": 12, \"u_missileTank\": 4}。按敌方构成针对性调整（敌空军多补防空，敌龟缩补远程）。",
        "parameters": {"type": "object",
                       "properties": {"composition": {"type": "object"}},
                       "required": ["composition"]}}},
    {"type": "function", "function": {
        "name": "train",
        "description": "军工生产（军产归你）：u_ 前缀的战斗单位动作ID（如 u_c_tank、u_missileTank、u_gunBoat），factory 是我方工厂 id。按目标构成补队列，每厂队列 2~3 个。建造者/工程师/机械师归建造 agent，升级动作也归建造 agent，别碰。",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"}, "factory": {"type": "integer"}},
                       "required": ["action", "factory"]}}},
    {"type": "function", "function": {
        "name": "stop",
        "description": "全军停止当前动作（原地驻防）。",
        "parameters": {"type": "object", "properties": {}}}},
    _QUERY_TOOL, _NOTE_TOOL,
]

COMMANDER_TOOLS = [_QUERY_TOOL, _NOTE_TOOL]


class SharedState:
    """三路循环的共享状态（非聊天室：计划书/构成/留言，外加新对局换代号）。"""

    def __init__(self, ctx):
        self.lock = threading.Lock()
        self.generation = 0           # 新对局 +1：循环据此重建 prompt/摘要器/历史
        self.map_msg = ctx["map_msg"]
        self.world_w = 0.0
        self.world_h = 0.0
        self.mine_spawn = ctx.get("mine_spawn")
        self.enemy_spawns = ctx.get("enemy_spawns") or []
        self.plan = ""                # 作战计划书（总指挥维护）
        self.plan_ts = 0.0
        self.site_blocks = {}         # 建造点位黑名单 (gx,gy)->解封时间（反射召回的敌区点，10 分钟）
        self.requests = deque(maxlen=6)  # 跨域需求板（如 战斗→建造 的 T2 升级需求）
        self.last_rally = None        # 上次集结点（同值去重，防每轮重发）
        self.pending = {"commander": deque(maxlen=8), "build": deque(maxlen=8),
                        "combat": deque(maxlen=8)}  # 玩家留言/系统提示，按路分发
        self.danger_map = ctx["danger_map"]
        self.conn_comp = ctx["conn_comp"]
        self.conn_n = ctx["conn_n"]
        m = self.map_msg or {}
        self.world_w = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
        self.world_h = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)

    def broadcast(self, text):
        with self.lock:
            for q in self.pending.values():
                q.append(text)


# ---------- 系统提示（静态，吃缓存） ----------

def _sys_commander(ctx, st):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    rows = ctx["downsample"](m.get("rows", []), max(1, len(m.get("rows", [])) // 24 or 1))
    res = m.get("resources", [])[:40]
    spawns_txt = f"我方≈{st.mine_spawn}；敌方: " + ",".join(
        f"({int(x)},{int(y)})" for x, y in st.enemy_spawns) if st.enemy_spawns else "未知"
    return f"""你是《Rusted Warfare》的总指挥。你不直接下指令——每 ~50 秒审视全局一次，更新《作战计划书》，建造 agent 与战斗 agent 按它分头执行。

【公平】你只看得见本队视野内的信息，迷雾中的敌情未知，不要臆测。
【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。资源点: {",".join(f"({p['x']},{p['y']})" for p in res)}
【地形图例】.=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯(机甲可爬) #=高崖(仅空军)
{chr(10).join(rows)}
【出生点（公开信息）】{spawns_txt}

【要求】输出计划书全文（≤500字，纯文本直接回复，不要用工具），固定五个小节：
【阶段】开局/扩张/中盘/决战 之一 + 一句话依据
【经济】采集器目标数、建造重点（厂型与顺序）、是否上海军/实验厂
【配兵】军队构成方针（主战/防空/远程比例与具体兵种，针对敌方构成；兵种克制不确定就 query 查武器数据）
【攻防】姿态(attack/defend/expand)与主攻方向（坐标或方位），以及何时转攻
【布局】写到坐标级可执行：厂区方位、防线弧（锚点坐标+每阶段座数+前推时机）、后勤安排、扩张方向
计划书要可被直接执行：给方向不给微操。以稳为先——没有实质局势变化只微调；变更主攻方向必须在【攻防】里给一句局势理由。只输出计划书本身，不要寒暄。"""


def _sys_build(ctx, st):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    return f"""你是《Rusted Warfare》的建造 agent，只管经济建设：采集器/工厂/炮塔/战略建筑/建筑升级 + 支援单位生产（建造者/战斗工程师/机械师/制造者/海上建造者）。战斗单位生产与军事调动归战斗 agent，别碰。

【坐标】世界 {tw:.0f}x{th:.0f} 像素。你只看得见本队视野内信息。
【动作ID规则】建造=b_类型名（如 b_extractorT1）；生产=u_类型名（如 u_c_tank、u_builder）；升级=动作表 升级[...] 里的ID（对建筑用 train）。ID 区分大小写，严禁编造，拿不准用 query 查。
【生产/建造动作表（生产者: 合法动作ID，兵种带 科技,攻击 标签：地/空/双/无）】
{ctx["pedia_production_table"](ctx["pedia"])}

【纪律】
- 选址一律省略坐标：系统按布局落位（工厂在主基地侧翼成行、后勤建筑在主基地背后成排、海厂在近水、采集器在最近空闲资源点）。只有明确知道该建哪才给坐标。
- 建造是有时间的：prog<1 是建造中，完工前工厂不能生产；建造者施工中(build@/repair→)不要派新活，用"空闲可派活"的建造者。
- 资金纪律：资金 <700 时只建采集器；工厂按需扩建（队列长期≥2 且有钱就扩，别一口气盖一排，也别死守 3 座上限）；每厂队列最多 2~3 个。
- 经济优先级：采集器(到计划书目标数) > 首座工厂 > 扩厂/升级 > 炮塔；战斗工程师 1~2 个（能直接建 T2 厂省升级）。
- 支援单位生产（你的专属）：建造者保持 3~4 个（有≥2 个闲置就先派活再产）；水图且有空闲水池时产 1~2 个海上建造者去建水池采集器。
- 升级时机：采集器达标且资金 >2600 开始升经济建筑，工厂资金 >4500 再升。
- 没活干（缺钱/建造者都在施工/目标全达成）时用 note 说明等待理由，不要硬造东西浪费钱。
- 严格按《作战计划书》的经济小节执行；计划书与现状冲突时在 note 里指出。"""


def _sys_combat(ctx, st):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    return f"""你是《Rusted Warfare》的战斗 agent，指挥委托部队打仗 + 军工生产：进攻/防守/骚扰/集结/配兵/工厂产战斗单位。经济建设与支援单位（建造者/工程师/机械师）生产归建造 agent，别碰。

【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。你只看得见本队视野内的信息，敌人离开视野即消失。
【战略格图例】每轮态势附 12x12 格：o=我方 数字=可见敌数 X=混居；地形 .=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯 #=高崖。

【铁律】
- 交战铁律：态势里有警报（遭袭/敌近）时，本回合必须包含军事动词——先活下来再谈别的；不知道打哪就 attack_nearest。
- 禁止军队闲置：构成成型且视野无敌情时，按计划书主攻方向推进/侦察；连续两轮不动是不允许的。
- 小队残血或被围就 defend_home，别硬拼；撤退后补满再按计划书打回去。
- 兵力没到临界（<6 个战斗单位）不决战，先在集结点蓄势。
- 你只给方向，微观走位/集火由游戏AI完成；attack/harass 时纯水上单位会自动拆分走水路，纯防空单位随军行动（充当防空火力）。
- 军产纪律：工厂是你的兵源——按目标构成用 train 持续补队列（每厂 2~3 个，别让工厂空转）；防空占比≤25%（防空是配角）；T2 厂只产 T2 兵（同价更强）。生产与战斗不冲突，有空就补。
- 用 set_composition 针对敌方构成调兵（敌空军多补防空、敌龟缩补远程炮、敌盾厚上闪电系）；兵种武器数据拿不准用 query 查（对地/对空/伤害/射程都有）。
- 严格按《作战计划书》的攻防小节执行；计划书明显过时（如主攻方向已被推平）时按现状决断并在 note 说明。"""


# ---------- 每轮用户文本 ----------

def _combat_status(ctx, st, snap):
    """军队构成现状 vs 目标（战斗循环用，也给建造循环看目标）。"""
    from collections import Counter
    own = snap.get("own", [])
    counts = Counter(u["t"] for u in own if not u.get("bld"))
    ex = ctx["exec_holder"].get("ex")
    comp = (ex.army_goal.get("composition") if ex else None) or {}
    now = ",".join(f"{k}x{v}" for k, v in counts.most_common(8)) or "无"
    tgt = ",".join(f"{k}:{v}" for k, v in comp.items()) or "（未设，用 set_composition 定）"
    return f"我军构成: {now}\n目标构成: {tgt}"


def _user_build(ctx, st, snap, text, receipts):
    parts = [text]
    ex = ctx["exec_holder"].get("ex")
    comp = (ex.army_goal.get("composition") if ex else None) or {}
    if comp:
        parts.append("战斗域目标构成（信息参考：工厂升级优先满足其 T2 需求）: "
                     + ",".join(f"{k}:{v}" for k, v in comp.items()))
    if st.requests:
        parts.append("跨域需求板（按需处理）: " + " | ".join(list(st.requests)[-4:]))
    if st.plan:
        parts.append("【作战计划书】\n" + st.plan)
    if receipts:
        parts.append("最近回执: " + " | ".join(receipts[-6:]))
    pend = _drain(st, "build")
    if pend:
        parts.extend(pend)
    return "\n\n".join(parts)


def _user_combat(ctx, st, snap, text, alerts, receipts):
    parts = [text]
    parts.append("【战略格】o=我方 数字=敌数 X=混居 其余为地形\n" + ctx["strategic_grid"](st.map_msg, snap))
    front = st.danger_map.front_status()
    if front != "unknown":
        parts.append("前线态势: " + front)
    parts.append(_combat_status(ctx, st, snap))
    ex = ctx["exec_holder"].get("ex")
    rally = ex.army_goal.get("rally") if ex else None
    if rally:
        parts.append(f"集结点: ({rally[0]:.0f},{rally[1]:.0f})")
    if st.plan:
        parts.append("【作战计划书】\n" + st.plan)
    if receipts:
        parts.append("最近回执: " + " | ".join(receipts[-6:]))
    pend = _drain(st, "combat")
    if pend:
        parts.extend(pend)
    if alerts:
        parts.append("[硬性] 有警报：本回合必须包含军事动词（attack/attack_nearest/harass/defend_home）。")
    return "\n\n".join(parts)


def _user_commander(ctx, st, snap, text, receipts):
    parts = [text]
    fog = snap.get("fog")
    if fog:
        unexplored = [k for k, v in fog.items() if v < 0.3]
        if unexplored:
            parts.append("未探索区域: " + ",".join(sorted(unexplored)))
    front = st.danger_map.front_status()
    if front != "unknown":
        parts.append("前线态势: " + front)
    parts.append(_combat_status(ctx, st, snap))
    if st.plan:
        parts.append("【当前计划书】（在其基础上修订，除非局势已变）\n" + st.plan)
    else:
        parts.append("（尚无计划书，请输出首版）")
    pend = _drain(st, "commander")
    if pend:
        parts.extend(pend)
    return "\n\n".join(parts)


def _drain(st, who):
    with st.lock:
        out = list(st.pending[who])
        st.pending[who].clear()
    return out


# ---------- 游戏内聊天台推送 ----------

_push_last = {}
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")  # 游戏字体无 emoji 字形（推送前剥掉，防豆腐块）


def _push_chat(ctx, who, text):
    """往游戏内聊天台推一行（NDJSON overlay chat 通道）。限速 1s/路，断连静默。"""
    now = time.time()
    if now - _push_last.get(who, 0) < 1.0:
        return
    _push_last[who] = now
    try:
        ctx["link"].send({"cmd": "overlay", "who": who, "chat": _EMOJI_RE.sub("", str(text))})
    except Exception:
        pass


# ---------- 指令翻译与守卫 ----------

def _facs(snap):
    return [u for u in snap.get("own", [])
            if u.get("bld") and ("actory" in u["t"].lower()
                                 or u["t"].startswith(("commandCenter", "outpost")))]


def _idle_builders(snap):
    return [u for u in snap.get("own", [])
            if ("uilder" in u["t"] or "abricator" in u["t"]) and not u.get("bld")
            and u.get("act") in (None, "", "idle")]


def _translate_build(ctx, st, name, args, snap, g):
    """build agent 的 build/train → 原子指令。返回 (cmd, desc) 或 (None, 拦截原因)。"""
    known = ctx["known_actions"]
    snap_time = time.time()
    if name == "build":
        action = str(args.get("action", ""))
        if action.startswith("s_"):
            action = "b_" + action[2:]
        if action not in known:
            g["fail_perm"].add(("build", action))
            return None, f"✗ build action={action}: 非法ID（必须原样取自动作表），本局不要再试"
        btype = action[2:] if action.startswith("b_") else action
        if snap_time - g["recent_builds"].get(btype, 0) < 30:
            return None, f"✗ build {action}: 30s 内已下过单（防重复），等它出现再补"
        cmd = {"cmd": "build", "action": action}
        # 建造者：显式指定 > 自动派空闲的（施工中绝不拉走）
        if args.get("builder") is not None:
            cmd["builder"] = int(args["builder"])
        else:
            idle = _idle_builders(snap)
            if idle:
                cmd["builder"] = idle[0]["id"]
        # 选址：LLM 给坐标就用；否则按类型填布局槽位（工厂成行/后勤成排/海厂近水）
        if args.get("x") is not None and args.get("y") is not None:
            cmd["x"] = ctx["clamp"](float(args["x"]), 0, st.world_w)
            cmd["y"] = ctx["clamp"](float(args["y"]), 0, st.world_h)
        else:
            spot = _slot_for(ctx, st, snap, btype)
            if spot:
                cmd["x"], cmd["y"] = float(spot[0]), float(spot[1])
        # 敌区采集器死循环封堵：反射层会秒召回赴险建造者，盖不成就会反复重下。
        # 必须用最新快照判定——LLM 调用耗几十秒，决策时的旧快照里敌人可能还没贴上来
        fresh = ctx["link"].snapshot() or snap
        if cmd.get("x") is not None and btype.startswith("extractor") \
                and st.danger_map.danger(cmd["x"], cmd["y"], fresh):
            return None, f"✗ build {action}: 目标点在敌区（危险记忆），从空闲池里换一个安全点"
        # 点位黑名单：近 10 分钟有建造者在此被反射召回（敌区），直接拦截换点
        if cmd.get("x") is not None:
            gkey = (int(cmd["x"] // 400), int(cmd["y"] // 400))
            if snap_time < st.site_blocks.get(gkey, 0):
                return None, f"✗ build {action}: 该点刚有建造者被敌区召回（10 分钟禁建），换其他位置"
        g["recent_builds"][btype] = snap_time
        return cmd, f"build({action})"
    if name == "train":
        action = str(args.get("action", ""))
        fid = int(args.get("factory", -1))
        # LLM 常省略 u_ 前缀（heavyTank → u_heavyTank），先归一化再判域
        if action and not action.startswith(("u_", "b_", "c_", "s_")) \
                and ("u_" + action) in ctx["known_actions"]:
            action = "u_" + action
        if action.startswith("u_") and action not in SUPPORT_PROD:
            return None, f"✗ train {action}: 战斗单位生产归战斗 agent（你只管建造者/工程师/机械师与升级）"
        if snap_time < g.get("action_bad", {}).get(action, 0):
            return None, f"✗ train {action}: 回执学习封禁中（刚被游戏判不可用），换别的或等解封"
        if action not in known:
            g["fail_perm"].add(("train", action))
            return None, f"✗ train action={action}: 非法ID，本局不要再试"
        own_ids = {u["id"] for u in snap.get("own", [])}
        if fid not in own_ids:
            g["fail_perm"].add(("train", action, fid))
            return None, f"✗ train factory={fid}: 不是我方单位id，factory 必须取快照里的#id"
        fac_u = next((u for u in snap.get("own", []) if u["id"] == fid), None)
        if fac_u is not None and (fac_u.get("q") or 0) >= 3:
            g["fail_temp"][("train", action, fid)] = snap_time + 60
            return None, f"✗ train {action}: 该工厂队列已满(3)，扩建新工厂或等队列消化"
        return {"cmd": "train", "action": action, "factory": fid}, f"train({action})"
    return None, None


def _slot_for(ctx, st, snap, btype):
    """布局槽位服务：给建造指令一个确定性落位（executor autopilot 关闭后照样可用）。
    有槽位网格（fp 占地表）时按网格整齐落位。"""
    ex = ctx["exec_holder"].get("ex")
    cc = next((u for u in snap.get("own", []) if u["t"] == "commandCenter" and u.get("bld")), None)
    if not ex or not cc:
        return None
    if btype == "extractorT1" or btype.startswith("extractor"):
        return None  # 采集器交给 agent 资源点九宫格选址
    occ = ex._occupancy(snap)
    if "actory" in btype.lower():
        if btype == "seaFactory":
            return ex.water_anchor(cc)
        facs = [u for u in _facs(snap) if u["t"] != "commandCenter"]
        land = [u for u in facs if u["t"] == "landFactory"]
        idx = sum(1 for u in facs if u["t"] != "seaFactory")
        return ex.factory_slot(cc, idx, land, occ=occ, btype=btype)
    if btype.startswith("fabricator") or "experimental" in btype.lower() or btype == "repairbay":
        idx = sum(1 for u in snap.get("own", []) if u.get("bld")
                  and (u["t"].startswith("fabricator") or "experimental" in u["t"].lower()
                       or u["t"] == "repairbay"))
        return ex.misc_slot(cc, idx, occ=occ, btype=btype)
    return None  # 炮塔等：agent 以主基地为锚螺旋选址


def _translate_combat(ctx, st, name, args, snap, g):
    """战斗 agent 动词 → 原子指令。"""
    link = ctx["link"]
    delegated = set(snap.get("delegated", []))
    if name == "train":  # 军产归战斗（构成是它定的，生产就归它管）；支援单位与升级归建造域
        action = str(args.get("action", ""))
        fid = int(args.get("factory", -1))
        # LLM 常省略 u_ 前缀（heavyTank → u_heavyTank），先归一化再判域
        if action and not action.startswith(("u_", "b_", "c_", "s_")) \
                and ("u_" + action) in ctx["known_actions"]:
            action = "u_" + action
        if action in SUPPORT_PROD or not action.startswith("u_"):
            return None, f"✗ train {action}: 支援单位/升级归建造 agent（你只管战斗单位生产）"
        if time.time() < g.get("action_bad", {}).get(action, 0):
            return None, f"✗ train {action}: 回执学习封禁中（刚被游戏判不可用），换别的或等解封"
        if action not in ctx["known_actions"]:
            g["fail_perm"].add(("train", action))
            return None, f"✗ train action={action}: 非法ID（必须原样取自动作表），本局不要再试"
        own_ids = {u["id"] for u in snap.get("own", [])}
        if fid not in own_ids:
            g["fail_perm"].add(("train", action, fid))
            return None, f"✗ train factory={fid}: 不是我方单位id，factory 必须取快照里的#id"
        fac_u = next((u for u in snap.get("own", []) if u["id"] == fid), None)
        if fac_u is not None and (fac_u.get("q") or 0) >= 3:
            g["fail_temp"][("train", action, fid)] = time.time() + 60
            return None, f"✗ train {action}: 该工厂队列已满(3)，扩建新工厂或等队列消化"
        return {"cmd": "train", "action": action, "factory": fid}, f"train({action})"
    if name in ("attack", "harass"):
        cmd = {"cmd": "attackmove",
               "x": ctx["clamp"](float(args.get("x", 0)), 0, st.world_w),
               "y": ctx["clamp"](float(args.get("y", 0)), 0, st.world_h)}
        _arm_attack(ctx, snap, cmd)
        return cmd, f"{name}({cmd['x']:.0f},{cmd['y']:.0f})"
    if name == "attack_nearest":
        es = snap.get("enemy", [])
        squad = [u for u in snap.get("own", []) if u["id"] in delegated and not u.get("bld")]
        if es and squad:
            cx = sum(u["x"] for u in squad) / len(squad)
            cy = sum(u["y"] for u in squad) / len(squad)
            tgt = min(es, key=lambda e: (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2)
            cmd = {"cmd": "attackmove", "x": float(tgt["x"]), "y": float(tgt["y"])}
        else:
            cmd = {"cmd": "attackmove", "x": st.world_w / 2, "y": st.world_h / 2}  # 无敌情向图心推进
        _arm_attack(ctx, snap, cmd)
        return cmd, "attack_nearest()"
    if name == "defend_home":
        home = _home(snap)
        if not home:
            return None, "defend_home(找不到主基地)"
        cmd = {"cmd": "move", "x": float(home["x"]), "y": float(home["y"])}
        gids = ctx["ground_combat_ids"](snap, delegated)
        if gids:
            cmd["units"] = gids
        return cmd, "defend_home()"
    if name == "stop":
        return {"cmd": "stop"}, "stop()"
    if name == "rally":
        rx = ctx["clamp"](float(args.get("x", 0)), 0, st.world_w)
        ry = ctx["clamp"](float(args.get("y", 0)), 0, st.world_h)
        if st.last_rally and (rx - st.last_rally[0]) ** 2 + (ry - st.last_rally[1]) ** 2 < 100 ** 2:
            return None, f"rally({rx:.0f},{ry:.0f})（未变，跳过）"  # 同值去重，防每轮重发烧 APM
        st.last_rally = (rx, ry)
        facs = [u for u in _facs(snap) if u.get("prog", 1) >= 1]
        ex = ctx["exec_holder"].get("ex")
        if ex:
            ex.set_army_goal(rally=(rx, ry))
        for fac in facs[:6]:  # 每厂一条，集结点统一
            link.send({"cmd": "rally", "factory": fac["id"], "x": rx, "y": ry})
        return None, f"rally({rx:.0f},{ry:.0f})→{len(facs)}厂"
    if name == "set_composition":
        ex = ctx["exec_holder"].get("ex")
        comp = args.get("composition") or {}
        if ex:
            # 归一化后与现值相同则跳过（防每轮重设同一构成）
            norm = {}
            for k, v in comp.items():
                key = str(k)
                if key in ex.known_prod:
                    norm[key] = int(v)
                elif ("u_" + key) in ex.known_prod:
                    norm["u_" + key] = int(v)
            if norm == (ex.army_goal.get("composition") or {}):
                return None, "set_composition（未变，跳过）"
            ex.set_army_goal(composition=comp)
        return None, f"set_composition({comp})"
    return None, None


def _arm_attack(ctx, snap, cmd):
    """attack/harass 共用：地面编队（含纯防空随军）+ 海军拆分。"""
    delegated = set(snap.get("delegated", []))
    gids = ctx["ground_combat_ids"](snap, delegated)
    if gids:
        cmd["units"] = gids
    ctx["naval_split"](ctx["link"], snap, cmd, gids, ctx["map_msg_holder"]["m"], ctx["naval_types"])


def _home(snap):
    own = snap.get("own", [])
    for u in own:
        if u["t"] == "commandCenter":
            return u
    return next((u for u in own if u.get("bld")), None)


# ---------- 循环骨架 ----------

def _apm_ok(win, cap):
    now = time.time()
    while win and now - win[0] > 60:
        win.popleft()
    return len(win) < cap


def _drain_receipts(ctx, link, role):
    """按指令域取回执（别的域留在队列）；train 无独立域，按动作细分（_train_domain）。
    建造域回执同步喂 executor 老学习（动作封禁/池点判死）。"""
    mine, rest = [], deque(maxlen=20)
    with link.lock:
        while link.cmd_results:
            r = link.cmd_results.popleft()
            c = r.get("cmd")
            hit = _train_domain(r.get("msg") or "") == role["name"] if c == "train" \
                else c in role["kinds"]
            (mine if hit else rest).append(r)
        link.cmd_results.extend(rest)
    ex = ctx["exec_holder"].get("ex")
    if ex and role["name"] == "build":
        ex.on_cmd_results(list(mine))
    return [("✓ " if r.get("ok") else "✗ ") + (r.get("msg") or "") for r in mine]


def _learn_receipts(role, receipts, g, st):
    """失败回执自学习（executor 老机制的搬运）：
    - "没有此动作: X" → 动作封 300s（防每分钟撞同一堵墙）
    - "不可用（科技/条件不满足）: X" → 120s
    战斗域 u_ 动作缺失 → 跨域需求板挂 T2 升级需求（建造域可见）。"""
    now = time.time()
    for r in receipts:
        if r.startswith("✓ "):
            continue
        m = re.search(r"没有此动作[:：]\s*([A-Za-z0-9_]+)", r)
        if m:
            act = m.group(1)
            # u_ 军产封 120s（工厂升级后要放行，别误伤新 T2 厂）；升级类 300s
            g.setdefault("action_bad", {})[act] = now + (120 if act.startswith("u_") else 300)
            if role["name"] == "combat" and act.startswith("u_"):
                st.requests.append(f"战斗域需要工厂升级/科技以生产 {act}（回执：没有此动作）")
            continue
        m = re.search(r"不可用（科技/条件不满足）[:：]\s*([A-Za-z0-9_]+)", r)
        if m:
            g.setdefault("action_bad", {})[m.group(1)] = now + 120


def _answer_queries(ctx, st, msg, snap, rounds_left):
    """本地应答 query（DeepSeek 要求每个 tool_call 都有应答）。返回 (msgs_patch, 剩余非query调用)。"""
    calls = msg.get("tool_calls") or []
    patch = [{"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}]
    rest = []
    for tc in calls:
        fn = tc.get("function") or {}
        if fn.get("name") == "query" and rounds_left > 0:
            try:
                qa = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                qa = {}
            ans = ctx["answer_query"](qa.get("topic", ""), qa.get("arg", ""), st.map_msg, ctx["pedia"], snap)
            patch.append({"role": "tool", "tool_call_id": tc.get("id"), "content": str(ans)[:2600]})
        else:
            rest.append(tc)
            patch.append({"role": "tool", "tool_call_id": tc.get("id"),
                          "content": "已收到，将在本回合处理" if fn.get("name") != "query" else "query 次数已用完"})
    return patch, rest


def _agent_loop(ctx, st, role):
    """单 agent 循环：prompt 重建（换代）→ 摘要 → LLM → 翻译 → 发送 → 历史。"""
    cfg = ctx["cfg"]
    gen = -1
    system = None
    smz = None
    history = deque(maxlen=4)
    apm = deque()
    g = {"fail_perm": set(), "fail_temp": {}, "recent_builds": {}}
    time.sleep(role["stagger"])
    while True:
        alerts = []
        try:
            if st.generation != gen:
                gen = st.generation
                system = {"role": "system", "content": role["make_system"](ctx, st)}
                smz = summarize.Summarizer((st.map_msg or {}).get("resources", []))
                # 资源点标注记忆威胁：敌区池进"危险勿去"列表（防建造 agent 往火线上派建造者死循环）
                smz.danger_filter = lambda x, y, s=None: st.danger_map.danger(x, y, s)
                history.clear()
                g["recent_builds"].clear()
                print(f"[{role['name']}] 系统提示已随对局重建")
            link = ctx["link"]
            snap = link.snapshot()
            if not snap or not snap.get("team") or snap["team"].get("ai"):
                time.sleep(1.5)  # 无快照/AI 观战局（本地队由游戏AI控制）：不动作
                continue
            if not ctx["ai_enabled"]() or not ctx["role_enabled"](role["name"]):
                time.sleep(1.5)  # AI 总开关或本角色被设置面板关闭：不动作
                continue
            if role["name"] != "commander" and not snap.get("delegated"):
                time.sleep(1.5)  # 无委托单位：等玩家接管
                continue
            text, alerts = smz.summarize(snap)
            receipts = _drain_receipts(ctx, link, role)
            _learn_receipts(role, receipts, g, st)
            user = role["make_user"](ctx, st, snap, text, alerts, receipts)
            msgs = [system] + list(history) + [{"role": "user", "content": user}]
            msg = llm.chat(cfg["base_url"], ctx["api_key"], role["model"], msgs,
                           role["tools"], timeout=role["timeout"])
            calls = msg.get("tool_calls") or []
            query_rounds = role.get("query_rounds", 2)
            # query 应答后必须再调用 LLM 拿真正决策（此前答完且无非query调用就 break，
            # 模型永远没有产出正式回复的机会——commander 因此一直空转）
            all_calls = []
            while any((tc.get("function") or {}).get("name") == "query" for tc in calls) and query_rounds > 0:
                patch, rest = _answer_queries(ctx, st, msg, snap, query_rounds)
                msgs.extend(patch)
                all_calls.extend(rest)
                query_rounds -= 1
                msg = llm.chat(cfg["base_url"], ctx["api_key"], role["model"], msgs,
                               role["tools"], timeout=role["timeout"])
                calls = msg.get("tool_calls") or []
            # 末轮响应的非 query 调用并入（超次数仍执着 query 的丢弃）
            all_calls.extend(tc for tc in calls if (tc.get("function") or {}).get("name") != "query")
            calls = all_calls
            if role["plan_sink"] and not (msg.get("content") or "").strip():
                # 查询螺旋兜底：收回工具强制纯文本输出（finish=tool_calls+空 content 的根治）
                msgs.append({"role": "user", "content":
                             "[系统强制] 资料已足够。立即以纯文本输出计划书全文（五小节），不许再查资料。"})
                msg = llm.chat(cfg["base_url"], ctx["api_key"], role["model"], msgs,
                               None, timeout=role["timeout"])
            if not calls and role["verbs"]:
                # 无工具调用=空过：升级 reasoner 追问（chat 分析癖的兜底，快慢阶梯的关键一跳）
                retry_model = role.get("ladder_model") or role["model"]
                if retry_model != role["model"]:
                    ctx["log_activity"]("status", f"{role['icon']} {role['name']}: chat 空转，升级 reasoner")
                msgs.append({"role": "user", "content":
                             "[系统强制] 本回合必须至少调用一个工具（指令或 note），不允许只回复文本。"})
                msg = llm.chat(cfg["base_url"], ctx["api_key"], retry_model, msgs,
                               role["tools"], timeout=role["timeout"])
                calls = msg.get("tool_calls") or []
            executed, blocked, note_parts = [], [], []
            if role["plan_sink"]:
                plan = (msg.get("content") or "").strip()
                if len(plan) > 60:
                    st.plan = plan
                    st.plan_ts = time.time()
                    ctx["log_activity"]("status", f"📋 总指挥更新作战计划书（{len(plan)}字）")
                    _push_chat(ctx, "commander", "📋 " + plan)
                    print(f"[commander] 计划书已更新（{len(plan)}字）")
                    executed.append(f"计划书v{int(st.plan_ts) % 10000}")
                elif plan:
                    note_parts.append("（输出过短，未采纳为计划书）")
                    ctx["log_activity"]("note", "📋 总指挥输出过短，未采纳")
            for tc in calls or []:
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
                if name == "query":
                    continue
                key = (name, json.dumps(args, sort_keys=True)[:80])
                if key in g["fail_perm"]:
                    blocked.append(f"{name}(非法，已永封)")
                    continue
                if time.time() < g["fail_temp"].get(key, 0):
                    blocked.append(f"{name}(条件不满足，稍后重试)")
                    continue
                if role["apm_kind"] and not _apm_ok(apm, APM_CAP[role["apm_kind"]]):
                    blocked.append("APM 限速")
                    break
                cmd, desc = role["translate"](ctx, st, name, args, snap, g)
                if desc is None and cmd is None and name not in role["verbs"]:
                    print(f"[{role['name']}] 未知工具: {name}")
                    continue
                if cmd is not None:
                    if link.send(cmd):
                        apm.append(time.time())
                    else:
                        break
                if desc and desc.startswith("✗"):
                    blocked.append(desc[2:])  # 翻译层拦截：进下轮提示，不算"已下发"
                    ctx["log_activity"]("cmd", f"{role['icon']} {desc}")
                elif desc:
                    executed.append(desc)
                    ctx["log_activity"]("cmd", f"{role['icon']} {desc}")
            if blocked:
                st.pending[role["name"]].append("系统拦截（之前已拒绝，不要再试）: " + ", ".join(blocked))
            # 空过违规：整轮只有 query/note、没有任何实质动作 → 下轮硬性要求（note 里说明等待理由的除外）
            if role["plan_sink"] or (role["verbs"] and not executed):
                raw = (msg.get("content") or "")[:150].replace("\n", " ")
                ctx["log_activity"](
                    "note", f"{role['icon']} raw: finish={msg.get('_finish_reason')} "
                            f"calls={[(tc.get('function') or {}).get('name') for tc in (calls or [])]} "
                            f"content={raw!r}")
            if role["verbs"] and not executed and not note_parts:
                st.pending[role["name"]].append(
                    "[系统强制] 你上回合没有任何实质动作（只分析/只查资料）。本回合必须包含 "
                    + "、".join(sorted(role["verbs"])) + " 之一；不知道打哪就 attack_nearest。")
            if role["plan_sink"] and not any(t.startswith("计划书") for t in executed):
                st.pending["commander"].append(
                    "[系统强制] 请直接以纯文本输出计划书全文（【阶段】【经济】【配兵】【攻防】【布局】五小节），"
                    "不要只用 note/query，你的唯一产出就是计划书。")
            history.append({"role": "user", "content": user})
            record = " ".join(note_parts)
            if executed:
                record += (" " if record else "") + "（系统记录：已下发 " + ", ".join(executed) + "）"
            history.append({"role": "assistant", "content": record or "（无动作）"})
            usage = msg.get("_usage") or {}
            if usage:
                print(f"[{role['name']}] tok={usage.get('prompt_tokens', '?')}"
                      f"(hit{usage.get('prompt_cache_hit_tokens', '?')})/{usage.get('completion_tokens', '?')}"
                      f" 动作: {','.join(executed) or '无'}")
            ctx["log_activity"]("status",
                                f"{role['icon']} {role['name']}: {', '.join(executed) or '无动作'}")
            if executed or blocked:
                line = ", ".join(executed + (["✗" + b for b in blocked] if blocked else []))
                _push_chat(ctx, role["name"], f"{role['icon']} {line}")
            elif note_parts:
                # 空转可见性：思考/分析也上聊天台（ truncated ），让用户分得清"在想"还是"死了"
                _push_chat(ctx, role["name"], f"{role['icon']} 💭 " + " ".join(note_parts)[:120])
            else:
                _push_chat(ctx, role["name"], f"{role['icon']} 观察中（无实质动作）")
        except llm.LlmError as e:
            print(f"[{role['name']}] LLM 调用失败: {e}")
            ctx["log_activity"]("error", f"{role['icon']} {role['name']} LLM失败: {e}")
            _push_chat(ctx, "system", f"{role['icon']} {role['name']} LLM失败: {e}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{role['name']}] 循环异常: {e}")
            ctx["log_activity"]("error", f"{role['icon']} {role['name']} 循环异常: {e}")
        sleep_s = role["cadence"]()
        if role["name"] == "combat" and alerts:  # 交战警报：战斗循环加速到快节拍
            sleep_s = float(cfg.get("fast_interval_sec", 2.5))
        time.sleep(sleep_s)


# ---------- 角色定义与启动 ----------

def _roles(ctx):
    cfg = ctx["cfg"]
    fast = cfg.get("model_fast") or cfg["model"]
    return [
        {"name": "commander", "icon": "📋", "model": cfg["model"], "tools": COMMANDER_TOOLS,
         "kinds": set(), "verbs": set(), "plan_sink": True, "apm_kind": None,
         "stagger": 1.0, "cadence": lambda: float(cfg.get("commander_interval_sec", 50)),
         "timeout": max(int(cfg["timeout_sec"]), 120),  # reasoner+全图长 prompt，45s 必超时
         "query_rounds": 4,  # 总指挥是数据收集型，查询预算放宽
         "make_system": _sys_commander,
         "make_user": lambda c, s, snap, text, alerts, rc: _user_commander(c, s, snap, text, rc),
         "translate": lambda *a: (None, None)},
        {"name": "build", "icon": "🔨", "model": fast, "tools": BUILD_TOOLS,
         "kinds": BUILD_KINDS, "verbs": {"build", "train"}, "plan_sink": False, "apm_kind": "build",
         "stagger": 4.0, "cadence": lambda: float(cfg.get("build_interval_sec", 10)),
         "timeout": int(cfg["timeout_sec"]), "ladder_model": cfg["model"],  # 空转升 reasoner
         "make_system": _sys_build,
         "make_user": lambda c, s, snap, text, alerts, rc: _user_build(c, s, snap, text, rc),
         "translate": _translate_build},
        {"name": "combat", "icon": "⚔", "model": fast, "tools": COMBAT_TOOLS,
         # chat 快层（10-25s）+ reasoner 兜底（空转升级）：军事反应不再吃 reasoner 的 40-70s
         "kinds": COMBAT_KINDS, "verbs": {"attack", "harass", "attack_nearest", "defend_home",
                                          "rally", "set_composition", "stop", "train"},
         "plan_sink": False, "apm_kind": "combat",
         "stagger": 7.0, "cadence": lambda: float(cfg.get("combat_interval_sec", 6)),
         "timeout": int(cfg["timeout_sec"]), "ladder_model": cfg["model"],  # 空转升 reasoner
         "make_system": _sys_combat,
         "make_user": _user_combat,
         "translate": _translate_combat},
    ]


def start(ctx):
    """建共享状态 + 三个 agent 循环线程（daemon）。返回 SharedState。"""
    st = SharedState(ctx)
    # 反射事件闭环：建造者召回 → 建造点位黑名单(10min)+建造待读；小队反射 → 战斗待读
    reflex = ctx["reflex"]

    def _on_recall(bid, tx, ty):
        st.site_blocks[(int(tx // 400), int(ty // 400))] = time.time() + 600
        st.pending["build"].append(
            f"[系统通报] 你派往 ({int(tx)},{int(ty)}) 的建造者#{bid} 被系统反射召回（敌区），"
            f"该点附近 10 分钟内禁止再建，请换安全位置。")

    def _on_squad(text):
        st.pending["combat"].append("[系统通报] " + text)

    reflex.on_builder_recall = _on_recall
    reflex.on_squad_reflex = _on_squad
    for role in _roles(ctx):
        threading.Thread(target=_agent_loop, args=(ctx, st, role),
                         daemon=True, name=f"rw-{role['name']}").start()
    print("[multiagent] 三循环已启动：commander(慢) build(快) combat(快)")
    return st


def supervisor_loop(ctx, st):
    """督导线程：新对局重置、反射层、委托热更新、玩家留言分发。"""
    link = ctx["link"]
    seen_gen = 0
    deleg_mtime = None
    smz = summarize.Summarizer((st.map_msg or {}).get("resources", []))
    while True:
        time.sleep(1.0)
        try:
            mv = getattr(link, "map_version", 0)
            if mv != seen_gen:
                seen_gen = mv
                new_map = link.map_message()
                if new_map:
                    with st.lock:
                        st.map_msg = new_map
                        st.world_w = (new_map.get("tilesW", 0) or 0) * (new_map.get("tileW", 0) or 0)
                        st.world_h = (new_map.get("tilesH", 0) or 0) * (new_map.get("tileH", 0) or 0)
                        ctx["map_msg_holder"]["m"] = new_map
                        st.plan = ""
                        st.site_blocks.clear()
                        st.requests.clear()
                        st.last_rally = None
                        st.conn_comp, st.conn_n = connmap.build_components(new_map.get("rows", []))
                        st.danger_map = danger_mod.DangerMap()
                        own_cc0 = next((u for u in (link.snapshot() or {}).get("own", [])
                                        if u["t"] == "commandCenter"), None)
                        st.mine_spawn, st.enemy_spawns = spawns.friendly_spawns(
                            new_map.get("path", ""),
                            (own_cc0["x"], own_cc0["y"]) if own_cc0 else None)
                        if st.enemy_spawns:
                            st.danger_map.seed_bases(st.enemy_spawns)
                    ex = exec_mod.Executor(new_map, ctx["pedia"], ctx["log_activity"])
                    ex.autopilot = False
                    ex.routine = bool(ctx["cfg"].get("routine_layer", True))  # 规则快层：例行+炮塔弧
                    ex.dmap = st.danger_map
                    ctx["exec_holder"]["ex"] = ex
                    ctx["reflex"].dmap = st.danger_map
                    smz = summarize.Summarizer(new_map.get("resources", []))
                    st.generation += 1
                    print("[multiagent] 检测到新对局，三循环上下文已重置")
                    ctx["log_activity"]("status", "新对局开始，多 agent 上下文已重置")
            snap = link.snapshot()
            if snap and snap.get("team") and not snap["team"].get("ai") and ctx["ai_enabled"]():
                home = _home(snap)
                delegated = set(snap.get("delegated", []))
                if delegated:
                    ctx["reflex"].tick(snap, (home["x"], home["y"]) if home else None,
                                       delegated, link.send, ctx["log_activity"])
                st.danger_map.update(snap, (home["x"], home["y"]) if home else None)
            while True:
                with link.lock:
                    if not link.user_msgs:
                        break
                    msg = link.user_msgs.popleft()
                st.broadcast(f"【玩家留言】（优先级最高，照做并说明怎么执行）: {msg}")
                ctx["log_activity"]("chat", f"💬 玩家: {msg}")
                _push_chat(ctx, "player", f"💬 {msg}")
        except Exception as e:
            print(f"[multiagent] 督导异常: {e}")


def run(ctx):
    st = start(ctx)
    supervisor_loop(ctx, st)  # 阻塞
