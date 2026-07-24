#!/usr/bin/env python3
"""DangerMap：动态危险区——敌人最后可见位置记忆（时间衰减）+ 威胁热力格 + 前线状态。

解决"敌人离开视野就敢往敌区派建造者"：危险判定 = 可见敌人 OR 记忆威胁，不止看当前帧。
"""
import time

CELL = 400.0          # 威胁格边长（px）
DECAY_SEC = 90.0      # 威胁减半时间
LIVE_RADIUS = 600.0   # 可见敌人即危险半径（与原逻辑一致）
MEM_RADIUS = 700.0    # 记忆威胁判定半径
MEM_THRESHOLD = 2.0   # 记忆威胁值阈值


GHOST_THREAT = 4.0    # 记忆敌建筑（浅黑幽灵）格威胁值——建筑≈长期威胁，每拍按幽灵表重建（不进衰减）


class DangerMap:
    def __init__(self):
        self.threat = {}       # (gx,gy) -> float 威胁值
        self.threat_struct = {}  # (gx,gy) -> float 幽灵建筑威胁（重建式：幽灵消失=被证伪/被拆，自然退热）
        self.last_seen = {}    # 敌id -> (x,y,t,ts)
        self.enemy_bases = []  # 敌方出生点 [(x,y)]
        self.front_hist = []   # [(ts, 敌最近距离到我家的平方)] 前线序列
        self.fog = None        # FogMap（接线后启用：幽灵加热 + 可见冷却）
        self._last_decay = time.time()

    def threat_at(self, x, y):
        """格威胁 = max(单位热力, 幽灵建筑热力)。"""
        key = (int(x // CELL), int(y // CELL))
        return max(self.threat.get(key, 0.0), self.threat_struct.get(key, 0.0))

    def seed_bases(self, enemy_spawns):
        self.enemy_bases = list(enemy_spawns or [])
        for x, y in self.enemy_bases:
            self._add(x, y, 8.0)  # 出生点开局即高威胁

    def _add(self, x, y, v):
        key = (int(x // CELL), int(y // CELL))
        self.threat[key] = self.threat.get(key, 0.0) + v

    def update(self, snap, home=None):
        """每拍喂快照：刷新可见敌人、记忆位置、幽灵加热、衰减旧威胁、可见冷却。"""
        now = time.time()
        seen_ids = set()
        for e in snap.get("enemy", []):
            seen_ids.add(e["id"])
            prev = self.last_seen.get(e["id"])
            if prev is None or prev[:2] != (e["x"], e["y"]):
                self._add(e["x"], e["y"], 0.5)  # 移动中的敌人持续加热
            self.last_seen[e["id"]] = (e["x"], e["y"], e.get("t", "?"), now)
        # 浅黑幽灵建筑加热（重建式：幽灵表即游戏"记忆敌建筑"，消失=被证伪/被拆，自然退热）
        if self.fog is not None and self.fog.fresh():
            self.threat_struct = {}
            for g in self.fog.ghosts:
                key = (int(g.get("x", 0) // CELL), int(g.get("y", 0) // CELL))
                self.threat_struct[key] = GHOST_THREAT
        # 衰减
        if now - self._last_decay > 15:
            factor = 0.5 ** ((now - self._last_decay) / DECAY_SEC)
            for k in list(self.threat):
                self.threat[k] *= factor
                if self.threat[k] < 0.2:
                    del self.threat[k]
            self._last_decay = now
        # 可见即真相：当前可见且无敌人的记忆格立即冷却（正情报）；看不见的格才靠衰减/幽灵
        if self.fog is not None and self.fog.fresh() and self.fog.enabled():
            enemies = snap.get("enemy", [])
            for k in list(self.threat):
                cx, cy = (k[0] + 0.5) * CELL, (k[1] + 0.5) * CELL
                if self.fog.state_at(cx, cy) != "visible":
                    continue
                if any((e["x"] - cx) ** 2 + (e["y"] - cy) ** 2 < CELL ** 2 for e in enemies):
                    continue
                del self.threat[k]
            for k in list(self.threat_struct):
                cx, cy = (k[0] + 0.5) * CELL, (k[1] + 0.5) * CELL
                if self.fog.state_at(cx, cy) == "visible" and not any(
                        e.get("bld") and (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2 < CELL ** 2
                        for e in enemies):
                    del self.threat_struct[k]
        # 前线序列：敌人离主基地最近距离
        if home:
            d2 = min(((e["x"] - home[0]) ** 2 + (e["y"] - home[1]) ** 2
                      for e in snap.get("enemy", [])), default=None)
            if d2 is not None:
                self.front_hist.append((now, d2))
                self.front_hist = self.front_hist[-20:]

    def danger(self, x, y, snap=None):
        """危险判定：可见敌人 LIVE_RADIUS 内，或记忆威胁（含幽灵建筑）MEM_RADIUS 内超过阈值。"""
        if snap is not None:
            for e in snap.get("enemy", []):
                if (e["x"] - x) ** 2 + (e["y"] - y) ** 2 < LIVE_RADIUS ** 2:
                    return True
        gx, gy = int(x // CELL), int(y // CELL)
        r = int(MEM_RADIUS // CELL) + 1
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                key = (gx + dx, gy + dy)
                if max(self.threat.get(key, 0), self.threat_struct.get(key, 0)) >= MEM_THRESHOLD:
                    return True
        return False

    def front_status(self):
        """推进/胶着/被压：看敌最近距离到我家的变化趋势。"""
        if len(self.front_hist) < 6:
            return "unknown"
        old = self.front_hist[0][1]
        new = self.front_hist[-1][1]
        if new < old * 0.6:
            return "被压（敌人在逼近我家）"
        if new > old * 1.5:
            return "推进（敌人在远离我家）"
        return "胶着"

    def enemy_base_text(self):
        if not self.enemy_bases:
            return ""
        return "敌方出生点: " + ",".join(f"({int(x)},{int(y)})" for x, y in self.enemy_bases)
