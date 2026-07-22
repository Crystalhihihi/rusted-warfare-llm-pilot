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
from collections import Counter

BUILDER_BUILD = {  # 建造者建造动作的静态优先级（按局面分支使用）
    "extractor": ["b_extractorT1"],
    "landFactory": ["b_landFactory"],
}
# 工厂生产的静态默认优先级（生产动作 ID，按 pedia 过滤后生效）
DEFAULT_PROD = {
    "landFactory": ["u_c_tank", "u_missileTank", "u_c_artillery", "u_plasmaTank", "u_heavyTank"],
    "airFactory": ["u_heavyInterceptor", "u_c_interceptor", "u_gunShip", "u_bomber"],
    "seaFactory": ["u_gunBoat", "u_missileShip", "u_battleShip"],
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
        self.stance = "expand"
        self.action_bad = {}           # 动作 -> 解封时间（None=永久：ID不存在；限时=科技未达，120s重试）
        self.cool = {}                 # 冷却：键 -> 到期时间
        self.rallied = set()           # 已设过集结点的工厂 id
        self.issued_builds = []        # [(建筑类型, 到期时间)] 刚下单、实体未出现的窗口期，防重复下单
        self.custom_builds = []        # LLM 指定建造队列 [(action, x, y)]
        self.prog_seen = {}            # 建筑id -> (prog, 时间戳)：识别烂尾（进度长期不涨）
        self.upgrade_bad = {}          # (建筑id, 动作) -> 解封时间：该建筑此升级失败后的冷却
        self.last_upgrade = None
        self.lock = threading.Lock()   # tick/目标设置/失败学习 共用（执行层独立线程）
        self.dmap = None               # DangerMap（pilot 注入）：敌人记忆/威胁热力
        self.last_military_order = 0.0  # 最近一次 LLM 军事指令时间（attack/harass/defend）
        self.auto_military_cd = 0.0
        self.auto_military = False      # 默认关闭：军事决策完全归 LLM（config 可开）
        self.start_ts = time.time()

    # ---------- LLM 目标接口 ----------
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
        idle_builders = [u for u in builders if not _busy(u)]

        # 危险区：可见敌人 + 记忆威胁（敌人离开视野仍算危险，由 DangerMap 提供）
        enemies = snap.get("enemy", [])

        def danger(x, y, r=600):
            if self.dmap is not None:
                return self.dmap.danger(x, y, snap)
            return any((e["x"] - x) ** 2 + (e["y"] - y) ** 2 < r * r for e in enemies)

        _cc = next((u for u in own if u["t"] == "commandCenter" and u.get("bld")), None)
        ccx = _cc["x"] if _cc else 0
        ccy = _cc["y"] if _cc else 0
        safe_pools = sorted(
            (p for p in self.resources
             if not p.get("w") and not danger(p["x"], p["y"])
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
            if cmd["cmd"] == "build":
                act = cmd.get("action", "")
                btype = act[2:] if act.startswith("b_") else act
                self.issued_builds.append((btype, now + 45.0))  # 施工窗口期，防重复下单
            return True

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
            if cc and mw and mh:  # 厂区落在主基地侧翼（垂直于前线方向），不挡正面
                dx, dy = mw / 2 - cc["x"], mh / 2 - cc["y"]
                d = max(1.0, math.hypot(dx, dy))
                fcmd["x"] = float(cc["x"] - dy / d * 260)
                fcmd["y"] = float(cc["y"] + dx / d * 260)
            emit(fcmd, f"build:lf:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 3.5) 战斗工程师：无存货且要扩厂时产1-2个（直接建T2厂，省升级时间）
        ce_count = sum(1 for u in own if u["t"] == "combatEngineer")
        if (len(ext_done) >= 2 and ce_count < 2 and funds >= 1500 and land_factories):
            lf = next((u for u in land_factories if (u.get("q") or 0) < QUEUE_MAX), None)
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
            if land_factories:  # 向首厂聚拢，形成厂区而非散点
                fcmd["x"] = float(land_factories[0]["x"]) + 120
                fcmd["y"] = float(land_factories[0]["y"]) + 120
            emit(fcmd, f"build:fac:{b['id']}", 4.0)
            idle_builders = [u for u in idle_builders if u["id"] != b["id"]]

        # 4.4) 生产调度（提到炮塔/升级之前：军力是之本，防止被经济动作饿死）
        #    按构成目标或默认优先级补队列；资金低于储备线且经济未达标时暂停
        econ_pending = (len(ext_done) + ext_inflight < ext_target) or (not land_factories)
        for fac in fac_producers:
            if (fac.get("q") or 0) >= QUEUE_MAX:
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
            fab_cap = 4 if funds >= 15000 else 2
            if stalemate and fab_count + fab_inflight < fab_cap and idle_builders \
                    and not self._bad("b_fabricatorT1", now):
                b = idle_builders[0]
                emit({"cmd": "build", "action": "b_fabricatorT1", "builder": b["id"]},
                     f"build:fab:{b['id']}", 8.0)
                idle_builders = idle_builders[1:]

        # 4.6) 高盈余战略建筑：实验厂（≥20000 且 ≥2 陆厂，限1座）与海军厂（水图才造）
        exp_count = sum(1 for u in own if "experimental" in u["t"].lower() and u.get("bld"))
        exp_inflight = sum(1 for t, _ in self.issued_builds if "experimental" in t.lower())
        if (funds >= 20000 and len(land_factories) >= 2 and exp_count + exp_inflight < 1
                and idle_builders and not self._bad("b_experimentalLandFactory", now)):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_experimentalLandFactory", "builder": b["id"]},
                 f"build:exp:{b['id']}", 8.0)
            idle_builders = idle_builders[1:]
        water_pools = sum(1 for p in self.resources if p.get("w"))
        sea_count = sum(1 for u in fac_producers if u["t"] == "seaFactory")
        sea_inflight = sum(1 for t, _ in self.issued_builds if t == "seaFactory")
        if (water_pools >= 2 and len(ext_done) >= 2 and sea_count + sea_inflight < 1
                and funds >= 1500 and idle_builders and not self._bad("b_seaFactory", now)):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_seaFactory", "builder": b["id"]},
                 f"build:sea:{b['id']}", 8.0)
            idle_builders = idle_builders[1:]

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
            elif cc and mw and mh:  # 无锚点：落在主基地后方（背对前线一侧）
                dx, dy = cc["x"] - mw / 2, cc["y"] - mh / 2
                d = max(1.0, math.hypot(dx, dy))
                bcmd["x"] = float(cc["x"] + dx / d * 220)
                bcmd["y"] = float(cc["y"] + dy / d * 220)
            if emit(bcmd, f"build:custom:{b['id']}", 6.0):
                self.custom_builds.pop(0)
                idle_builders = idle_builders[1:]
            else:
                break

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

    def _pick_builder(self, idle_builders, for_factory=False):
        """盖工厂优先派战斗工程师（直接建 T2 厂省升级时间），否则第一个空闲建造者。"""
        if for_factory and idle_builders:
            ce = next((u for u in idle_builders if u["t"] == "combatEngineer"), None)
            if ce:
                return ce
        return idle_builders[0] if idle_builders else None

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
        # 默认优先级模式
        for act in DEFAULT_PROD.get(ftype, []):
            if self._bad(act, now):
                continue
            if avail is not None and act not in avail:
                continue
            if not tech_ok(act):
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
        stalled_txt = f" 烂尾{getattr(self, 'last_stalled', 0)}座(请手动补盖)" if getattr(self, "last_stalled", 0) else ""
        return (f"执行层: 池{ext}/{self.econ_goal['extractors']} 厂{facs} "
                f"建造者{len(builders)}(闲{idle_b}) 目标{self.stance}{stalled_txt}")
