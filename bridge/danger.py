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


class DangerMap:
    def __init__(self):
        self.threat = {}       # (gx,gy) -> float 威胁值
        self.last_seen = {}    # 敌id -> (x,y,t,ts)
        self.enemy_bases = []  # 敌方出生点 [(x,y)]
        self.front_hist = []   # [(ts, 敌最近距离到我家的平方)] 前线序列
        self._last_decay = time.time()

    def seed_bases(self, enemy_spawns):
        self.enemy_bases = list(enemy_spawns or [])
        for x, y in self.enemy_bases:
            self._add(x, y, 8.0)  # 出生点开局即高威胁

    def _add(self, x, y, v):
        key = (int(x // CELL), int(y // CELL))
        self.threat[key] = self.threat.get(key, 0.0) + v

    def update(self, snap, home=None):
        """每拍喂快照：刷新可见敌人、记忆位置、衰减旧威胁。"""
        now = time.time()
        seen_ids = set()
        for e in snap.get("enemy", []):
            seen_ids.add(e["id"])
            prev = self.last_seen.get(e["id"])
            if prev is None or prev[:2] != (e["x"], e["y"]):
                self._add(e["x"], e["y"], 0.5)  # 移动中的敌人持续加热
            self.last_seen[e["id"]] = (e["x"], e["y"], e.get("t", "?"), now)
        # 衰减
        if now - self._last_decay > 15:
            factor = 0.5 ** ((now - self._last_decay) / DECAY_SEC)
            for k in list(self.threat):
                self.threat[k] *= factor
                if self.threat[k] < 0.2:
                    del self.threat[k]
            self._last_decay = now
        # 前线序列：敌人离主基地最近距离
        if home:
            d2 = min(((e["x"] - home[0]) ** 2 + (e["y"] - home[1]) ** 2
                      for e in snap.get("enemy", [])), default=None)
            if d2 is not None:
                self.front_hist.append((now, d2))
                self.front_hist = self.front_hist[-20:]

    def danger(self, x, y, snap=None):
        """危险判定：可见敌人 LIVE_RADIUS 内，或记忆威胁 MEM_RADIUS 内超过阈值。"""
        if snap is not None:
            for e in snap.get("enemy", []):
                if (e["x"] - x) ** 2 + (e["y"] - y) ** 2 < LIVE_RADIUS ** 2:
                    return True
        gx, gy = int(x // CELL), int(y // CELL)
        r = int(MEM_RADIUS // CELL) + 1
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if self.threat.get((gx + dx, gy + dy), 0) >= MEM_THRESHOLD:
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
