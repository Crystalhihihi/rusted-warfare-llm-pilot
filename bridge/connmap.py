#!/usr/bin/env python3
"""陆地连通域分析：洪水填充地图，回答"敌区陆地能不能到、从哪绕"。

用地图 ASCII 行（MapData 下发的 . = ^ 等），计算连通分量：
- 主基地所在连通域 = 陆军可达范围
- 目标敌区同域 → 给出己方域内离目标最近的格子作绕行点
- 不同域 → 明确"陆军不可达，需两栖/空军/机甲"
"""

WALK = set(".=^")  # 陆地/桥/阶梯（机甲可爬）视作可走（近似）


def build_components(rows):
    """返回 (comp 网格, 分量数)。comp[y][x] = 连通分量编号，-1=不可走。"""
    h = len(rows)
    w = len(rows[0]) if h else 0
    comp = [[-1] * w for _ in range(h)]
    n = 0
    for y in range(h):
        row = rows[y]
        for x in range(w):
            if row[x] not in WALK or comp[y][x] >= 0:
                continue
            stack = [(x, y)]
            comp[y][x] = n
            while stack:
                cx, cy = stack.pop()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < w and 0 <= ny < h and rows[ny][nx] in WALK and comp[ny][nx] < 0:
                        comp[ny][nx] = n
                        stack.append((nx, ny))
            n += 1
    return comp, n


def comp_at(comp, x, y, tw, th):
    tx, ty = int(x // tw), int(y // th)
    if 0 <= ty < len(comp) and 0 <= tx < len(comp[0]):
        return comp[ty][tx]
    return -1


def path_hint(comp, home_comp, tx, ty, tw, th):
    """在己方连通域内找离目标最近的格子当绕行点。返回 ((x,y), 距目标px)。"""
    best = None
    bd = float("inf")
    for y, row in enumerate(comp):
        for x, c in enumerate(row):
            if c == home_comp:
                wx, wy = x * tw + tw / 2, y * th + th / 2
                d = (wx - tx) ** 2 + (wy - ty) ** 2
                if d < bd:
                    bd, best = d, (wx, wy)
    return best, bd ** 0.5 if best else 0
