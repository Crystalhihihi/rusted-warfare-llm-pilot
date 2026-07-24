# -*- coding: utf-8 -*-
"""迷雾三态模型（对齐游戏真实机制；数据=agent 快照的 fogGrid/ghosts 字段，~10s 一跳）。

三态（每 tile 一格，agent 侧 team.N：<5 可见 / 5..9 浅黑 / 10 全黑）：
  visible = 当前可见（所见即真实：这里没敌人就是真没有）
  shallow = 浅黑（探索过但已回盖；游戏在此保留"记忆敌建筑"=幽灵，可能已变）
  black   = 全黑（从未探索，什么都不知道）
  nofog   = 无迷雾/全图揭开（全图可见）
  unknown = 数据未到（老 jar/暂停中），消费方按旧逻辑兜底
迷雾模式（map.E/F/G，对应大厅 fogMode 0/1/2 + revealed）：
  E=false      → 无迷雾
  E=true F=false → 基础迷雾：探索过即永久可见，只剩"未探索=全黑"一态
  E=true F=true  → LOS：离开视野回盖浅黑（幽灵只在此模式产生）
  G=true       → 全图揭开
幽灵 = 游戏自维护的浅黑区记忆建筑（玩家在浅黑区所见，公平信息，不是透视）。
"""

import time


def _decode_row(s):
    out = bytearray()
    for part in s.split(";"):
        if not part:
            continue
        v, _, n = part.partition("x")
        try:
            out.extend(bytes((int(v),)) * int(n))
        except ValueError:
            break
    return out


class FogMap:
    def __init__(self, map_msg=None):
        self.mode = None        # {"e","f","g"} 或 None=未知
        self.w = 0
        self.h = 0
        self.rows = []          # [bytearray(w)] * h，值 0/1/2
        self.ghosts = []        # [{t,x,y,team}]（浅黑区记忆敌建筑）
        self.raw = None         # 最近一次 fogGrid 原始负载（dashboard 转发用）
        self.ts = 0.0           # 最近 fogGrid 到达时刻（0=从未收到）
        self.tile = 20.0
        if map_msg:
            self.set_map(map_msg)

    def set_map(self, map_msg):
        tw = (map_msg or {}).get("tileW") or 0
        if tw > 0:
            self.tile = float(tw)

    def update(self, snap):
        """每拍随快照调用；fogGrid/ghosts 只在 ~10s 一跳的快照里出现（pilot 已留存 fog_snap）。
        fogGrid 允许"仅模式"负载（无迷雾/全图揭开时 rows 为空：也要记下模式）。"""
        if not snap:
            return
        fg = snap.get("fogGrid")
        if fg:
            self.mode = {"e": bool(fg.get("e")), "f": bool(fg.get("f")), "g": bool(fg.get("g"))}
            rows = [_decode_row(r) for r in fg.get("rows", [])]
            if rows and all(len(r) == len(rows[0]) for r in rows):
                self.rows = rows
                self.w = len(rows[0])
                self.h = len(rows)
            else:
                self.rows = []
                self.w = self.h = 0
            self.raw = fg
            self.ts = time.time()
        if "ghosts" in snap:
            self.ghosts = [g for g in (snap.get("ghosts") or []) if isinstance(g, dict)]

    # ---------- 查询 ----------

    def fresh(self):
        """fogGrid 数据是否新鲜（40s 内到过）。不新鲜=老 jar/暂停，消费方兜底旧逻辑。"""
        return self.ts > 0 and time.time() - self.ts < 40.0

    def enabled(self):
        """迷雾是否启用（无迷雾/全图揭开=False：全图可见）。"""
        if not self.mode:
            return True  # 未知时按有迷雾保守处理
        return self.mode["e"] and not self.mode["g"]

    def state_at(self, x, y):
        """世界坐标 → visible/shallow/black/nofog/unknown。"""
        if not self.fresh():
            return "unknown"
        if not self.enabled():
            return "nofog"
        tx, ty = int(x / self.tile), int(y / self.tile)
        if 0 <= tx < self.w and 0 <= ty < self.h:
            return ("visible", "shallow", "black")[self.rows[ty][tx]]
        return "black"  # 图外当全黑

    def fracs(self):
        """(visible, shallow, black) 比例；无数据返回 None。"""
        if not self.fresh():
            return None
        if not self.enabled():
            return (1.0, 0.0, 0.0)
        if not self.rows:
            return None
        cnt = [0, 0, 0]
        for r in self.rows:
            for v in r:
                cnt[v] += 1
        total = max(1, self.w * self.h)
        return (cnt[0] / total, cnt[1] / total, cnt[2] / total)

    _ZONES = ("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE")

    def black_zones(self, thresh=0.5):
        """3x3 方位区中全黑占比 > thresh 的区名列表（给摘要用）。"""
        if not self.fresh() or not self.rows or not self.enabled():
            return []
        out = []
        for sy in range(3):
            for sx in range(3):
                x0, x1 = sx * self.w // 3, (sx + 1) * self.w // 3
                y0, y1 = sy * self.h // 3, (sy + 1) * self.h // 3
                total = black = 0
                for y in range(y0, y1):
                    row = self.rows[y]
                    for x in range(x0, x1):
                        total += 1
                        if row[x] == 2:
                            black += 1
                if total and black / total > thresh:
                    out.append(self._ZONES[sy * 3 + sx])
        return out

    def ghosts_near(self, x, y, r=400.0):
        return [g for g in self.ghosts
                if (g.get("x", 0) - x) ** 2 + (g.get("y", 0) - y) ** 2 < r * r]

    def mode_text(self):
        """给 prompt 的迷雾模式语义（中文一行）。"""
        if not self.mode:
            return "未知（数据未同步，按有迷雾保守处理）"
        if self.mode["g"]:
            return "全图揭开：无迷雾，全图敌情真实可见"
        if not self.mode["e"]:
            return "无迷雾：全图可见"
        if self.mode["f"]:
            return ("战争迷雾(LOS)：可见=真实敌情（没敌人=真没有）；浅黑=记忆区"
                    "（只记得当时的敌建筑，可能已变）；全黑=从未探索，不许臆测")
        return "基础迷雾：探索过的区域永久可见；全黑=从未探索，不许臆测"
