#!/usr/bin/env python3
"""powergrid：战斗力标签 + 领土控制图（全确定性，快照驱动，无 LLM）。

- 战力标签：pedia 武器数据 → 每单位类型 (对地战力, 对空战力)，公式 dps×hp/100
- 控制图：400px 格，安全值=军力×1.0+建筑×1.5+离家加成−威胁热力；状态四类
  （军+建筑 / 纯军队 / 纯建筑 / 未控），供扩张安稳判定与战力概况展示
"""
import math

CELL = 400.0
HOME_BONUS_MAX = 60.0     # 主基地加成上限（贴脸）
HOME_BONUS_RANGE = 2400.0
SECURE_MIN = 30.0         # 安稳阈值：安全值≥此且（无威胁或家近）
SECURE_HOLD = 20.0        # 安稳需持续秒数
FOG_BLACK_PENALTY = 25.0  # 全黑（未探索）安全惩罚：未知≠安全（阈值 30 下需硬实力压过）
FOG_SHALLOW_PENALTY = 8.0  # 浅黑（记忆区）安全惩罚：幽灵威胁另计，此处小惩


def power_table(pedia):
    """单位类型名 -> (对地战力, 对空战力)。无武器=0。"""
    table = {}
    for u in (pedia or {}).get("units", []):
        atk = u.get("atk") or {}
        hp = float(u.get("hp") or 100)
        dps = float(atk.get("dps") or 0)
        base = dps * hp / 100.0
        g = base if atk.get("g") else 0.0
        a = base if atk.get("a") else 0.0
        table[u["name"]] = (g, a)
    return table


def unit_power(u, table):
    """单个快照单位的 (对地, 对空) 战力。"""
    return table.get(u["t"], (0.0, 0.0))


def sum_power(units, table):
    g = sum(unit_power(u, table)[0] for u in units)
    a = sum(unit_power(u, table)[1] for u in units)
    return g, a


class ControlMap:
    """领土控制图：每格 (军力, 建筑力, 威胁) → 安全值与状态。每 2s 由规则层喂快照。"""

    def __init__(self, table):
        self.table = table
        self.cells = {}        # (gx,gy) -> {"army": (g,a), "struct": (g,a), "threat": float}
        self.secure_since = {} # 点 key -> 达标起始 ts
        self.home = None
        self.threat_of = None  # fn(x,y,snap)->float，由调用方从 DangerMap 注入
        self.fog = None        # FogMap（接线后：全黑/浅黑安全惩罚，未知≠安全）

    def update(self, snap, dmap, home=None):
        if home:
            self.home = home
        cells = {}
        for u in snap.get("own", []):
            gx, gy = int(u["x"] // CELL), int(u["y"] // CELL)
            c = cells.setdefault((gx, gy), {"army": [0.0, 0.0], "struct": [0.0, 0.0], "threat": 0.0})
            p = unit_power(u, self.table)
            if u.get("bld"):
                c["struct"][0] += p[0]
                c["struct"][1] += p[1]
            else:
                c["army"][0] += p[0]
                c["army"][1] += p[1]
        for (gx, gy), c in cells.items():
            wx, wy = (gx + 0.5) * CELL, (gy + 0.5) * CELL
            c["threat"] = self.threat_of(wx, wy, snap) if self.threat_of else 0.0
        self.cells = cells

    def set_threat_fn(self, fn):
        """威胁取数：DangerMap.threat.get((gx,gy), 0) 的封装。"""
        self.threat_of = fn

    def home_bonus(self, x, y):
        if not self.home:
            return 0.0
        d = math.hypot(x - self.home[0], y - self.home[1])
        if d >= HOME_BONUS_RANGE:
            return 0.0
        return HOME_BONUS_MAX * (1.0 - d / HOME_BONUS_RANGE)

    def safety(self, x, y, snap=None):
        """点安全值 = 军力 + 建筑×1.5 + 离家加成 − 威胁热力 − 迷雾惩罚（未知≠安全）。"""
        gx, gy = int(x // CELL), int(y // CELL)
        army = [0.0, 0.0]
        struct = [0.0, 0.0]
        threat = 0.0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                c = self.cells.get((gx + dx, gy + dy))
                if not c:
                    continue
                w = 1.0 if (dx == 0 and dy == 0) else 0.5
                army[0] += c["army"][0] * w
                struct[0] += c["struct"][0] * w
                threat = max(threat, c["threat"])
        fog_penalty = 0.0
        if self.fog is not None:
            st = self.fog.state_at(x, y)
            if st == "black":
                fog_penalty = FOG_BLACK_PENALTY    # 全黑=未知：不许当安全区
            elif st == "shallow":
                fog_penalty = FOG_SHALLOW_PENALTY  # 浅黑=记忆：幽灵威胁已进热力，再小惩
        return (army[0] + struct[0] * 1.5 + self.home_bonus(x, y)) - threat * 10.0 - fog_penalty

    def state(self, x, y):
        """占领形态：军+建筑 / 纯军队 / 纯建筑 / 未控。"""
        gx, gy = int(x // CELL), int(y // CELL)
        c = self.cells.get((gx, gy))
        army = (c["army"][0] if c else 0.0)
        struct = (c["struct"][0] if c else 0.0)
        if army > 20 and struct > 10:
            return "军+建筑"
        if army > 20:
            return "纯军队"
        if struct > 10:
            return "纯建筑"
        return "未控"

    def secured(self, x, y, now):
        """安稳判定：安全值≥阈值并持续 SECURE_HOLD 秒。建造者放行依据。"""
        s = self.safety(x, y)
        key = (int(x // 200), int(y // 200))
        if s >= SECURE_MIN:
            start = self.secure_since.setdefault(key, now)
            return now - start >= SECURE_HOLD
        self.secure_since.pop(key, None)
        return False

    def overview(self, snap):
        """战力概况行（给 LLM 摘要用）：我方 vs 可见敌，分对地/对空。"""
        og, oa = sum_power([u for u in snap.get("own", []) if not u.get("bld")], self.table)
        eg, ea = sum_power(snap.get("enemy", []), self.table)
        return (f"战力概况: 我军 对地{og:.0f}/对空{oa:.0f} vs 可见敌 对地{eg:.0f}/对空{ea:.0f}"
                + ("（敌强我弱，勿决战）" if eg > og * 1.5 else ""))
