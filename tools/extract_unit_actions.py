# -*- coding: utf-8 -*-
"""单位动作语义提取器：直读游戏本体 assets/units/**/*.ini（RW 单位定义明文），
输出 单位→动作表（位置ID/名字/价格/转换目标/描述），喂 units.md 生成器。

pedia 的 builds 列表里：带名字的动作=c<name>（如 cbuildAntiNuke），
纯数字 _N = 该单位 ini 里 [action_*] 段的位置序。本工具把两者对上。
用法: python tools/extract_unit_actions.py [--out tools/unit_actions.json]
"""
import configparser
import glob
import json
import os
import re
import sys

GAME_DIR = os.environ.get("RW_GAME_DIR", r"D:/SteamLibrary/steamapps/common/Rusted Warfare")


def parse_unit_ini(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # 保大小写
    try:
        cp.read(path, encoding="utf-8-sig")
    except Exception:
        return None
    core = cp["core"] if cp.has_section("core") else {}
    name = core.get("name") or os.path.splitext(os.path.basename(path))[0]
    actions = []
    for sec in cp.sections():
        if not sec.startswith("action"):
            continue
        a = {"section": sec}
        for k in ("text", "description", "price", "convertTo", "isVisible",
                  "displayType", "buildSpeed", "pos"):
            if k in cp[sec]:
                a[k] = cp[sec][k].strip()
        m = re.match(r"action_(\d+)$", sec)
        a["pos"] = int(m.group(1)) if m else None
        actions.append(a)
    actions.sort(key=lambda a: (a["pos"] is None, a["pos"] or 0))
    return {"name": name, "file": os.path.relpath(path, GAME_DIR),
            "class": core.get("class", ""), "actions": actions}


def main():
    out = "tools/unit_actions.json"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    roots = [os.path.join(GAME_DIR, "assets", "units"),
             os.path.join(GAME_DIR, "mods", "units")]
    result = {}
    for root in roots:
        for ini in sorted(glob.glob(os.path.join(root, "**", "*.ini"), recursive=True)):
            u = parse_unit_ini(ini)
            if u and u["actions"]:
                result[u["name"]] = u
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"units with actions: {len(result)} -> {out}")
    # 摘要：关键单位的动作表
    for key in ("experimentalGunship", "c_amphibiousJet", "nautilusSubmarine",
                "dropship", "experimentalDropship", "hovercraft", "mechFlying",
                "c_turret_t1", "nukeLauncherC"):
        u = result.get(key)
        if not u:
            print(f"[{key}] 未找到"); continue
        print(f"[{key}] ({u['file']})")
        for i, a in enumerate(u["actions"]):
            txt = a.get("text", "")
            extra = a.get("convertTo", "")
            pr = a.get("price", "")
            print(f"  _{i:<2} {a['section']:28s} {txt:44s} {extra} {pr}")


if __name__ == "__main__":
    main()
