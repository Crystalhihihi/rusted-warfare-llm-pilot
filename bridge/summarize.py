#!/usr/bin/env python3
"""Snapshot -> compact battlefield summary for the LLM, plus alert detection."""
import math
from collections import Counter

_DIRS = ["东", "东北", "北", "西北", "西", "西南", "南", "东南"]


def bearing(dx: float, dy: float) -> str:
    # 游戏坐标 y 向下为正（屏幕坐标系），北 = -y
    ang = math.degrees(math.atan2(-dy, dx))
    idx = int((ang + 22.5) // 45) % 8
    return _DIRS[idx]


def dist(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def centroid(units: list) -> dict:
    if not units:
        return {"x": 0, "y": 0}
    return {
        "x": sum(u["x"] for u in units) / len(units),
        "y": sum(u["y"] for u in units) / len(units),
    }


def top_counts(units: list, n: int = 8) -> str:
    c = Counter(u["t"] for u in units)
    return ",".join(f"{k}x{v}" for k, v in c.most_common(n)) or "无"


def ally_clusters(allies: list, max_clusters: int = 3) -> str:
    """盟友按 900px 网格聚类，报告每群构成与大致位置。"""
    if not allies:
        return ""
    cells = {}
    for u in allies:
        key = (u["x"] // 900, u["y"] // 900)
        cells.setdefault(key, []).append(u)
    groups = sorted(cells.values(), key=len, reverse=True)[:max_clusters]
    parts = []
    for g in groups:
        cx = sum(u["x"] for u in g) // len(g)
        cy = sum(u["y"] for u in g) // len(g)
        parts.append(f"({top_counts(g, 5)})@({cx},{cy})附近")
    rest = len(allies) - sum(len(g) for g in groups)
    txt = " | ".join(parts)
    if rest > 0:
        txt += f" | 其他{rest}个"
    return txt


class Summarizer:
    def __init__(self, resources=None):
        self.prev_squad_hp = None
        self.prev_squad_ids = set()
        self.prev_enemy_ids = set()
        self.prev_funds = None
        self.prev_tick = None
        self.resources = resources or []  # 地图资源点 [{x,y}]

    def resource_lines(self, snap):
        """资源点：按离主基地距离排序；我方/盟友建筑占用检测；敌方控制的单独标出。"""
        if not self.resources:
            return []
        buildings = [u for u in snap.get("own", []) + snap.get("allies", [])]
        enemies = snap.get("enemy", [])
        free_land, free_water, taken, hostile = [], [], 0, []
        for p in self.resources:
            if any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40 for u in buildings):
                taken += 1
            elif any(abs(u["x"] - p["x"]) < 200 and abs(u["y"] - p["y"]) < 200 for u in enemies):
                hostile.append(p)
            elif getattr(self, "danger_filter", None) and self.danger_filter(p["x"], p["y"], snap):
                hostile.append(p)  # 记忆威胁（敌人离开视野仍危险，勿去）
            elif p.get("w"):
                free_water.append(p)
            else:
                free_land.append(p)

        def key_home(p):
            if not getattr(self, "home", None):
                return 0
            return (p["x"] - self.home["x"]) ** 2 + (p["y"] - self.home["y"]) ** 2

        free_land.sort(key=key_home)
        free_water.sort(key=key_home)
        lines = []
        if free_land:
            lines.append("空闲资源点(陆地,按离家近到远,请从第一个开始建): "
                         + ",".join(f"({p['x']},{p['y']})" for p in free_land[:10]))
        if free_water:
            lines.append("空闲资源点(水上,需海上建造者): " + ",".join(f"({p['x']},{p['y']})" for p in free_water[:8]))
        if hostile:
            lines.append("敌方控制资源点(危险,勿去): " + ",".join(f"({p['x']},{p['y']})" for p in hostile[:6]))
        if taken:
            lines.append(f"已占用资源点: {taken} 个（不要在这些位置建采集器）")
        return lines

    def summarize(self, snap: dict) -> tuple:
        """Returns (summary_text, alerts_list)."""
        alerts = []
        tick = snap.get("tick", 0)
        team = snap.get("team") or {}
        funds = team.get("funds", 0)
        own = snap.get("own", [])
        allies = snap.get("allies", [])
        enemies = snap.get("enemy", [])
        delegated = set(snap.get("delegated", []))

        squad = [u for u in own if u["id"] in delegated]
        squad_hp = sum(u["hp"] for u in squad)
        squad_ids = {u["id"] for u in squad}
        c = centroid(squad)

        # 主基地锚点：指挥中心 → 第一个建筑 → 首个建造者（无 CC 开局也能定位"家"）
        home = None
        for u in own:
            if u["t"] == "commandCenter":
                home = u
                break
        if home is None:
            home = next((u for u in own if u.get("bld")), None)
        if home is None:
            home = next((u for u in own if "uilder" in u["t"] or "abricator" in u["t"]), None)
        self.home = home

        # ---- alerts from diffs ----
        if self.prev_squad_hp is not None and squad:
            drop = self.prev_squad_hp - squad_hp
            if drop > max(80, 0.08 * self.prev_squad_hp):
                alerts.append(f"小队受击！总血量 {self.prev_squad_hp:.0f}→{squad_hp:.0f}")
        lost = self.prev_squad_ids - squad_ids
        if self.prev_squad_ids and lost:
            alerts.append(f"小队减员！损失 {len(lost)} 个单位，剩余 {len(squad)}")
        enemy_ids = {u["id"] for u in enemies}
        new_enemies = [u for u in enemies if u["id"] not in self.prev_enemy_ids]
        near_new = [u for u in new_enemies if squad and dist(u, c) < 900]
        if near_new:
            alerts.append(f"新发现 {len(near_new)} 个敌单位接近小队（<900）")

        self.prev_squad_hp = squad_hp if squad else None
        self.prev_squad_ids = squad_ids
        self.prev_enemy_ids = enemy_ids

        # ---- income estimate ----
        income = ""
        if self.prev_funds is not None and self.prev_tick is not None and tick > self.prev_tick:
            dtick = tick - self.prev_tick
            if dtick > 0:
                rate = (funds - self.prev_funds) / dtick * 60  # 每秒
                income = f" (净变化 {rate:+.0f}/s)"
        if tick - (self.prev_tick or 0) >= 300:
            self.prev_funds = funds
            self.prev_tick = tick

        # ---- text ----
        lines = [f"tick={tick} 时间={tick // 3600}:{(tick // 60) % 60:02d}"]
        lines.append(f"资金={funds:.0f}{income}")
        if self.home:
            lines.append(f"主基地: {self.home['t']}@({self.home['x']},{self.home['y']})（建筑应建在主基地附近，不要建在前线或敌人家附近）")
        if squad:
            su = "; ".join(
                f"{u['t']}{'(建筑)' if u.get('bld') else ''}#{u['id']}(hp {u['hp']}/{u['maxHp']})@({u['x']},{u['y']})"
                + (f"[{u['act']}]" if u.get("act") else "")
                for u in squad
            )
            lines.append(f"你的小队({len(squad)}): {su}")
        else:
            lines.append("你的小队: 空（等待玩家把单位编入委托编队）")
        # 建造者忙闲状态（防重复派活；施工走路=build@，盖着时=repair→，都算忙）
        busy_b, idle_b = [], []
        for u in squad:
            if "uilder" in u["t"] or "abricator" in u["t"]:
                act = u.get("act") or ""
                if act not in ("", "idle"):
                    busy_b.append(f"#{u['id']}[{act}]")
                else:
                    idle_b.append(f"#{u['id']}")
        if busy_b or idle_b:
            parts = []
            if busy_b:
                parts.append("施工中(勿重复派活): " + ",".join(busy_b))
            if idle_b:
                parts.append("空闲可派活: " + ",".join(idle_b))
            lines.append("建造者状态: " + " | ".join(parts))
        facs = [u for u in own
                if "actory" in u["t"] or "actory" in u["t"].lower()
                or u["t"].startswith(("commandCenter", "outpost"))]
        if facs:
            lines.append("我方工厂: " + "; ".join(
                f"{u['t']}#{u['id']}"
                + (f"(建造中{u['prog'] * 100:.0f}%,暂不能生产)" if 0 < u.get("prog", 1) < 1 else "")
                + (f"[队列{u['q']}]" if u.get("q") is not None else "")
                + f"@({u['x']},{u['y']})" for u in facs[:10]))
        if enemies:
            eu = []
            for u in enemies[:12]:
                d = dist(u, c) if squad else 0
                b = bearing(u["x"] - c["x"], u["y"] - c["y"]) if squad else "?"
                eu.append(
                    f"{u['t']}(hp {u['hp']}/{u['maxHp']},队{u.get('team', '?')})@({u['x']},{u['y']})"
                    + (f" 在小队{b}方{d:.0f}" if squad else "")
                )
            lines.append(f"可见敌人({len(enemies)}): " + "; ".join(eu))
        else:
            lines.append("可见敌人: 无（视野内无敌情）")
        lines.append(f"我方其他部队: {top_counts([u for u in own if u['id'] not in delegated])}")
        building = [u for u in own if 0 < u.get("prog", 1) < 1]
        if building:
            lines.append("建造中: " + ", ".join(
                f"{u['t']}({u['prog'] * 100:.0f}%)@({u['x']},{u['y']})" for u in building[:6]))
        if allies:
            lines.append(f"盟友态势({len(allies)}): {ally_clusters(allies)}")
        fog = snap.get("fog")
        if fog:
            unexplored = [k for k, v in fog.items() if v < 0.3]
            if unexplored:
                lines.append("未探索区域: " + ",".join(sorted(unexplored))
                             + "（其余区域已探索>30%）")
        lines.extend(self.resource_lines(snap))
        if alerts:
            lines.append("⚠ 警报: " + " | ".join(alerts))
        return "\n".join(lines), alerts
