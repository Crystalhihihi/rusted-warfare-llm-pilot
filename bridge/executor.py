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
import re
import threading
import time
from collections import Counter, deque

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
UPGRADE_FUNDS = 2600       # 超过此资金且采集器达标后开始升级
QUEUE_MAX = 2              # 每厂队列上限
MAX_CMDS_PER_TICK = 4      # 每拍指令上限（2 会被经济/升级占满，生产饿死）
ACTION_COOLDOWN = 2.0      # 同类指令最小间隔
UPGRADE_COOLDOWN = 30.0    # 同一建筑升级冷却


def _is_builder(u):
    return ("uilder" in u["t"] or "abricator" in u["t"]) and not u.get("bld")


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
            # 升级动作候选：排除 b_/u_/c_/s_ 前缀、垃圾 id（如 "_0"）、以及取消类动作（_1 结尾=回退，会扣钱）
            ups = [b for b in builds
                   if not b.startswith(("b_", "u_", "c_", "s_"))
                   and len(b) >= 3 and not b.startswith("_")
                   and not b.endswith("_1")]
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
        self.cool = {}                 # 冷却：键 -> 到期时间
        self.rallied = set()           # 已设过集结点的工厂 id
        self.issued_builds = []        # [(建筑类型, 到期时间)] 刚下单、实体未出现的窗口期，防重复下单
        self.issued_cells = {}         # 类型 -> [(x, y, 到期时间)] 在途建筑占位（槽位网格用）
        self.custom_builds = []        # LLM 指定建造队列 [(action, x, y)]
        # 建造工单（BuilderManager）：有序建造任务队列，规则层调度建造者连干
        # 任务 = {"action": str, "slot": spec, "status": pending/assigned/done/failed,
        #         "builder": id, "ts": float, "x": x, "y": y, "note": str}
        self.work_order = []
        self.wo_version = 0
        self.ext_pool_bad = {}         # (gx,gy) -> 解封时间：agent 判"找不到合法建造位置"的池（120s 跳过）
        self._last_ext_xy = None       # (x, y, ts) 最近一次采集器下单（池失败归因用）
        self._last_build_xy = None     # (x, y, ts) 最近一次带坐标建造下单（选址失败归因用）
        self.site_bad = {}             # (gx,gy) -> 解封时间：选址判死格（水域/隔断/校验不过，600s）
        self.prog_seen = {}            # 建筑id -> (prog, 时间戳)：识别烂尾（进度长期不涨）
        self.upgrade_bad = {}          # (建筑id, 动作) -> 解封时间：该建筑此升级失败后的冷却
        self.last_upgrade = None
        self.lock = threading.Lock()   # tick/目标设置/失败学习 共用（执行层独立线程）
        self.dmap = None               # DangerMap（pilot 注入）：敌人记忆/威胁热力
        self.on_action = None          # fn(cmd)：指令下发回调（multiagent 注入，推游戏内聊天台）
        self.train_hist = {}           # 工厂id -> deque(发车时间)：q 缺失（如 mechFactoryT2）时的本地队列估计
        self.last_military_order = 0.0  # 最近一次 LLM 军事指令时间（attack/harass/defend）
        self.auto_military_cd = 0.0
        self.auto_military = False      # 默认关闭：军事决策完全归 LLM（config 可开）
        self.autopilot = True           # False=多 agent 模式：只跑跟踪/抢修机械，经济生产决策归 LLM agent
        self.routine = False            # 多 agent 模式的规则快层：例行（建造者/采集器/炮塔弧）2s 级执行
        self.naval = None               # 水图判定（lazy）：水域占比>=20% 或 水池>=2
        self.front_dir = None           # 布局前线方向（CC→图心单位向量），首个 CC 出现时定
        self._water_anchor = "?"        # 近水锚点缓存（None=无水域；"?"=未计算）
        self.resume_cd = {}             # 烂尾建筑id -> 续建派工冷却时间
        self._wext_issued = 0.0         # 水池建造下单窗口（45s 内不再派第二单）
        self.start_ts = time.time()
        # 战力标签 + 领土控制图（确定性，快照驱动）
        self.power_table = powergrid.power_table(pedia)
        self.control = powergrid.ControlMap(self.power_table)
        self.control.set_threat_fn(
            lambda x, y, snap: (self.dmap.threat.get((int(x // 400), int(y // 400)), 0.0)
                                if self.dmap else 0.0))

    # ---------- LLM 目标接口 ----------
    def set_work_order(self, tasks):
        """下发建造工单（LLM 规划员或模板）：有序任务队列。
        tasks: [{"action": "b_x", "slot": spec, "note": str?}, ...]
        slot 规格（派发时现场解析，队列随局自适应）：
          "pool:k"=第 k 近安全池 | "factory:k"=厂区槽位k | "defense:k"=防线弧位k
          "misc:k"=后勤槽位k | "water"=近水点 | (x,y)=精确坐标
        状态机：pending -> assigned -> done/failed。规则层 2s 拍调度建造者连干。"""
        self.work_order = []
        for i, t in enumerate(tasks or []):
            act = str(t.get("action", ""))
            if not act.startswith(("b_", "u_", "c_")):
                act = "b_" + act
            self.work_order.append({"idx": i, "action": act, "slot": t.get("slot"),
                                  "note": str(t.get("note", "")), "status": "pending",
                                  "builder": None, "ts": 0.0, "x": None, "y": None})
        self.wo_version += 1
        self.log("status", f"⚙ 建造工单 v{self.wo_version} 已接收（{len(self.work_order)} 项）")

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
                return self.misc_slot(cc, idx, occ=occ, btype=btype)
            if slot == "water":
                return self.water_anchor(cc)
        return None

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
                    continue
                # 危险判定分家：防守类建筑在防线圈(≤1200px)内挨打照盖（防线挨打正是该盖的时候）；
                # 扩张类(采集器/远征)危险照样禁（治开局乱跑送死）
                defense_types = ("c_turret", "c_antiAirTurret", "laserDefence", "repairbay", "wall")
                if danger(spot[0], spot[1], 400) and not (
                        t["action"][2:].startswith(defense_types)
                        and cc and (spot[0] - cc["x"]) ** 2 + (spot[1] - cc["y"]) ** 2 <= 1200 ** 2):
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
            elif now - task["ts"] > 90:
                task["status"] = "pending"  # 超 90s 没起来（建造者阵亡等）→ 重派
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
            self.econ_goal["builders"] = max(1, min(6, int(builders)))

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

    # ---------- 失败回执学习 ----------
    def on_cmd_results(self, results):
        for r in results or []:
            if r.get("ok"):
                continue
            msg = r.get("msg") or ""
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
            # 选址失败签名（水域/隔断不可达、找不到合法位置）：归因最近带坐标建造，600s 判死；
            # 不可达的池直接永久判死（本局不会变得可达）
            if ("无法靠近" in msg or "找不到合法建造位置" in msg) and self._last_build_xy \
                    and time.time() - self._last_build_xy[2] < 10:
                gx, gy = int(self._last_build_xy[0] // 400), int(self._last_build_xy[1] // 400)
                self.site_bad[(gx, gy)] = time.time() + 600
                self.log("status", f"⚙ 选址判死10分钟: 格({gx},{gy})")
                if "无法靠近" in msg and self._last_ext_xy \
                        and self._last_ext_xy[0] == self._last_build_xy[0] \
                        and self._last_ext_xy[1] == self._last_build_xy[1]:
                    self.ext_pool_bad[(gx, gy)] = time.time() + 3600
                    self.log("status", f"⚙ 池点不可达永久判死 ({self._last_build_xy[0]:.0f},{self._last_build_xy[1]:.0f})")

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
             if not p.get("w") and (_cc is not None and (p["x"] - ccx) ** 2 + (p["y"] - ccy) ** 2 < 600 ** 2
                                    or not danger(p["x"], p["y"]))
             and not any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40
                         for u in own + snap.get("allies", []) if u.get("bld"))),
            key=lambda p: (p["x"] - ccx) ** 2 + (p["y"] - ccy) ** 2)
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
                self._dispatch_rescue(own, emit, stalled, idle_builders, danger, now)
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
                sx, sy = self.factory_slot(cc, 0, [], occ=self._occupancy(snap))
                fcmd["x"], fcmd["y"] = float(sx), float(sy)
            emit(fcmd, f"build:lf:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 3.5) 战斗工程师：无存货且要扩厂时产1-2个（直接建T2厂，省升级时间）
        ce_count = sum(1 for u in own if u["t"] == "combatEngineer")
        if (len(ext_done) >= 2 and ce_count < 2 and funds >= 1500 and land_factories):
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
                sx, sy = self.factory_slot(cc, idx, land_factories,
                                           occ=self._occupancy(snap), btype=fac_action[2:])
                fcmd["x"], fcmd["y"] = float(sx), float(sy)
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
                    sx, sy = self.misc_slot(cc, misc_n)
                    fbcmd["x"], fbcmd["y"] = float(sx), float(sy)
                if emit(fbcmd, f"build:fab:{b['id']}", 8.0):
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
                sx, sy = self.misc_slot(cc, misc_n)
                excmd["x"], excmd["y"] = float(sx), float(sy)
            if emit(excmd, f"build:exp:{b['id']}", 8.0):
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
            sea_done = [u for u in fac_producers if u["t"] == "seaFactory"]
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
                sx, sy = self.misc_slot(cc, misc_n)
                bcmd["x"], bcmd["y"] = float(sx), float(sy)
            if emit(bcmd, f"build:custom:{b['id']}", 6.0):
                self.custom_builds.pop(0)
                misc_n += 1
                idle_builders = idle_builders[1:]
            else:
                break

        # 4.8) 抢修：烂尾(>60s 无进展)与受损建筑(hp<85%)，派最近的空闲建造者 resume
        self._dispatch_rescue(own, emit, stalled, idle_builders, danger, now)

        # 5) 建筑升级双轨：采集器绝对优先（经济是根本），工厂并行不挤占；优先于常规补兵
        def try_upgrade(units, need):
            if funds < need:
                return
            for u in units:
                up = self.upgrade_action.get(u["t"])
                if not up or self._bad(up, now):
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

    def _dispatch_rescue(self, own, emit, stalled, idle_builders, danger, now):
        """抢修派工：烂尾(>60s 无进展)与受损建筑(hp<85%)，派最近的空闲建造者 resume——
        repair 路点对未完工=续建、对已完工受损=修理（游戏右键同款路径）。
        敌区不救（别送建造者），同一建筑 45s 内不重复派。"""
        rescue = [(0, u) for u in stalled]
        rescue += [(1, u) for u in own
                   if u.get("bld") and not _constructing(u)
                   and u["hp"] < u.get("maxHp", u["hp"]) * 0.85]
        for kind, u in rescue:
            if not idle_builders:
                break
            if now < self.resume_cd.get(u["id"], 0):
                continue
            if danger(u["x"], u["y"], 500):
                continue
            b = min(idle_builders, key=lambda q: (q["x"] - u["x"]) ** 2 + (q["y"] - u["y"]) ** 2)
            if emit({"cmd": "resume", "builder": b["id"], "target": u["id"]},
                    f"resume:{u['id']}", 15.0):
                self.resume_cd[u["id"]] = now + 45.0
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
        # ② 采集器补池（工单里有采集任务时避让；被 agent 判死的池 120s 跳过；在途池不重派）
        ext_inflight = sum(1 for t, _ in self.issued_builds if t.startswith("extractor"))
        inflight_xy = [(x, y) for x, y, _e in self.issued_cells.get("extractorT1", [])]
        pools = [p for p in safe_pools
                 if now >= self.ext_pool_bad.get((int(p["x"] // 400), int(p["y"] // 400)), 0)
                 and not any(abs(p["x"] - x) < 40 and abs(p["y"] - y) < 40 for x, y in inflight_xy)
                 and (self.control.secured(p["x"], p["y"], now)
                      or (cc and (p["x"] - cc["x"]) ** 2 + (p["y"] - cc["y"]) ** 2 < 600 ** 2))]
        if (len(ext_done) < self.econ_goal["extractors"] and ext_inflight < 2
                and not self._wo_pending("b_extractor")
                and funds >= EXTRACTOR_PRICE and idle_builders and pools):
            b = idle_builders[0]
            px, py = float(pools[0]["x"]), float(pools[0]["y"])
            if emit({"cmd": "build", "action": "b_extractorT1", "builder": b["id"],
                     "x": px, "y": py}, f"build:ext:{b['id']}", 4.0):
                self._last_ext_xy = (px, py, now)
            idle_builders = idle_builders[1:]
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
        if (land_factories and funds >= need and n < t_cap
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
                b = idle_builders[0]
                emit({"cmd": "build", "action": tur_action, "builder": b["id"],
                      "x": float(spot[0]), "y": float(spot[1])}, f"build:tur:{b['id']}", 6.0)
                idle_builders = idle_builders[1:]
        # 内圈维修湾（网格版：主线后 6 格）：≥2 座炮塔后配，cap 2
        bays = sum(1 for u in own if u["t"] == "repairbay" and u.get("bld"))
        bay_inflight = sum(1 for t, _ in self.issued_builds if t == "repairbay")
        if (land_factories and n >= 2 and bays + bay_inflight < 2 and funds >= 2600
                and idle_builders and not self._bad("b_repairbay", now) and cc and mw and mh):
            anchor = self.army_goal.get("rally")
            tx, ty = anchor if anchor else (mw / 2, mh / 2)
            ax, ay = tx - cc["x"], ty - cc["y"]
            alen = max(1.0, math.hypot(ax, ay))
            occ = self._occupancy(snap)
            spot = self.defense_slot(cc, occ, "repairbay", bays, (ax / alen, ay / alen), 6)
            if spot and spot != "LANE" and not danger(spot[0], spot[1], 500):
                b = idle_builders[0]
                emit({"cmd": "build", "action": "b_repairbay", "builder": b["id"],
                      "x": float(spot[0]), "y": float(spot[1])}, f"build:bay:{b['id']}", 8.0)
                idle_builders = [x for x in idle_builders if x["id"] != b["id"]]

        # ④ 工厂：首座陆厂 + 按资金扩到 3 座（工单里有工厂任务时避让）
        facs = [u for u in factories if u["t"] != "commandCenter"]
        fac_inflight = sum(1 for t, _ in self.issued_builds if "actory" in t)
        fac_want = min(3, 1 + int(funds // 4000))
        if (len(ext_done) >= 2 and len(facs) + fac_inflight < fac_want
                and not any(self._wo_pending(p) for p in ("b_landFactory", "b_airFactory", "b_mechFactory"))
                and funds >= 700 and idle_builders and cc and not self._bad("b_landFactory", now)):
            b = idle_builders[0]
            sx, sy = self.factory_slot(cc, len(facs) + fac_inflight,
                                       [u for u in facs if u["t"] == "landFactory"],
                                       occ=self._occupancy(snap))
            emit({"cmd": "build", "action": "b_landFactory", "builder": b["id"],
                  "x": float(sx), "y": float(sy)}, f"build:fac:{b['id']}", 6.0)
            idle_builders = idle_builders[1:]

        # ⑤ 升级：采集器(≥3200) > 工厂(≥4500)（终态/回退动作已排除，30s 建筑级冷却）
        def _try_upgrade(units, need):
            if funds < need:
                return
            for u in units:
                up = self.upgrade_action.get(u["t"])
                if not up or self._bad(up, now):
                    continue
                if (u["id"], up) in self.upgrade_bad and now < self.upgrade_bad[(u["id"], up)]:
                    continue
                if now < self.cool.get(f"up:{u['id']}", 0):
                    continue
                if emit({"cmd": "train", "action": up, "factory": u["id"]}, f"upemit:{u['id']}"):
                    self.cool[f"up:{u['id']}"] = now + UPGRADE_COOLDOWN
                    self.last_upgrade = (u["id"], up, now)
                return

        _try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                      and (u["t"].startswith("extractor") or u["t"].startswith("fabricator"))
                      and "overclocked" not in u["t"] and "reinforced" not in u["t"]], 3200)
        _try_upgrade([u for u in own if u.get("bld") and not _constructing(u)
                      and "actory" in u["t"].lower()], 4500)

        # ⑥ 前线哨戒：集结点离家 >900 时在其旁起炮塔（前线占领区要有防御，不是乱逛）
        rally = self.army_goal.get("rally")
        if (rally and cc and mw and mh and land_factories and funds >= 1600
                and idle_builders and not self._bad("b_c_turret_t1", now)):
            d_home = math.hypot(rally[0] - cc["x"], rally[1] - cc["y"])
            fwd = [u for u in own if "turret" in u["t"].lower() and u.get("bld")
                   and math.hypot(u["x"] - rally[0], u["y"] - rally[1]) < 400]
            fwd_inf = sum(1 for x, y, _e in self.issued_cells.get("c_turret_t1", [])
                          if math.hypot(x - rally[0], y - rally[1]) < 400)
            if d_home > 900 and len(fwd) + fwd_inf < 2 and not danger(rally[0], rally[1], 400):
                ang = (len(fwd) + fwd_inf) * 2.1
                b = idle_builders[0]
                emit({"cmd": "build", "action": "b_c_turret_t1", "builder": b["id"],
                      "x": float(rally[0] + math.cos(ang) * 140),
                      "y": float(rally[1] + math.sin(ang) * 140)},
                     f"build:fwd:{b['id']}", 10.0)
                idle_builders = idle_builders[1:]

        # ⑦ 军工生产：构成由战斗域/计划书定（set_composition），队列执行归规则层——
        #    稳态期零 LLM 也持续产兵（科技过滤/防空占比/队列上限都在 _next_production 内）
        for fac in [u for u in factories if u["t"] != "commandCenter"]:
            if self._q(fac, now) >= QUEUE_MAX:
                continue
            if funds < FUNDS_RESERVE and len(ext_done) + ext_inflight < self.econ_goal["extractors"]:
                continue
            act = self._next_production(fac, own)
            if act:
                price = self.price.get(act, 0)
                if price and funds < price:
                    continue  # 资金不够就憋着（此前无视价格每 2s 刷一次失败）
                emit({"cmd": "train", "action": act, "factory": fac["id"]}, f"train:{fac['id']}")

    # ---------- 布局槽位（全局长持方向，建筑成行成排而非螺旋散点） ----------
    def factory_slot(self, cc, idx, existing, occ=None, btype="landFactory"):
        """厂区槽位：0 号位在 CC 侧翼（垂直前线），后续沿同侧密排成行。
        有占位图时走槽位网格（按 fp 格数贴死，整齐密集）；无则退回几何公式。"""
        fx, fy = self.front_dir or (0.0, 1.0)
        lx, ly = -fy, fx
        if idx > 0 and existing:  # 厂区方向跟随首厂所在侧（防 LLM 先盖厂后方向反转）
            if (existing[0]["x"] - cc["x"]) * lx + (existing[0]["y"] - cc["y"]) * ly < 0:
                lx, ly = -lx, -ly
        if occ is not None:
            spot = self._row_slot(occ, btype, cc, idx, lx, ly, 0)  # 厂区贴死（用户密集造法）
            if spot:
                return spot
        return cc["x"] + lx * (260 + 170 * idx), cc["y"] + ly * (260 + 170 * idx)

    def misc_slot(self, cc, idx, occ=None, btype="fabricatorT1"):
        """后勤槽位：主基地背后（背对前线）一排，1 格缝。"""
        fx, fy = self.front_dir or (0.0, 1.0)
        if occ is not None:
            spot = self._row_slot(occ, btype, cc, idx, -fx, -fy, 1)
            if spot:
                return spot
        return cc["x"] - fx * (240 + 170 * idx), cc["y"] - fy * (240 + 170 * idx)

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
        """离 CC 最近的水域 tile（世界坐标），海厂锚点；无水域返回 None。结果缓存。"""
        if self._water_anchor == "?":
            rows = self.map.get("rows") or []
            tw = self.map.get("tileW") or 1
            th = self.map.get("tileH") or 1
            best, bd = None, None
            for ty, row in enumerate(rows):
                for tx, ch in enumerate(row):
                    if ch != "~":
                        continue
                    wx, wy = (tx + 0.5) * tw, (ty + 0.5) * th
                    d2 = (wx - cc["x"]) ** 2 + (wy - cc["y"]) ** 2
                    if bd is None or d2 < bd:
                        best, bd = (wx, wy), d2
            self._water_anchor = best
        return self._water_anchor

    # ---------- 槽位网格（以主基地锚定晶格，建筑按 fp 格数分配空间，整齐是规则不是运气） ----------
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

    def _occupancy(self, snap):
        """占位图：己方/盟友建筑（按 fp）+ 在途下单（45s 窗口）。"""
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
        return all((gx + i, gy + j) not in occ
                   for i in range(ox, ox + w) for j in range(oy, oy + h))

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
    def _next_production(self, fac, own):
        now = time.time()
        ftype = fac["t"]
        avail = self.factory_actions.get(ftype)
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
                if not self._bad(act, now) and tech_ok(act):
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
