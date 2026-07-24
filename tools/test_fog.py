# -*- coding: utf-8 -*-
"""迷雾三态/安全区重修离线验证：RLE 编解码、FogMap 三态、DangerMap 幽灵加热+可见冷却、
ControlMap 迷雾惩罚、危险判定含幽灵。"""
import sys

sys.path.insert(0, "bridge")
import danger as danger_mod
import fog as fog_mod
import powergrid


def rle_row(vals):
    parts, cur, n = [], vals[0], 1
    for v in vals[1:]:
        if v == cur:
            n += 1
        else:
            parts.append(f"{cur}x{n}")
            cur, n = v, 1
    parts.append(f"{cur}x{n}")
    return ";".join(parts)


def fog_with(grid2d, ghosts=None, mode=("e", "f")):
    """grid2d[y][x] = 0可见/1浅黑/2全黑；mode 控制 E/F/G 布尔。"""
    fm = fog_mod.FogMap()
    fm.tile = 20.0
    snap = {"fogGrid": {"w": len(grid2d[0]), "h": len(grid2d),
                        "e": "e" in mode, "f": "f" in mode, "g": "g" in mode,
                        "rows": [rle_row(r) for r in grid2d]}}
    if ghosts is not None:
        snap["ghosts"] = ghosts
    fm.update(snap)
    return fm


def main():
    # 1. RLE 解码
    assert fog_mod._decode_row("1x3;0x2;2x1") == bytearray([1, 1, 1, 0, 0, 2])
    assert fog_mod._decode_row("0x5") == bytearray([0] * 5)

    # 2. 三态分类 + 世界坐标换算（tile=20）
    fm = fog_with([[0, 0, 1, 1, 2, 2]])
    assert fm.fresh() and fm.enabled()
    assert fm.state_at(10, 10) == "visible"
    assert fm.state_at(50, 10) == "shallow"   # x=50 → tile 2
    assert fm.state_at(90, 10) == "black"     # x=90 → tile 4
    assert fm.state_at(99999, 10) == "black"  # 图外=全黑
    assert fm.fracs() == (2 / 6, 2 / 6, 2 / 6)
    # 方位区用正常二维图：东 1/3 全黑 → E/NE/SE 报全黑，W 不报
    fm_z = fog_with([[0] * 40 + [2] * 20 for _ in range(60)])
    zones = fm_z.black_zones(thresh=0.5)
    assert "E" in zones and "NE" in zones and "SE" in zones, zones
    assert "W" not in zones and "C" not in zones, zones

    # 3. 无迷雾/全图揭开=nofog；老数据=unknown；仅模式负载（rows 空）也要记下模式
    assert fog_with([[2, 2]], mode=()).state_at(10, 10) == "nofog"
    assert fog_with([[2, 2]], mode=("g",)).state_at(10, 10) == "nofog"
    stale = fog_mod.FogMap()
    assert stale.state_at(0, 0) == "unknown" and stale.fracs() is None
    mode_only = fog_mod.FogMap()
    mode_only.update({"fogGrid": {"e": False, "f": False, "g": False, "rows": []}})
    assert mode_only.fresh() and not mode_only.enabled()
    assert mode_only.state_at(0, 0) == "nofog" and mode_only.fracs() == (1.0, 0.0, 0.0)
    assert "无迷雾" in mode_only.mode_text()
    assert "无迷雾" in fog_with([[0]], mode=()).mode_text()
    assert "LOS" in fog_with([[0]], mode=("e", "f")).mode_text()
    assert "基础迷雾" in fog_with([[0]], mode=("e",)).mode_text()

    # 4. 幽灵查询
    fm_g = fog_with([[1]], ghosts=[{"t": "turret", "x": 810, "y": 810, "team": 2}])
    assert len(fm_g.ghosts_near(800, 800, 400)) == 1
    assert len(fm_g.ghosts_near(0, 0, 100)) == 0

    # 5. DangerMap 幽灵加热 + threat_at + danger 邻域
    dm = danger_mod.DangerMap()
    dm.fog = fog_with([[1]], ghosts=[{"t": "turret", "x": 810, "y": 810, "team": 2}])
    dm.update({"enemy": []})
    assert dm.threat_at(810, 810) >= danger_mod.GHOST_THREAT
    assert dm.danger(810, 810), "幽灵建筑应判危险"
    assert dm.danger(810, 810 + 500), "幽灵 500px 邻域（MEM_RADIUS 内）应判危险"
    assert not dm.danger(810, 810 + 2000), "远处不应判危险"

    # 6. 可见即真相：可见空格记忆威胁立即冷却；看不见的格保留
    dm2 = danger_mod.DangerMap()
    dm2.fog = fog_with([[0] * 100 for _ in range(100)])  # 全图可见
    dm2._add(500, 500, 5.0)
    dm2.update({"enemy": []})
    assert not dm2.threat, f"可见空格威胁应冷却: {dm2.threat}"
    dm3 = danger_mod.DangerMap()
    dm3.fog = fog_with([[2] * 100 for _ in range(100)])   # 全图全黑
    dm3._add(500, 500, 5.0)
    dm3.update({"enemy": []})
    assert dm3.threat, "全黑格威胁应保留（靠衰减）"
    # 可见但有敌人在场：不冷却
    dm4 = danger_mod.DangerMap()
    dm4.fog = fog_with([[0] * 100 for _ in range(100)])
    dm4._add(500, 500, 5.0)
    dm4.update({"enemy": [{"id": 1, "x": 500, "y": 500, "t": "tank"}]})
    assert dm4.threat, "可见但有敌：威胁不冷却"

    # 7. ControlMap 迷雾惩罚：全黑重惩/浅黑小惩/可见不惩/未知不惩
    cm = powergrid.ControlMap({})
    cm.fog = fog_with([[2] * 10 for _ in range(10)])
    assert cm.safety(100, 100) == -powergrid.FOG_BLACK_PENALTY
    cm.fog = fog_with([[1] * 10 for _ in range(10)])
    assert cm.safety(100, 100) == -powergrid.FOG_SHALLOW_PENALTY
    cm.fog = fog_with([[0] * 10 for _ in range(10)])
    assert cm.safety(100, 100) == 0.0
    cm.fog = None
    assert cm.safety(100, 100) == 0.0, "未接线 fog 时按旧逻辑（无惩罚）"

    print("PASS test_fog：RLE/三态/幽灵加热/可见冷却/迷雾惩罚全部符合预期")


if __name__ == "__main__":
    main()
