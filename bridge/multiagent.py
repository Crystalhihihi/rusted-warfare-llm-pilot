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
import math
import os
import re
import threading
import time
from collections import Counter, deque

import connmap
import danger as danger_mod
import executor as exec_mod
import fog
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
        "name": "set_work_order",
        "description": "下发建造工单（你的主要职责）：一组有序建造任务，规则层调度建造者连续施工（完成一个自动接下一个，不用你逐个点）。tasks 是 [{action, slot, note?}]，按执行顺序排，覆盖未来 2~3 分钟。slot 用规格不写坐标：pool:k=第k近安全池 | factory:k=厂区槽位k | defense:k=防线弧位k | misc:k=后勤槽位k | water=近水 | [x,y]=精确坐标（极少用）。",
        "parameters": {"type": "object",
                       "properties": {"tasks": {"type": "array", "items": {"type": "object"}}},
                       "required": ["tasks"]}}},
    {"type": "function", "function": {
        "name": "build",
        "description": "单点建造（工单之外的临时纠正用，优先用 set_work_order）。action 是建造动作ID（形如 b_extractorT1）。坐标省略时系统按槽位落位。",
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

# 建造指挥（thinker，v3.2）：只出工单，无 build/train 直发权
BUILD_THINKER_TOOLS = [
    {"type": "function", "function": {
        "name": "set_work_order",
        "description": "下发建造工单（你的唯一职责）：一组有序建造任务 + 建造者目标数。tasks 是 [{action, slot, note?}]，按执行顺序排，覆盖未来 2~3 分钟。slot 用规格：pool:k=第k近安全池 | factory:k=厂区槽位k | defense:k=防线弧位k | misc:k=后勤槽位k | water=近水 | [x,y]=精确坐标（极少用——你没有逐格地形/占位数据，坐标落点容易压水/压崖/撞占位）。建防线/阵地用阵地单，别写坐标：{action:\"position\", kind:\"defense_ground(对地)|defense_aa(对空)|defense_mixed(混合)\", anchor:[x,y]=阵地锚点, toward:[x,y]=威胁来向, count:座数}——系统按防线弧规则自动展开成弧（格距/防空后排/主通道留空/地形校验），方向对不对全看你给的 anchor 和 toward。",
        "parameters": {"type": "object",
                       "properties": {"tasks": {"type": "array", "items": {"type": "object"}},
                                      "builders_target": {"type": "integer"}},
                       "required": ["tasks"]}}},
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
        self.plan_params = {}         # 计划书 ```json 参数块（机器消费：econ 目标/主攻/防线锚点）
        # 小队编制（军种三层：动态编号 L陆/A空/N海，军种中层写任务，系统维护成员，squad chat 执行；
        # 无守家队——全员出战，守家=炮塔+反射层按距离拉最近队，用户 2026-07-24 拍板）
        self.squads = {}
        # 建造小队 chat（小模型现场执行层）：按域分组（econ 经济/defense 防御阵型），
        # 工单域内条目归它派（规则层让位，45s 静默回退 BuilderManager 兜底）
        self.bgroups = {}
        self.move_map = {u["name"]: u.get("move") for u in (ctx.get("pedia") or {}).get("units", [])}
        self.mission_stale = False    # 任务失效信号（produce 完成/小队团灭 → 召唤战斗思考改派）
        self.plan_ts = 0.0
        self.plan_seen = {}           # 角色 -> 已见计划书版本（全文只在更新时注入，省延迟）
        self.site_blocks = {}         # 建造点位黑名单 (gx,gy)->解封时间（反射召回的敌区点，10 分钟）
        self.requests = deque(maxlen=6)  # 跨域需求板（如 战斗→建造 的 T2 升级需求）
        self.last_rally = None        # 上次集结点（同值去重，防每轮重发）
        self.defeated = False         # 阵亡待机：own 全空持续 15s 判定（队伍资金冻结是旁证）
        self.defeated_since = None    # own 开始全空的时刻（活着不可能是 0=游戏已判负）
        self.pending = {"commander": deque(maxlen=8), "build": deque(maxlen=8),
                        "combat": deque(maxlen=8),  # squad chat 总闸遗留键（反射通报路由前的兜底）
                        "land": deque(maxlen=8), "air": deque(maxlen=8), "navy": deque(maxlen=8)}  # 军种中层
        self.danger_map = ctx["danger_map"]
        self.fog_map = fog.FogMap(self.map_msg)  # 迷雾三态（快照 fogGrid/ghosts 驱动，~10s 一跳）
        self.danger_map.fog = self.fog_map       # 幽灵加热 + 可见冷却（可见空格威胁快速消散）
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
【迷雾三态】可见=真实（没看到敌人=真没有）；浅黑=记忆区（敌建筑为上次所见，可能已变）；全黑=从未探索，主攻/扩张路径上的全黑区先派侦察确认。
【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。资源点: {",".join(f"({p['x']},{p['y']})" for p in res)}
【地形图例】.=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯(机甲可爬) #=高崖(仅空军)
{chr(10).join(rows)}
【出生点（公开信息）】{spawns_txt}

【要求】输出计划书全文（≤500字，纯文本直接回复，不要用工具），固定五个小节：
【阶段】开局/扩张/中盘/决战 之一 + 一句话依据
【经济】采集器目标数、建造重点（厂型与顺序）、是否上海军/实验厂
【配兵】军队构成方针（主战/防空/远程比例与具体兵种，针对敌方构成；兵种克制不确定就 query 查武器数据）。构成要多厂型混编——陆/空/机甲按图制宜（高崖图空军价值高、阶梯图机甲价值高），单一路被克制就是全灭；需要新厂型时在【经济】里点名
【攻防】姿态(attack/defend/expand)与主攻方向（坐标或方位），以及何时转攻
【布局】写到坐标级可执行：厂区方位、防线弧（锚点坐标+每阶段座数+前推时机）、后勤安排、扩张方向
计划书要可被直接执行：给方向不给微操。以稳为先——没有实质局势变化只微调；变更主攻方向必须在【攻防】里给一句局势理由。
文本之后必须追加一个 ```json 参数块（机器消费，建造/战斗域照此执行，字段必填）：
```json
{{"阶段": "扩张", "采集器目标": 5, "建造者目标": 4, "厂型清单": ["landFactory","airFactory"],
 "主攻": [1200, 800], "防线": {{"锚点": [1830, 1100], "座数": 8}}, "姿态": "expand"}}
```
只输出计划书本身，不要寒暄。"""


def _sys_build(ctx, st):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    return f"""你是《Rusted Warfare》的建造规划员。你不搬砖——规则层（2 秒一拍）负责施工调度、采集器补给、工厂扩建升级、防线炮塔、抢修。你的活是**出规划**：

【首要职责：set_work_order】
被召唤时给一份覆盖未来 2~3 分钟的有序建造工单，规则层会调度建造者连续施工（完成一个自动接下一个，像玩家拉一长条）。工单原则：
- 序列即规划：先经济（采集器 pool:k 从近到远）、再产能（factory:k）、再防线（defense:k）与大建筑（misc:k/water）
- 每份工单 4~10 项，别贪多；阶段要匹配计划书【阶段】（开局=池+厂，扩张=池+厂+防线，中盘=升级链+战略建筑）
- slot 一律用规格（pool:k/factory:k/defense:k/misc:k/water），系统按槽位网格+实时占位解析落位，别写坐标
- 之前失败过的点位系统在黑名单里，你规划的规格会自动避让，不用管
- 建造不许停："看起来够用"不是完工标准——对照计划书逐项核对（采集器数/厂数/防线成型），没达标就在工单执行完毕时立即续下一份；回答"无动作/等待"只在资金断流且无事可建时才合法，否则按空过违规处理

【次要职责】
- 支援单位生产：建造者保持 3~4 个（CC 生产）；水图产 1~2 个海上建造者（建水池采集器）
- 临时纠正：规则层方向明显错时用 build 单点纠正（极少用）
- 需求板/计划书里有你的活就干；没有就用 note 说明等待

【坐标】世界 {tw:.0f}x{th:.0f} 像素。你只看得见本队视野内信息。
【动作ID规则】建造=b_类型名；生产=u_类型名；升级=动作表 升级[...] 里的ID（对建筑用 train）。ID 区分大小写，严禁编造，拿不准用 query 查。
【生产/建造动作表（生产者: 合法动作ID，兵种带 科技,攻击 标签：地/空/双/无）】
{ctx["pedia_production_table"](ctx["pedia"])}

【纪律】
- 资金纪律：决策看收入率不看余额（摘要"收入+X/s 支出-Y/s"是游戏原生数据）——收入 <5 先补采集器链；余额高但收入低同样是穷；大建筑（实验厂/核弹系）只在收入 ≥15 时考虑。资金 <700 只留作采集器；每厂队列最多 2~3 个。
- 不许编造原因：资金/收入/单位/警报全部来自快照，没看到的别瞎说（摘要没写"采集器被打了"就是没有）。
- 战斗单位生产与军事调动归战斗 agent，别碰。
- 严格按《作战计划书》的经济小节执行；计划书与现状冲突时在 note 里指出。"""


def _sys_build_thinker(ctx, st):
    """建造指挥（pro 思考版，v3.2）：出参数化工单，不搬砖不聊天。"""
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    params = getattr(st, "plan_params", None) or {}
    return f"""你是《Rusted Warfare》的建造指挥。规则层（2 秒一拍）负责施工调度、采集器补给、池点哨戒、抢修重建、工厂升级——这些不用你管。你只出**参数化规划**：

【职责：set_work_order（唯一动作面）】
被召唤时给一份覆盖未来 2~3 分钟的有序建造工单 + 建造者目标数（builders_target，按局势 3~8 个）。工单原则：
- 序列即规划：先经济（pool:k 从近到远）、再产能（factory:k）、再防线与大建筑
- 防线点位必须给**精确坐标**（slot=[x,y]），由你按布局规则决定，不许甩给 defense:k：
  防线弧在基地面朝敌侧展开，格距=建筑宽+3 格；主通道（基地→图心方向±200px 走廊）留空不堵；
  防空压后排（比炮塔线靠后 60px+）；维修湾在防线内圈；家圈(≤1200px)内是安全区可放心盖
- 厂区槽位 factory:k 贴死密排成排；大建筑 misc:k 在基地背后
- 每份工单 4~10 项；阶段匹配计划书参数块
- 失败点位系统在黑名单里会自动避让，不用重复试

【布局规则（防堵路）】
建筑之间留缝：小建筑≥40px 节距、工厂≥60px；别把建筑盖在部队集结点/主通道上；
防线弧开口朝敌，别把基地围死。

【计划书参数块（机器消费，照此执行，不是参考）】
{json.dumps(params, ensure_ascii=False) if params else "（暂无——按开局默认：3 池 2 厂 1 防线弧）"}

【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。你只看得见本队视野内信息。
【动作ID规则】建造=b_类型名。ID 区分大小写，严禁编造，拿不准用 query 查价格表。

【纪律】
- 资金纪律：决策看收入率不看余额——收入 <5 先补采集器链；大建筑只在收入 ≥15 时考虑。
- 建造不许停：对照计划书参数逐项核对（池数/厂数/防线座数/建造者数），没达标就在工单执行完毕时立即续下一份。
- 不许编造原因：资金/收入/单位/警报全部来自快照，没看到的别瞎说。
- 战斗单位生产与军事调动归战斗域，别碰。"""


def _sys_combat(ctx, st):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    return f"""你是《Rusted Warfare》的战斗 agent，指挥委托部队打仗 + 军工生产：进攻/防守/骚扰/集结/配兵/工厂产战斗单位。经济建设与支援单位（建造者/工程师/机械师）生产归建造 agent，别碰。

【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。你只看得见本队视野内的信息，敌人离开视野即消失（摘要里的"最后可见敌情"与浅黑区"记忆中的敌建筑"是历史信息不是现状；可见区没敌人=真没有；全黑=未知，先侦察再进）。
【战略格图例】每轮态势附 12x12 格：o=我方 数字=可见敌数 X=混居；地形 .=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯 #=高崖。

【铁律】
- 交战铁律：态势里有警报（遭袭/敌近）时，本回合必须包含军事动词——先活下来再谈别的；不知道打哪就 attack_nearest。
- 禁止军队闲置：构成成型且视野无敌情时，按计划书主攻方向推进/侦察；连续两轮不动是不允许的。
- 小队残血或被围就 defend_home，别硬拼；撤退后补满再按计划书打回去。
- 兵力没到临界（<6 个战斗单位）不决战，先在集结点蓄势。
- 你只给方向，微观走位/集火由游戏AI完成；attack/harass 时纯水上单位会自动拆分走水路，纯防空单位随军行动（充当防空火力）；海厂集结点自动落到最近水域、海军回防自动去近家水域（海军的坐标系统会按地图水域处理，不用你想）。
- 军产纪律：工厂是你的兵源——按目标构成用 train 持续补队列（每厂 2~3 个，别让工厂空转）；防空占比≤25%（防空是配角）；T2 厂只产 T2 兵（同价更强）。生产与战斗不冲突，有空就补。
- 用 set_composition 针对敌方构成调兵（敌空军多补防空、敌龟缩补远程炮、敌盾厚上闪电系）；兵种武器数据拿不准用 query 查（对地/对空/伤害/射程都有）。需要现有厂型产不出的兵种（空军/机甲/海军）时，用 note 把需求写清楚挂需求板，建造域会看到。
- 严格按《作战计划书》的攻防小节执行；计划书明显过时（如主攻方向已被推平）时按现状决断并在 note 说明。"""


# ---------- 每轮用户文本 ----------

def _build_event(ctx, st, snap, alerts, now, g):
    """建造 LLM 召唤条件：它是规划员不是搬砖的——只在需要新规划时召唤。"""
    with st.lock:
        if st.pending["build"]:
            return "待读消息"
    if st.plan_ts > g.get("plan_ts_fired", 0):
        g["plan_ts_fired"] = st.plan_ts
        return "计划书更新"
    if st.requests:
        return "需求板新单"
    ex = ctx["exec_holder"].get("ex")
    if ex:
        if not ex.work_order:
            return "无工单待规划"
        open_n = sum(1 for t in ex.work_order if t["status"] in ("pending", "assigned"))
        if open_n == 0:
            return "工单执行完毕"
        if open_n <= 2:
            return f"工单低水位（剩{open_n}项，续单防建造者闲置）"
    if now - g.get("last_fire", 0) > 180:
        return "心跳"
    return None


def _combat_event(ctx, st, snap, alerts, now, g):
    """战斗 LLM 召唤条件（事件触发替代固定节拍轮询——文献共识：稳态期零调用）。
    返回触发原因或 None。心跳兜底 90s（防漏报导致长期不召唤）。"""
    if alerts:
        return "警报: " + alerts[0]
    with st.lock:
        if st.pending["combat"]:
            return "待读消息"
    if st.plan_ts > g.get("plan_ts_fired", 0):
        g["plan_ts_fired"] = st.plan_ts
        return "计划书更新"
    ex = ctx["exec_holder"].get("ex")
    comp = (ex.army_goal.get("composition") if ex else None) or {}
    from collections import Counter
    counts = Counter(u["t"] for u in snap.get("own", []) if not u.get("bld"))
    army = sum(counts.values())
    if comp:
        # 蓄势触发：构成补齐 ≥70% 就召唤（旧版要求缺口=0 从没满足过——完美主义等于没有触发）
        total = sum(comp.values()) or 1
        deficit = sum(max(0, tgt - counts.get(a[2:] if a.startswith("u_") else a, 0))
                      for a, tgt in comp.items())
        if deficit / total <= 0.3 and now - g.get("last_fire", 0) > 60:
            return f"蓄势已成（兵力{total - deficit}/{total}），按计划书行动"
    # 兵力+姿态触发：军队成型（≥8）且姿态非死守 → 该动了（构成未设也触发，先于心跳）
    posture = str((st.plan_params or {}).get("姿态", ""))
    if army >= 8 and posture not in ("defend", "龟缩") and now - g.get("last_fire", 0) > 90:
        return f"兵力已备（{army}员）且姿态{posture or '未定'}非死守，该动了"
    # 心跳/蓄势：军队未成气候(<6)且无敌情时不召唤——规则层产兵蓄势即可，
    # 几个兵就拉出去打等于喂敌（此前心跳无脑召唤导致小兵送死循环）
    enemies_n = len(snap.get("enemy", []))
    if now - g.get("last_fire", 0) > 90 and (army >= 6 or enemies_n > 0):
        return "心跳"
    return None


def _combat_status(ctx, st, snap):
    """军队构成现状 vs 目标（战斗循环用，也给建造循环看目标），附战力概况。"""
    from collections import Counter
    own = snap.get("own", [])
    counts = Counter(u["t"] for u in own if not u.get("bld"))
    ex = ctx["exec_holder"].get("ex")
    comp = (ex.army_goal.get("composition") if ex else None) or {}
    now = ",".join(f"{k}x{v}" for k, v in counts.most_common(8)) or "无"
    tgt = ",".join(f"{k}:{v}" for k, v in comp.items()) or "（未设，用 set_composition 定）"
    pw = ("\n" + ex.control.overview(snap)) if ex else ""
    return f"我军构成: {now}\n目标构成: {tgt}{pw}"


def _plan_block(st, who):
    """计划书注入：只在更新后给全文（每轮全文 ~700 字纯属烧延迟），未变给一行提示。"""
    if not st.plan:
        return None
    if st.plan_seen.get(who) == st.plan_ts:
        return "（作战计划书未变，按上版执行）"
    st.plan_seen[who] = st.plan_ts
    return "【作战计划书】\n" + st.plan


def _fog_intel(st, snap):
    """迷雾三态情报段（fogGrid/ghosts 新鲜时才有）：模式语义/覆盖比例/全黑方位/记忆敌建筑/最后可见敌情。"""
    fm = st.fog_map
    if not fm.fresh():
        return None
    parts = ["【迷雾】" + fm.mode_text()]
    fr = fm.fracs()
    if fr and fm.enabled():
        parts.append(f"视野覆盖: 可见{fr[0] * 100:.0f}% 浅黑{fr[1] * 100:.0f}% 全黑{fr[2] * 100:.0f}%")
        bz = fm.black_zones()
        if bz:
            parts.append("全黑方位: " + ",".join(bz) + "（未知区域，进攻/扩张前先侦察）")
    if fm.ghosts:
        gh = "; ".join(f"{g.get('t', '?')}@({int(g.get('x', 0))},{int(g.get('y', 0))})"
                       for g in fm.ghosts[:8])
        parts.append(f"记忆中的敌建筑（浅黑区，上次所见可能已变，共{len(fm.ghosts)}座）: {gh}")
    ls = st.danger_map.last_seen
    if ls:
        now = time.time()
        gone = sorted(((now - v[3], v) for v in ls.values() if now - v[3] > 15),
                      key=lambda t: t[0])[:6]
        if gone:
            parts.append("最后可见敌情（已离开视野）: " + "; ".join(
                f"{v[2]}@({int(v[0])},{int(v[1])}) {int(age)}s前" for age, v in gone))
    return "\n".join(parts)


def _user_build(ctx, st, snap, text, receipts):
    parts = [text]
    fi = _fog_intel(st, snap)
    if fi:
        parts.append(fi)
    ex = ctx["exec_holder"].get("ex")
    if ex is not None:
        sp = getattr(ex, "last_safe_pools", None)
        if sp:
            parts.append("可用安全池（pool:k 只许用这里的下标，别凭空编号）: "
                         + " ".join(f"pool:{k}=({int(p['x'])},{int(p['y'])})"
                                    for k, p in enumerate(sp[:8]))
                         + (f" 共{len(sp)}个" if len(sp) > 8 else ""))
        else:
            parts.append("可用安全池：0 个——pool:k 现在不可用（被威胁/未探明卡闸），"
                         "先下炮塔/工厂或等威胁清除，别下 pool 单")
        pools_all = (st.map_msg or {}).get("resources", [])[:10]
        cc2 = next((u for u in snap.get("own", []) if u.get("bld") and u["t"] == "commandCenter"), None)
        if pools_all and cc2:
            lbls = []
            for p in pools_all:
                if p.get("w"):
                    lbl = "水池"
                elif (p["x"] - cc2["x"]) ** 2 + (p["y"] - cc2["y"]) ** 2 <= 600 ** 2:
                    lbl = "家圈"
                elif ex.control.secured(p["x"], p["y"], time.time()):
                    lbl = "安稳"
                else:
                    lbl = "未控勿去"
                lbls.append(f"({int(p['x'])},{int(p['y'])}){lbl}")
            parts.append("资源池实控状态（采集器只许去 家圈/安稳，未控=敌区/争夺区，要去先派兵实控）: "
                         + " ".join(lbls))
        # 推荐防线/经济锚点（隘口必守/防线贴实控边界/经济区贴制造仪群——"哪里需要防护"的规则依据）
        if cc2:
            ax2, ay2 = (st.world_w or 0) / 2.0 - cc2["x"], (st.world_h or 0) / 2.0 - cc2["y"]
            alen2 = max(1.0, math.hypot(ax2, ay2))
            axis2 = (ax2 / alen2, ay2 / alen2)
            recs = [f"隘口({int(x)},{int(y)})宽{w}px" for x, y, w in ex.chokes(cc2, axis2)]
            fa = ex.frontier_anchor(cc2, axis2, time.time())
            if fa:
                recs.append(f"实控前沿({int(fa[0])},{int(fa[1])})安全值{fa[2]:.0f}")
            fabs = [u for u in snap.get("own", []) if u.get("bld") and u["t"].startswith("fabricator")]
            if len(fabs) >= 3:
                fx = sum(u["x"] for u in fabs) / len(fabs)
                fy = sum(u["y"] for u in fabs) / len(fabs)
                recs.append(f"制造仪群({int(fx)},{int(fy)})共{len(fabs)}座（经济区贴此扩）")
            if recs:
                parts.append("推荐锚点（可采纳也可推翻，推翻写理由）: " + " | ".join(recs))
    comp = (ex.army_goal.get("composition") if ex else None) or {}
    if comp:
        parts.append("战斗域目标构成（信息参考：工厂升级优先满足其 T2 需求）: "
                     + ",".join(f"{k}:{v}" for k, v in comp.items()))
    if st.requests:
        parts.append("跨域需求板（按需处理）: " + " | ".join(list(st.requests)[-4:]))
    pb = _plan_block(st, "build")
    if pb:
        parts.append(pb)
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
    fi = _fog_intel(st, snap)
    if fi:
        parts.append(fi)
    ex = ctx["exec_holder"].get("ex")
    rally = ex.army_goal.get("rally") if ex else None
    if rally:
        parts.append(f"集结点: ({rally[0]:.0f},{rally[1]:.0f})")
    pb = _plan_block(st, "combat")
    if pb:
        parts.append(pb)
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
    fi = _fog_intel(st, snap)
    if fi:
        parts.append(fi)
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
            if ("uilder" in u["t"] or "abricator" in u["t"])
            and "editor" not in u["t"].lower() and not u.get("bld")
            and u.get("act") in (None, "", "idle")]


def _translate_build(ctx, st, name, args, snap, g):
    """build agent 的工单/建造/生产 → 原子指令。返回 (cmd, desc) 或 (None, 拦截原因)。"""
    known = ctx["known_actions"]
    snap_time = time.time()
    if name == "set_work_order":
        ok, bad = [], 0
        for t in (args.get("tasks") or [])[:24]:
            a = str((t or {}).get("action", ""))
            if not a:
                bad += 1
                continue
            if a == "position":  # 阵地单：LLM 只给锚点/朝向/类型/座数，executor 按防线弧展开
                anchor = (t or {}).get("anchor")
                if not (isinstance(anchor, list) and len(anchor) == 2):
                    bad += 1
                    continue
                kind = str((t or {}).get("kind", "defense_mixed"))
                if kind not in ("defense_ground", "defense_aa", "defense_mixed"):
                    kind = "defense_mixed"
                ok.append({"action": "position", "kind": kind,
                           "anchor": [float(anchor[0]), float(anchor[1])],
                           "toward": (t or {}).get("toward"),
                           "count": (t or {}).get("count"),
                           "note": str((t or {}).get("note", ""))})
                continue
            if not a.startswith(("b_", "u_", "c_")):
                a = "b_" + a
            if a not in known:
                bad += 1
                continue
            ok.append({"action": a, "slot": (t or {}).get("slot"), "note": str((t or {}).get("note", ""))})
        if not ok:
            return None, f"✗ set_work_order: 全部{len(args.get('tasks') or [])}项动作ID非法（必须原样取自动作表；阵地单 action=position 需 anchor）"
        ex = ctx["exec_holder"].get("ex")
        if ex:
            ex.set_work_order(ok, snap)
            # 建造者目标数（建造思考按局势定）：写进经济目标，规则层照此补建造者
            bt = args.get("builders_target")
            if isinstance(bt, int) and 0 < bt <= 12:
                ex.econ_goal["builders"] = bt
        return None, (f"set_work_order({len(ok)}项已下发" + (f"，丢弃非法{bad}项" if bad else "")
                      + (f"，建造者目标{args['builders_target']}" if isinstance(args.get("builders_target"), int) else "") + "）")
    if name == "build":
        action = str(args.get("action", ""))
        if action.startswith("s_"):
            action = "b_" + action[2:]
        if action not in known:
            g["fail_perm"].add(("build", action))
            return None, f"✗ build action={action}: 非法ID（必须原样取自动作表），本局不要再试"
        btype = action[2:] if action.startswith("b_") else action
        funds_now = (snap.get("team") or {}).get("funds", 0)
        if funds_now < 700:
            return None, f"✗ build {action}: 资金仅{funds_now:.0f}<700，先留钱建采集器恢复经济，别的不许盖"
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
        # 点位黑名单：近 10 分钟有建造者在此被反射召回（敌区）。
        # 防守类建筑在防线圈(≤1200px)内豁免——防线挨打正是该盖的时候（黑名单初衷是治远征乱跑）
        if cmd.get("x") is not None:
            gkey = (int(cmd["x"] // 400), int(cmd["y"] // 400))
            if snap_time < st.site_blocks.get(gkey, 0):
                cc = next((u for u in snap.get("own", []) if u["t"] == "commandCenter"), None)
                near_home = cc and (cmd["x"] - cc["x"]) ** 2 + (cmd["y"] - cc["y"]) ** 2 <= 1200 ** 2
                is_defense = btype.startswith(("c_turret", "c_antiAirTurret", "laserDefence", "repairbay", "wall"))
                if not (is_defense and near_home):
                    return None, f"✗ build {action}: 该点刚有建造者被敌区召回（10 分钟禁建），换其他位置"
            g["recent_build_xy"] = (cmd["x"], cmd["y"], snap_time)  # 选址失败回执归因用
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
        nids = set()
        if gids and ctx["naval_types"]:
            nids = {str(u["id"]) for u in snap.get("own", [])
                    if u["id"] in delegated and not u.get("bld") and u["t"] in ctx["naval_types"]}
        if nids:
            # 海军回防不去陆地家：改去近家水域（海军要水不要地）
            gset = [i for i in gids.split(",") if i and i not in nids]
            cmd["units"] = ",".join(gset)
            ex = ctx["exec_holder"].get("ex")
            cc = next((u for u in snap.get("own", []) if u["t"] == "commandCenter"), None)
            wa = ex.water_anchor(cc) if ex and cc else None
            if wa:
                link.send({"cmd": "move", "units": ",".join(nids),
                           "x": float(wa[0]), "y": float(wa[1])})
            else:
                cmd["units"] = gids  # 无水域：全员回家（海军将就上岸）
        elif gids:
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
        map_m = ctx["map_msg_holder"]["m"]
        cc = next((u for u in snap.get("own", []) if u["t"] == "commandCenter"), None)
        sea_spot = None
        for fac in facs[:6]:  # 每厂一条，集结点统一；海厂自动改落最近水域（海军要水不要地）
            if fac["t"] == "seaFactory":
                if sea_spot is None:
                    sea_spot = (ctx["nearest_water"](map_m, rx, ry)
                                or (ex.water_anchor(cc) if ex and cc else None))
                if sea_spot:
                    link.send({"cmd": "rally", "factory": fac["id"],
                               "x": float(sea_spot[0]), "y": float(sea_spot[1])})
            else:
                link.send({"cmd": "rally", "factory": fac["id"], "x": rx, "y": ry})
        return None, f"rally({rx:.0f},{ry:.0f})→{len(facs)}厂"
    if name == "set_composition":
        ex = ctx["exec_holder"].get("ex")
        comp = args.get("composition") or {}
        if ex:
            # 科技过滤：构成本厂当前可产的才算数（没 T2 厂要 heavyTank = 生产死锁，先升级再谈）
            avail = set()
            for u in _facs(snap):
                if u.get("prog", 1) >= 1:
                    avail |= ex.factory_actions.get(u["t"], set())
            norm, dropped = {}, []
            for k, v in comp.items():
                key = str(k)
                act = key if key in ex.known_prod else ("u_" + key if ("u_" + key) in ex.known_prod else None)
                if act is None:
                    dropped.append(key)
                elif avail and act not in avail:
                    dropped.append(f"{key}(本厂暂无此动作/科技未达)")
                else:
                    norm[act] = int(v)
            if norm == (ex.army_goal.get("composition") or {}):
                if dropped:
                    return None, f"set_composition（未变，跳过）；已丢弃不可产项: {','.join(dropped)}"
                return None, "set_composition（未变，跳过）"
            ex.set_army_goal(composition=norm if norm else comp)
            if dropped:
                st.requests.append(f"战斗域需要工厂升级以生产: {','.join(dropped)}")
                return None, f"set_composition({norm})；已丢弃不可产项: {','.join(dropped)}（升级已挂需求板）"
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


# ---------- 小队子系统（军种三层：动态编号 L陆/A空/N海，军种过滤归队，编号系统校验） ----------

_BRANCH_PREFIX = {"land": "L", "air": "A", "navy": "N"}
_BRANCH_OF_MOVE = {"AIR": "air", "WATER": "navy",
                   "LAND": "land", "HOVER": "land",  # 悬浮/两栖归陆军（允许过水，用户拍板）
                   "OVER_CLIFF": "land", "OVER_CLIFF_WATER": "land"}
_SUPPORT_KEYS = ("uilder", "abricator", "ngineer")
# 编成提示关键词 → 单位名子串（中层写中文编成，系统按此归队；军种过滤优先于 hint）
_HINT_MAP = (
    ("防空", ("missileTank", "interceptor", "antiAir", "flak", "heavyInterceptor")),
    ("炮兵", ("artillery",)),
    ("机甲", ("mech",)),
    ("侦察", ("scout",)),
    ("海军", ("ship", "boat", "sub", "hovercraft")),
    ("空军", ("gunship", "jet", "bomber", "helicopter", "airship", "drone")),
)

_CAPS_OVERRIDE = {}  # dashboard /api/squads 写入（运行中改，下拍生效）


def _branch_of(unit_type, move_map):
    """单位类型 → 军种（按 pedia move 域：AIR=空 WATER=海 其余=陆；未知按陆）。"""
    return _BRANCH_OF_MOVE.get(move_map.get(unit_type) or "", "land")


def _sid_branch(sid):
    return {"L": "land", "A": "air", "N": "navy"}.get(sid[:1], "land")


def _next_squad_id(squads, branch):
    k = 1
    while f"{_BRANCH_PREFIX[branch]}{k}" in squads:
        k += 1
    return f"{_BRANCH_PREFIX[branch]}{k}"


def _current_caps(ctx):
    """军种小队数上限：面板覆盖层优先，缺省读 config（squad_cap_land/air/navy）。"""
    cfg = ctx.get("cfg") or {}
    default = {"land": 3, "air": 2, "navy": 1}
    return {br: int(_CAPS_OVERRIDE[br] if _CAPS_OVERRIDE.get(br) is not None
                    else cfg.get(f"squad_cap_{br}", default[br]))
            for br in ("land", "air", "navy")}


def squad_assign(ctx, st, snap):
    """战斗单位归队（军种三层）：按 move 类型分军种；军种内编成 hint 加分+人数最少优先；
    无队且未达上限开新队（达上限等下拍）；某队 >2×军种均值且未达上限→拆分一半成新队；
    阵亡清零且无任务的小队注销。无守家队：新单位全部进军种小队，不囤。"""
    combat = {u["id"]: u for u in snap.get("own", [])
              if not u.get("bld") and not any(k in u["t"] for k in _SUPPORT_KEYS)}
    # 阵亡清除 + 空队注销
    for sid in list(st.squads):
        sq = st.squads[sid]
        sq["members"] = {i for i in sq["members"] if i in combat}
        if not sq["members"] and sq.get("mission") is None:
            del st.squads[sid]
    caps = _current_caps(ctx)
    assigned = set().union(*(sq["members"] for sq in st.squads.values())) if st.squads else set()
    for uid, u in combat.items():
        if uid in assigned:
            continue
        br = _branch_of(u["t"], st.move_map)
        best_sid, best_score = None, -1e9
        for sid, sq in st.squads.items():
            if _sid_branch(sid) != br:
                continue  # 军种绝不越界
            hint = ((sq.get("mission") or {}).get("编成") or "")
            score = 0.0
            for kw, pats in _HINT_MAP:
                if kw in hint and any(p in u["t"] for p in pats):
                    score = 2.0
                    break
            score -= len(sq["members"]) * 1.0  # 人数最少优先
            if score > best_score:
                best_sid, best_score = sid, score
        if best_sid is None:
            if sum(1 for sid in st.squads if _sid_branch(sid) == br) >= caps.get(br, 1):
                continue  # 已达军种上限：等拆分/注销腾位（下拍再归）
            best_sid = _next_squad_id(st.squads, br)
            st.squads[best_sid] = {"mission": None, "members": set()}
        st.squads[best_sid]["members"].add(uid)
        assigned.add(uid)
    # 拆分：某队 ≥8 且 ≥2×(军种均值含拟建队) 且未达上限 → 分一半成新队（任务不带，等中层改派）
    for br in ("land", "air", "navy"):
        sids = [sid for sid in st.squads if _sid_branch(sid) == br]
        if not sids:
            continue
        while len(sids) < caps.get(br, 1):
            total = sum(len(st.squads[s]["members"]) for s in sids)
            donor = next((s for s in sids
                          if len(st.squads[s]["members"]) >= 8
                          and len(st.squads[s]["members"]) >= 2 * (total / (len(sids) + 1))), None)
            if donor is None:
                break
            half = sorted(st.squads[donor]["members"])[: len(st.squads[donor]["members"]) // 2]
            nid = _next_squad_id(st.squads, br)
            st.squads[nid] = {"mission": None, "members": set(half)}
            st.squads[donor]["members"] -= set(half)
            sids.append(nid)


SET_MISSIONS_TOOL = {"type": "function", "function": {
    "name": "set_missions",
    "description": "下发《小队任务书》（全量替换）：给编号小队定任务。squads=[{id,任务,目标?,编成?,备注?,动作?,数量?}]。id 只允许当前存在的编号（L陆/A空/N海+数字，系统分配，不许编造）；任务=attack(目标[x,y])/defend(守家)/hold(集结待命)/harass(目标[x,y])/produce(动作=生产动作ID,数量=累计目标数)。未点名的小队维持原任务。",
    "parameters": {"type": "object",
                   "properties": {"squads": {"type": "array", "items": {"type": "object"}}},
                   "required": ["squads"]}}}


def _translate_missions(ctx, st, args, g):
    """任务书校验+落盘：非法编号报错忽略（防混淆），合法项写入 st.squads。"""
    ok, errs = [], []
    for it in (args.get("squads") or [])[:8]:
        if not isinstance(it, dict):
            errs.append("非对象项")
            continue
        sid = str(it.get("id", ""))
        if sid not in st.squads:
            errs.append(f"{sid}不存在（当前: {'/'.join(st.squads) or '无'}）")
            continue
        mission = {"任务": str(it.get("任务", "hold")), "编成": str(it.get("编成", "")),
                   "备注": str(it.get("备注", "")), "规模": int(it.get("规模") or 0)}
        tgt = it.get("目标")
        if isinstance(tgt, list) and len(tgt) == 2:
            mission["目标"] = [float(tgt[0]), float(tgt[1])]
        else:
            mission["目标"] = None
        if mission["任务"] == "produce":
            mission["动作"] = str(it.get("动作", ""))
            mission["数量"] = int(it.get("数量", 4) or 4)
            mission["_done"] = False
        st.squads[sid]["mission"] = mission
        st.squads[sid]["mission_ts"] = time.time()  # 看门狗宽限起算点（先让 LLM 打，45s 不动才代打）
        ok.append(f"{sid}:{mission['任务']}")
    desc = ("set_missions(" + ",".join(ok) + ("；忽略: " + ",".join(errs) if errs else "") + ")") \
        if ok else f"✗ set_missions: 全部非法（当前: {'/'.join(st.squads) or '无'}）: {','.join(errs)}"
    return None, desc


def _produce_tick(ctx, st, snap):
    """任务书 produce 条目 → train（定向猛造的例外通道；常规补水归规则层⑦构成驱动）。
    累计在役数达标即任务完成（标记待思考改派），工厂挑选吃按厂封禁/可用表学习。
    活跃 produce 条目同步进 ex.produce_focus——规则层补水避开该兵种，防撞车。"""
    ex = ctx["exec_holder"].get("ex")
    if not ex:
        return
    own = snap.get("own", [])
    counts = Counter(u["t"] for u in own if not u.get("bld"))
    facs = [u for u in own if u.get("bld") and u["t"] != "commandCenter"
            and u["t"].lower().endswith("actory")]
    now = time.time()
    focus = {}
    for sid, sq in st.squads.items():
        m = sq.get("mission") or {}
        if m.get("任务") != "produce" or m.get("_done"):
            continue
        action = m.get("动作") or ""
        if action and not action.startswith("u_") and ("u_" + action) in ctx["known_actions"]:
            action = "u_" + action
        if action not in ctx["known_actions"]:
            continue
        want = int(m.get("数量", 4))
        remaining = want - counts.get(action[2:], 0)
        if remaining <= 0:
            m["_done"] = True
            st.mission_stale = True  # 召唤思考改派
            ctx["log_activity"]("status", f"⚙ {sid} 生产任务完成: {action}×{want}")
            continue
        focus[action] = remaining
        if now < g_action_bad(ctx, action):
            continue
        br = _sid_branch(sid)
        for fac in facs:
            if _fac_branch(fac["t"]) != br:
                continue  # produce 军种分流：陆兵陆厂、空兵空厂、海兵海厂（军种不越界）
            if (fac.get("q") or 0) >= 3 or ex._fac_bad(fac["id"], action, now) \
                    or ex._bad(action, now):
                continue
            learned = ex.fac_avail.get(fac["id"])
            avail = (learned[0] if learned and now - learned[1] < 120 else None) \
                or ex.factory_actions.get(fac["t"])
            if avail is not None and action not in avail:
                continue
            price = ex.price.get(action, 0)
            if price and (snap.get("team") or {}).get("funds", 0) < price:
                break  # 没钱就憋一拍
            ctx["link"].send({"cmd": "train", "action": action, "factory": fac["id"]})
            break  # 每拍每任务最多一发
    ex.produce_focus = focus


def g_action_bad(ctx, action):
    """跨域封禁时间（multiagent g 是各循环局部，生产刻度走共享 g_shared）。"""
    return ctx.setdefault("g_shared", {}).get("action_bad", {}).get(action, 0)


def _sys_squad(ctx, st, sid):
    sq = st.squads[sid]
    mi = sq.get("mission") or {}
    return f"""你是《Rusted Warfare》的 {sid} 小队队长，只指挥本队成员（系统已在指令层限定范围，你碰不了别队）。
当前任务：{mi.get('任务', '待命')}{'→' + str(mi['目标']) if mi.get('目标') else ''}（{mi.get('备注', '')}）
职责：执行任务 + 快速反应。铁律：
- 任务=军令：attack/harass 就必须打，"敌强我弱"不是不动的理由；有异议边执行边用 note 上报（战斗指挥会改派），脚下不许停。45 秒无动作系统会代你打（那是丢脸的）。
- 任务明显失效（目标已灭/本队打残/目标不可达）用 note 上报，别擅自改任务。
- 攻击目标必须在陆军可达范围（别点水域/岩浆/高崖另一侧）；回执说不可达就换目标或上报，别重复撞。
- 到达目标点且周围无敌 = 目标已清场：stop 驻守并 note 上报等改派（同目标无敌时点重复坐标系统会拦，有敌则随便打）。
- 本队血量急跌/被围 → defend 撤退保命（这是保命豁免，不是抗命），缓过来立刻回到任务。
- 不生产、不管别队、不改计划。动作要果断，一轮至少一个动作或 note。"""


SQUAD_TOOLS = [
    {"type": "function", "function": {
        "name": "attack",
        "description": "本队主攻 (x,y)。",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                       "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "harass",
        "description": "本队骚扰 (x,y)（佯攻/牵制/摸资源点）。",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                       "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "defend",
        "description": "本队撤回主基地防守/撤退保命。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "stop",
        "description": "本队停止当前动作原地待命。",
        "parameters": {"type": "object", "properties": {}}}},
    _QUERY_TOOL, _NOTE_TOOL,
]


def _translate_squad(ctx, st, sid, name, args, snap, g):
    """squad chat 动词 → 原子指令（units 限定本队成员）。"""
    members = st.squads[sid]["members"]
    if not members:
        return None, f"✗ {name}: 本队已无成员（等战斗指挥改派）"
    ids = ",".join(str(i) for i in sorted(members))
    if name in ("attack", "harass"):
        tx = ctx["clamp"](float(args.get("x", 0)), 0, st.world_w)
        ty = ctx["clamp"](float(args.get("y", 0)), 0, st.world_h)
        # 同目标去重（放宽）：25s 内同点且到点无敌才拦——敌人在场的连续进攻不受限（治正当进攻被误杀）
        last = st.squads[sid].get("last_atk")
        now = time.time()
        if last and (tx - last[0]) ** 2 + (ty - last[1]) ** 2 < 100 ** 2 and now - last[2] < 25:
            foes = any((e["x"] - tx) ** 2 + (e["y"] - ty) ** 2 < 600 ** 2
                       for e in snap.get("enemy", []))
            if not foes:
                return None, (f"✗ 同目标({int(tx)},{int(ty)})已在执行——到点无敌就 stop+note 待命"
                              "（目标附近有敌则直接打，不受此限）")
        st.squads[sid]["last_atk"] = (tx, ty, now)
        cmd = {"cmd": "attackmove", "x": tx, "y": ty, "units": ids}
        return cmd, f"{name}({cmd['x']:.0f},{cmd['y']:.0f})"
    if name == "defend":
        home = _home(snap)
        if not home:
            return None, "defend(找不到主基地)"
        return {"cmd": "move", "x": float(home["x"]), "y": float(home["y"]),
                "units": ids}, "defend()"
    if name == "stop":
        return {"cmd": "stop", "units": ids}, "stop()"
    return None, None


# ---------- 循环骨架 ----------

def _apm_ok(win, cap):
    now = time.time()
    while win and now - win[0] > 60:
        win.popleft()
    return len(win) < cap


def _drain_receipts(ctx, link, role):
    """按指令域取回执（别的域留在队列）；train 无独立域，按动作细分（_train_domain）。
    各域回执全部喂 executor 老学习（动作封禁/按厂封禁/池点判死）。"""
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
    if ex:
        # 全角色回执都喂 executor 学习（train 失败按域归 combat，但 executor 的生产循环
        # 需要"工厂#N 没有此动作"做按厂封禁——只喂建造域会漏掉军产失败，T1 厂被无限刷 T2 单）
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
        # 选址失败签名（水域/隔断/找不到合法位置）：归因本循环最近带坐标建造，600s 禁建
        if ("无法靠近" in r or "找不到合法建造位置" in r):
            rb = g.get("recent_build_xy")
            if rb and rb[0] is not None and now - rb[2] < 60:
                st.site_blocks[(int(rb[0] // 400), int(rb[1] // 400))] = now + 600


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
            if st.defeated:
                time.sleep(2.0)  # 已阵亡：全军覆没，LLM 无需再分析（死后还在那规划"重建"的根治）
                continue
            if not ctx["ai_enabled"]() or not ctx["role_enabled"](role["name"]):
                # 角色被设置面板关闭也要排空本域回执喂 executor——否则 train 失败滞留队列，
                # "工厂#N 没有此动作"永远到不了按厂封禁，规则层对同一厂无限刷失败单
                _drain_receipts(ctx, link, role)
                time.sleep(1.5)  # AI 总开关或本角色被设置面板关闭：不动作
                continue
            if role["name"] != "commander" and not snap.get("delegated"):
                time.sleep(1.5)  # 无委托单位：等玩家接管
                continue
            text, alerts = smz.summarize(snap)
            # 回执每拍必排（学习/封禁时效敏感，不许等召唤——否则事件驱动角色的封禁学习永远慢半拍）
            receipts = _drain_receipts(ctx, link, role)
            reason = None
            if role.get("event_driven"):
                event_fn = role.get("event_fn") or (_build_event if role["name"] == "build" else _combat_event)
                reason = event_fn(ctx, st, snap, alerts, time.time(), g)
                if reason is None:
                    time.sleep(2.0)  # 稳态期：零 LLM 调用，规则层+反射层自理
                    continue
                g["last_fire"] = time.time()
                ctx["log_activity"]("status", f"{role['icon']} {role['name']} 召唤: {reason}")
            _learn_receipts(role, receipts, g, st)
            user = role["make_user"](ctx, st, snap, text, alerts, receipts)
            if reason:
                user += f"\n\n【召唤原因】{reason}（事件触发模式：无事不召唤，被召唤就给出决断）"
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
                    # ```json 参数块 → st.plan_params（机器消费：建造/战斗域与规则层照此执行）
                    st.plan_params = {}
                    pj = re.search(r"```json\s*(\{.*?\})\s*```", plan, re.S)
                    if pj:
                        try:
                            st.plan_params = json.loads(pj.group(1))
                        except json.JSONDecodeError:
                            ctx["log_activity"]("note", "📋 计划书参数块 JSON 解析失败，忽略")
                    ex = ctx["exec_holder"].get("ex")
                    if ex and st.plan_params:
                        ex.apply_plan_params(st.plan_params)
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


# ---------- 军种中层指挥（三层：总指挥→军种中层→squad chat；军种绝不越界） ----------

_BRANCH_META = {
    "land": {"icon": "🛡", "cn": "陆军", "ids": "L*",
             "rule": "陆军全地形机动但不过水（悬浮单位除外）；地面目标都归你。",
             "facs": "landFactory/mechFactory/experimental"},
    "air": {"icon": "✈", "cn": "空军", "ids": "A*",
            "rule": "空军无视地形；优先拆防空薄弱目标，别拿空军撞防空网；地面防御部署不归你。",
            "facs": "airFactory"},
    "navy": {"icon": "⚓", "cn": "海军", "ids": "N*",
             "rule": "海军只在水域行动，绝不上岸；水池/沿岸/水面目标归你。",
             "facs": "seaFactory"},
}


def _fac_branch(t):
    """工厂型号 → 军种（produce 分流用）。"""
    tl = t.lower()
    if "air" in tl or "hangar" in tl:
        return "air"
    if "sea" in tl or "naval" in tl or "shipyard" in tl:
        return "navy"
    return "land"


def _branch_event(ctx, st, snap, alerts, now, g, branch):
    """军种中层召唤条件：警报/待读/计划书更新/任务失效/军种蓄势/心跳。无兵无队零召唤。"""
    if alerts:
        return "警报: " + alerts[0]
    with st.lock:
        if st.pending[branch]:
            return "待读消息"
    if st.plan_ts > g.get("plan_ts_fired", 0):
        g["plan_ts_fired"] = st.plan_ts
        return "计划书更新"
    if st.mission_stale:
        st.mission_stale = False
        return "小队任务失效/完成待改派"
    own_b = [u for u in snap.get("own", [])
             if not u.get("bld") and _branch_of(u["t"], st.move_map) == branch]
    if not own_b and not any(_sid_branch(s) == branch for s in st.squads):
        return None  # 本军种无兵无队：零召唤（旱图海军省 token）
    # 军种蓄势（阶段 3 触发的落地版：≥8 且姿态非死守——它曾接在已退役的旧角色上）
    posture = str((st.plan_params or {}).get("姿态", ""))
    if len(own_b) >= 8 and posture not in ("defend", "龟缩") and now - g.get("last_fire", 0) > 90:
        return f"{_BRANCH_META[branch]['cn']}兵力已备（{len(own_b)}员）且姿态{posture or '未定'}非死守，该动了"
    if now - g.get("last_fire", 0) > 90:
        return "心跳"
    return None


def _sys_branch(ctx, st, branch):
    m = st.map_msg or {}
    tw = (m.get("tilesW", 0) or 0) * (m.get("tileW", 0) or 0)
    th = (m.get("tilesH", 0) or 0) * (m.get("tileH", 0) or 0)
    meta = _BRANCH_META[branch]
    return f"""你是《Rusted Warfare》的{meta['cn']}指挥（指挥链：总指挥定战略 → 你管{meta['cn']} → 小队队长执行）。
【军种铁律】{meta['rule']}军种绝不越界：任务书只许下给本军种小队（{meta['ids']}），别军种的事别碰；需要配合用 note 说明（总指挥会看到）。
【坐标】世界 {tw:.0f}x{th:.0f} 像素，y 向下为正。你只看得见本队视野内的信息：可见=真实（没敌人=真没有），浅黑=记忆（可能已变），全黑=未知先侦察。
【职责】用 set_missions 给本军种小队派 attack/defend/hold/harass/produce；produce 只能下本军种厂型（{meta['facs']}）。
【纪律】兵力成型（≥8）且计划姿态非死守就该推，别让小队闲置；任务书全量替换要果断；只分析不派单=空过违规。"""


def _user_branch(ctx, st, snap, text, alerts, receipts, branch):
    parts = [text]
    own_b = [u for u in snap.get("own", [])
             if not u.get("bld") and _branch_of(u["t"], st.move_map) == branch]
    cnt = Counter(u["t"] for u in own_b)
    parts.append(f"本军种兵力({len(own_b)}): "
                 + (",".join(f"{k}x{v}" for k, v in cnt.most_common(8)) or "无"))
    sq_lines = []
    for sid, sq in st.squads.items():
        if _sid_branch(sid) != branch:
            continue
        mi = sq.get("mission") or {}
        sq_lines.append(f"{sid}({len(sq['members'])}员): {mi.get('任务', '待命')}"
                        + (f"→{[int(v) for v in mi['目标']]}" if mi.get("目标") else ""))
    parts.append("本军种小队: " + (" | ".join(sq_lines) or "无（系统按兵力自动建队，可直接派单）"))
    enemies = snap.get("enemy", [])
    if enemies:
        parts.append("可见敌情: " + "; ".join(f"{e['t']}@({e['x']},{e['y']})" for e in enemies[:8]))
    fi = _fog_intel(st, snap)
    if fi:
        parts.append(fi)
    pb = _plan_block(st, branch)
    if pb:
        parts.append(pb)
    if receipts:
        parts.append("最近回执: " + " | ".join(receipts[-4:]))
    pend = _drain(st, branch)
    if pend:
        parts.extend(pend)
    if alerts:
        parts.append("[硬性] 有警报：本回合必须给本军种小队明确指令。")
    return "\n\n".join(parts)


def _translate_branch_missions(ctx, st, args, g, branch):
    """军种任务书：跨军种编号直接拒绝（军种绝不越界的系统保证），其余走共享校验。"""
    ok_items, bad = [], []
    for it in (args.get("squads") or [])[:12]:
        sid = str((it or {}).get("id", ""))
        if sid in st.squads and _sid_branch(sid) != branch:
            bad.append(sid)
            continue
        ok_items.append(it)
    _c, desc = _translate_missions(ctx, st, {"squads": ok_items}, g)
    if bad:
        desc += f"；拒绝跨军种编号({','.join(bad)})——你只管{_BRANCH_META[branch]['ids']}"
    return None, desc


def _branch_tools(branch):
    import copy
    t = copy.deepcopy(SET_MISSIONS_TOOL)
    meta = _BRANCH_META[branch]
    t["function"]["description"] = (
        f"下发《{meta['cn']}小队任务书》（全量替换）：id 只允许本军种编号（{meta['ids']}，"
        "当前存在的小队）。任务=attack(目标[x,y])/defend/hold/harass(目标[x,y])/"
        "produce(动作=生产动作ID,数量=累计目标数)。未点名的小队维持原任务。")
    return [t, _QUERY_TOOL, _NOTE_TOOL]


def _branch_role(ctx, branch):
    cfg = ctx["cfg"]
    meta = _BRANCH_META[branch]
    return {"name": branch, "icon": meta["icon"],
            "model": cfg.get("combat_thinker_model") or cfg["model"],
            "tools": _branch_tools(branch),
            "kinds": COMBAT_KINDS, "verbs": {"set_missions"},
            "plan_sink": False, "apm_kind": None, "event_driven": True,
            "event_fn": lambda c, s, snap, a, n, g: _branch_event(c, s, snap, a, n, g, branch),
            "stagger": 7.0, "cadence": lambda: float(cfg.get("combat_interval_sec", 6)),
            "timeout": max(int(cfg["timeout_sec"]), 120),
            "query_rounds": 1,
            "make_system": lambda c, s: _sys_branch(c, s, branch),
            "make_user": lambda c, s, snap, text, alerts, rc: _user_branch(c, s, snap, text, alerts, rc, branch),
            "translate": lambda c, s, name, args, snap, g: (
                _translate_branch_missions(c, s, args, g, branch)
                if name == "set_missions" else (None, None))}


# ---------- 角色定义与启动 ----------

def _roles(ctx):
    cfg = ctx["cfg"]
    fast = cfg.get("combat_squad_model") or cfg.get("model_fast") or cfg["model"]
    build_chat = bool(cfg.get("build_chat_enabled", False))
    build_role = {"name": "build", "icon": "🔨", "model": fast, "tools": BUILD_TOOLS,
                  "kinds": BUILD_KINDS, "verbs": {"build", "train", "set_work_order"}, "plan_sink": False,
                  "apm_kind": "build", "event_driven": True,
                  "stagger": 4.0, "cadence": lambda: float(cfg.get("build_interval_sec", 10)),
                  "timeout": int(cfg["timeout_sec"]),
                  "query_rounds": 1,
                  "make_system": _sys_build,
                  "make_user": lambda c, s, snap, text, alerts, rc: _user_build(c, s, snap, text, rc),
                  "translate": _translate_build}
    if not build_chat:
        # 建造 chat 退役（v3.2）：建造指挥(pro) 出 JSON 工单，无 build/train 直发权
        build_role.update({
            "model": cfg.get("build_model") or cfg["model"],
            "tools": BUILD_THINKER_TOOLS,
            "verbs": {"set_work_order"},
            "timeout": max(int(cfg["timeout_sec"]), 120),
            "query_rounds": 2,
            "make_system": _sys_build_thinker})
    return [
        {"name": "commander", "icon": "📋", "model": cfg.get("commander_model") or cfg["model"], "tools": COMMANDER_TOOLS,
         "kinds": set(), "verbs": set(), "plan_sink": True, "apm_kind": None,
         "stagger": 1.0, "cadence": lambda: float(cfg.get("commander_interval_sec", 50)),
         "timeout": max(int(cfg["timeout_sec"]), 120),  # reasoner+全图长 prompt，45s 必超时
         "query_rounds": 4,  # 总指挥是数据收集型，查询预算放宽
         "make_system": _sys_commander,
         "make_user": lambda c, s, snap, text, alerts, rc: _user_commander(c, s, snap, text, rc),
         "translate": lambda *a: (None, None)},
        build_role,
        # 军种中层指挥（三层：总指挥→军种中层→squad chat；统一 combat 指挥已退役）
        _branch_role(ctx, "land"),
        _branch_role(ctx, "air"),
        _branch_role(ctx, "navy"),
    ]


def _squad_chat_loop(ctx, st, sid):
    """squad chat 循环：上下文=本队任务+本队态势+最近4条自己的动作（不滚全对话）。"""
    import powergrid as pg
    cfg = ctx["cfg"]
    link = ctx["link"]
    # 执行层可挂第二供应商（硅基流动小模型）：key 存在即启用，否则回退主供应商
    sf_key = cfg.get("squad_api_key")
    if sf_key and cfg.get("squad_base_url"):
        base_url, api_key = cfg["squad_base_url"], sf_key
        model = cfg.get("squad_model_sf") or "Qwen/Qwen3-30B-A3B-Instruct-2507"
    else:
        base_url, api_key = cfg["base_url"], ctx["api_key"]
        model = cfg.get("combat_squad_model") or cfg.get("model_fast") or cfg["model"]
    timeout = int(cfg["timeout_sec"])
    own_actions = deque(maxlen=4)
    print(f"[{sid}] squad chat 启动")
    ctx["log_activity"]("status", f"🏳 {sid} 队长上线（{model}）")
    while True:
        try:
            sq = st.squads.get(sid)
            if not sq or not sq["members"]:
                break  # 团灭/解散：线程退出，管理器按需重开
            if not ctx["ai_enabled"]() or not ctx["role_enabled"]("combat"):
                time.sleep(1.5)
                continue
            snap = link.snapshot()
            if not snap or not snap.get("team") or snap["team"].get("ai") or not snap.get("delegated"):
                time.sleep(1.5)
                continue
            members = [u for u in snap.get("own", []) if u["id"] in sq["members"]]
            if not members:
                time.sleep(2.0)
                continue
            cx = sum(u["x"] for u in members) / len(members)
            cy = sum(u["y"] for u in members) / len(members)
            hp = sum(u["hp"] for u in members)
            mx = sum(u["maxHp"] for u in members) or 1
            enemies = snap.get("enemy", [])
            near = sorted(enemies, key=lambda e: (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2)[:5]
            mi = sq.get("mission") or {}
            power_txt = ""
            ex = ctx["exec_holder"].get("ex")
            if ex:
                og, oa = pg.sum_power(members, ex.power_table)
                eg, ea = pg.sum_power(near, ex.power_table)
                # 进攻任务不给看"勿硬拼"（敌强我弱曾是抗命借口）；防守/待命才警告
                warn = ("（敌强我弱，勿硬拼）" if eg > og * 1.5
                        and mi.get("任务") not in ("attack", "harass") else "")
                power_txt = (f"战力对比: 本队 对地{og:.0f}/对空{oa:.0f} vs 近敌 对地{eg:.0f}/对空{ea:.0f}"
                             + warn + "\n")
            user = (f"本队: {len(members)}员 血量{hp}/{mx} 质心({int(cx)},{int(cy)})\n"
                    f"近敌: " + (", ".join(f"{e['t']}@({e['x']},{e['y']})" for e in near) or "无") + "\n"
                    + power_txt +
                    f"【本队任务】{mi.get('任务', '待命')}{'→' + str(mi['目标']) if mi.get('目标') else ''}"
                    f"（{mi.get('备注', '')}）\n"
                    f"【地形格】.=陆地 ~=水域 ==桥 L=岩浆 ^=阶梯 #=高崖 o=我方 数字=敌数\n"
                    + ctx["strategic_grid"](st.map_msg, snap) + "\n"
                    f"【你最近的行动】{' | '.join(own_actions) or '（暂无）'}")
            msgs = [{"role": "system", "content": _sys_squad(ctx, st, sid)},
                    {"role": "user", "content": user}]
            msg = llm.chat(base_url, api_key, model, msgs, SQUAD_TOOLS, timeout=timeout)
            calls = msg.get("tool_calls") or []
            if any((tc.get("function") or {}).get("name") == "query" for tc in calls):
                patch, _rest = _answer_queries(ctx, st, msg, snap, 1)
                msgs.extend(patch)
                msg = llm.chat(base_url, api_key, model, msgs, SQUAD_TOOLS, timeout=timeout)
                calls = [tc for tc in (msg.get("tool_calls") or [])
                         if (tc.get("function") or {}).get("name") != "query"]
            if not calls:
                own_actions.append("（空过）")
                time.sleep(2.0)
                continue
            g = ctx.setdefault("g_shared", {})
            for tc in calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    continue
                if name == "note":
                    t = str(args.get("text", "")).strip()
                    if t:
                        ts = time.strftime("%H:%M:%S")
                        st.pending["combat"].append(f"[{ts}] [{sid} 上报] {t}")
                        own_actions.append("note:" + t[:24])
                        ctx["log_activity"]("status", f"🏳 {sid} 上报: {t}")
                    continue
                cmd, desc = _translate_squad(ctx, st, sid, name, args, snap, g)
                if cmd is not None:
                    link.send(cmd)
                    sq["last_act_ts"] = time.time()  # 服从看门狗的"活着"凭证
                if desc:
                    own_actions.append(desc)
                    ctx["log_activity"]("cmd", f"🏳 {sid} {desc}")
        except Exception as e:
            print(f"[{sid}] squad chat 异常: {e}")
            ctx["log_activity"]("error", f"🏳 {sid} 循环异常: {e}")
            time.sleep(2.0)
        time.sleep(float(ctx["cfg"].get("combat_interval_sec", 6)))
    print(f"[{sid}] squad chat 退出")
    ctx["log_activity"]("status", f"🏳 {sid} 队长下线")


def _squad_watchdog(ctx, st, snap, now):
    """服从看门狗（系统兜底）：attack/harass 任务下达 45s 小队 LLM 仍无动作，
    系统直接 attackmove 代执行（与反射层同哲学：LLM 会漏，机器兜底——
    实测 attack 小队以"敌强我弱"拒战 56 分钟零军事指令）。
    质心有危险时让位反射层（不推人上火线）；阵亡/未委托/角色关闭时不动作。"""
    if st.defeated:
        return
    for sid, sq in st.squads.items():
        mi = sq.get("mission") or {}
        if mi.get("任务") not in ("attack", "harass") or not mi.get("目标") or not sq["members"]:
            continue
        if now - max(sq.get("last_act_ts", 0.0), sq.get("mission_ts", 0.0)) < 45.0:
            continue
        ms = [u for u in snap.get("own", []) if u["id"] in sq["members"]]
        if not ms:
            continue
        cx = sum(u["x"] for u in ms) / len(ms)
        cy = sum(u["y"] for u in ms) / len(ms)
        if st.danger_map.danger(cx, cy, snap):
            continue  # 挨打中：反射层管撤退，看门狗不抢方向盘
        tx, ty = mi["目标"]
        ids = ",".join(str(i) for i in sorted(sq["members"]))
        ctx["link"].send({"cmd": "attackmove", "x": float(tx), "y": float(ty), "units": ids})
        sq["last_act_ts"] = now
        ctx["log_activity"]("cmd", f"🏳 {sid} 看门狗代执行 attack({int(tx)},{int(ty)})（小队 45s 未动）")


def _squad_manager(ctx, st):
    """小队管理器（2s/拍）：归队 + 生产刻度 + 按任务开 squad chat 线程。
    兼回执速喂：角色循环被 LLM 调用阻塞时（~25s）学习滞后，这里 2s 偷看喂 executor。"""
    threads = {}
    seen_receipts = set()  # 已喂过的回执对象 id（角色线程仍会取走做提示，互不冲突）
    while True:
        try:
            snap = ctx["link"].snapshot()
            if snap and snap.get("team") and not snap["team"].get("ai"):
                ex = ctx["exec_holder"].get("ex")
                if ex:
                    with ctx["link"].lock:
                        fresh = [r for r in ctx["link"].cmd_results if id(r) not in seen_receipts]
                    for r in fresh:
                        seen_receipts.add(id(r))
                    if len(seen_receipts) > 400:
                        seen_receipts.clear()
                    if fresh:
                        ex.on_cmd_results(fresh)
                squad_assign(ctx, st, snap)
                _produce_tick(ctx, st, snap)
                # 进攻任务到点清场自动完成（别让任务无限挂着——重复点原地的根治）
                for sid, sq in st.squads.items():
                    mi = sq.get("mission") or {}
                    if mi.get("任务") != "attack" or not mi.get("目标") or not sq["members"]:
                        continue
                    ms = [u for u in snap.get("own", []) if u["id"] in sq["members"]]
                    if not ms:
                        continue
                    cx = sum(u["x"] for u in ms) / len(ms)
                    cy = sum(u["y"] for u in ms) / len(ms)
                    tx, ty = mi["目标"]
                    if (cx - tx) ** 2 + (cy - ty) ** 2 > 150 ** 2:
                        continue
                    if any((e["x"] - tx) ** 2 + (e["y"] - ty) ** 2 < 500 ** 2
                           for e in snap.get("enemy", [])):
                        continue
                    sq["mission"] = None
                    st.mission_stale = True
                    ctx["log_activity"]("status", f"⚙ {sid} 进攻目标已清场，任务完成待改派")
                if ctx["ai_enabled"]() and ctx["role_enabled"]("combat") and snap.get("delegated"):
                    _squad_watchdog(ctx, st, snap, time.time())  # 服从看门狗：LLM 不动系统代打
                    for sid, sq in st.squads.items():
                        mi = sq.get("mission") or {}
                        if not sq["members"] or mi.get("任务") in (None, "produce"):
                            continue  # 无任务/生产任务：无需 chat（生产走刻度，守家靠反射）
                        th = threads.get(sid)
                        if th is None or not th.is_alive():
                            threads[sid] = threading.Thread(
                                target=_squad_chat_loop, args=(ctx, st, sid), daemon=True)
                            threads[sid].start()
        except Exception as e:
            print(f"[squad-manager] 异常: {e}")
        time.sleep(2.0)


# ---------- 建造小队 chat（小模型现场执行层，2026-07-25 探针实证后落地） ----------

_BCHAT_DOMAINS = ("econ", "defense")
_BCHAT_GNAME = {"econ": "经济建造", "defense": "防御阵型"}


def _bchat_gids(cfg):
    """每域组数（用户拍板：30B 便宜，多开执行层）：econ/econ2/defense/defense2…"""
    k = max(1, min(4, int(cfg.get("builder_chat_per_domain", 2))))
    return [d if i == 1 else f"{d}{i}" for d in _BCHAT_DOMAINS for i in range(1, k + 1)]


def _bchat_dom(gid):
    return gid.rstrip("0123456789")


def _bchat_gname(gid):
    dom = _bchat_dom(gid)
    return _BCHAT_GNAME.get(dom, dom) + gid[len(dom):]


def _bchat_slot(ctx, gid):
    """(域, 组内序号, 同域组数)：工单按 idx 奇偶在同域组间分流（防两组抢同一单）。"""
    dom = _bchat_dom(gid)
    grp = [g for g in _bchat_gids(ctx["cfg"]) if _bchat_dom(g) == dom]
    return dom, grp.index(gid), len(grp)

_BCHAT_SYS = """你是 RTS 游戏建造小队的现场指挥（域：{gname}），指挥 ≤{cap} 个建造者执行工单。只输出一行 JSON，禁止任何其他文字。
输出契约：{{"actions":[{{"action":"build|move|repair|hold|note","idx":任务编号,"anchor":"合法锚点表之一","builders":[建造者编号],"reason":"≤20字"}}]}}
军规：
1. 每拍至多 3 个动作（同批任务连着派，别磨叽）；没有好选择就 hold（actions 里只放一个 hold）
2. anchor 只能原样选自合法锚点表，禁止自造坐标、禁止编造锚点
3. build 的 idx 只能选【本组任务】里的编号，系统按锚点落点派工
4. 遇袭优先保命（move 到安全锚点）；主基地被打优先 repair
5. reason 不超过 20 字，不许解释规则
6. 觉得任务本身不合理（点位危险/重复/超实控/没必要）→ note 上报留言（带理由），上级建造指挥下一轮会看到{extra}"""

_BCHAT_EXTRA = {
    "econ": "",
    "defense": "\n6. 造整齐：同批炮塔相邻成排、节距均匀、防空排在炮塔后排、别堵主通道",
}


def _new_bgroup():
    return {"members": set(), "last_act": time.time(),  # 建组即给 90s 宽限（防"从未接活=未动"假接管）
            "fallback_until": 0.0,
            "thread": None, "actions": deque(maxlen=4), "wake": threading.Event()}


def _bchat_llm(ctx):
    """建造 chat 供应商（squad chat 同款第二供应商回退链：硅基小模型优先）。"""
    cfg = ctx["cfg"]
    sf_key = cfg.get("squad_api_key")
    if sf_key and cfg.get("squad_base_url"):
        return (cfg["squad_base_url"], sf_key,
                cfg.get("squad_model_sf") or "Qwen/Qwen3-30B-A3B-Instruct-2507")
    return (cfg["base_url"], ctx["api_key"],
            cfg.get("combat_squad_model") or cfg.get("model_fast") or cfg["model"])


def _dir8(dx, dy):
    ang = math.degrees(math.atan2(-dy, dx))  # y 向下为正，取反成方位角
    names = ["东", "东北", "北", "西北", "西", "西南", "南", "东南"]
    return names[int((ang % 360) / 45 + 0.5) % 8]


def _bchat_anchors(ctx, st, ex, snap, domain, pending=None):
    """合法锚点表（文字签↔坐标）：池/弧位/厂位/后勤位/家圈 + 任务自带精确点（阵地展开项），
    全带方位与安全描述。"""
    cc = next((u for u in snap.get("own", []) if u["t"] == "commandCenter"), None)
    if not cc:
        return []

    def desc(x, y, base=""):
        dx, dy = x - cc["x"], y - cc["y"]
        hot = st.danger_map.threat_at(x, y) > 0
        return f"{base}{_dir8(dx, dy)}{int(math.hypot(dx, dy))}px,{'有敌情' if hot else '安全'}"

    occ = ex._occupancy(snap)
    anchors = []
    for i, p in enumerate((ex.last_safe_pools or [])[:2]):
        px, py = (p["x"], p["y"]) if isinstance(p, dict) else (p[0], p[1])
        anchors.append({"label": f"池{chr(65 + i)}", "x": px, "y": py, "desc": desc(px, py)})
    front = ex.army_goal.get("rally")
    tx, ty = front if front else (st.world_w / 2.0, st.world_h / 2.0)
    ax, ay = tx - cc["x"], ty - cc["y"]
    alen = max(1.0, math.hypot(ax, ay))
    d = (ax / alen, ay / alen)
    for i, bt in enumerate(("c_turret_t1", "c_antiAirTurret")):
        spot = None
        for k in range(3):  # 0 号弧位撞主通道(LANE)就跳号（_wo_resolve 同款处理）
            spot = ex.defense_slot(cc, occ, bt, k, d, 0)
            if spot and spot != "LANE":
                break
            spot = None
        if spot:
            anchors.append({"label": f"弧位{chr(68 + i)}", "x": spot[0], "y": spot[1],
                            "desc": desc(spot[0], spot[1], "防线")})
            ex._mark_fp(occ, bt, spot[0], spot[1])  # 锚点互斥：后查者避开先中者
    fac = ex.factory_slot(cc, 0, [], occ=occ, btype="landFactory")
    if fac:
        anchors.append({"label": "厂位F", "x": fac[0], "y": fac[1], "desc": desc(fac[0], fac[1])})
    mis = ex.misc_slot(cc, 0, occ=occ, btype="fabricatorT1")
    if mis:
        anchors.append({"label": "后勤位G", "x": mis[0], "y": mis[1], "desc": desc(mis[0], mis[1])})
    anchors.append({"label": "家圈", "x": cc["x"], "y": cc["y"], "desc": desc(cc["x"], cc["y"])})
    for t in (pending or [])[:8]:  # 阵地展开等精确坐标任务：自带点进锚点表（不被重锚散开）
        if isinstance(t.get("slot"), (list, tuple)):
            anchors.append({"label": f"任务#{t['idx']}点", "x": t["slot"][0], "y": t["slot"][1],
                            "desc": desc(t["slot"][0], t["slot"][1], "任务点")})
    if domain == "defense":
        anchors.sort(key=lambda a: 0 if a["label"].startswith("弧位") else 1)
    return anchors


def _bchat_loop(ctx, st, gid):
    """建造 chat 循环：事件触发（新工单/遇袭/心跳到点有活），无活不召唤（零 token）。
    上下文=本组建造者+本组任务+合法锚点表+最近4条动作；输出契约 JSON，非法即弃。"""
    cfg = ctx["cfg"]
    link = ctx["link"]
    base_url, api_key, model = _bchat_llm(ctx)
    bg = st.bgroups[gid]
    timeout = int(cfg["timeout_sec"])
    acts = bg["actions"]
    sysp = _BCHAT_SYS.format(gname=_bchat_gname(gid), cap=int(cfg.get("builder_chat_cap", 4)),
                             extra=_BCHAT_EXTRA[_bchat_dom(gid)])
    ctx["log_activity"]("status", f"🧰 {gid} 建造队长上线（{model}）")
    while True:
        try:
            if not cfg.get("builder_chat_enabled") or not bg.get("members"):
                break
            if not ctx["ai_enabled"]():
                time.sleep(1.5)
                continue
            snap = link.snapshot()
            if not snap or not snap.get("team") or snap["team"].get("ai") or not snap.get("delegated"):
                time.sleep(1.5)
                continue
            ex = ctx["exec_holder"].get("ex")
            if not ex:
                time.sleep(1.5)
                continue
            members = [u for u in snap.get("own", []) if u["id"] in bg["members"] and not u.get("bld")]
            if not members:
                time.sleep(2.0)
                continue
            dom, slot, ngrp = _bchat_slot(ctx, gid)
            pending = [t for t in ex.wo_pending(dom) if t["idx"] % ngrp == slot]
            # 遇袭=敌武器实际够得着（距离≤射程×1.15），不是"近"就怕——
            # 750px 外一座 290 射程的炮塔曾让全队永远处于"遇袭"死循环
            prange = {u["name"]: (u.get("atk") or {}).get("rng", 0)
                      for u in (ctx["pedia"].get("units") or [])}
            near = []
            for e in snap.get("enemy", []):
                d2 = min((e["x"] - u["x"]) ** 2 + (e["y"] - u["y"]) ** 2 for u in members)
                rng = prange.get(e["t"], 0) or 0
                near.append((math.sqrt(d2), rng, e))
            near.sort(key=lambda t: t[0])
            near = near[:3]
            under_fire = any(d <= rng * 1.15 for d, rng, _e in near)
            woke = bg["wake"].wait(float(cfg.get("builder_chat_cadence", 15)))
            bg["wake"].clear()
            if not pending and not under_fire and not woke:
                continue  # 无任务无事件：不召唤
            anchors = _bchat_anchors(ctx, st, ex, snap, dom, pending)
            mtxt = " | ".join(f"#{u['id']}@({int(u['x'])},{int(u['y'])}) act={u.get('act') or 'idle'} hp{u['hp']}"
                              for u in members[:6])
            ttxt = "；".join(f"#{t['idx']} {t['action']}[{t.get('slot')}]"
                             for t in pending[:8]) or "（无）"
            atxt = "；".join(f"{a['label']}({a['desc']})" for a in anchors)
            etxt = "；".join(f"{e['t']}@({int(e['x'])},{int(e['y'])})距{int(d)}射程{int(rng)}"
                             f"{'=够得着你!' if d <= rng * 1.15 else '=打不到你'}"
                             for d, rng, e in near) or "无"
            user = (f"【本组建造者】{mtxt}\n【本组任务】{ttxt}\n【合法锚点表】{atxt}\n"
                    f"【近敌】{etxt}\n"
                    f"【你最近的行动】{' | '.join(acts) or '（暂无）'}")
            if under_fire:
                user = "⚠ 有敌武器够得着你，保命优先！\n" + user
            msg = llm.chat(base_url, api_key, model,
                           [{"role": "system", "content": sysp},
                            {"role": "user", "content": user}], tools=[], timeout=timeout, max_retries=1)
            txt = (msg.get("content") or "").strip()
            i, j = txt.find("{"), txt.rfind("}")
            try:
                obj = json.loads(txt[i:j + 1]) if 0 <= i < j else {}
            except json.JSONDecodeError:
                obj = {}
            objs = obj.get("actions") if isinstance(obj.get("actions"), list) else [obj]
            anchor_map = {a["label"]: a for a in anchors}
            for obj in objs[:3]:
                if not isinstance(obj, dict):
                    continue
                act = obj.get("action")
                if act not in ("build", "move", "repair", "hold", "note"):
                    acts.append(f"（非法输出已丢弃: {txt[:30]}）")
                    ctx["log_activity"]("error", f"🧰 {gid} 非法输出: {txt[:60]}")
                    break
                if act == "note":
                    t = str(obj.get("reason", "")).strip()
                    if t:
                        ts = time.strftime("%H:%M:%S")
                        st.pending["build"].append(f"[{ts}] [🧰{gid} 上报] {t}")
                        acts.append("note:" + t[:24])
                        ctx["log_activity"]("status", f"🧰 {gid} 上报: {t}")
                    continue
                if act == "hold":
                    acts.append(f"hold({str(obj.get('reason', ''))[:20]})")
                    ctx["log_activity"]("status", f"🧰 {gid} hold（{str(obj.get('reason', ''))[:24]}）")
                    break  # hold=本拍收工
                lab = obj.get("anchor")
                a = anchor_map.get(lab)
                if act == "move":
                    if not a:
                        acts.append(f"（move 锚点非法已丢弃: {lab}）")
                        continue
                    ids = ",".join(str(u["id"]) for u in members)
                    link.send({"cmd": "move", "x": float(a["x"]), "y": float(a["y"]), "units": ids})
                    bg["last_act"] = time.time()
                    acts.append(f"move→{lab}")
                    ctx["log_activity"]("cmd", f"🧰 {gid} move→{lab}（{str(obj.get('reason', ''))[:20]}）")
                    break  # move 是全组动作，不再叠加
                if act == "repair":
                    try:
                        tid = int(obj.get("idx"))
                    except (TypeError, ValueError):
                        tid = None
                    if not tid or not any(u["id"] == tid and u.get("bld") for u in snap.get("own", [])):
                        acts.append(f"（repair 目标非法已丢弃: {obj.get('idx')}）")
                        continue
                    free = [u for u in members if u.get("act") in (None, "", "idle")]
                    b = free[0] if free else members[0]
                    link.send({"cmd": "resume", "builder": b["id"], "target": tid})
                    bg["last_act"] = time.time()
                    acts.append(f"repair→#{tid}")
                    ctx["log_activity"]("cmd", f"🧰 {gid} repair→#{tid}（建造者#{b['id']}）")
                    continue
                # build：idx 必须命中本组任务，anchor 必须合法
                try:
                    tidx = int(obj.get("idx"))
                except (TypeError, ValueError):
                    tidx = None
                task = next((t for t in pending if t["idx"] == tidx), None)
                if task is None or a is None:
                    acts.append(f"（build 任务/锚点非法已丢弃: idx={obj.get('idx')} {lab}）")
                    continue
                want = obj.get("builders") or []
                bid = next((u["id"] for u in members
                            if u["id"] in want and u.get("act") in (None, "", "idle")), None)
                if bid is None:
                    free = [u for u in members if u.get("act") in (None, "", "idle")]
                    bid = (free[0] if free else members[0])["id"]
                link.send({"cmd": "build", "action": task["action"], "builder": bid,
                           "x": float(a["x"]), "y": float(a["y"])})
                ex.wo_mark_assigned(tidx, bid, a["x"], a["y"])
                bg["last_act"] = time.time()
                acts.append(f"build #{tidx}→{lab}")
                ctx["log_activity"]("cmd", f"🧰 {gid} build #{tidx} {task['action']}→{lab}（建造者#{bid}）")
                pending = [t for t in pending if t["idx"] != tidx]  # 连派本拍内不重复命中
        except llm.LlmError as e:
            acts.append(f"（LLM 失败: {str(e)[:30]}）")
            time.sleep(3.0)
        except Exception as e:
            print(f"[bchat {gid}] 异常: {e}")
            ctx["log_activity"]("error", f"🧰 {gid} 循环异常: {e}")
            time.sleep(2.0)
    ctx["log_activity"]("status", f"🧰 {gid} 建造队长下线")


def _bchat_manager(ctx, st):
    """建造 chat 管理器（2s/拍）：归队（人少优先、≤cap）/剪除/45s 静默回退（规则层接管）
    /新工单唤醒/按需开 chat 线程。"""
    seen_gen = st.generation
    seen_wo = -1
    while True:
        time.sleep(2.0)
        try:
            cfg = ctx["cfg"]
            if not cfg.get("builder_chat_enabled"):
                continue
            if st.generation != seen_gen:
                seen_gen = st.generation
                st.bgroups.clear()  # 换图重建分组（线程随 members 空自退）
                seen_wo = -1
            snap = ctx["link"].snapshot()
            ex = ctx["exec_holder"].get("ex")
            if not snap or not snap.get("team") or snap["team"].get("ai") or not ex:
                continue
            delegated = set(snap.get("delegated", []))
            builders = [u for u in snap.get("own", [])
                        if u["id"] in delegated and not u.get("bld")
                        and ("uilder" in u["t"] or u["t"] == "combatEngineer")
                        and "editor" not in u["t"].lower()]
            cap = int(cfg.get("builder_chat_cap", 4))
            live = {u["id"] for u in builders}
            gids = _bchat_gids(cfg)
            for gid in gids:
                bg = st.bgroups.setdefault(gid, _new_bgroup())
                bg["members"] &= live
            grouped = set().union(*(bg["members"] for bg in st.bgroups.values())) if st.bgroups else set()
            for u in builders:
                if u["id"] in grouped:
                    continue
                gid = min(gids, key=lambda g: len(st.bgroups[g]["members"]))
                if len(st.bgroups[gid]["members"]) < cap:
                    st.bgroups[gid]["members"].add(u["id"])
                    grouped.add(u["id"])
            now = time.time()
            for gid, bg in st.bgroups.items():
                dom, slot, ngrp = _bchat_slot(ctx, gid)
                pend = [t for t in ex.wo_pending(dom) if t["idx"] % ngrp == slot]
                if pend and now - bg["last_act"] > 90 and now > bg["fallback_until"]:
                    bg["fallback_until"] = now + 45
                    if bg["last_act"]:
                        ctx["log_activity"]("status", f"🧰 {gid} 90s 未动，规则层接管 45s")
                if bg["members"] and not (bg["thread"] and bg["thread"].is_alive()):
                    bg["thread"] = threading.Thread(target=_bchat_loop, args=(ctx, st, gid), daemon=True)
                    bg["thread"].start()
            if ex.wo_version != seen_wo:
                seen_wo = ex.wo_version
                for bg in st.bgroups.values():
                    bg["wake"].set()
        except Exception as e:
            print(f"[bchat mgr] 异常: {e}")


def _cmd_brief(cmd):
    """规则层指令的紧凑可读形式（聊天台显示用）。"""
    c = cmd.get("cmd")
    if c == "build":
        return f"盖 {cmd.get('action')}@({cmd.get('x', '?')},{cmd.get('y', '?')})"
    if c == "train":
        return f"产 {cmd.get('action')} @厂#{cmd.get('factory')}"
    if c == "resume":
        return f"抢修 #{cmd.get('target')} ← 建造者#{cmd.get('builder')}"
    if c == "rally":
        return f"集结点 ({cmd.get('x')},{cmd.get('y')})"
    return str(cmd)[:60]


def start(ctx):
    """建共享状态 + 三个 agent 循环线程（daemon）。返回 SharedState。"""
    st = SharedState(ctx)
    # 规则层动作上聊天台（看得见才有"在干活"的体感）
    ex0 = ctx["exec_holder"].get("ex")
    if ex0:
        ex0.on_action = lambda c: _push_chat(ctx, "system", "⚙ " + _cmd_brief(c))
        ex0.control.fog = st.fog_map  # 安全值迷雾惩罚（全黑/浅黑）
        ctx["reflex"].power_table = ex0.power_table  # 受击撤退的战力闸门（战力高于敌方就不许撤）
    # 反射事件闭环：建造者召回 → 建造点位黑名单(10min)+建造待读；小队反射 → 战斗待读
    reflex = ctx["reflex"]

    def _on_recall(bid, tx, ty):
        st.site_blocks[(int(tx // 400), int(ty // 400))] = time.time() + 600
        ex = ctx["exec_holder"].get("ex")
        if ex:  # 规则层同步禁建（此前只有 LLM 路径被拦，规则层照往同一敌区池派人）
            ex.ext_pool_bad[(int(tx // 400), int(ty // 400))] = time.time() + 600
        st.pending["build"].append(
            f"[系统通报] 你派往 ({int(tx)},{int(ty)}) 的建造者#{bid} 被系统反射召回（敌区），"
            f"该点附近 10 分钟内禁止再建，请换安全位置。")

    def _on_squad(text):
        # 反射通报按 sid 路由到对应军种中层（解析不出归陆军兜底）
        m0 = re.search(r"\b([LAN]\d+)\b", text)
        st.pending[_sid_branch(m0.group(1)) if m0 else "land"].append("[系统通报] " + text)

    reflex.on_builder_recall = _on_recall
    reflex.on_squad_reflex = _on_squad

    def _chat_yield(domain):
        """建造 chat 让位闸（executor._wo_tick 查）：该域任一组有员且不在回退期。"""
        if not ctx["cfg"].get("builder_chat_enabled") or not ctx["ai_enabled"]():
            return False
        return any(bg["members"] and time.time() > bg["fallback_until"]
                   for gid, bg in st.bgroups.items() if _bchat_dom(gid) == domain)

    ctx["chat_yield_fn"] = _chat_yield
    if ex0:
        ex0.chat_yield_fn = _chat_yield
    for role in _roles(ctx):
        threading.Thread(target=_agent_loop, args=(ctx, st, role),
                         daemon=True, name=f"rw-{role['name']}").start()
    threading.Thread(target=_squad_manager, args=(ctx, st),
                     daemon=True, name="rw-squad-manager").start()
    threading.Thread(target=_bchat_manager, args=(ctx, st),
                     daemon=True, name="rw-bchat-manager").start()
    print("[multiagent] 循环已启动：commander(慢) build(指挥) combat(指挥) squad-manager bchat-manager")
    return st


def _update_defeated(ctx, st, snap, now):
    """阵亡判定：队伍在、非 AI 观战，但 own（单位+建筑）全空持续 15s → 阵亡待机。
    活着不可能是 0（最后一个资产没了=游戏已判负）；资金冻结是旁证（实测 $20836 冻死）。
    死后队伍对象仍在（ai=false、funds 冻结），旧闸门只看"有没有队伍"所以拦不住——
    LLM 对着零资产分析半小时、把敌人建筑幻觉成"我方 CC 被占"（2026-07-24 实测）。
    新对局由 supervisor 重置清旗。"""
    if snap.get("own"):
        st.defeated_since = None
        st.defeated = False
        return
    if st.defeated_since is None:
        st.defeated_since = now
        return
    if not st.defeated and now - st.defeated_since >= 15.0:
        st.defeated = True
        ctx["log_activity"]("alert", "☠ 已阵亡：全部单位与建筑损失，AI 转入待机（新对局自动恢复）")
        print("[multiagent] 已阵亡，三循环转入待机")


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
                        st.defeated = False          # 新对局清阵亡旗（三循环恢复）
                        st.defeated_since = None
                        st.site_blocks.clear()
                        st.requests.clear()
                        st.last_rally = None
                        st.conn_comp, st.conn_n = connmap.build_components(new_map.get("rows", []))
                        st.danger_map = danger_mod.DangerMap()
                        st.fog_map = fog.FogMap(new_map)  # 换图重建迷雾模型（网格随新快照到位）
                        st.danger_map.fog = st.fog_map
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
                    ex.routine_defense = bool(ctx["cfg"].get("routine_defense", True))  # 脚本防线（false=只听阵地单）
                    ex.auto_production = bool(ctx["cfg"].get("auto_production", True))  # 规则层自动补军工队列
                    ex.dmap = st.danger_map
                    ex.control.fog = st.fog_map  # 安全值迷雾惩罚（全黑/浅黑）
                    ex.on_action = lambda c: _push_chat(ctx, "system", "⚙ " + _cmd_brief(c))
                    ex.chat_yield_fn = ctx.get("chat_yield_fn")  # 建造 chat 管辖域，规则层让位
                    ctx["exec_holder"]["ex"] = ex
                    ctx["reflex"].dmap = st.danger_map
                    smz = summarize.Summarizer(new_map.get("resources", []))
                    st.generation += 1
                    print("[multiagent] 检测到新对局，三循环上下文已重置")
                    ctx["log_activity"]("status", "新对局开始，多 agent 上下文已重置")
            snap = link.snapshot()
            fog_snap = getattr(link, "fog_snap", None)
            if fog_snap or snap:
                st.fog_map.update(fog_snap or snap)  # 迷雾三态/幽灵 ~10s 一跳；AI 关了也照更（面板要看）
            if snap and snap.get("team") and not snap["team"].get("ai"):
                _update_defeated(ctx, st, snap, time.time())
            if snap and snap.get("team") and not snap["team"].get("ai") and ctx["ai_enabled"]():
                home = _home(snap)
                delegated = set(snap.get("delegated", []))
                if delegated:
                    # 小队结构喂反射层（守家队已取消：回防=按距离拉最近的有员小队）
                    sq_view = {sid: {"members": sq["members"]} for sid, sq in st.squads.items()}
                    ctx["reflex"].tick(snap, (home["x"], home["y"]) if home else None,
                                       delegated, link.send, ctx["log_activity"], squads=sq_view)
                st.danger_map.update(snap, (home["x"], home["y"]) if home else None)
            while True:
                with link.lock:
                    if not link.user_msgs:
                        break
                    msg = link.user_msgs.popleft()
                st.broadcast(f"【玩家留言】（优先级最高，照做并说明怎么执行）: {msg}")
                ctx["log_activity"]("chat", f"💬 玩家: {msg}")
                _push_chat(ctx, "player", f"💬 {msg}")
            # 工单注入（静音/测试通道）：wo_inject.json → set_work_order 同款校验，注完即删
            inj = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wo_inject.json")
            if os.path.exists(inj):
                try:
                    with open(inj, encoding="utf-8") as f:
                        payload = json.load(f)
                    os.remove(inj)
                    if isinstance(payload, dict) and ctx["exec_holder"].get("ex"):
                        _, desc = _translate_build(ctx, st, "set_work_order", payload,
                                                   link.snapshot(), {})
                        ctx["log_activity"]("status", f"⚙ 工单注入: {desc}")
                except Exception as e:
                    ctx["log_activity"]("error", f"✗ 工单注入失败: {e}")
        except Exception as e:
            print(f"[multiagent] 督导异常: {e}")


def run(ctx):
    st = start(ctx)
    try:  # 外部监控面板（总控台）：开关覆盖层+HTTP 服务，挂了不拖累桥接
        import dashboard
        dashboard.start(ctx, st)
    except Exception as e:
        print(f"[dashboard] 启动失败（不影响桥接）: {e}")
    supervisor_loop(ctx, st)  # 阻塞
