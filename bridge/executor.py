#!/usr/bin/env python3
"""rw-executor: 确定性执行层——经济引擎 / 生产调度 / 建造者调度。

输入与 pilot 相同的快照 dict，输出原子指令（与 agent 协议一致的 cmd dict）。
LLM 只能通过三个 goal 接口影响它；设 LLM_OFF=1 时可完全独立运行（离线验证用）。

设计原则：
- 一切微观操作确定性：选址交给 agent 自动选址，本层只做"该不该建/建几个"。
- 动作可用性自学习：被 agent 回执拒绝的 (单位类型, 动作) 本局标记不再尝试。
- 建造者真并行：每个空闲建造者独立派活，施工中不打扰（agent 侧也有施工保护）。
"""
import re
import time
from collections import Counter

BUILDER_BUILD = {  # 建造者建造动作的静态优先级（按局面分支使用）
    "extractor": ["b_extractorT1"],
    "landFactory": ["b_landFactory"],
}
# 工厂生产的静态默认优先级（生产动作 ID，按 pedia 过滤后生效）
DEFAULT_PROD = {
    "landFactory": ["u_c_tank", "u_missileTank", "u_c_artillery", "u_plasmaTank", "u_heavyTank"],
    "airFactory": ["u_lightGunship", "u_c_interceptor", "u_gunShip"],
    "seaFactory": ["u_gunBoat", "u_missileShip", "u_battleShip"],
    "mechFactory": ["u_c_mech", "u_mech"],
}
# 各目标默认配置
DEFAULT_ECON = {"extractors": 4, "builders": 3, "max_factories": 2}

EXTRACTOR_PRICE = 700
FUNDS_RESERVE = 700        # 非采集器开支的资金底线
UPGRADE_FUNDS = 2600       # 超过此资金且采集器达标后开始升级
QUEUE_MAX = 2              # 每厂队列上限
MAX_CMDS_PER_TICK = 2
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
            ups = [b for b in builds if not b.startswith(("b_", "u_", "c_"))]
            if ups:
                self.upgrade_action[u["name"]] = ups[0]
        self.econ_goal = dict(DEFAULT_ECON)
        self.army_goal = {"composition": None, "rally": None}  # composition: {action: 目标数}
        self.stance = "expand"
        self.action_bad = {}           # 动作 -> 解封时间（None=永久：ID不存在；限时=科技未达，120s重试）
        self.cool = {}                 # 冷却：键 -> 到期时间
        self.rallied = set()           # 已设过集结点的工厂 id
        self.issued_builds = []        # [(建筑类型, 到期时间)] 刚下单、实体未出现的窗口期，防重复下单
        self.prog_seen = {}            # 建筑id -> (prog, 时间戳)：识别烂尾（进度长期不涨）
        self.start_ts = time.time()

    # ---------- LLM 目标接口 ----------
    def set_economy_goal(self, extractors=None, builders=None):
        if extractors is not None:
            self.econ_goal["extractors"] = max(0, int(extractors))
        if builders is not None:
            self.econ_goal["builders"] = max(1, int(builders))

    def set_army_goal(self, composition=None, rally=None):
        if composition is not None:
            # 归一化键名：LLM 常省略 u_ 前缀（'c_tank' -> 'u_c_tank'）
            norm = {}
            for k, v in composition.items():
                key = str(k)
                if not key.startswith(("u_", "b_", "c_")):
                    key = "u_" + key
                norm[key] = int(v)
            self.army_goal["composition"] = norm
        if rally is not None:
            self.army_goal["rally"] = (float(rally[0]), float(rally[1]))
            self.rallied.clear()

    def set_stance(self, stance):
        self.stance = str(stance)

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
                self.action_bad[m.group(1)] = None  # ID 不存在：永久封
                self.log("status", f"⚙ 动作不存在已永封: {m.group(1)}")
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

        builders = [u for u in own if _is_builder(u)]
        idle_builders = [u for u in builders if not _busy(u)]
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

        # 2) 采集器扩建（最高优先；agent 自动选最近空闲池；完工数达标即停）
        ext_target = self.econ_goal["extractors"]
        ext_inflight = (sum(1 for u in constructing if u["t"].startswith("extractor"))
                        + sum(1 for t, _ in self.issued_builds if t.startswith("extractor")))
        if (len(ext_done) < ext_target and ext_inflight < 2
                and funds >= EXTRACTOR_PRICE and idle_builders):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_extractorT1", "builder": b["id"]},
                 f"build:ext:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 3) 第一座陆工厂（2 池后）
        land_factories = [u for u in fac_producers if u["t"] == "landFactory"]
        lf_inflight = (sum(1 for u in constructing if u["t"] == "landFactory")
                       + sum(1 for t, _ in self.issued_builds if t == "landFactory"))
        if (len(ext_done) >= 2 and len(land_factories) + lf_inflight < 1
                and funds >= max(FUNDS_RESERVE, 700) and idle_builders):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_landFactory", "builder": b["id"]},
                 f"build:lf:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 4) 工厂扩建：军队目标大或资金富余时扩厂（此前只在队列塞满时扩，钱再多也不动）
        army_total = sum((self.army_goal["composition"] or {}).values())
        fac_want = max(2, min(4, 1 + army_total // 10))
        saturated = bool(land_factories) and all((u.get("q") or 0) >= 1 for u in land_factories)
        if (len(land_factories) >= 1 and len(fac_producers) + lf_inflight < fac_want
                and (saturated or funds >= 2200) and funds >= 1400 and idle_builders):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_landFactory", "builder": b["id"]},
                 f"build:lf2:{b['id']}", 4.0)
            idle_builders = idle_builders[1:]

        # 4.5) 防御：有工厂且资金富余时，补最多2座炮塔（动作不可用时静默跳过）
        turrets = [u for u in own if "turret" in u["t"].lower() and u.get("bld")]
        t_inflight = sum(1 for t, _ in self.issued_builds if "turret" in t.lower())
        if (land_factories and funds >= 1600 and len(turrets) + t_inflight < 2
                and idle_builders and not self._bad("b_c_turret_t1", now)):
            b = idle_builders[0]
            emit({"cmd": "build", "action": "b_c_turret_t1", "builder": b["id"]},
                 f"build:tur:{b['id']}", 6.0)
            idle_builders = idle_builders[1:]

        # 5) 建筑升级（资金富余即可；所有可升级建筑轮换，工厂升级需更多余钱；优先于常规补兵）
        if funds >= 3200:
            for u in own:
                if not u.get("bld") or _constructing(u):
                    continue
                up = self.upgrade_action.get(u["t"])
                if not up or self._bad(up, now) or now < self.cool.get(f"up:{u['id']}", 0):
                    continue
                if "actory" in u["t"].lower() and funds < 4500:
                    continue
                self.cool[f"up:{u['id']}"] = now + UPGRADE_COOLDOWN
                emit({"cmd": "train", "action": up, "factory": u["id"]}, f"upemit:{u['id']}")
                break

        # 6) 生产调度：按构成目标或默认优先级补队列
        #    资金低于储备线时暂停补兵——经济建设（采集器/工厂）优先，防止"造兵把钱抽空、建造停工"
        econ_pending = (len(ext_done) + ext_inflight < ext_target) or (not land_factories)
        for fac in fac_producers:
            if (fac.get("q") or 0) >= QUEUE_MAX:
                continue
            if funds < FUNDS_RESERVE and econ_pending:
                continue
            act = self._next_production(fac, own)
            if act:
                emit({"cmd": "train", "action": act, "factory": fac["id"]}, f"train:{fac['id']}")

        # 7) 集结点（army_goal 指定时，每厂设一次）
        if self.army_goal["rally"]:
            rx, ry = self.army_goal["rally"]
            for fac in fac_producers:
                if fac["id"] not in self.rallied:
                    self.rallied.add(fac["id"])
                    emit({"cmd": "rally", "factory": fac["id"], "x": rx, "y": ry},
                         f"rally:{fac['id']}", 5.0)

        return cmds

    # ---------- 生产决策 ----------
    def _next_production(self, fac, own):
        now = time.time()
        ftype = fac["t"]
        avail = self.factory_actions.get(ftype)
        # 构成目标模式：按缺口最大者优先
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
                unit_type = action[2:] if action.startswith("u_") else action
                deficit = target - counts.get(unit_type, 0)
                if deficit > best_deficit:
                    best, best_deficit = action, deficit
            return best
        # 默认优先级模式
        for act in DEFAULT_PROD.get(ftype, []):
            if self._bad(act, now):
                continue
            if avail is not None and act not in avail:
                continue
            return act
        # 静态表没有时退回百科里第一个可用的
        if avail:
            for act in sorted(avail):
                if not self._bad(act, now):
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
