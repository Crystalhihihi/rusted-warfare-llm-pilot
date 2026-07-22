#!/usr/bin/env python3
"""tmx 出生点解析：从地图文件提取各队伍出生点（commandCenter tile 位置）。

原理：RW 的 skirmish 出生点是 tmx "Units" 图层里带 unit/team 属性的特殊 tile，
tile 的 team 值 = 出生槽位；世界坐标 = 格坐标 × 20 + 10。
数据层是 base64+gzip 压缩的 gid 网格，单位映射表在 assets/tilesets/units.tsx。
"""
import base64
import gzip
import os
import re
import xml.etree.ElementTree as ET

GAME_ROOT = r"D:\SteamLibrary\steamapps\common\Rusted Warfare"
TILE_PX = 20


def _tsx_units(tsx_path):
    """解析 units.tsx / 内联 tileset：返回 {gid: (unit, team)}（含 commandCenter/builder 即可）。"""
    out = {}
    root = ET.parse(tsx_path).getroot() if os.path.exists(tsx_path) else None
    firstgid = 1
    return out, firstgid, root


def _read_tile_props(tileset_root, firstgid):
    """从 tileset 元素（外部 units.tsx 或 tmx 内联）读 {gid: (unit, team)}。"""
    out = {}
    for tile in tileset_root.iter("tile"):
        tid = int(tile.get("id"))
        props = {p.get("name"): p.get("value") for p in tile.iter("property")}
        unit = props.get("unit")
        if unit:
            out[firstgid + tid] = (unit, props.get("team"))
    return out


def parse_spawns(map_path, game_root=GAME_ROOT):
    """返回 [(team, x, y), ...]（世界坐标像素）；失败返回 []。"""
    full = os.path.join(game_root, "assets", map_path.replace("/", os.sep))
    if not os.path.exists(full):
        return []
    try:
        root = ET.parse(full).getroot()
        width = int(root.get("width"))
        # tileset 映射（可能多个，取含 unit 属性的那个）
        gid_map = {}
        for ts in root.iter("tileset"):
            firstgid = int(ts.get("firstgid", "1"))
            src = ts.get("source")
            if src:
                tsx = os.path.join(game_root, "assets", "tilesets", os.path.basename(src))
                if os.path.exists(tsx):
                    gid_map.update(_read_tile_props(ET.parse(tsx).getroot(), firstgid))
            else:
                gid_map.update(_read_tile_props(ts, firstgid))  # 内联 tileset
        if not gid_map:
            return []
        # 找 Units 图层并解码
        for layer in root.iter("layer"):
            name = (layer.get("name") or "").lower()
            if "unit" not in name:
                continue
            data = layer.find("data")
            if data is None or data.get("encoding") != "base64":
                continue
            raw = base64.b64decode(data.text.strip())
            if data.get("compression") == "gzip":
                raw = gzip.decompress(raw)
            gids = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
            spawns = []
            for idx, gid in enumerate(gids):
                info = gid_map.get(gid)
                if info and info[0] == "commandCenter":
                    tx, ty = idx % width, idx // width
                    team = int(info[1]) if str(info[1] or "0").isdigit() else 0
                    spawns.append((team, tx * TILE_PX + TILE_PX // 2, ty * TILE_PX + TILE_PX // 2))
            return spawns
    except Exception:
        return []
    return []


def friendly_spawns(map_path, own_cc=None, game_root=GAME_ROOT):
    """返回 (我方出生点, 敌方出生点列表)。own_cc=(x,y) 用于消除换队洗牌的影响。"""
    spawns = parse_spawns(map_path, game_root)
    if not spawns:
        return None, []
    mine = None
    if own_cc:
        mine = min(spawns, key=lambda s: (s[1] - own_cc[0]) ** 2 + (s[2] - own_cc[1]) ** 2)
    else:
        mine = spawns[0]
    enemies = [s for s in spawns if s is not mine]
    return (mine[1], mine[2]), [(s[1], s[2]) for s in enemies]
