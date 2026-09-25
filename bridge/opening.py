#!/usr/bin/env python3
"""开局任务单生成器（架构二期）：cliloop 物化进 executor 工单/目标 + tasks/opening.json。

用户定调（2026-09-26）：**开局只给基础任务**——够不死的经济骨架（几口池+一座厂+建造者补员），
防线/构成/小队任务/主攻方向一律不出脚本的手，全归 AI 层（总指挥 CLI / 中层四席 / Jev）。

generate_opening(snap, pedia, spawns, is_naval, size_class, map_msg=None) -> {
  "work_order": [ {action, slot, note}, ... ],   # 与 Executor.set_work_order 同构（只用 pool:k/factory:k）
  "plan_params": {阶段, 采集器目标, 建造者目标, 厂型清单, 姿态},
  "plan_text": "开局任务单 v2：...（种子计划书）",
  "composition": {},            # 刻意为空：不代 AI 决定造什么兵
  "mission_templates": [],      # 刻意为空：不代 AI 决定小队去哪
}
"""

# ---- 基础任务表（用户手调区） ----
OPENING_POOLS = 3                    # 开局采集器数（pool:0..k 按离家近到远，安全池闸在 executor）
OPENING_BUILDERS = 3                 # 建造者目标数
OPENING_MAIN_FAC = "b_landFactory"   # 开局厂型（陆厂）


def _builder_has(pedia, key, default):
    """建造者动作表里含 key 子串的第一个 b_ 动作（如 extractor/landFactory）；没有回退 default。"""
    for u in (pedia or {}).get("units", []):
        if not u.get("builder"):
            continue
        for b in u.get("builds", []):
            if b.startswith("b_") and key in b:
                return b
    return default


def generate_opening(snap, pedia, spawns, is_naval, size_class, map_msg=None):
    mine, _enemies = spawns or (None, [])
    cc = next((u for u in (snap or {}).get("own", [])
               if u["t"] == "commandCenter" and u.get("bld")), None)
    home = (cc["x"], cc["y"]) if cc else mine
    if home is None:
        return None  # 家都没法定（无 CC 无出生点）——调用方下拍重试

    ext_act = _builder_has(pedia, "extractor", "b_extractorT1")
    fac_act = _builder_has(pedia, "landFactory", OPENING_MAIN_FAC)
    work_order = [{"action": ext_act, "slot": f"pool:{k}", "note": "开局经济"}
                  for k in range(OPENING_POOLS)]
    work_order.append({"action": fac_act, "slot": "factory:0", "note": "开局产能"})

    plan_params = {"阶段": "开局", "采集器目标": OPENING_POOLS, "建造者目标": OPENING_BUILDERS,
                   "厂型清单": [fac_act[2:]], "姿态": "expand"}
    plan_text = (
        "开局任务单 v2（系统基础骨架，其余全部归 AI 层决策）\n"
        f"【阶段】开局：{OPENING_POOLS}池+{fac_act[2:]}，只保经济骨架\n"
        f"【经济】采集器补到{OPENING_POOLS}，建造者保持{OPENING_BUILDERS}\n"
        "【配兵】未定——归总指挥/中层\n"
        "【攻防】未定——归总指挥/中层\n"
        "【布局】未定——归总指挥/中层")

    return {"work_order": work_order, "plan_params": plan_params,
            "plan_text": plan_text, "composition": {},
            "mission_templates": []}
