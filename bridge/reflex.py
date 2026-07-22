#!/usr/bin/env python3
"""rw-reflex: 反射层——毫秒级规则反应，不经过 LLM。

职责（仅姿态反应，不抢战略目标）：
- 小队受击撤退：委托小队血量短时间急跌且敌人在附近 → 向主基地撤（带滞回，防抖动）
- 基地遇袭回防：敌人逼近主基地 → 小队拉回防守（冷却，防与 LLM 指令打架）
"""
import re
import time

RETREAT_HP_DROP = 0.15     # 血量跌幅阈值（相对上次快照）
ENEMY_NEAR = 1100          # “敌人在小队附近”的距离
HOME_THREAT = 900          # “敌人逼近主基地”的距离
RETREAT_COOL = 12.0        # 撤退冷却（滞回）
DEFEND_COOL = 20.0         # 回防冷却


class Reflex:
    def __init__(self, log=None):
        self.log = log or (lambda kind, text: None)
        self.prev_hp = None
        self.prev_ts = 0.0
        self.retreat_cd = 0.0
        self.defend_cd = 0.0
        self.dmap = None              # DangerMap（pilot 注入）
        self.builder_recall_cd = {}   # 建造者id -> 召回冷却
        # 事件钩子（multiagent 注入；pilot 单 LLM 模式留 None）
        self.on_builder_recall = None  # fn(builder_id, tx, ty)：建造者赴险被召回（含目标点）
        self.on_squad_reflex = None    # fn(text)：小队撤退/基地回防（通报战斗 agent）

    @staticmethod
    def _centroid(units):
        if not units:
            return None
        return (sum(u["x"] for u in units) / len(units),
                sum(u["y"] for u in units) / len(units))

    def tick(self, snap, home, delegated_ids, send, log_activity=None):
        """每循环调用；home=(x,y) 或 None；send(cmd_dict) 返回是否成功。"""
        now = time.time()
        if home is None:
            return
        own = snap.get("own", [])
        enemies = snap.get("enemy", [])
        squad = [u for u in own if u["id"] in delegated_ids and not u.get("bld")]
        if not squad:
            self.prev_hp = None
            return
        c = self._centroid(squad)
        hp = sum(u["hp"] for u in squad)
        hx, hy = home

        # 1) 受击撤退：血量急跌 + 敌近 → 撤向主基地
        if self.prev_hp and now - self.prev_ts < 6.0:
            drop = (self.prev_hp - hp) / max(1.0, self.prev_hp)
            near = [e for e in enemies if (e["x"] - c[0]) ** 2 + (e["y"] - c[1]) ** 2 < ENEMY_NEAR ** 2]
            if drop > RETREAT_HP_DROP and near and now >= self.retreat_cd:
                self.retreat_cd = now + RETREAT_COOL
                if send({"cmd": "move", "x": hx, "y": hy}):
                    self.log("alert", f"⚡ 反射：小队受击撤退（血量 -{drop * 100:.0f}%）")
                    if self.on_squad_reflex:
                        self.on_squad_reflex(f"系统反射：小队受击已撤退回家（血量 -{drop * 100:.0f}%），"
                                             f"{int(RETREAT_COOL)}s 冷却内勿立刻反扑")
        self.prev_hp = hp
        self.prev_ts = now

        # 2) 基地回防：敌人逼近主基地且小队不在附近 → 拉回
        if now >= self.defend_cd and enemies:
            threat = [e for e in enemies if (e["x"] - hx) ** 2 + (e["y"] - hy) ** 2 < HOME_THREAT ** 2]
            if threat and (c[0] - hx) ** 2 + (c[1] - hy) ** 2 > HOME_THREAT ** 2:
                self.defend_cd = now + DEFEND_COOL
                if send({"cmd": "move", "x": hx, "y": hy}):
                    self.log("alert", f"⚡ 反射：基地遇袭回防（敌 {len(threat)} 个逼近）")
                    if self.on_squad_reflex:
                        self.on_squad_reflex(f"系统反射：基地遇袭已拉回小队回防（敌 {len(threat)} 个逼近），"
                                             f"{int(DEFEND_COOL)}s 冷却内勿再外派")

        # 3) 建造者召回：赶路去危险区的建造者立即撤回（act=build@(x,y) 且目标点危险）
        if self.dmap is not None:
            for u in own:
                if "uilder" not in u["t"] and "abricator" not in u["t"]:
                    continue
                act = u.get("act") or ""
                if not act.startswith("build@"):
                    continue
                m = re.search(r"build@\((-?\d+),(-?\d+)\)", act)
                if not m:
                    continue
                tx, ty = float(m.group(1)), float(m.group(2))
                if self.dmap.danger(tx, ty, snap) and now >= self.builder_recall_cd.get(u["id"], 0):
                    self.builder_recall_cd[u["id"]] = now + 20.0
                    if send({"cmd": "move", "units": str(u["id"]), "x": hx, "y": hy}):
                        self.log("alert", f"⚡ 反射：召回赴险建造者#{u['id']}（目标点在敌区）")
                        if self.on_builder_recall:
                            self.on_builder_recall(u["id"], tx, ty)
