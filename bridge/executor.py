#!/usr/bin/env python3
"""rw-executor: 确定性执行层——经济引擎 / 生产调度 / 建造者调度。

输入与 pilot 相同的快照 dict，输出原子指令（与 agent 协议一致的 cmd dict）。
LLM 只能通过三个 goal 接口影响它；设 LLM_OFF=1 时可完全独立运行（离线验证用）。

设计原则：
- 一切微观操作确定性：选址交给 agent 自动选址，本层只做"该不该建/建几个"。
- 动作可用性自学习：被 agent 回执拒绝的 (单位类型, 动作) 本局标记不再尝试。
- 建造者真并行：每个空闲建造者独立派活，施工中不打扰（agent 侧也有施工保护）。
"""
import math
import os
import re
import threading
import time
from collections import Counter, deque

import connmap
import powergrid

BUILDER_BUILD = {  # 建造者建造动作的静态优先级（按局面分支使用）
    "extractor": ["b_extractorT1"],
    "landFactory": ["b_landFactory"],
}
# 纯防空单位：默认生产时限制在军队 25% 以内（防空是配角不是主角）
AA_PROD = {"u_missileTank", "u_c_interceptor", "u_heavyInterceptor",
           "u_mechMissile", "u_mechHeavyMissile", "u_aaBeamGunship", "u_heavyAAShip"}
AA_PROD_NAMES = {"missileTank", "interceptor", "c_interceptor", "heavyInterceptor",
                 "mechMissile", "mechHeavyMissile", "aaBeamGunship", "aaBeamGunship_afterburn",
                 "heavyAAShip"}
AA_RATIO = 0.25
# 工厂生产的静态默认优先级（生产动作 ID，按 pedia 过滤后生效）
DEFAULT_PROD = {
    "landFactory": ["u_c_tank", "u_missileTank", "u_c_artillery", "u_plasmaTank", "u_heavyTank"],
    "airFactory": ["u_heavyInterceptor", "u_c_interceptor", "u_gunShip", "u_bomber"],
    "seaFactory": ["u_gunBoat", "u_missileShip", "u_battleShip", "u_heavyBattleship", "u_heavyAAShip"],
    "mechFactory": ["u_mechGun", "u_mechMissile", "u_mechArtillery", "u_mechBunker"],
}
# 各目标默认配置
DEFAULT_ECON = {"extractors": 4, "builders": 3, "max_factories": 2}

EXTRACTOR_PRICE = 700
FUNDS_RESERVE = 700        # 非采集器开支的资金底线
FACTORY_SAFE_FLOOR = 0.0   # 家圈工厂安全值地板：低于此=威胁压过一切，不盖（高于则被袭也照盖）
SAFE_PICK_FLOOR = -5.0     # 槽位择优地板：低于此的合法槽位跳过继续找（全热才退回最安全候选）
UPGRADE_FUNDS = 2600       # 超过此资金且采集器达标后开始升级
QUEUE_MAX = 2              # 每厂队列上限
MAX_CMDS_PER_TICK = 4      # 每拍指令上限（2 会被经济/升级占满，生产饿死）
ACTION_COOLDOWN = 2.0      # 同类指令最小间隔
UPGRADE_COOLDOWN = 30.0    # 同一建筑升级冷却


def _is_builder(u):
    # editorOrBuilder（沙盒编辑器单位）不是建造者：派给它=扔进虚空（Volcano 沙盒实测）
    return ("uilder" in u["t"] or "abricator" in u["t"]) \
        and "editor" not in u["t"].lower() and not u.get("bld")


def _is_factory(u):
    if not u.get("bld"):
        return False
    t = u["t"].lower()
    return "actory" in t or u["t"] == "commandCenter" or u["t"].startswith("outpost")


def _busy(u):
    # 实测：走路去施工 act="build@(x,y)"，真正盖着时 act="repair→目标"——
    # 除 None/""/"idle" 外都算忙，否则盖一半会被当成空闲而拉走
    return u.get("act") not in (None, "", "idle")


def _constructing(u):
    # prog 仅在未完成时下发且 0=刚放置：小于 1 即未完成（0 不能排除，否则新厂被当成完工）
    return u.get("prog", 1) < 1


class Executor:
    def __init__(self, map_msg, pedia, log=None):
        self.map = map_msg or {}
        self.log = log or (lambda kind, text: None)
        self.resources = self.map.get("resources", [])
        # pedia: 工厂类型 -> 可产动作集合；单位名 -> 升级动作ID（非 b_/u_/c_ 前缀组）
        self.factory_actions = {}
        self.upgrade_action = {}
        for u in (pedia or {}).get("units", []):
            builds = u.get("builds", [])
            prod = [b for b in builds if b.startswith("u_")]
            if prod:
                self.factory_actions[u["name"]] = set(prod)
            # 升级动作候选：排除 b_/u_/s_ 前缀、垃圾 id（如 "_0"）、以及取消类动作（_1 结尾=回退，会扣钱）；
            # c_ 前缀特殊：炮塔系的升级动作就是 c_ 开头（c_turret_t2_gun_0）——按与单位名的词干重合放行
            ups = [b for b in builds
                   if not b.startswith(("b_", "u_", "s_"))
                   and len(b) >= 3 and not b.startswith("_")
                   and not b.endswith("_1")
                   and (not b.startswith("c_")
                        or len(os.path.commonprefix([b, u["name"]])) >= 6)]
            if ups:
                self.upgrade_action[u["name"]] = ups[0]
        self.known_prod = set()        # 全部已知生产动作ID（构成键归一化用）
        for acts in self.factory_actions.values():
            self.known_prod.update(acts)
        # 动作 -> 产出单位科技等级（T2工厂不产T1兵的依据）
        self.action_tech = {}
        for u in (pedia or {}).get("units", []):
            name = u.get("name")
            for acts in self.factory_actions.values():
                if ("u_" + name) in acts:
                    self.action_tech["u_" + name] = u.get("tech", 1)
        self.econ_goal = dict(DEFAULT_ECON)
        self.army_goal = {"composition": None, "rally": None}  # composition: {action: 目标数}
        # 建筑占地表（pedia fp 字段）：类型名 -> (w, h, ox, oy)，槽位网格按此分配空间
        self.fp = {}
        # 动作价格表（pedia price 字段）：生产动作ID -> 价格（规则层生产的资金感知）
        self.price = {}
        for u in (pedia or {}).get("units", []):
            f = u.get("fp")
            if f:
                try:
                    self.fp[u["name"]] = (int(f.get("w", 1)), int(f.get("h", 1)),
                                          int(f.get("ox", 0)), int(f.get("oy", 0)))
                except (TypeError, ValueError):
                    pass
            if u.get("price") is not None:
                self.price["u_" + u["name"]] = int(u["price"])
        self.stance = "expand"
        self.action_bad = {}           # 动作 -> 解封时间（None=永久：ID不存在；限时=科技未达，120s重试）
        self.fac_action_bad = {}       # (工厂id, 动作) -> 解封时间：该厂当前产不了（T1 被下 T2 单），120s 重试
        self.fac_avail = {}            # 工厂id -> (可用动作集合, 学到时刻)：从"没有此动作"回执的可用列表学习；120s 过期防升级后卡死
        self.fac_upgraded = set()      # 已 T2 的工厂 id（可用列表含 T2 兵）——升级轨永久跳过，不再每 120s 探针
        self.cool = {}                 # 冷却：键 -> 到期时间
        self.rallied = set()           # 已设过集结点的工厂 id
        self.issued_builds = []        # [(建筑类型, 到期时间)] 刚下单、实体未出现的窗口期，防重复下单
        self.issued_cells = {}         # 类型 -> [(x, y, 到期时间)] 在途建筑占位（槽位网格用）
        self._comp = None              # 陆地连通域缓存（connmap，可达性判定用）
        self.custom_builds = []        # LLM 指定建造队列 [(action, x, y)]
        # 建造工单（BuilderManager）：有序建造任务队列，规则层调度建造者连干
        # 任务 = {"action": str, "slot": spec, "status": pending/assigned/done/failed,
        #         "builder": id, "ts": float, "x": x, "y": y, "note": str}
        self.work_order = []
        self.wo_version = 0
        self.ext_pool_bad = {}         # (gx,gy) -> 解封时间：agent 判"找不到合法建造位置"的池（120s 跳过）
        self.last_safe_pools = []      # 最近一拍的安全池清单（建造 prompt 报 pool:k 可用下标用）
        self._last_ext_xy = None       # (x, y, ts) 最近一次采集器下单（池失败归因用）
        self._last_build_xy = None     # (x, y, ts) 最近一次带坐标建造下单（选址失败归因用）
        self.site_bad = {}             # (gx,gy) -> 解封时间：选址判死格（水域/隔断/校验不过，600s）
        self.prog_seen = {}            # 建筑id -> (prog, 时间戳)：识别烂尾（进度长期不涨）
        self.upgrade_bad = {}          # (建筑id, 动作) -> 解封时间：该建筑此升级失败后的冷却
        self.last_upgrade = None
        self.up_pending = {}           # 厂id -> 升级单发出时刻（⑦ 停产让路窗口，60s 或学成 T2 解除）
        self.up_tries = {}             # 厂id -> 升级连续空转次数（回执 ok 但不升 T2，≥3 判死 10min）
        self.lock = threading.Lock()   # tick/目标设置/失败学习 共用（执行层独立线程）
        self.dmap = None               # DangerMap（pilot 注入）：敌人记忆/威胁热力
        self.on_action = None          # fn(cmd)：指令下发回调（multiagent 注入，推游戏内聊天台）
        self.train_hist = {}           # 工厂id -> deque(发车时间)：q 缺失（如 mechFactoryT2）时的本地队列估计
        self.last_military_order = 0.0  # 最近一次 LLM 军事指令时间（attack/harass/defend）
        self.auto_military_cd = 0.0
        self.auto_military = False      # 默认关闭：军事决策完全归 LLM（config 可开）
        self.autopilot = True           # False=多 agent 模式：只跑跟踪/抢修机械，经济生产决策归 LLM agent
        self.auto_production = True     # False=规则快层不自动补军工队列（产量归战斗 LLM 自己 train）
        self.produce_focus = {}         # 战斗 LLM produce 任务的定向猛造 {action: 剩余数}——规则层补水避开，防撞车
        self.routine = False            # 多 agent 模式的规则快层：例行（建造者/采集器）2s 级执行
        self.routine_defense = True     # 脚本自动防线（防线弧/池点哨戒/前线哨戒/内圈维修湾）——
        #                                 用户 2026-07-25 拍板取消（"实在太乱"）：防御只听计划书/阵地单
        self.naval = None               # 水图判定（lazy）：水域占比>=20% 或 水池>=2
        self.front_dir = None           # 布局前线方向（CC→图心单位向量），首个 CC 出现时定
        self._water_anchor = "?"        # 近水锚点缓存（None=无水域；"?"=未计算）
        self.resume_cd = {}             # 烂尾建筑id -> 续建派工冷却时间
        self.resume_by = {}             # 烂尾建筑id -> (建造者id, 派工时刻)：在途不重复派
        self.resume_strike = {}         # 烂尾建筑id -> 换人次数：3 次未建成判死 10min
        self.resume_bad = {}            # 烂尾建筑id -> 判死解封时间（不可达/三派未建成）
        self.chat_yield_fn = None       # 建造 chat 让位闸（multiagent 注入）：fn(domain)->True 时该域工单归小模型派
        self._chokes = None             # 隘口检测结果缓存（地图不变）：[(x,y,宽px)]
        self._wext_issued = 0.0         # 水池建造下单窗口（45s 内不再派第二单）
        self.start_ts = time.time()
        # 战力标签 + 领土控制图（确定性，快照驱动）
        self.power_table = powergrid.power_table(pedia)
        self.control = powergrid.ControlMap(self.power_table)
        self.control.set_threat_fn(
            lambda x, y, snap: (self.dmap.threat_at(x, y)
                                if self.dmap else 0.0))

    def _mobile_builder(self, idle_builders, x, y, cc):
        """按可达性挑建造者：点陆军可达→顺排第一；不可达→悬浮建造者（HOVER 过水过岩浆），
        没有悬浮才退回顺排第一（让它自己试错，别拦）。返回 None=无人可派。"""
        if not idle_builders:
            return None
        if cc and not self._reachable(cc, x, y):
            hov = next((u for u in idle_builders
                        if u["t"] == "combatEngineer" or "hover" in u["t"].lower()), None)
            if hov:
                return hov
        return idle_builders[0]

    # ---------- LLM 目标接口 ----------
    def set_work_order(self, tasks, snap=None):
        """下发建造工单（LLM 规划员）：有序任务队列。
        tasks: [{"action": "b_x", "slot": spec, "note": str?}, ...]
        slot 规格（派发时现场解析，队列随局自适应）：
          "pool:k"=第 k 近安全池 | "factory:k"=厂区槽位k | "defense:k"=防线弧位k
          "misc:k"=后勤槽位k | "water"=近水点 | (x,y)=精确坐标
        阵地单：{"action": "position", "kind": defense_ground/aa/mixed, "anchor": (x,y),
                 "toward": (x,y)?, "count": n}——按防线弧规则现场展开（LLM 只给锚点/朝向/
                 类型/座数，精确落点归 defense_slot，地形/占位校验全在确定性层）。
        施工中的任务并回新工单（全量替换不清在干的——低水位续单不打断施工）。
        状态机：pending -> assigned -> done/failed。规则层 2s 拍调度建造者连干。"""
        keep = [t for t in self.work_order if t["status"] == "assigned"]
        self.work_order = list(keep)
        for t in tasks or []:
            if str((t or {}).get("action", "")) == "position":
                self.work_order.extend(self._expand_position(t, snap))
                continue
            act = str((t or {}).get("action", ""))
            if not act.startswith(("b_", "u_", "c_")):
                act = "b_" + act
            self.work_order.append({"action": act, "slot": (t or {}).get("slot"),
                                    "note": str((t or {}).get("note", "")), "status": "pending",
                                    "builder": None, "ts": 0.0, "x": None, "y": None})
        for i, t in enumerate(self.work_order):
            t["idx"] = i
        self.wo_version += 1
        self.log("status", f"⚙ 建造工单 v{self.wo_version} 已接收（{len(self.work_order)} 项"
                           + (f"，并回在干 {len(keep)} 项" if keep else "") + "）")

    def chokes(self, cc, axis, n_max=2):
        """隘口检测：沿 axis（家→前线/图心方向）每 200px 采样（600~1800px），
        垂直两翼量陆地走廊宽（≤600px 才算隘口），取最窄 n_max 个。结果缓存。
        "哪里需要防护"的规则答案之一：窄口必守。"""
        if self._chokes is not None:
            return self._chokes
        rows = self.map.get("rows") or []
        self._chokes = []
        if not rows:
            return self._chokes
        tw, th = self._tw()

        def _land(gx, gy):
            return 0 <= gy < len(rows) and 0 <= gx < len(rows[gy]) and rows[gy][gx] in (".", "=")

        perp = (-axis[1], axis[0])
        out = []
        for step in range(3, 10):
            bx = cc["x"] + axis[0] * step * 200
            by = cc["y"] + axis[1] * step * 200
            if not self._in_bounds(bx, by):
                continue
            if not _land(int(bx // tw), int(by // th)):
                continue  # 采样点本身不是陆地，不成通道
            w = 20.0  # 中心格
            for sign in (1, -1):
                run = 0.0
                while run < 800.0:
                    px = bx + perp[0] * sign * (run + 20)
                    py = by + perp[1] * sign * (run + 20)
                    if not _land(int(px // tw), int(py // th)):
                        break
                    run += 20
                w += run
            if w <= 600:
                out.append((w, bx, by))
        out.sort()
        self._chokes = [(x, y, int(w)) for w, x, y in out[:n_max]]
        return self._chokes

    def frontier_anchor(self, cc, axis, now):
        """实控前沿锚点：安全值≥30 的格中沿敌向投影最远的格心（防线贴实控边界走，
        §十二路线项"防线锚点改实控边界"落地）。无实控前沿返回 None。"""
        best = None
        for (gx, gy), c in (self.control.cells or {}).items():
            cx, cy = (gx + 0.5) * 400.0, (gy + 0.5) * 400.0
            s = self.control.safety(cx, cy)
            if s < 30.0:
                continue
            proj = (cx - cc["x"]) * axis[0] + (cy - cc["y"]) * axis[1]
            if proj < 200:  # 家圈内不算前沿
                continue
            if best is None or proj > best[0]:
                best = (proj, cx, cy, s)
        return (best[1], best[2], best[3]) if best else None

    def _formation(self, center, axis, kind, count, occ):
        """密集阵型生成（defense_sample.json 用户 837 座实战样本参数）：
        沿敌向垂直线两翼展开（0,+100,-100,+200,-200…，2x2+3格缝=100px 防线甜点），
        炮塔顶第一排、防空压后排 120px（mixed=每 3 座插 1 防空），
        主通道留空，逐点占位/可达/出界校验，落子即占（阵型内不撞点）。"""
        seq = {"defense_ground": [("c_turret_t1", 0)],
               "defense_aa": [("c_antiAirTurret", 0), ("c_antiAirTurret", -120)],
               "defense_mixed": [("c_turret_t1", 0), ("c_turret_t1", 0), ("c_antiAirTurret", -120)]
               }.get(kind, [("c_turret_t1", 0), ("c_turret_t1", 0), ("c_antiAirTurret", -120)])
        perp = (-axis[1], axis[0])
        pitch = 100.0
        out = []
        n = 0
        while len(out) < count and n < count * 4:
            btype, back = seq[n % len(seq)]
            # 排位随 n 逐个推进：0,+100,-100,+200,-200…（同排两座必在不同翼位）
            off = 0.0 if n == 0 else ((n + 1) // 2) * (1 if n % 2 == 1 else -1) * pitch
            cx = center["x"] + perp[0] * off + axis[0] * back
            cy = center["y"] + perp[1] * off + axis[1] * back
            n += 1
            if not self._in_bounds(cx, cy):
                continue
            if self._in_lane(center, cx, cy):
                continue  # 主通道留空（堵路比没盖好更糟）
            if not self._reachable(center, cx, cy):
                continue
            tw, th = self._tw()
            if not self._cell_free(occ, btype, int(cx // tw), int(cy // th)):
                continue
            self._mark_fp(occ, btype, cx, cy)
            out.append((btype, (cx, cy)))
        return out

    def _expand_position(self, t, snap):
        """阵地单 → 密集阵型任务序列（LLM 只给锚点/朝向/类型/座数，几何全在确定性层）。"""
        anchor = t.get("anchor")
        if not (isinstance(anchor, (list, tuple)) and len(anchor) == 2):
            self.log("note", "⚙ 阵地单被丢弃：anchor 非法")
            return []
        ax, ay = float(anchor[0]), float(anchor[1])
        toward = t.get("toward")
        mw = (self.map.get("tilesW", 0) or 0) * (self.map.get("tileW", 0) or 0)
        mh = (self.map.get("tilesH", 0) or 0) * (self.map.get("tileH", 0) or 0)
        tx, ty = (float(toward[0]), float(toward[1])) \
            if isinstance(toward, (list, tuple)) and len(toward) == 2 else (mw / 2, mh / 2)
        dx, dy = tx - ax, ty - ay
        dl = max(1.0, math.hypot(dx, dy))
        axis = (dx / dl, dy / dl)
        center = {"x": ax, "y": ay}
        kind = str(t.get("kind", "defense_mixed"))
        count = max(1, min(12, int(t.get("count") or 4)))
        note = str(t.get("note", ""))
        occ = self._occupancy(snap) if snap else set()
        spots = self._formation(center, axis, kind, count, occ)
        out = [{"action": "b_" + btype, "slot": [spot[0], spot[1]],
                "note": (note + "|" if note else "") + f"阵地{kind}#{i + 1}",
                "status": "pending", "builder": None, "ts": 0.0, "x": None, "y": None}
               for i, (btype, spot) in enumerate(spots)]
        self.log("status", f"⚙ 阵地单展开(密集阵): {kind}@({int(ax)},{int(ay)}) → {len(out)} 座")
        return out

    def _wo_resolve(self, task, snap, cc, safe_pools, occ):
        """slot 规格 → 世界坐标（派发时现场解析）。"""
        slot = task["slot"]
        btype = task["action"][2:]
        if isinstance(slot, (list, tuple)) and len(slot) == 2:
            return float(slot[0]), float(slot[1])
        if not cc:
            return None
        if isinstance(slot, str):
            if slot.startswith("pool:"):
                k = int(slot.split(":")[1])
                if k < len(safe_pools):
                    return float(safe_pools[k]["x"]), float(safe_pools[k]["y"])
                return None
            if slot.startswith("factory:"):
                land = [u for u in snap.get("own", []) if u.get("bld") and u["t"] == "landFactory"]
                idx = sum(1 for u in snap.get("own", [])
                          if u.get("bld") and "actory" in u["t"].lower()
                          and u["t"] not in ("commandCenter", "seaFactory"))
                return self.factory_slot(cc, idx, land, occ=occ, btype=btype)
            if slot.startswith("defense:"):
                anchor = self.army_goal.get("rally")
                mw = (self.map.get("tilesW", 0) or 0) * (self.map.get("tileW", 0) or 0)
                mh = (self.map.get("tilesH", 0) or 0) * (self.map.get("tileH", 0) or 0)
                tx, ty = anchor if anchor else (mw / 2, mh / 2)
                ax, ay = tx - cc["x"], ty - cc["y"]
                alen = max(1.0, math.hypot(ax, ay))
                idx = int(slot.split(":")[1]) if ":" in slot else 0
                spot = self.defense_slot(cc, occ, btype, idx, (ax / alen, ay / alen), 0)
                if spot == "LANE":  # 主通道留空，跳一号
                    spot = self.defense_slot(cc, occ, btype, idx + 1, (ax / alen, ay / alen), 0)
                return None if spot in (None, "LANE") else spot
            if slot.startswith("misc:"):
                idx = sum(1 for u in snap.get("own", []) if u.get("bld")
                          and (u["t"].startswith("fabricator") or "experimental" in u["t"].lower()
                               or u["t"] == "repairbay"))
                spot = self.misc_slot(cc, idx, occ=occ, btype=btype)
                if spot is None and idx:
                    # idx=存量计数：爆兵/沙盒局（43 座制造仪溢出 7km 出界）降级到排首
                    spot = self.misc_slot(cc, 0, occ=occ, btype=btype)
                return spot
            if slot == "water":
                wa = self.water_anchor(cc)  # (x, y, shore?)——岸边与否由派发层按建造者移动域接
                return (wa[0], wa[1]) if wa else None
        return None

    # ---------- 建造 chat 域分流（工单条目共经济/防御两域） ----------
    _WO_DEFENSE_TYPES = ("c_turret", "c_antiAirTurret", "laserDefence", "repairbay", "wall")

    def _wo_domain(self, t):
        b = t["action"][2:] if t["action"].startswith(("b_", "u_")) else t["action"]
        return "defense" if b.startswith(self._WO_DEFENSE_TYPES) else "econ"

    def wo_pending(self, domain=None):
        """待派工单条目（建造 chat 上下文/看门狗用）。"""
        return [t for t in self.work_order if t["status"] == "pending"
                and (domain is None or self._wo_domain(t) == domain)]

    def wo_mark_assigned(self, idx, builder_id, x, y):
        """建造 chat 派单落账：任务标 assigned（完成/烂尾判定照 BuilderManager 老路径）。"""
        for t in self.work_order:
            if t["idx"] == idx and t["status"] == "pending":
                t["status"] = "assigned"
                t["builder"] = builder_id
                t["x"], t["y"] = float(x), float(y)
                t["ts"] = time.time()
                t["note"] += "|chat派"
                return True
        return False

    def _wo_tick(self, snap, emit, now, own, funds, idle_builders, safe_pools, cc, occ, danger):
        """建造工单调度（BuilderManager 核心）：空闲建造者按序领最多 3 个连续任务，
        以 buildq 追加进游戏原生路点队列（盖完一个自动去下一个，玩家拉条同款）。
        资金按累计价停派（攒钱不乱序）；科技/占地/可达不过判 failed 不拖累队列。"""
        if not self.work_order:
            return
        for task in self.work_order:
            if not idle_builders:
                break
            if task["status"] != "pending":
                continue
            batch = []
            funds_left = funds
            for t in self.work_order:
                if t["status"] != "pending":
                    continue
                if self.chat_yield_fn and self.chat_yield_fn(self._wo_domain(t)):
                    continue  # 建造 chat 管辖域：规则层让位（小模型派单，45s 静默回退兜底）
                btype = t["action"][2:]
                price = self.price.get("u_" + btype) or self.price.get(btype) or 0
                if price and funds_left < price:
                    break  # 有序队列：攒钱再派
                if self._bad(t["action"], now):
                    t["status"] = "failed"
                    t["note"] += "|动作不可用"
                    continue
                spot = self._wo_resolve(t, snap, cc, safe_pools, occ)
                if not spot:
                    t["status"] = "failed"
                    t["note"] += "|槽位解析失败"
                    if isinstance(t["slot"], str) and t["slot"].startswith("pool:"):
                        # 无声失败曾是全灭盲区（Dividing_River 工单 3/3 失败零归因）
                        t["note"] += f"（安全池共{len(safe_pools)}个）"
                        self.log("note", f"⚙ {t['slot']} 解析失败：安全池仅{len(safe_pools)}个"
                                         "（迷雾卡闸/威胁未清/家圈有敌）")
                    elif isinstance(t["slot"], str) and t["slot"].startswith("misc:"):
                        t["note"] += "（背后排满/出界，含 idx=0 降级）"
                        self.log("note", f"⚙ {t['slot']} 解析失败：基地背后排满或出界（制造仪农场局）")
                    continue
                if cc and self._in_lane(cc, spot[0], spot[1]) \
                        and not t["action"].startswith("b_extractor") \
                        and not (isinstance(t["slot"], str) and t["slot"].startswith(("pool:", "water"))):
                    # 压主通道：垂直让位 240px（撞道自动跳位——建筑把路堵死比没盖好更糟）；
                    # 采集器/水池豁免：池点是固定的，挪了就不是池了
                    fx, fy = self.front_dir
                    nx, ny = spot[0] - fy * 240, spot[1] + fx * 240
                    if self._in_bounds(nx, ny):
                        spot = (nx, ny)  # 让位出界就不让（负坐标垃圾单的根治）
                # 显式坐标落点预检：撞占位（已有建筑/在途/判死格/工单兄弟任务）就近挪位，
                # 否则 agent 校验跳过只剩 "{0}" 模板——工单任务失败得无声无息（"感觉被吞"）
                if isinstance(t["slot"], (list, tuple)):
                    tw, th = self._tw()
                    btype = t["action"][2:]
                    gx0, gy0 = int(spot[0] // tw), int(spot[1] // th)
                    occ_ns = self._occupancy(snap, exclude_idx=t["idx"])  # 豁免本任务自身坐标
                    if not self._cell_free(occ_ns, btype, gx0, gy0):
                        moved = None
                        for r in range(1, 7):
                            for k in range(8):
                                ang = k * math.pi / 4
                                gx, gy = gx0 + int(round(math.cos(ang) * r)), gy0 + int(round(math.sin(ang) * r))
                                if self._cell_free(occ, btype, gx, gy):
                                    moved = self._cell_xy(gx, gy)
                                    break
                            if moved:
                                break
                        if moved:
                            t["note"] += f"|撞占位挪位({int(spot[0])},{int(spot[1])})→({int(moved[0])},{int(moved[1])})"
                            spot = moved
                if cc and not self._reachable(cc, spot[0], spot[1]):
                    # 陆军不可达≠死路：悬浮建造者（combatEngineer，HOVER 过水过岩浆）接手；
                    # 没有就保持 pending 等它上线（此前直接判死"陆军不可达"——
                    # Volcano 26 池 24 个陆建够不着，一判死经济直接断气）
                    ce = next((u for u in idle_builders
                               if u["t"] == "combatEngineer" or "hover" in u["t"].lower()), None)
                    if ce is None:
                        continue
                    if emit({"cmd": "build", "action": t["action"], "builder": ce["id"],
                             "x": float(spot[0]), "y": float(spot[1])}, f"wo:{t['idx']}:{ce['id']}", 3.0):
                        t["status"] = "assigned"
                        t["builder"] = ce["id"]
                        t["x"], t["y"] = float(spot[0]), float(spot[1])
                        t["note"] += "|悬浮接手"
                        idle_builders = [u for u in idle_builders if u["id"] != ce["id"]]
                        funds_left -= price
                    continue
                # 扩张类（采集器）坐标单也要过实控闸：家圈 600px 外必须安稳（secured 持续 20s），
                # 否则就是"资源点选进敌区"——此前只有 pool:k 规格走 safe_pools 有这道闸，
                # LLM 写精确坐标就绕过了（用户实局：builder 孤悬敌境）
                if btype.startswith("extractor") and cc \
                        and (spot[0] - cc["x"]) ** 2 + (spot[1] - cc["y"]) ** 2 > 600 ** 2 \
                        and not self.control.secured(spot[0], spot[1], now):
                    t["status"] = "failed"
                    t["note"] += "|超出实控区（远征采集需先安稳）"
                    continue
                # 危险判定三家：防守类防线圈(≤1200px)内挨打照盖（防线挨打正是该盖的时候）；
                # 家圈工厂看安全值地板（被袭时家圈全线危险会彻底停产，危险不一刀切，
                # 安全值<0=威胁压过一切才否）；扩张类(采集器/远征)危险照样禁（治开局乱跑送死）
                defense_types = ("c_turret", "c_antiAirTurret", "laserDefence", "repairbay", "wall")
                near_home = cc and (spot[0] - cc["x"]) ** 2 + (spot[1] - cc["y"]) ** 2 <= 1200 ** 2
                is_factory = "actory" in btype.lower() or btype.startswith("fabricator")
                if btype.startswith(defense_types) and near_home:
                    pass
                elif is_factory and near_home:
                    if self.control.safety(spot[0], spot[1]) < FACTORY_SAFE_FLOOR:
                        t["status"] = "failed"
                        t["note"] += "|点位危险(安全值过低)"
                        continue
                elif danger(spot[0], spot[1], 400):
                    t["status"] = "failed"
                    t["note"] += "|点位危险"
                    continue
                batch.append((t, spot, price))
                funds_left -= price
                if len(batch) >= 3:
                    break
            if not batch:
                break
            b = idle_builders[0]
            if len(batch) == 1:
                t, s, _p = batch[0]
                ok = emit({"cmd": "build", "action": t["action"], "builder": b["id"],
                           "x": float(s[0]), "y": float(s[1])}, f"wo:{t['idx']}:{b['id']}", 3.0)
            else:
                items = ";".join(f"{t['action']}@{s[0]:.0f},{s[1]:.0f}" for t, s, _p in batch)
                ok = emit({"cmd": "buildq", "builder": b["id"], "items": items},
                          f"woq:{b['id']}", 3.0)
            if ok:
                for t, s, _p in batch:
                    t["status"] = "assigned"
                    t["builder"] = b["id"]
                    t["ts"] = now
                    t["x"], t["y"] = s
                idle_builders = idle_builders[1:]
        # 完成/烂尾判定
        live = {(u["x"], u["y"], u["t"]) for u in own if u.get("bld")}
        for task in self.work_order:
            if task["status"] != "assigned":
                continue
            done = any(abs(x - task["x"]) < 60 and abs(y - task["y"]) < 60 and t == task["action"][2:]
                       for x, y, t in live)
            if done:
                task["status"] = "done"
            else:
                # 烂尾时限按距离给：建造者 ~25px/s，远任务 90s 一刀切=永远换人重走
                # （3000px 远征单循环空转的根治）；加成封顶 240s
                dist = math.hypot(task["x"] - cc["x"], task["y"] - cc["y"]) if cc else 0
                stall_to = 90 + min(240, dist / 25)
                if now - task["ts"] > stall_to:
                    task["status"] = "pending"  # 超时没起来（建造者阵亡等）→ 重派
                    task["builder"] = None
        done_n = sum(1 for t in self.work_order if t["status"] == "done")
        pending_n = sum(1 for t in self.work_order if t["status"] == "pending")
        self.wo_progress = (done_n, pending_n, len(self.work_order))

    def _wo_pending(self, prefix):
        """工单里是否还有 prefix 开头的未完成动作（响应式补给规则避让用）。"""
        return any(t["status"] in ("pending", "assigned") and t["action"].startswith(prefix)
                   for t in self.work_order)

    def set_economy_goal(self, extractors=None, builders=None):
        if extractors is not None:
            # 硬上限12：防 LLM 把扩张目标抬到天上去（8→10→22）拖垮全局
            self.econ_goal["extractors"] = max(0, min(12, int(extractors)))
        if builders is not None:
            self.econ_goal["builders"] = max(1, min(8, int(builders)))

    def apply_plan_params(self, params):
        """总指挥计划书 ```json 参数块 → 经济/姿态/集结目标（机器消费，不靠子层猜）。
        字段缺失回退现值；数值经 set_economy_goal 硬上限防天马行空。"""
        if not isinstance(params, dict):
            return
        ext, bld = params.get("采集器目标"), params.get("建造者目标")
        if isinstance(ext, (int, float)) or isinstance(bld, (int, float)):
            self.set_economy_goal(
                extractors=int(ext) if isinstance(ext, (int, float)) else None,
                builders=int(bld) if isinstance(bld, (int, float)) else None)
        stance = params.get("姿态")
        if isinstance(stance, str) and stance in ("expand", "defend", "attack"):
            self.stance = stance
        rally = params.get("主攻")
        if (isinstance(rally, list) and len(rally) == 2
                and all(isinstance(v, (int, float)) for v in rally)):
            self.army_goal["rally"] = (float(rally[0]), float(rally[1]))
        self.log("status", f"⚙ 计划书参数已消费: 池{self.econ_goal['extractors']} "
                           f"建造者{self.econ_goal['builders']} 姿态{self.stance}")

    def set_army_goal(self, composition=None, rally=None):
        if composition is not None:
            # 归一化按"真实生产动作表"匹配：'c_tank'→'u_c_tank'；无法识别的键丢弃并上报（防静默死键）
            norm = {}
            dropped = []
            for k, v in composition.items():
                key = str(k)
                if key in self.known_prod:
                    norm[key] = int(v)
                elif ("u_" + key) in self.known_prod:
                    norm["u_" + key] = int(v)
                else:
                    dropped.append(key)
            self.army_goal["composition"] = norm
            if dropped:
                self.log("status", f"⚙ 构成键无法识别已丢弃: {','.join(dropped)}（用 query prices 查真实动作ID）")
        if rally is not None:
            self.army_goal["rally"] = (float(rally[0]), float(rally[1]))
            self.rallied.clear()

    def set_stance(self, stance):
        self.stance = str(stance)

    def queue_build(self, action, x=None, y=None):
        """LLM 指定建造（build_building 动词）：归一化 b_ 前缀后进入建造队列。"""
        action = str(action)
        if not action.startswith(("b_", "u_", "c_")):
            action = "b_" + action  # LLM 常省略 b_ 前缀（landFactory → b_landFactory）
        self.custom_builds.append((action, x, y))

    def _bad(self, act, now):
        """动作当前是否不可用（None=永久；限时项到期自动解封重试）。"""
        if act not in self.action_bad:
            return False
        exp = self.action_bad[act]
        return exp is None or now < exp

    def _fac_bad(self, fid, act, now):
        """该厂当前是否产不了此动作（按厂封禁，不误伤已升级的同类厂）。"""
        exp = self.fac_action_bad.get((fid, act))
        return exp is not None and now < exp

    # ---------- 失败回执学习 ----------
    def on_cmd_results(self, results):
        for r in results or []:
            if r.get("ok"):
                continue
            msg = r.get("msg") or ""
            m = re.search(r"工厂#(\d+) 没有此动作[:：]\s*([A-Za-z0-9_]+)(?:；可用[:：]\s*([A-Za-z0-9_,]+))?", msg)
            if m:
                # 按厂封禁（回执带厂号）：T1 厂被下 T2 单只封该厂，120s 后重试（升级后放行）；
                # 同时学走该厂真实可用动作表——比逐个动作试错快一档，打地鼠式刷屏的根治
                fid = int(m.group(1))
                self.fac_action_bad[(fid, m.group(2))] = time.time() + 120
                if m.group(3):
                    avail_set = set(m.group(3).split(","))
                    self.fac_avail[fid] = (avail_set, time.time())
                    if any(self.action_tech.get(a, 1) >= 2 for a in avail_set):
                        self.fac_upgraded.add(fid)  # 可用列表含 T2 兵=已是 T2，升级轨永久免探
                        self.up_tries.pop(fid, None)   # 升级落地：清空转/让路窗口
                        self.up_pending.pop(fid, None)
                self.log("status", f"⚙ 厂#{fid} 无此动作，120s后重试: {m.group(2)}")
                continue
            m = re.search(r"没有此动作[:：]\s*([A-Za-z0-9_]+)", msg)
            if m:
                # 不再全局永封：该动作对其他工厂可能合法（如 110 只存在于部分工厂）
                self.action_bad[m.group(1)] = time.time() + 300
                self.log("status", f"⚙ 动作此处不可用，300s后重试: {m.group(1)}")
                # 若能归因到最近的升级尝试，按建筑级冷却（防同一建筑反复失败）
                if self.last_upgrade and self.last_upgrade[1] == m.group(1) \
                        and time.time() - self.last_upgrade[2] < 6:
                    self.upgrade_bad[(self.last_upgrade[0], m.group(1))] = time.time() + 300
                continue
            m = re.search(r"不可用（科技/条件不满足）[:：]\s*([A-Za-z0-9_]+)", msg)
            if m:
                self.action_bad[m.group(1)] = time.time() + 120  # 科技未达：120s 后重试
                self.log("status", f"⚙ 动作科技未达，120s后重试: {m.group(1)}")
            # 池点被占（agent 全盘拒绝）：归因到最近采集器下单点位，120s 跳过
            if "找不到合法建造位置" in msg and self._last_ext_xy \
                    and time.time() - self._last_ext_xy[2] < 10:
                gx, gy = int(self._last_ext_xy[0] // 400), int(self._last_ext_xy[1] // 400)
                self.ext_pool_bad[(gx, gy)] = time.time() + 120
                self.log("status", f"⚙ 池点被占判死，120s跳过 ({self._last_ext_xy[0]:.0f},{self._last_ext_xy[1]:.0f})")
            # 选址失败签名（水域/隔断不可达、找不到合法位置）：回执带 @(x,y) 直接用
            # （多路并发建造共用单槽归因会张冠李戴）；旧格式无坐标才回退最近带坐标建造，600s 判死；
            # 不可达的池直接永久判死（本局不会变得可达）
            xy = re.search(r"@\((\d+),(\d+)\)", msg)
            if ("无法靠近" in msg or "找不到合法建造位置" in msg):
                bx = by = None
                if xy:
                    bx, by = float(xy.group(1)), float(xy.group(2))
                elif self._last_build_xy and time.time() - self._last_build_xy[2] < 10:
                    bx, by = self._last_build_xy[0], self._last_build_xy[1]
                if bx is not None:
                    gx, gy = int(bx // 400), int(by // 400)
                    self.site_bad[(gx, gy)] = time.time() + 600
                    self.log("status", f"⚙ 选址判死10分钟: 格({gx},{gy})")
                    if "无法靠近" in msg and self._last_ext_xy \
                            and self._last_ext_xy[0] == bx and self._last_ext_xy[1] == by:
                        self.ext_pool_bad[(gx, gy)] = time.time() + 3600
                        self.log("status", f"⚙ 池点不可达永久判死 ({bx:.0f},{by:.0f})")

    # ---------- 主循环 ----------
    def tick(self, snap):
        now = time.time()
        self.issued_builds = [e for e in self.issued_builds if e[1] > now]
        cmds = []
        own = snap.get("own", [])
        funds = (snap.get("team") or {}).get("funds", 0)
        mw = (self.map.get("tilesW", 0) or 0) * (self.map.get("tileW", 0) or 0)
        mh = (self.map.get("tilesH", 0) or 0) * (self.map.get("tileH", 0) or 0)

        builders = [u for u in own if _is_builder(u)]
        # 海上建造者只走水池调度（它上不了岸，陆地派活只会反复被可达性拒绝）
        idle_builders = [u for u in builders if not _busy(u) and u["t"] != "builderShip"]

        # 危险区：可见敌人 + 记忆威胁（敌人离开视野仍算危险，由 DangerMap 提供）
        enemies = snap.get("enemy", [])

        def danger(x, y, r=600):
            if self.dmap is not None:
                return self.dmap.danger(x, y, snap)
            return any((e["x"] - x) ** 2 + (e["y"] - y) ** 2 < r * r for e in enemies)

        _cc = next((u for u in own if u["t"] == "commandCenter" and u.get("bld")), None)
        ccx = _cc["x"] if _cc else 0
        ccy = _cc["y"] if _cc else 0
        # 控制图喂快照（每拍）：战力/占领/安全值全量刷新
        self.control.update(snap, self.dmap, (ccx, ccy) if _cc else None)
        # 布局前线方向（CC→图心）：全局长持，工厂槽位/后勤槽位/防线都以此定向
        if _cc and mw and mh and self.front_dir is None:
            fdx, fdy = mw / 2 - _cc["x"], mh / 2 - _cc["y"]
            fd = max(1.0, math.hypot(fdx, fdy))
            self.front_dir = (fdx / fd, fdy / fd)
        safe_pools = sorted(
            (p for p in self.resources
             if not p.get("w")
             and ((_cc is not None and (p["x"] - ccx) ** 2 + (p["y"] - ccy) ** 2 < 600 ** 2
                   # 家圈豁免迷雾评估，但不豁免眼皮底下的敌人
                   and not (self.dmap and self.dmap.danger(p["x"], p["y"], snap)))
                  # 远池必须过安稳闸门（安全值≥30 持续 20s，雾感知版威胁+全黑惩罚）
                  or self.control.secured(p["x"], p["y"], now))
             and not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                         for u in own + snap.get("allies", []) if u.get("bld"))),
            key=lambda p: (p["x"] - ccx) ** 2 + (p["y"] - ccy) ** 2)
        self.last_safe_pools = safe_pools  # 存证：建造 prompt 报 pool:k 可用清单（治盲下 pool 单）
        extractors = [u for u in own if u["t"].startswith("extractor") and u.get("bld")]
        ext_done = [u for u in extractors if not _constructing(u)]
        factories = [u for u in own if _is_factory(u) and not _constructing(u)]
        fac_producers = [u for u in factories if u["t"] != "commandCenter"]
        cc = next((u for u in factories if u["t"] == "commandCenter"), None)
        constructing = []
        for u in own:
            if u.get("bld") and _constructing(u):
                # 进度跟踪：60s 内进度有涨的才算"在盖"，不涨的记为烂尾（不占用在建数）
                last = self.prog_seen.get(u["id"])
                prog = u.get("prog", 1)
                if last is None or abs(prog - last[0]) > 0.005:
                    self.prog_seen[u["id"]] = (prog, now)
                    constructing.append(u)
                elif now - last[1] < 60:
                    constructing.append(u)
                # else: 烂尾（>60s 无进展）——不计入 constructing
        stalled = [u for u in own if u.get("bld") and _constructing(u) and u not in constructing]
        self.last_stalled = len(stalled)
        live_ids = {u["id"] for u in own}
        self.prog_seen = {k: v for k, v in self.prog_seen.items() if k in live_ids}
        self.resume_by = {k: v for k, v in self.resume_by.items() if k in live_ids}
        self.resume_strike = {k: v for k, v in self.resume_strike.items() if k in live_ids}
        busy_cnt = len(builders) - len(idle_builders)

        def emit(cmd, cool_key, cooldown=ACTION_COOLDOWN):
            if len(cmds) >= MAX_CMDS_PER_TICK:
                return False
            if now < self.cool.get(cool_key, 0):
                return False
            self.cool[cool_key] = now + cooldown
            cmds.append(cmd)
            if self.on_action:
                try:
                    self.on_action(cmd)
                except Exception:
                    pass
            if cmd["cmd"] == "build":
                act = cmd.get("action", "")
                btype = act[2:] if act.startswith("b_") else act
                self.issued_builds.append((btype, now + 45.0))  # 施工窗口期，防重复下单
                if cmd.get("x") is not None:
                    self.issued_cells.setdefault(btype, []).append((cmd["x"], cmd["y"], now + 45.0))
                    self._last_build_xy = (cmd["x"], cmd["y"], now)  # 失败回执归因用
            if cmd["cmd"] == "buildq":
                for it in cmd.get("items", "").split(";"):
                    if "@" not in it:
                        continue
                    act, xy = it.split("@", 1)
                    try:
                        x, y = (float(v) for v in xy.split(","))
                    except ValueError:
                        continue
                    btype = act[2:] if act.startswith("b_") else act
                    self.issued_builds.append((btype, now + 45.0))
                    self.issued_cells.setdefault(btype, []).append((x, y, now + 45.0))
            if cmd["cmd"] == "train":
                self.train_hist.setdefault(cmd["factory"], deque()).append(now)  # 本地队列估计用
            return True

        if not self.autopilot:
            # 多 agent 模式：本层保留进度跟踪 + 抢修兜底；routine=True 时再加规则快层。
            # 铁律：无委托单位=完全旁观（用户在手动玩/沙盒时，机器一拍都不许动）
            if snap.get("delegated"):
                self._dispatch_rescue(own, emit, stalled, idle_builders, danger, now, cc)
                if self.routine:
                    self._routine_tick(snap, emit, now, own, funds, builders, idle_builders,
                                       ext_done, factories, safe_pools, cc, mw, mh, danger)
            return cmds

        # 1) 建造者数量：低于目标且有钱就补（此前条件写反导致归零后永不补产）
        if cc and len(builders) < self.econ_goal["builders"] and (cc.get("q") or 0) < QUEUE_MAX:
            if funds >= 250:
                emit({"cmd": "train", "action": "u_builder", "factory": cc["id"]}, "train:cc")

        # 2) 采集器扩建（最高优先；只去安全池——敌区600px内不派建造者；完工数达标即停）
        ext_target = self.econ_goal["extractors"]
        ext_inflight = (sum(1 for u in constructing if u["t"].startswith("extractor"))
                        + sum(1 for t, _ in self.issued_builds if t.startswith("extractor")))
        if (len(ext_done) < ext_target and ext_inflight < 2
                and funds >= EXTRACTOR_PRICE and idle_builders and safe_pools):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_extractorT1", "builder": b["id"],
                  "x": float(safe_pools[0]["x"]), "y": float(safe_pools[0]["y"])},
                 f"build:ext:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 3) 第一座工厂（2 池后；只认"没有任何生产型工厂"，LLM 先建空厂/机甲厂就不重复塞陆厂）
        land_factories = [u for u in fac_producers if u["t"] == "landFactory"]
        lf_inflight = (sum(1 for u in constructing if u["t"] == "landFactory")
                       + sum(1 for t, _ in self.issued_builds if t == "landFactory"))
        any_fac_inflight = sum(1 for t, _ in self.issued_builds if "actory" in t)
        if (len(ext_done) >= 2 and len(fac_producers) + any_fac_inflight < 1
                and funds >= max(FUNDS_RESERVE, 700) and idle_builders):
            b = idle_builders[0]
            fcmd = {"cmd": "build", "action": "b_landFactory", "builder": b["id"]}
            if cc:  # 厂区槽位 0 号位：主基地侧翼（垂直于前线方向），不挡正面
                spot = self.factory_slot(cc, 0, [], occ=self._occupancy(snap))
                if spot is None:
                    fcmd = None  # 图内无合法厂位（图边）：跳过，不发非法坐标
                else:
                    fcmd["x"], fcmd["y"] = float(spot[0]), float(spot[1])
            if fcmd:
                emit(fcmd, f"build:lf:{b['id']}", 4.0)
                idle_builders = idle_builders[1:]

        # 3.5) 战斗工程师：扩厂时产 1-2 个；水/岩浆图主力（HOVER 过水过岩浆，
        #      陆建够不着的点全靠它——Volcano 实测陆建全废时它是唯一出路，不卡 ext_done）
        ce_count = sum(1 for u in own if u["t"] == "combatEngineer")
        ce_target = 3 if self.is_naval_map() else 2
        if ((len(ext_done) >= 2 or self.is_naval_map()) and ce_count < ce_target
                and funds >= 1500 and land_factories):
            lf = next((u for u in land_factories if self._q(u, now) < QUEUE_MAX), None)
            if lf and not self._bad("u_combatEngineer", now):
                emit({"cmd": "train", "action": "u_combatEngineer", "factory": lf["id"]},
                     "train:ce", 10.0)

        # 4) 工厂扩建：军力目标+资金双驱动；厂型跟随军队构成；优先派战斗工程师盖（直接T2省升级）
        army_total = sum((self.army_goal["composition"] or {}).values())
        fac_want = max(2, 1 + army_total // 10)
        if funds > 8000:
            fac_want += 1
        if funds > 15000:
            fac_want += 1
        if funds > 30000:
            fac_want += 2
        fac_want = min(8, fac_want)
        comp = self.army_goal["composition"] or {}
        air_acts = {"u_lightGunship", "u_c_interceptor", "u_gunShip", "u_c_helicopter", "u_bomber"}
        fac_action = "b_airFactory" if any(a in comp for a in air_acts) else "b_landFactory"
        fac_inflight = sum(1 for t, _ in self.issued_builds if "actory" in t)
        if (len(ext_done) >= 2 and len(fac_producers) + fac_inflight < fac_want
                and funds >= 1400 and idle_builders and not self._bad(fac_action, now)):
            b = self._pick_builder(idle_builders, for_factory=True)
            fcmd = {"cmd": "build", "action": fac_action, "builder": b["id"]}
            if cc:  # 厂区槽位：沿侧翼密排成行（海厂不参与厂区编号）
                idx = sum(1 for u in fac_producers if u["t"] != "seaFactory") + fac_inflight
                spot = self.factory_slot(cc, idx, land_factories,
                                         occ=self._occupancy(snap), btype=fac_action[2:])
                if spot is None:
                    fcmd = None
                else:
                    fcmd["x"], fcmd["y"] = float(spot[0]), float(spot[1])
            if fcmd:
                emit(fcmd, f"build:fac:{b['id']}", 4.0)
                idle_builders = [u for u in idle_builders if u["id"] != b["id"]]

        # 4.4) 生产调度（提到炮塔/升级之前：军力是之本，防止被经济动作饿死）
        #    按构成目标或默认优先级补队列；资金低于储备线且经济未达标时暂停
        econ_pending = (len(ext_done) + ext_inflight < ext_target) or (not land_factories)
        for fac in fac_producers:
            if self._q(fac, now) >= QUEUE_MAX:
                continue
            if funds < FUNDS_RESERVE and econ_pending:
                continue
            act = self._next_production(fac, own)
            if act:
                emit({"cmd": "train", "action": act, "factory": fac["id"]}, f"train:{fac['id']}")

        # 4.5) 防御：炮塔数量随资金增长（2→6），敌方有空军时穿插防空炮塔
        turrets = [u for u in own if "turret" in u["t"].lower() and u.get("bld")]
        t_inflight = sum(1 for t, _ in self.issued_builds if "turret" in t.lower())
        enemy_air = any(k in e["t"].lower() for e in snap.get("enemy", [])
                        for k in ("gunship", "jet", "bomber", "interceptor", "airship", "helicopter", "drone"))
        tur_action = "b_c_antiAirTurret" if (enemy_air and len(turrets) % 2 == 1) else "b_c_turret_t1"
        t_cap = min(6, 2 + int(funds // 20000))
        if (land_factories and funds >= 1600 and len(turrets) + t_inflight < t_cap
                and idle_builders and not self._bad(tur_action, now)):
            b = idle_builders[0]
            tcmd = {"cmd": "build", "action": tur_action, "builder": b["id"]}
            mw = (self.map.get("tilesW", 0) or 0) * (self.map.get("tileW", 0) or 0)
            mh = (self.map.get("tilesH", 0) or 0) * (self.map.get("tileH", 0) or 0)
            if cc and mw and mh:  # 防线弧形前推：朝地图中心逐座前移并左右展开（不再挤一条线）
                dx, dy = mw / 2 - cc["x"], mh / 2 - cc["y"]
                d = max(1.0, math.hypot(dx, dy))
                off = min(900, 260 + len(turrets) * 160)
                lat = 130 if len(turrets) % 2 else -130
                tcmd["x"] = float(cc["x"] + dx / d * off - dy / d * lat)
                tcmd["y"] = float(cc["y"] + dy / d * off + dx / d * lat)
            if "x" in tcmd and danger(tcmd["x"], tcmd["y"], 500):
                pass  # 炮塔位在敌区500px内，本拍不落（防线崩溃时不往火线上送）
            else:
                emit(tcmd, f"build:tur:{b['id']}", 6.0)
                idle_builders = idle_builders[1:]

        # 后勤建筑计数（fabricator/实验厂/维修湾）：misc 槽位按此排号，在主基地背后成排
        misc_n = (sum(1 for u in own if u.get("bld") and (u["t"].startswith("fabricator")
                      or "experimental" in u["t"].lower() or u["t"] == "repairbay"))
                  + sum(1 for t, _ in self.issued_builds
                        if t.startswith("fabricator") or "experimental" in t.lower() or t == "repairbay"))

        # 4.55) 僵局经济：资源点被占完、或采集器达标且资金爆表(≥15000)时，
        #      造资源制造仪发展额外经济（≤2座）——此前条件过严永远不会触发
        if funds >= 2500:
            fab_count = sum(1 for u in own if u["t"].startswith("fabricator") and u.get("bld"))
            fab_inflight = sum(1 for t, _ in self.issued_builds if t.startswith("fabricator"))
            friendly = own + snap.get("allies", [])
            free_pools = [p for p in self.resources
                          if not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                                     for u in friendly if u.get("bld"))]
            stalemate = (not free_pools) or (len(ext_done) >= self.econ_goal["extractors"] and funds >= 15000)
            inc = (snap.get("team") or {}).get("income")
            if inc is not None and inc < 8:
                stalemate = True  # 收入率低就是穷（不看余额）——触发资源制造仪补经济
            fab_cap = 4 if funds >= 15000 else 2
            if stalemate and fab_count + fab_inflight < fab_cap and idle_builders \
                    and not self._bad("b_fabricatorT1", now):
                b = idle_builders[0]
                fbcmd = {"cmd": "build", "action": "b_fabricatorT1", "builder": b["id"]}
                if cc:
                    spot = self.misc_slot(cc, misc_n)
                    if spot is None:
                        fbcmd = None
                    else:
                        fbcmd["x"], fbcmd["y"] = float(spot[0]), float(spot[1])
                if fbcmd and emit(fbcmd, f"build:fab:{b['id']}", 8.0):
                    misc_n += 1
                    idle_builders = idle_builders[1:]

        # 4.6) 高盈余战略建筑：实验厂（≥20000 且 ≥2 陆厂，限1座）与海军厂（水图才造，锚定近水）
        exp_count = sum(1 for u in own if "experimental" in u["t"].lower() and u.get("bld"))
        exp_inflight = sum(1 for t, _ in self.issued_builds if "experimental" in t.lower())
        # 大开支看收入率（有钱就乱用的根源）：收入 ≥15 或实在没这个字段才只看余额
        inc = (snap.get("team") or {}).get("income")
        rich = (inc >= 15) if inc is not None else (funds >= 20000)
        if (rich and funds >= 8000 and len(land_factories) >= 2 and exp_count + exp_inflight < 1
                and idle_builders and not self._bad("b_experimentalLandFactory", now)):
            b = idle_builders[0]
            excmd = {"cmd": "build", "action": "b_experimentalLandFactory", "builder": b["id"]}
            if cc:
                spot = self.misc_slot(cc, misc_n)
                if spot is None:
                    excmd = None
                else:
                    excmd["x"], excmd["y"] = float(spot[0]), float(spot[1])
            if excmd and emit(excmd, f"build:exp:{b['id']}", 8.0):
                misc_n += 1
                idle_builders = idle_builders[1:]
        sea_count = sum(1 for u in fac_producers if u["t"] == "seaFactory")
        sea_inflight = sum(1 for t, _ in self.issued_builds if t == "seaFactory")
        if (self.is_naval_map() and len(ext_done) >= 2 and sea_count + sea_inflight < 1
                and funds >= 1500 and idle_builders and not self._bad("b_seaFactory", now)):
            wa = cc and self.water_anchor(cc)
            if wa:  # 无水域锚点就不盖——海厂乱落位等于白盖
                b = idle_builders[0]
                emit({"cmd": "build", "action": "b_seaFactory", "builder": b["id"],
                      "x": float(wa[0]), "y": float(wa[1])},
                     f"build:sea:{b['id']}", 8.0)
                idle_builders = idle_builders[1:]

        # 4.65) 海上经济（水图限定）：海厂产 builderShip（≤2），派它去空闲水池建采集器；
        #      同一时刻只派一单（45s 窗口），防两船抢同一池
        if self.is_naval_map():
            friendly = own + snap.get("allies", [])
            free_water = [p for p in self.resources if p.get("w") and not danger(p["x"], p["y"])
                          and not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                                      for u in friendly if u.get("bld"))]
            bships = [u for u in own if u["t"] == "builderShip"]
            sea_done = [u for u in factories if u["t"] == "seaFactory" and not _constructing(u)]
            if free_water and sea_done and len(bships) < 2 and funds >= 600 \
                    and not self._bad("u_builderShip", now):
                sf = next((u for u in sea_done if self._q(u, now) < QUEUE_MAX), None)
                if sf:
                    emit({"cmd": "train", "action": "u_builderShip", "factory": sf["id"]},
                         "train:bship", 15.0)
            idle_bs = [u for u in bships if not _busy(u)]
            if idle_bs and free_water and funds >= EXTRACTOR_PRICE and now >= self._wext_issued:
                nb = idle_bs[0]
                p = min(free_water, key=lambda q: (q["x"] - nb["x"]) ** 2 + (q["y"] - nb["y"]) ** 2)
                if emit({"cmd": "build", "action": "b_extractorT1", "builder": nb["id"],
                         "x": float(p["x"]), "y": float(p["y"])},
                        f"build:wext:{nb['id']}", 8.0):
                    self._wext_issued = now + 45.0

        # 4.7) LLM 指定建造（build_building 队列）：海厂/机甲厂/维修湾/实验厂等战略建筑
        while self.custom_builds and idle_builders and funds >= 500:
            action, bx, by = self.custom_builds[0]
            if self._bad(action, now):
                self.custom_builds.pop(0)
                continue
            b = idle_builders[0]
            bcmd = {"cmd": "build", "action": action, "builder": b["id"]}
            if bx is not None and by is not None:
                bcmd["x"], bcmd["y"] = float(bx), float(by)
            elif cc:  # 无锚点：后勤槽位（主基地背后一排，不挡正面不挤厂区）
                spot = self.misc_slot(cc, misc_n)
                if spot is None:
                    bcmd = None
                else:
                    bcmd["x"], bcmd["y"] = float(spot[0]), float(spot[1])
            if bcmd and emit(bcmd, f"build:custom:{b['id']}", 6.0):
                self.custom_builds.pop(0)
                misc_n += 1
                idle_builders = idle_builders[1:]
            else:
                break

        # 4.8) 抢修：烂尾(>60s 无进展)与受损建筑(hp<85%)，派最近的空闲建造者 resume
        self._dispatch_rescue(own, emit, stalled, idle_builders, danger, now, cc)

        # 5) 建筑升级双轨：采集器绝对优先（经济是根本），工厂并行不挤占；优先于常规补兵
        def try_upgrade(units, need):
            if funds < need:
                return
            for u in units:
                if u["id"] in self.fac_upgraded:
                    continue  # 已 T2（可用列表含 T2 兵学来的），不再探升级
                if self._upgrade_pending(u["id"], now):
                    continue  # 升级在途 60s 内不重发（自打断疑云：上一发没落地又来一发）
                up = self.upgrade_action.get(u["t"])
                if not up or self._bad(up, now) or self._fac_bad(u["id"], up, now):
                    continue
                if (u["id"], up) in self.upgrade_bad and now < self.upgrade_bad[(u["id"], up)]:
                    continue
                if now < self.cool.get(f"up:{u['id']}", 0):
                    continue
                if emit({"cmd": "train", "action": up, "factory": u["id"]}, f"upemit:{u['id']}"):
                    # emit 成功才扣冷却（此前先扣后失败会吞掉这次升级）
                    self.cool[f"up:{u['id']}"] = now + UPGRADE_COOLDOWN
                    self.last_upgrade = (u["id"], up, now)
                return

        try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                     and (u["t"].startswith("extractor") or u["t"].startswith("fabricator"))
                     and "overclocked" not in u["t"] and "reinforced" not in u["t"]],
                    3200)             # 轨A：经济建筑绝对优先（T3加强版是终态，其动作是取消，排除防来回烧钱）
        try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                     and "actory" in u["t"].lower()], 4500)  # 轨B：工厂并行
        try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                     and "turret" in u["t"].lower()], 1500)  # 轨C：炮塔（最便宜升级700起）

        # 7) 集结点（army_goal 指定时，每厂设一次；敌占区自动后撤到主基地）
        if self.army_goal["rally"]:
            rx, ry = self.army_goal["rally"]
            if danger(rx, ry, 500) and cc:
                self.army_goal["rally"] = (float(cc["x"]), float(cc["y"]))
                self.rallied.clear()
                rx, ry = self.army_goal["rally"]
                self.log("status", "⚙ 集结点已沦陷，自动后撤到主基地")
            # 集结点落在敌人 500px 内 = 让部队排队送死，本拍跳过并告知
            if any((e["x"] - rx) ** 2 + (e["y"] - ry) ** 2 < 500 ** 2 for e in snap.get("enemy", [])):
                self.log("status", "⚙ 集结点距敌人过近已跳过（LLM 选点不慎，需重设）")
            else:
                for fac in fac_producers:
                    if fac["id"] not in self.rallied:
                        if emit({"cmd": "rally", "factory": fac["id"], "x": rx, "y": ry},
                                f"rally:{fac['id']}", 5.0):
                            self.rallied.add(fac["id"])

        # 8) LLM 沉默时的默认军事行为（默认关闭，config auto_military=true 才启用）
        army = [u for u in own if not u.get("bld") and not _is_builder(u)]
        if (self.auto_military and len(army) >= 6
                and now - max(self.last_military_order, self.start_ts) > 90
                and now >= self.auto_military_cd and self.stance != "defend"):
            self.auto_military_cd = now + 60
            enemies = snap.get("enemy", [])
            if enemies and army:
                # 打最近可见敌人
                cx = sum(u["x"] for u in army) / len(army)
                cy = sum(u["y"] for u in army) / len(army)
                tgt = min(enemies, key=lambda e: (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2)
                emit({"cmd": "attackmove", "x": float(tgt["x"]), "y": float(tgt["y"])}, "auto:atk", 5.0)
                self.log("status", "⚙ LLM沉默，自动进攻最近可见敌人")
            else:
                # 无敌情：向地图中心推进侦察
                mw = (self.map.get("tilesW", 0) or 0) * (self.map.get("tileW", 0) or 0)
                mh = (self.map.get("tilesH", 0) or 0) * (self.map.get("tileH", 0) or 0)
                if mw and mh:
                    emit({"cmd": "attackmove", "x": mw / 2, "y": mh / 2}, "auto:scout", 5.0)
                    self.log("status", "⚙ LLM沉默，自动向地图中心推进侦察")

        return cmds

    def _q(self, fac, now):
        """工厂队列长度：q 字段缺失时（多标签工厂反射失败）用本地近 45s 发车数估计。"""
        q = fac.get("q")
        if q is not None:
            return q
        hist = self.train_hist.setdefault(fac["id"], deque())
        while hist and now - hist[0] > 45:
            hist.popleft()
        return len(hist)

    def _pick_builder(self, idle_builders, for_factory=False):
        """盖工厂优先派战斗工程师（直接建 T2 厂省升级时间），否则第一个空闲建造者。"""
        if for_factory and idle_builders:
            ce = next((u for u in idle_builders if u["t"] == "combatEngineer"), None)
            if ce:
                return ce
        return idle_builders[0] if idle_builders else None

    def _dispatch_rescue(self, own, emit, stalled, idle_builders, danger, now, cc):
        """抢修派工：烂尾(>60s 无进展)与受损建筑(hp<85%)，派最近的空闲建造者 resume——
        repair 路点对未完工=续建、对已完工受损=修理（游戏右键同款路径）。
        敌区不救（别送建造者），同一建筑 45s 内不重复派；**在途 90s 内不换人**
        （沙盒 4 座塔 12 建造者循环抢占的根治：抢占者把工单 buildq 压进队尾永不开工）；
        陆军不可达且无悬浮/三派未建成 → 判死 10min。"""
        rescue = [(0, u) for u in stalled]
        rescue += [(1, u) for u in own
                   if u.get("bld") and not _constructing(u)
                   and u["hp"] < u.get("maxHp", u["hp"]) * 0.85]
        live_units = {u["id"] for u in own}
        for kind, u in rescue:
            if not idle_builders:
                break
            if now < self.resume_cd.get(u["id"], 0):
                continue
            if now < self.resume_bad.get(u["id"], 0):
                continue
            if danger(u["x"], u["y"], 500):
                continue
            prev = self.resume_by.get(u["id"])
            if prev and prev[0] in live_units and now - prev[1] < 90:
                continue  # 已有人在途（90s 宽限，走不到再换）
            b = self._mobile_builder(idle_builders, u["x"], u["y"], cc)
            if b is None:
                continue
            if not self._reachable(cc, u["x"], u["y"]) \
                    and b["t"] != "combatEngineer" and "hover" not in b["t"].lower():
                self.resume_bad[u["id"]] = now + 600
                self.log("status", f"⚙ 抢修判死10min：{u['t']}#{u['id']} 陆军不可达且无悬浮建造者")
                continue
            if prev and prev[0] in live_units:
                self.resume_strike[u["id"]] = self.resume_strike.get(u["id"], 0) + 1
                if self.resume_strike[u["id"]] >= 3:
                    self.resume_bad[u["id"]] = now + 600
                    self.log("status", f"⚙ 抢修判死10min：{u['t']}#{u['id']} 三派未建成")
                    continue
            if emit({"cmd": "resume", "builder": b["id"], "target": u["id"]},
                    f"resume:{u['id']}", 15.0):
                self.resume_cd[u["id"]] = now + 45.0
                self.resume_by[u["id"]] = (b["id"], now)
                what = "烂尾续建" if kind == 0 else "修理受损"
                self.log("status", f"⚙ {what}：{u['t']}#{u['id']} ← 建造者#{b['id']}")
                idle_builders = [x for x in idle_builders if x["id"] != b["id"]]

    def _routine_tick(self, snap, emit, now, own, funds, builders, idle_builders,
                      ext_done, factories, safe_pools, cc, mw, mh, danger):
        """规则快层（多 agent 模式）：LLM 管决策，例行公事机器节拍执行。
        只做参数内的例行——不盖新厂型、不改方向（那是 chat 建造层/计划书的事）。"""
        # 建造工单调度（BuilderManager）：规划层给的队列优先执行
        occ = self._occupancy(snap)
        self._wo_tick(snap, emit, now, own, funds, idle_builders, safe_pools, cc, occ, danger)

        # ① 建造者维持（<目标且有钱就补，CC 队列不满时；价格从 pedia 取，不写死）
        if cc and len(builders) < self.econ_goal["builders"] and (cc.get("q") or 0) < QUEUE_MAX:
            if funds >= self.price.get("u_builder", 250):
                emit({"cmd": "train", "action": "u_builder", "factory": cc["id"]}, "train:cc")
        # ①.5 战斗工程师（HOVER 过水过岩浆）：只从实验系厂产（tech2——landFactory 可用表
        #     没有 u_combatEngineer，实测 T1 陆厂刷失败；builderShip 才是水图前期答案）
        if cc:
            ce_count = sum(1 for u in own if u["t"] == "combatEngineer")
            ce_target = 3 if self.is_naval_map() else 2
            ce_facs = [u for u in factories if "experimental" in u["t"].lower()]
            if (ce_facs and ce_count < ce_target and funds >= 3500):
                lf = next((u for u in ce_facs if self._q(u, now) < QUEUE_MAX), None)
                if lf and not self._bad("u_combatEngineer", now):
                    emit({"cmd": "train", "action": "u_combatEngineer", "factory": lf["id"]},
                         "train:ce", 10.0)
        # ② 采集器补池（工单里有采集任务时避让；被 agent 判死的池 120s 跳过；在途池不重派）
        ext_inflight = sum(1 for t, _ in self.issued_builds if t.startswith("extractor"))
        inflight_xy = [(x, y) for x, y, _e in self.issued_cells.get("extractorT1", [])]
        pools = [p for p in safe_pools
                 if now >= self.ext_pool_bad.get((int(p["x"] // 400), int(p["y"] // 400)), 0)
                 and not any(abs(p["x"] - x) < 40 and abs(p["y"] - y) < 40 for x, y in inflight_xy)
                 and (self.control.secured(p["x"], p["y"], now)
                      or (cc and (p["x"] - cc["x"]) ** 2 + (p["y"] - cc["y"]) ** 2 < 600 ** 2))]
        if not pools and cc and any(u["t"] == "combatEngineer" or "hover" in u["t"].lower()
                                    for u in idle_builders):
            # 陆建够不着的池开悬浮专场（HOVER 过水过岩浆）：无威胁+未判死+未被占即可——
            # Volcano 26 池 24 个陆建够不着，不开这条路水图经济直接断气
            pools = sorted(
                (p for p in self.resources
                 if not p.get("w")
                 and not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                             for u in own + snap.get("allies", []) if u.get("bld"))
                 and now >= self.ext_pool_bad.get((int(p["x"] // 400), int(p["y"] // 400)), 0)
                 and not danger(p["x"], p["y"])),
                key=lambda p: (p["x"] - cc["x"]) ** 2 + (p["y"] - cc["y"]) ** 2)
        if (len(ext_done) < self.econ_goal["extractors"] and ext_inflight < 2
                and not self._wo_pending("b_extractor")
                and funds >= EXTRACTOR_PRICE and idle_builders and pools):
            px, py = float(pools[0]["x"]), float(pools[0]["y"])
            b = self._mobile_builder(idle_builders, px, py, cc)
            if b and emit({"cmd": "build", "action": "b_extractorT1", "builder": b["id"],
                           "x": px, "y": py}, f"build:ext:{b['id']}", 4.0):
                self._last_ext_xy = (px, py, now)
            if b:
                idle_builders = [u for u in idle_builders if u["id"] != b["id"]]
        # ③ 防线工程（网格版：格距=建筑宽+3格，主通道留空；防空压后排对空侧不占弧尖）
        land_factories = [u for u in factories if u["t"] != "commandCenter"]
        turrets = [u for u in own if "turret" in u["t"].lower() and u.get("bld")]
        t_inflight = sum(1 for t, _ in self.issued_builds if "turret" in t.lower())
        enemy_air = any(k in e["t"].lower() for e in snap.get("enemy", [])
                        for k in ("gunship", "jet", "bomber", "interceptor", "airship", "helicopter", "drone"))
        n = len(turrets) + t_inflight
        # 混编（方向敏感）：防空统一压主线后 3 格；每3座炮塔配1防空（敌空军每2配1）；每4座配1激光
        if enemy_air and n % 2 == 1:
            tur_action, need, back = "b_c_antiAirTurret", 1600, 3
        elif n % 3 == 2:
            tur_action, need, back = "b_c_antiAirTurret", 1600, 3
        elif n % 4 == 3:
            tur_action, need, back = "b_laserDefence", 2200, 0
        else:
            tur_action, need, back = "b_c_turret_t1", 1600, 0
        t_cap = min(10, 2 + int(funds // 10000))  # 防线是大工程：2 座起步，资金每万 +1，上限 10
        if (self.routine_defense and land_factories and funds >= need and n < t_cap
                and idle_builders and not self._bad(tur_action, now) and cc and mw and mh):
            anchor = self.army_goal.get("rally")
            tx, ty = anchor if anchor else (mw / 2, mh / 2)
            ax, ay = tx - cc["x"], ty - cc["y"]
            alen = max(1.0, math.hypot(ax, ay))
            occ = self._occupancy(snap)
            btype = tur_action[2:]
            idx = n
            spot = self.defense_slot(cc, occ, btype, idx, (ax / alen, ay / alen), back)
            if spot == "LANE":  # 主通道留空，跳一号
                idx += 1
                spot = self.defense_slot(cc, occ, btype, idx, (ax / alen, ay / alen), back)
            if spot and spot != "LANE" and (
                    not danger(spot[0], spot[1], 500)
                    or (cc and (spot[0] - cc["x"]) ** 2 + (spot[1] - cc["y"]) ** 2 <= 1200 ** 2)):
                # 防线圈(≤1200)内挨打照盖炮塔——防线挨打正是该盖的时候（危险只拦远征）
                b = self._mobile_builder(idle_builders, spot[0], spot[1], cc)
                if b and emit({"cmd": "build", "action": tur_action, "builder": b["id"],
                               "x": float(spot[0]), "y": float(spot[1])}, f"build:tur:{b['id']}", 6.0):
                    idle_builders = [u for u in idle_builders if u["id"] != b["id"]]
        # 内圈维修湾（网格版：主线后 6 格）：≥2 座炮塔后配，cap 2
        bays = sum(1 for u in own if u["t"] == "repairbay" and u.get("bld"))
        bay_inflight = sum(1 for t, _ in self.issued_builds if t == "repairbay")
        if (self.routine_defense and land_factories and n >= 2 and bays + bay_inflight < 2
                and funds >= 2600 and idle_builders and not self._bad("b_repairbay", now) and cc and mw and mh):
            anchor = self.army_goal.get("rally")
            tx, ty = anchor if anchor else (mw / 2, mh / 2)
            ax, ay = tx - cc["x"], ty - cc["y"]
            alen = max(1.0, math.hypot(ax, ay))
            occ = self._occupancy(snap)
            spot = self.defense_slot(cc, occ, "repairbay", bays, (ax / alen, ay / alen), 6)
            if spot and spot != "LANE" and not danger(spot[0], spot[1], 500):
                b = self._mobile_builder(idle_builders, spot[0], spot[1], cc)
                if b and emit({"cmd": "build", "action": "b_repairbay", "builder": b["id"],
                               "x": float(spot[0]), "y": float(spot[1])}, f"build:bay:{b['id']}", 8.0):
                    idle_builders = [x for x in idle_builders if x["id"] != b["id"]]

        # ③.5 池点哨戒：家圈(>600px)外的采集器各配 1 座炮塔护矿（远池不再裸奔）
        if self.routine_defense and cc and idle_builders and funds >= 1600:
            for ext in [u for u in ext_done
                        if (u["x"] - cc["x"]) ** 2 + (u["y"] - cc["y"]) ** 2 > 600 ** 2]:
                if not self.control.secured(ext["x"], ext["y"], now):
                    continue  # 不安稳的池不配哨戒（军队先站稳）
                guarded = any("turret" in t["t"].lower() and t.get("bld")
                              and abs(t["x"] - ext["x"]) < 300 and abs(t["y"] - ext["y"]) < 300
                              for t in own)
                inflight = any(abs(x - ext["x"]) < 300 and abs(y - ext["y"]) < 300
                               for x, y, _e in self.issued_cells.get("c_turret_t1", []))
                if guarded or inflight:
                    continue
                dx, dy = cc["x"] - ext["x"], cc["y"] - ext["y"]
                dd = max(1.0, math.hypot(dx, dy))
                gx, gy = ext["x"] + dx / dd * 80, ext["y"] + dy / dd * 80
                b = self._mobile_builder(idle_builders, gx, gy, cc)
                if b and emit({"cmd": "build", "action": "b_c_turret_t1", "builder": b["id"],
                               "x": float(gx), "y": float(gy)},
                              f"build:pg:{ext['id']}", 30.0):
                    idle_builders = [u for u in idle_builders if u["id"] != b["id"]]
                if not idle_builders:
                    break

        # ③.7 海军链路（水图限定，autopilot 同款移植——此前整段死在 autopilot 里，
        #      多 agent 模式从不见天日，"海军压根没碰过"的病根）：海厂落地→产 builderShip→水池采集
        if self.is_naval_map():
            if now >= self.cool.get("naval:dbg", 0):  # TODO 临时诊断：海厂不发的逐项原因，查明后删
                self.cool["naval:dbg"] = now + 30
                self.log("status", f"⚙ 海军诊断: ext_done={len(ext_done)} idle={len(idle_builders)} "
                                   f"funds={int(funds)} bad={self._bad('b_seaFactory', now)} "
                                   f"wo={self._wo_pending('b_seaFactory')} "
                                   f"wa={self.water_anchor(cc) if cc else None}")
            sea_count = sum(1 for u in factories if u["t"] == "seaFactory" and not _constructing(u))
            sea_inflight = sum(1 for t, _ in self.issued_builds if t == "seaFactory")
            if (sea_count + sea_inflight < 1 and funds >= 1500 and idle_builders
                    and not self._bad("b_seaFactory", now)
                    and not self._wo_pending("b_seaFactory")):  # 水图海厂不等陆池（海岛没陆池可等）
                wa = cc and self.water_anchor(cc)
                if wa:
                    ax, ay, shore = wa
                    if shore:
                        b = idle_builders[0]
                    else:  # 悬浮专场锚点：必须 CE 去盖（陆建 85px 够不着=送死循环）
                        b = next((u for u in idle_builders
                                  if u["t"] == "combatEngineer" or "hover" in u["t"].lower()), None)
                    if b and emit({"cmd": "build", "action": "b_seaFactory", "builder": b["id"],
                                   "x": float(ax), "y": float(ay)},
                                  f"build:sea:{b['id']}", 8.0):
                        idle_builders = [u for u in idle_builders if u["id"] != b["id"]]
            friendly = own + snap.get("allies", [])
            free_water = [p for p in self.resources if p.get("w") and not danger(p["x"], p["y"])
                          and not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                                      for u in friendly if u.get("bld"))]
            bships = [u for u in own if u["t"] == "builderShip"]
            sea_done = [u for u in factories if u["t"] == "seaFactory" and not _constructing(u)]
            if free_water and sea_done and len(bships) < 2 and funds >= 600 \
                    and not self._bad("u_builderShip", now):
                sf = next((u for u in sea_done if self._q(u, now) < QUEUE_MAX), None)
                if sf:
                    emit({"cmd": "train", "action": "u_builderShip", "factory": sf["id"]},
                         "train:bship", 15.0)
            idle_bs = [u for u in bships if not _busy(u)]
            if idle_bs and free_water and funds >= EXTRACTOR_PRICE and now >= self._wext_issued:
                nb = idle_bs[0]
                p = min(free_water, key=lambda q: (q["x"] - nb["x"]) ** 2 + (q["y"] - nb["y"]) ** 2)
                if emit({"cmd": "build", "action": "b_extractorT1", "builder": nb["id"],
                         "x": float(p["x"]), "y": float(p["y"])},
                        f"build:wext:{nb['id']}", 8.0):
                    self._wext_issued = now + 45.0

        # ④ 工厂：首座陆厂 + 按资金扩到 3 座（工单里有工厂任务时避让）
        facs = [u for u in factories if u["t"] != "commandCenter"]
        fac_inflight = sum(1 for t, _ in self.issued_builds if "actory" in t)
        fac_want = min(3, 1 + int(funds // 4000))
        if (len(ext_done) >= 2 and len(facs) + fac_inflight < fac_want
                and not any(self._wo_pending(p) for p in ("b_landFactory", "b_airFactory", "b_mechFactory"))
                and funds >= 700 and idle_builders and cc and not self._bad("b_landFactory", now)):
            occ4 = self._occupancy(snap)
            spot = None
            for bump in range(4):  # 槽位不可达就换下一号（看得见走不到=无限撞墙）
                cand = self.factory_slot(cc, len(facs) + fac_inflight + bump,
                                         [u for u in facs if u["t"] == "landFactory"], occ=occ4)
                if cand and self._reachable(cc, cand[0], cand[1]):
                    spot = cand
                    break
            if spot:
                b = self._mobile_builder(idle_builders, spot[0], spot[1], cc)
                if b and emit({"cmd": "build", "action": "b_landFactory", "builder": b["id"],
                               "x": float(spot[0]), "y": float(spot[1])}, f"build:fac:{b['id']}", 6.0):
                    idle_builders = [u for u in idle_builders if u["id"] != b["id"]]
            elif now >= self.cool.get("fac:unreach", 0):
                self.cool["fac:unreach"] = now + 60
                self.log("status", "⚙ 厂区扩建暂缓：候选槽位陆军不可达（水/隔断），等新方向（防原地撞墙）")

        # ⑤ 升级：采集器(≥3200) > 工厂(≥4500)（终态/回退动作已排除，30s 建筑级冷却）
        def _try_upgrade(units, need):
            if funds < need:
                return
            for u in units:
                if u["id"] in self.fac_upgraded:
                    continue  # 已 T2（可用列表含 T2 兵学来的），不再探升级
                if self._upgrade_pending(u["id"], now):
                    continue  # 升级在途 60s 内不重发（自打断疑云：上一发没落地又来一发）
                up = self.upgrade_action.get(u["t"])
                if not up or self._bad(up, now) or self._fac_bad(u["id"], up, now):
                    continue
                if (u["id"], up) in self.upgrade_bad and now < self.upgrade_bad[(u["id"], up)]:
                    continue
                if now < self.cool.get(f"up:{u['id']}", 0):
                    continue
                if emit({"cmd": "train", "action": up, "factory": u["id"]}, f"upemit:{u['id']}"):
                    self.cool[f"up:{u['id']}"] = now + UPGRADE_COOLDOWN
                    self.last_upgrade = (u["id"], up, now)
                    self.up_pending[u["id"]] = now  # ⑦ 停产让路窗口（队列放空，升级单才落得进去）
                    tries = self.up_tries.get(u["id"], 0) + 1
                    self.up_tries[u["id"]] = tries
                    if tries >= 3:
                        # 回执 ok 但始终不升 T2——判死 2 分钟（用户定性：10 分钟太长，钱够就该早放出来）
                        self.upgrade_bad[(u["id"], up)] = now + 120
                        self.up_tries[u["id"]] = 0
                        self.log("note", f"⚙ 厂#{u['id']} 升级 {up} 连续空转 3 次未落地，"
                                         "判死 2 分钟（疑似资金不足/机制不吃）")
                return

        _try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                      and (u["t"].startswith("extractor") or u["t"].startswith("fabricator"))
                      and "overclocked" not in u["t"] and "reinforced" not in u["t"]], 3200)
        _try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                      and "actory" in u["t"].lower()], 4500)

        # ⑥ 前线哨戒：集结点离家 >900 时在其旁起炮塔（前线占领区要有防御，不是乱逛）
        rally = self.army_goal.get("rally")
        if (self.routine_defense and rally and cc and mw and mh and land_factories and funds >= 1600
                and idle_builders and not self._bad("b_c_turret_t1", now)):
            d_home = math.hypot(rally[0] - cc["x"], rally[1] - cc["y"])
            fwd = [u for u in own if "turret" in u["t"].lower() and u.get("bld")
                   and math.hypot(u["x"] - rally[0], u["y"] - rally[1]) < 400]
            fwd_inf = sum(1 for x, y, _e in self.issued_cells.get("c_turret_t1", [])
                          if math.hypot(x - rally[0], y - rally[1]) < 400)
            if d_home > 900 and len(fwd) + fwd_inf < 2 and not danger(rally[0], rally[1], 400):
                ang = (len(fwd) + fwd_inf) * 2.1
                fx2, fy2 = float(rally[0] + math.cos(ang) * 140), float(rally[1] + math.sin(ang) * 140)
                b = self._mobile_builder(idle_builders, fx2, fy2, cc)
                if b and emit({"cmd": "build", "action": "b_c_turret_t1", "builder": b["id"],
                               "x": fx2, "y": fy2},
                              f"build:fwd:{b['id']}", 10.0):
                    idle_builders = [u for u in idle_builders if u["id"] != b["id"]]

        # ⑦ 军工生产：构成由战斗域/计划书定（set_composition），队列补水归规则层——
        #    稳态期零 LLM 也持续产兵（科技过滤/防空占比/队列上限都在 _next_production 内）。
        #    战斗 LLM 的 produce 条目=定向猛造例外通道，补水经 produce_focus 让路防撞车。
        # 预算仲裁（治"工厂一直下单、没钱升级/建造"）：军事只花盈余，储备线见 _military_reserve；
        # ⑤ 在 ⑦ 前拍，钱到线先拿升级，军建不再互抢。
        reserve = self._military_reserve(own)
        for fac in ([u for u in factories if u["t"] != "commandCenter"] if self.auto_production else []):
            if self._q(fac, now) >= QUEUE_MAX:
                continue
            if funds < reserve:
                continue  # 盈余线以下：攒钱给工单/升级（军事只花盈余）
            if self._upgrade_pending(fac["id"], now):
                continue  # 升级窗口：停产让路，队列放空让 110 落得进去（110 刷屏的根治）
            act = self._next_production(fac, own)
            if act:
                price = self.price.get(act, 0)
                if price and funds < price:
                    continue  # 资金不够就憋着（此前无视价格每 2s 刷一次失败）
                emit({"cmd": "train", "action": act, "factory": fac["id"]}, f"train:{fac['id']}")

    # ---------- 布局槽位（全局长持方向，建筑成行成排而非螺旋散点） ----------
    def factory_slot(self, cc, idx, existing, occ=None, btype="landFactory"):
        """厂区槽位：0 号位在 CC 侧翼（垂直前线），后续沿同侧密排成行。
        有首厂就锚定首厂实际方位成行（比垂直前线忠实——垂直算出西南=进水带）；
        有占位图时走槽位网格（按 fp 格数贴死，整齐密集）；无则退回几何公式。"""
        fx, fy = self.front_dir or (0.0, 1.0)
        lx, ly = -fy, fx
        if existing:
            bx, by = existing[0]["x"] - cc["x"], existing[0]["y"] - cc["y"]
            bl = max(1.0, math.hypot(bx, by))
            lx, ly = bx / bl, by / bl
        if occ is not None:
            spot = self._row_slot_safe(occ, btype, cc, idx, lx, ly, 0)  # 厂区贴死+安全值择优
            if spot:
                return spot
        fx_, fy_ = cc["x"] + lx * (260 + 170 * idx), cc["y"] + ly * (260 + 170 * idx)
        return (fx_, fy_) if self._in_bounds(fx_, fy_) else None

    def misc_slot(self, cc, idx, occ=None, btype="fabricatorT1"):
        """后勤槽位：主基地背后（背对前线）一排，1 格缝。"""
        fx, fy = self.front_dir or (0.0, 1.0)
        if occ is not None:
            spot = self._row_slot_safe(occ, btype, cc, idx, -fx, -fy, 1)
            if spot:
                return spot
        fx_, fy_ = cc["x"] - fx * (240 + 170 * idx), cc["y"] - fy * (240 + 170 * idx)
        return (fx_, fy_) if self._in_bounds(fx_, fy_) else None

    def is_naval_map(self):
        """水图判定：水域占比≥20% 或 水池≥2。结果缓存（地图不变）。"""
        if self.naval is None:
            rows = self.map.get("rows") or []
            total = sum(len(r) for r in rows) or 1
            water = sum(r.count("~") for r in rows)
            self.naval = water / total >= 0.20 or self._water_pools() >= 2
        return self.naval

    def _water_pools(self):
        return sum(1 for p in self.resources if p.get("w"))

    def water_anchor(self, cc):
        """海厂锚点 (x, y, 岸边陆军可达?)：优先岸边 85px 内有陆军落脚点的水点（陆建可盖）；
        没有就给最近自由水点（悬浮建造者专场——CE 过水过岩浆，不需要岸边）。
        全图无合格水域返回 None（结果缓存）。"""
        if self._water_anchor == "?":
            rows = self.map.get("rows") or []
            tw = self.map.get("tileW") or 1
            th = self.map.get("tileH") or 1
            max_x = len(rows[0]) if rows else 0
            cands = []
            for ty, row in enumerate(rows):
                for tx, ch in enumerate(row):
                    if ch != "~":
                        continue
                    wx, wy = (tx + 0.5) * tw, (ty + 0.5) * th
                    cands.append(((wx - cc["x"]) ** 2 + (wy - cc["y"]) ** 2, tx, ty, wx, wy))
            cands.sort()
            best = None
            now = time.time()
            for _d, tx, ty, wx, wy in cands[:800]:  # 第一优先：岸边陆军可达
                if self.site_bad.get((tx, ty), 0) > now:
                    continue
                if not self._cell_free(set(), "seaFactory", tx, ty):
                    continue
                shore_ok = False
                for dx in range(-4, 5):
                    for dy in range(-4, 5):
                        sx, sy = tx + dx, ty + dy
                        if not (0 <= sx < max_x and 0 <= sy < len(rows)):
                            continue
                        if rows[sy][sx] not in (".", "="):
                            continue
                        if (dx * tw) ** 2 + (dy * th) ** 2 > 85 ** 2:
                            continue
                        if self._reachable(cc, (sx + 0.5) * tw, (sy + 0.5) * th):
                            shore_ok = True
                            break
                    if shore_ok:
                        break
                if shore_ok:
                    best = (wx, wy, True)
                    break
            if best is None:  # 第二优先：悬浮专场（无岸边点，CE 直接坐水里盖）
                for _d, tx, ty, wx, wy in cands[:800]:
                    if self.site_bad.get((tx, ty), 0) > now:
                        continue
                    if not self._cell_free(set(), "seaFactory", tx, ty):
                        continue
                    best = (wx, wy, False)
                    break
            self._water_anchor = best
            if best is None:
                self.log("note", "⚙ 海厂锚点无：全图无合格水域（或全判死）")
        return self._water_anchor

    # ---------- 槽位网格（以主基地锚定晶格，建筑按 fp 格数分配空间，整齐是规则不是运气） ----------
    def _in_bounds(self, x, y):
        """世界坐标图内判定（几何兜底槽位可能算出图外/负坐标，b_c_turret@(-70,120) 实测）。"""
        tw, th = self._tw()
        return 0 <= x < (self.map.get("tilesW", 0) or 0) * tw \
            and 0 <= y < (self.map.get("tilesH", 0) or 0) * th

    def _tw(self):
        return (self.map.get("tileW") or 20), (self.map.get("tileH") or 20)

    def _footprint(self, name):
        return self.fp.get(name, (2, 2, -1, -1))  # 未知默认 2x2 居中（多数建筑如此）

    def _mark_fp(self, occ, name, x, y):
        tw, th = self._tw()
        w, h, ox, oy = self._footprint(name)
        gx, gy = int(x // tw), int(y // th)
        for i in range(ox, ox + w):
            for j in range(oy, oy + h):
                occ.add((gx + i, gy + j))

    def _occupancy(self, snap, exclude_idx=None):
        """占位图：己方/盟友建筑（按 fp）+ 在途下单（45s 窗口）+ 工单待派/在途坐标
        （exclude_idx=派发自查时豁免本任务——否则阵地展开总被当成"撞占位"挪散）。"""
        occ = set()
        for u in snap.get("own", []) + snap.get("allies", []):
            if u.get("bld"):
                self._mark_fp(occ, u["t"], u["x"], u["y"])
        now = time.time()
        for t, cells in self.issued_cells.items():
            live = [(x, y, e) for x, y, e in cells if e > now]
            self.issued_cells[t] = live
            for x, y, _e in live:
                self._mark_fp(occ, t, x, y)
        # 工单坐标统一进图（阵地展开与 defense:k 撞点的根治：两套槽位共享一张图）
        for t in self.work_order:
            if t["idx"] == exclude_idx or t["status"] not in ("pending", "assigned"):
                continue
            xy = (t.get("x"), t.get("y")) if t["status"] == "assigned" and t.get("x") is not None else None
            if xy is None:
                slot = t.get("slot")
                xy = (slot[0], slot[1]) if isinstance(slot, (list, tuple)) else None
            if xy:
                self._mark_fp(occ, t["action"][2:], xy[0], xy[1])
        # 选址判死格（水域/隔断/校验不过签名，600s）按占位处理，槽位自然避让
        for (gx, gy), exp in list(self.site_bad.items()):
            if exp <= now:
                del self.site_bad[(gx, gy)]
            else:
                for i in range(-1, 2):
                    for j in range(-1, 2):
                        occ.add((gx + i, gy + j))
        return occ

    def _cell_free(self, occ, name, gx, gy):
        w, h, ox, oy = self._footprint(name)
        rows = self.map.get("rows") or []
        for i in range(ox, ox + w):
            for j in range(oy, oy + h):
                if (gx + i, gy + j) in occ:
                    return False
                # 地形闸：水/岩浆/高崖/虚空不可建（槽位此前只查占位不查地形，工厂撞水反复失败的根治）；
                # 海厂/水池采集器反过来要水
                if not (0 <= gy + j < len(rows) and 0 <= gx + i < len(rows[gy + j])):
                    return False
                ch = rows[gy + j][gx + i]
                if ch in (".", "="):
                    continue
                if ch == "~" and (name.startswith("sea") or name.startswith("extractor")):
                    continue
                return False
        return True

    def _cell_xy(self, gx, gy):
        tw, th = self._tw()
        return (gx + 0.5) * tw, (gy + 0.5) * th

    def _row_slot(self, occ, btype, cc, idx, ox_dir, oy_dir, gap_cells):
        """沿 (ox_dir,oy_dir) 方向按 fp宽+gap 格距密排的第 idx 个空位（厂区/特区用）。"""
        tw, th = self._tw()
        w, h, ox, oy = self._footprint(btype)
        pitch = w + gap_cells
        ccx, ccy = int(cc["x"] // tw), int(cc["y"] // th)
        step = 0
        for n in range(idx * 2 + 4):  # 顺排找空，被占顺延
            if n * pitch > 60:
                break
            gx = int(round(ccx + ox_dir * (13 + n * pitch)))
            gy = int(round(ccy + oy_dir * (13 + n * pitch)))
            if self._cell_free(occ, btype, gx, gy):
                if step == idx:
                    return self._cell_xy(gx, gy)
                step += 1
        return None

    def _row_slot_safe(self, occ, btype, cc, idx, ox_dir, oy_dir, gap_cells,
                       floor=SAFE_PICK_FLOOR):
        """_row_slot 的安全择优版：跳过 idx 个空位（保序）后，沿排走到第一个
        safety ≥ floor 的槽位（工厂/后勤偏向安全区——近排被威胁覆盖时自动往
        安全侧走远，而不是原地硬盖或整单失败）；一路全热则退回已见最安全候选
        （不空手，由派发闸的安全值地板终判）。无控制图数据时全 0 = 顺排。"""
        tw, th = self._tw()
        w, h, ox, oy = self._footprint(btype)
        pitch = w + gap_cells
        ccx, ccy = int(cc["x"] // tw), int(cc["y"] // th)
        seen = 0
        best = best_s = None
        for n in range(idx * 2 + 40):  # 顺排找空，被占/危险顺延（~1600px 内）
            if n * pitch > 80:
                break
            gx = int(round(ccx + ox_dir * (13 + n * pitch)))
            gy = int(round(ccy + oy_dir * (13 + n * pitch)))
            if not self._cell_free(occ, btype, gx, gy):
                continue
            if seen < idx:
                seen += 1
                continue
            spot = self._cell_xy(gx, gy)
            s = self.control.safety(spot[0], spot[1])
            if s >= floor:
                return spot
            if best is None or s > best_s:
                best, best_s = spot, s
        return best

    def _reachable(self, cc, x, y):
        """陆军可达性：点与主基地同陆地连通域（connmap 洪水填充，地图不变结果缓存）。
        槽位只查占位+地形仍会选中"看得见走不到"的点（工厂撞水带南侧反复失败的根治）。"""
        if cc is None:
            return True
        if self._comp is None:
            rows = self.map.get("rows") or []
            self._comp = connmap.build_components(rows)[0] if rows else []
        if not self._comp:
            return True
        tw, th = self._tw()
        home_c = connmap.comp_at(self._comp, cc["x"], cc["y"], tw, th)
        if home_c < 0:
            return True
        return connmap.comp_at(self._comp, x, y, tw, th) == home_c

    def _in_lane(self, cc, x, y, half=200.0):
        """点是否压在主通道上（基地→前线方向 ±half px 走廊、前方 80~1400px 段）。
        建筑堵通道=部队绕路/集结卡死，落点统一过此闸。"""
        if not cc or not self.front_dir:
            return False
        fx, fy = self.front_dir
        dx, dy = x - cc["x"], y - cc["y"]
        fwd = dx * fx + dy * fy
        if fwd < 80 or fwd > 1400:
            return False
        return abs(-dx * fy + dy * fx) < half

    def defense_slot(self, cc, occ, btype, index, axis, back=0):
        """防线弧第 index 个空位：格距=建筑宽+3格（3 格缝，火力密集且全线可穿）；
        back>0 放主线后 back 格（防空/火炮后排）；k=0 留空作主通道（≥2 格距宽）。"""
        tw, th = self._tw()
        w, h, ox, oy = self._footprint(btype)
        pitch = w + 3
        ccx, ccy = int(cc["x"] // tw), int(cc["y"] // th)
        base = math.atan2(axis[1], axis[0])
        k = (index + 1) // 2 * (1 if index % 2 else -1)  # 0,+1,-1,+2,-2... 两翼交替
        if back == 0 and k == 0:
            return "LANE"  # 主通道留空（调用方跳号）
        for R in range(16, 34):
            r = R - back
            if r < 8:
                break
            ang = base + k * (pitch / R)
            gx = int(round(ccx + math.cos(ang) * r))
            gy = int(round(ccy + math.sin(ang) * r))
            if self._cell_free(occ, btype, gx, gy):
                return self._cell_xy(gx, gy)
        return None

    # ---------- 生产决策 ----------
    def _military_reserve(self, own):
        """军事开支储备线（预算仲裁）：底线 + 工单未完成估价(封顶6000) + 升级目标
        （兵力构成七成后才攒：厂 T2 4500 优先、采集器 3200 其次）。
        低于此线只攒钱不产兵——军事只花盈余（"工厂一直下单、没钱升级/建造"的根治）。"""
        wo_due = 0
        for t in self.work_order:
            if t["status"] == "pending":
                bt = t["action"][2:]
                wo_due += self.price.get("u_" + bt) or self.price.get(bt) or 0
        comp = self.army_goal["composition"] or {}
        ready = True
        if comp:
            counts_ = Counter(u["t"] for u in own if not u.get("bld"))
            total_ = sum(comp.values()) or 1
            deficit_ = sum(max(0, tg - counts_.get(a[2:] if a.startswith("u_") else a, 0))
                           for a, tg in comp.items())
            ready = deficit_ / total_ <= 0.3
        up_due = 0
        if ready:
            now_ = time.time()

            def _upgradable(u):
                """升级当前可得（不在判死期）——判死=升级机制不吃，为它攒钱=白断粮。"""
                up = self.upgrade_action.get(u["t"])
                if not up:
                    return False
                return not ((u["id"], up) in self.upgrade_bad
                            and now_ < self.upgrade_bad[(u["id"], up)])

            if any(u.get("bld") and "actory" in u["t"].lower() and not _constructing(u)
                   and u["id"] not in self.fac_upgraded and _upgradable(u) for u in own):
                up_due = 4500
            elif any(u.get("bld") and not _constructing(u)
                     and (u["t"].startswith("extractor") or u["t"].startswith("fabricator"))
                     and "overclocked" not in u["t"] and "reinforced" not in u["t"]
                     and _upgradable(u) for u in own):
                up_due = 3200
        return FUNDS_RESERVE + min(wo_due, 6000) + up_due

    def _upgrade_pending(self, fid, now):
        """升级窗口判定：升级单已发未满 60s 且未学成 T2——⑦ 应对该厂停产让路。"""
        ts = self.up_pending.get(fid)
        if ts is None or fid in self.fac_upgraded or now - ts > 60:
            self.up_pending.pop(fid, None)
            return False
        return True

    def _next_production(self, fac, own):
        now = time.time()
        ftype = fac["t"]
        learned = self.fac_avail.get(fac["id"])
        # 该厂真实可用表（从失败回执学到，120s 内有效）优先于型号级并集——T1/T2 档按厂区分
        avail = (learned[0] if learned and now - learned[1] < 120 else None) \
            or self.factory_actions.get(ftype)
        # 有 T2+ 可产就不产 T1（按可产列表最高科技算，不看厂名——空军/改名厂同样生效）
        max_tech = max((self.action_tech.get(a, 1) for a in (avail or []) if not self._bad(a, now)),
                       default=1)

        def tech_ok(action):
            return max_tech <= 1 or self.action_tech.get(action, 1) >= 2

        # 构成目标模式：按缺口最大者优先；全部达标/被过滤时回落默认优先级（防工厂空转）
        comp = self.army_goal["composition"]
        if comp:
            counts = Counter()
            for u in own:
                if not u.get("bld"):
                    counts[u["t"]] += 1
            best, best_deficit = None, 0
            for action, target in comp.items():
                if self._bad(action, now):
                    continue
                if self._fac_bad(fac["id"], action, now):
                    continue
                if (self.produce_focus or {}).get(action, 0) > 0:
                    continue  # 战斗 LLM 正定向猛造该兵种，补水让路防撞车
                if avail is not None and action not in avail:
                    continue
                if not tech_ok(action):
                    continue
                unit_type = action[2:] if action.startswith("u_") else action
                deficit = target - counts.get(unit_type, 0)
                if deficit > best_deficit:
                    best, best_deficit = action, deficit
            if best:
                return best
        # 默认优先级模式（防空单位占比超 25% 就跳过，防空是配角）
        combat_units = [u for u in own if not u.get("bld")]
        aa_now = sum(1 for u in combat_units if u["t"] in AA_PROD_NAMES)
        aa_full = bool(combat_units) and aa_now >= max(1, len(combat_units) * AA_RATIO)
        for act in DEFAULT_PROD.get(ftype, []):
            if self._bad(act, now):
                continue
            if self._fac_bad(fac["id"], act, now):
                continue
            if avail is not None and act not in avail:
                continue
            if not tech_ok(act):
                continue
            if aa_full and act in AA_PROD:
                continue
            return act
        # 静态表没有时退回百科里第一个可用的（建造者/侦察兵不算生产，防兜底刷民夫）
        if avail:
            for act in sorted(avail):
                if act in ("u_builder", "u_scout"):
                    continue
                if not self._bad(act, now) and not self._fac_bad(fac["id"], act, now) and tech_ok(act):
                    return act
        return None

    # ---------- 状态行（仪表盘/摘要复用） ----------
    def status_line(self, snap):
        own = snap.get("own", [])
        builders = [u for u in own if _is_builder(u)]
        idle_b = sum(1 for u in builders if not _busy(u))
        ext = sum(1 for u in own if u["t"].startswith("extractor") and u.get("bld") and not _constructing(u))
        facs = sum(1 for u in own if _is_factory(u) and not _constructing(u))
        stalled_txt = f" 烂尾{getattr(self, 'last_stalled', 0)}座(续建已派)" if getattr(self, "last_stalled", 0) else ""
        naval_txt = " 水图" if self.naval else ""
        inc = (snap.get("team") or {}).get("income")
        inc_txt = f" 收{inc:+.0f}/s" if inc is not None else ""
        return (f"执行层: 池{ext}/{self.econ_goal['extractors']} 厂{facs} "
                f"建造者{len(builders)}(闲{idle_b}) 目标{self.stance}{inc_txt}{naval_txt}{stalled_txt}")
