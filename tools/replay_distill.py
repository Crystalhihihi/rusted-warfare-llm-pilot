# -*- coding: utf-8 -*-
"""回放语料蒸馏器：421k 指令 → 人类建造基准表（分桶分位数）+ 个人风格画像。
用法:
  python tools/replay_distill.py <回放目录>            全语料基准报告
  python tools/replay_distill.py <回放目录> --who 名字  追加该玩家个人画像
口径说明：
  - 建造事件=指令流里 au 带坐标的落点；建筑分类名单见 BLD_*（pedia 名单实证+经验）
  - 家=该玩家前 5 座建筑坐标中位数（出生点代理；save 单位区未解析）
  - 胜负=chat 的 "X has been wiped out" 出局名单对照玩家记录；幸存者=胜
  - 时长档：<8min 速攻 / 8-20min 中局 / >20min 后期（元数据缺则用最后指令帧）
  - 图尺寸=全部带坐标指令的覆盖范围（max x/y），≥4500 大图 / 2500-4500 中图 / <2500 小图
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay_parse import parse_replay

# 建筑分类（utype → 类别）；#1/#2/#3=vanilla ordinal 未命名（#1 经验≈陆厂）
BLD = {
    "extractorT1": "pool", "extractorT2": "pool", "extractorT3": "pool",
    "c_turret_t1": "turret", "c_turret_t2_gun": "turret", "c_turret_t2_art": "turret",
    "c_antiAirTurret": "aa", "c_antiAirTurretT2": "aa",
    "fabricatorT1": "fab", "fabricatorT2": "fab", "fabricatorT3": "fab",
    "nukeLauncherC": "nuke", "antiNukeLauncherC": "antinuke",
    "landFactory": "factory", "airFactory": "factory", "mechFactory": "factory",
    "seaFactory": "factory", "experimentalLandFactory": "expfactory",
    "repairbay": "support", "laserDefence": "support",
    "#1": "factory1?", "#2": "factory2?", "#3": "bld3?",
}
KEY_FIRST = [("pool", "首池"), ("factory", "首厂"), ("turret", "首塔"), ("aa", "首防空"),
             ("fab", "首制造仪"), ("nuke", "首核弹井")]
SUPPORT = {"fab", "support", "antinuke"}


def parse_players(blob):
    """玩家记录地标扫描：[team B][credits i32][group i32][01][u16 len][utf name]。
    credits≥100 过滤 customUnits 块的同构误命中。"""
    hits, seen = [], set()
    i, n = 0, len(blob)
    while i < n - 60:
        team = blob[i]
        if team <= 31 and blob[i + 9] == 1:
            credits = int.from_bytes(blob[i + 1:i + 5], "big", signed=True)
            group = int.from_bytes(blob[i + 5:i + 9], "big", signed=True)
            nl = int.from_bytes(blob[i + 10:i + 12], "big")
            if 100 <= credits <= 20_000_000 and -64 <= group <= 64 and 1 <= nl <= 48:
                raw = blob[i + 12:i + 12 + nl]
                try:
                    name = raw.decode("utf-8")
                except UnicodeDecodeError:
                    name = ""
                if name and all(ch.isprintable() for ch in name) and team not in seen:
                    seen.add(team)
                    hits.append({"team": team, "credits": credits,
                                 "group": group, "name": name})
                    i += 12 + nl
                    continue
        i += 1
    return hits


def game_record(path):
    rep = parse_replay(path)
    evs = rep["events"]
    players = parse_players(rep["save"])
    wiped = []
    for c in rep["chat"]:
        b = c.get("b") or ""
        if "has been wiped out" in b:
            wiped.append(b.split(" has been wiped out")[0])
    frames = int(rep["meta"]["frames"]) if rep["meta"].get("frames") else \
        (max(e["frame"] for e in evs) if evs else 0)
    has_meta = bool(rep["meta"].get("frames"))
    xs = [e["x"] for e in evs if e["x"] is not None]
    ys = [e["y"] for e in evs if e["y"] is not None]
    extent = (max(xs) if xs else 0, max(ys) if ys else 0)
    # 每玩家建造履历
    per = defaultdict(list)
    for e in evs:
        cat = BLD.get(e["action"])
        if cat and e["x"] is not None:
            per[e["team"]].append({"f": e["frame"], "cat": cat, "t": e["action"],
                                   "x": e["x"], "y": e["y"]})
    for lst in per.values():
        lst.sort(key=lambda d: d["f"])
    # 每玩家指标
    pstats = []
    for team, lst in per.items():
        home_x = sorted(p["x"] for p in lst[:5])[len(lst[:5]) // 2] if lst else 0
        home_y = sorted(p["y"] for p in lst[:5])[len(lst[:5]) // 2] if lst else 0
        first = {}
        for cat, _label in KEY_FIRST:
            hit = next((p for p in lst if p["cat"] == cat or
                        (cat == "factory" and p["cat"].startswith("factory"))), None)
            first[cat] = hit["f"] if hit else None
        phases = [0, 0, 0]
        for p in lst:
            phases[0 if p["f"] < 10800 else (1 if p["f"] < 28800 else 2)] += 1
        sup = [((p["x"] - home_x) ** 2 + (p["y"] - home_y) ** 2) ** 0.5
               for p in lst if p["cat"] in SUPPORT]
        pstats.append({"team": team, "first": first, "phases": phases,
                       "builds": len(lst), "sup_dist": sup,
                       "cmds": sum(1 for e in evs if e["team"] == team)})
    name_of = {p["team"]: p["name"] for p in players}
    group_of = {p["team"]: p["group"] for p in players}
    return {"file": os.path.basename(path), "frames": frames, "extent": extent,
            "players": players, "name_of": name_of, "group_of": group_of,
            "wiped": wiped, "builds": per,
            "pstats": pstats, "dur_min": frames / 3600, "has_meta": has_meta}


# ---------- 防线遮盖与夺点巩固（保护关系，不是裸距离） ----------

DEF_CATS = {"turret", "aa"}
FAB_CATS = {"factory", "expfactory", "fab", "factory1?", "factory2?", "support"}


def _home(lst):
    """前 5 座建筑坐标中位数（出生点代理）。"""
    if not lst:
        return (0, 0)
    xs = sorted(p["x"] for p in lst[:5])
    ys = sorted(p["y"] for p in lst[:5])
    return (xs[len(lst[:5]) // 2], ys[len(lst[:5]) // 2])


def protection_stats(g):
    """每玩家：①厂/制造仪的防线遮盖率（有本队塔/防空比它更靠近敌家=被保护）
    ②穿插率（200px 内有塔）③裸奔率（自己就是全队最靠敌的建筑="工厂保护防线"倒挂）
    ④远池（>800px）护矿：后 180s 内 300px 内有塔的比例与延迟。"""
    out = {}
    homes = {t: _home(lst) for t, lst in g["builds"].items() if len(lst) >= 3}
    for t, lst in g["builds"].items():
        if t not in homes:
            continue
        hx, hy = homes[t]
        my_group = g["group_of"].get(t)
        # 敌家=最近的不同组玩家家（组信息缺失时全部当敌）
        enemy = None
        for t2, h2 in homes.items():
            if t2 == t or (my_group is not None and g["group_of"].get(t2) == my_group):
                continue
            d = (h2[0] - hx) ** 2 + (h2[1] - hy) ** 2
            if enemy is None or d < enemy[0]:
                enemy = (d, h2)
        if enemy is None:
            continue
        ex, ey = enemy[1]

        def d_enemy(p):
            return ((p["x"] - ex) ** 2 + (p["y"] - ey) ** 2) ** 0.5

        defs = [p for p in lst if p["cat"] in DEF_CATS]
        fabs = [p for p in lst if p["cat"] in FAB_CATS]
        covered = interleaved = exposed = 0
        for b in fabs:
            db = d_enemy(b)
            if any(d_enemy(d) < db - 50 for d in defs):
                covered += 1
            if any((d["x"] - b["x"]) ** 2 + (d["y"] - b["y"]) ** 2 <= 200 ** 2 for d in defs):
                interleaved += 1
            front = min((d_enemy(p) for p in lst), default=1e9)
            if db <= front + 1:
                exposed += 1
        # 远池护矿：>800px 扩张池，180s(10800帧)内 300px 内有塔=稳固（用户口径"打下来能及时稳固"）
        pools = [p for p in lst if p["cat"] == "pool"
                 and ((p["x"] - hx) ** 2 + (p["y"] - hy) ** 2) ** 0.5 > 800]
        guarded, delays = 0, []
        for p in pools:
            gds = [d for d in defs if (d["x"] - p["x"]) ** 2 + (d["y"] - p["y"]) ** 2 <= 300 ** 2
                   and 0 <= d["f"] - p["f"] <= 10800]
            if gds:
                guarded += 1
                delays.append((min(d["f"] for d in gds) - p["f"]) / 60)
        out[t] = {"fabs": len(fabs),
                  "covered": covered / len(fabs) if fabs else None,
                  "interleaved": interleaved / len(fabs) if fabs else None,
                  "exposed": exposed / len(fabs) if fabs else None,
                  "pools_far": len(pools),
                  "guarded": guarded / len(pools) if pools else None,
                  "guard_delay": q(delays, .5) if delays else None}
    return out


def q(vals, pct):
    vals = sorted(vals)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(len(vals) * pct))]


def size_bucket(extent):
    m = max(extent)
    return "大图" if m >= 4500 else ("中图" if m >= 2500 else "小图")


def dur_bucket(mins):
    return "速攻<8" if mins < 8 else ("中局8-20" if mins < 20 else "后期>20")


def firsts_table(rows, title):
    """rows: [(label, pstats, won)] → 分位数表"""
    print(f"\n### {title}（n={len(rows)}）")
    head = ["指标", "p25", "p50", "p75", "有率"]
    print("|".join(head))
    for cat, label in KEY_FIRST:
        ts = [s["first"][cat] / 60 for _, s, _w in rows if s["first"].get(cat)]
        if not ts:
            continue
        rate = f"{len(ts) * 100 // len(rows)}%"
        print(f"{label}|{q(ts, .25):.0f}s|{q(ts, .5):.0f}s|{q(ts, .75):.0f}s|{rate}")
    builds = [s["builds"] for _, s, _w in rows]
    print(f"总建造座数|{q(builds, .25)}|{q(builds, .5)}|{q(builds, .75)}|")
    sup = [d for _l, s, _w in rows for d in s["sup_dist"]]
    if sup:
        print(f"支持建筑距家px|{q(sup, .25):.0f}|{q(sup, .5):.0f}|{q(sup, .75):.0f}|")


def main():
    root = sys.argv[1]
    who = None
    complete_only = "--complete-only" in sys.argv
    if "--who" in sys.argv:
        who = sys.argv[sys.argv.index("--who") + 1].split(",")  # 支持多名字（同人多昵称）
    files = sorted(glob.glob(os.path.join(root, "*.replay")))
    games = []
    skipped = 0
    for i, p in enumerate(files):
        try:
            g = game_record(p)
            # 完整局口径：有结尾元数据（录制正常收尾）且 ≥3 分钟——
            # 手机闪退/早退局缺 endReplayMetaData，剔除（用户 2026-07-26 定性）
            if complete_only and (not g["has_meta"] or g["dur_min"] < 3):
                skipped += 1
                continue
            games.append(g)
        except Exception as ex:
            print(f"跳过 {os.path.basename(p)}: {ex!r}", file=sys.stderr)
        if (i + 1) % 30 == 0:
            print(f"... {i + 1}/{len(files)}", file=sys.stderr)
    print(f"# 回放蒸馏报告（{len(games)} 局" +
          (f"，已剔不完整局 {skipped}" if complete_only else "") + "）\n")
    # 语料总览
    durs = [g["dur_min"] for g in games]
    print(f"时长 p25/p50/p75: {q(durs, .25):.1f}/{q(durs, .5):.1f}/{q(durs, .75):.1f} 分钟")
    print(f"图尺寸: {dict(Counter(size_bucket(g['extent']) for g in games))}")
    names = Counter(p["name"] for g in games for p in g["players"])
    print(f"识别玩家 {len(names)} 人，出场 Top8: {[n for n, _ in names.most_common(8)]}")
    # 分桶：图尺寸×时长档，每格出 胜/出局 两张表
    buckets = defaultdict(list)
    for g in games:
        wiped_names = set(g["wiped"])
        for s in g["pstats"]:
            nm = g["name_of"].get(s["team"], "")
            won = nm not in wiped_names
            buckets[(size_bucket(g["extent"]), dur_bucket(g["dur_min"]))].append(
                (g["file"], s, won))
    order = [("小图", "速攻<8"), ("小图", "中局8-20"), ("中图", "速攻<8"),
             ("中图", "中局8-20"), ("中图", "后期>20"), ("大图", "中局8-20"),
             ("大图", "后期>20")]
    for key in order:
        rows = buckets.get(key)
        if not rows or len(rows) < 8:
            continue
        win = [r for r in rows if r[2]]
        lose = [r for r in rows if not r[2]]
        if len(win) >= 5:
            firsts_table(win, f"{key[0]}·{key[1]} · 幸存者")
        if len(lose) >= 5:
            firsts_table(lose, f"{key[0]}·{key[1]} · 出局者")
    # 防线遮盖与夺点巩固（幸存者，按图尺寸）
    print("\n## 防线遮盖与夺点巩固（幸存者口径）")
    print("图档|厂数|遮盖率(塔比厂更靠敌)|穿插率(200px内有塔)|裸奔率(厂成最外层)|远池|护矿率(180s内)|护矿延迟p50")
    by_size = defaultdict(lambda: defaultdict(list))
    for g in games:
        wiped_names = set(g["wiped"])
        for t, ps in protection_stats(g).items():
            nm = g["name_of"].get(t, "")
            if nm in wiped_names:
                continue
            b = by_size[size_bucket(g["extent"])]
            for k, v in ps.items():
                if v is not None and k != "fabs" and k != "pools_far":
                    b[k].append(v)
    for sz in ("小图", "中图", "大图"):
        b = by_size.get(sz)
        if not b or not b.get("covered"):
            continue
        gd = f"|{q(b['guard_delay'], .5):.0f}s" if b.get("guard_delay") else "|—"
        print(f"{sz}|—|{q(b['covered'], .5) * 100:.0f}%|{q(b['interleaved'], .5) * 100:.0f}%"
              f"|{q(b['exposed'], .5) * 100:.0f}%|—|{q(b['guarded'], .5) * 100:.0f}%{gd}")
    # 个人画像
    if who:
        who_set = set(who)
        mine = []
        for g in games:
            for s in g["pstats"]:
                if g["name_of"].get(s["team"]) in who_set:
                    mine.append((g, s, g["name_of"].get(s["team"]) not in set(g["wiped"])))
        label = "/".join(who)
        print(f"\n## 个人画像：{label}（{len(mine)} 局，存活 {sum(1 for _g, _s, w in mine if w)}）")
        firsts_table([(g["file"], s, w) for g, s, w in mine], f"{label} 全部局")
        nukes = [(g["dur_min"], s["first"].get("nuke"), s["builds"], w)
                 for g, s, w in mine if s["first"].get("nuke")]
        if nukes:
            print(f"\n屯蛋局（下过核）{len(nukes)} 局：时长/首核/总建造/存活")
            for d, n, b, w in nukes[:12]:
                print(f"  {d:.0f}min 首核{n / 60:.0f}s {b}座 {'胜' if w else '出局'}")


if __name__ == "__main__":
    main()
