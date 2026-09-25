#!/usr/bin/env python3
"""Kev（本地 Jev 类决策模型）探针：中文 RTS 场景三题型质量 + 延迟 + 并发。

用法:
  python tools/test_kev_probe.py [--base http://127.0.0.1:8009] [--conc 6] [--rounds 3]

口径:
- 质量：5 组手写场景（优势接战/劣势拒战/建造选址/骚扰目标/迷雾记忆），
  期望为人工标注区间，pass/fail 仅作参考（校准未按本项目标定）。
- 延迟：同一 state 连发（第二次起吃 prefix cache），报 new/cached 两档。
- 并发：N 线程模拟 N 小队同时发问，报每轮墙钟与单请求延迟分布。
"""
import argparse
import concurrent.futures as cf
import json
import statistics
import sys
import time
import urllib.request


def post(base, payload, timeout=120):
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/systemone",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer local"},
        method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body, (time.time() - t0) * 1000


def get(base, path, timeout=30):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def q_engage(instr):
    return {"type": "noul", "instructions": instr}


def q_choice(instr, opts):
    return {"type": "choice", "instructions": instr, "criteria": opts}


def q_threat(instr):
    return {"type": "score", "instructions": instr,
            "criteria": ["无威胁", "轻微威胁", "中等威胁", "严重威胁"]}


# (名称, state, questions, 期望判定函数->[(题, 期望描述, 判定)])
SCENARIOS_ZH = [
    ("S1优势接战",
     "本队 L1(12员,82%血)@北矿: c_tank×8,missileTank×4。任务: attack→北矿(已抵达)。"
     "近敌: heavyTank×3(60%血)@北矿东侧350px。战力对比: 本队对地520/对空180 vs 近敌对地260/对空40。"
     "迷雾: 北矿周边可见,无敌方增援迹象。",
     {"engage": q_engage("当前态势下,本队应该立即与近敌交战吗?"),
      "target": q_choice("优先攻击哪个目标?",
                         {"heavyTank群": "3辆60%血的重坦克,数量劣势",
                          "原地待命": "不攻击,继续驻守"}),
      "threat": q_threat("近敌对本队的威胁程度?")},
     [("engage", "noul>0.5(该打)", lambda a: a["noul"] > 0.5),
      ("target", "选heavyTank群", lambda a: a["choice"] == "heavyTank群"),
      ("threat", "威胁≤1(轻微)", lambda a: a["score"] <= 1.5)]),

    ("S2劣势拒战",
     "本队 L2(6员,45%血): c_tank×5,scout×1。任务: hold@隘口A。"
     "近敌: heavyTank×14(90%血)+c_artillery×4@隘口A北侧400px,正在逼近。",
     {"engage": q_engage("敌我悬殊,本队应该主动迎击这股敌军吗?"),
      "action": q_choice("当前最合适的行动?",
                         {"原地死守": "不退一步,战至最后",
                          "撤退靠拢": "向家方向撤退,与守军汇合",
                          "侧翼绕行": "绕过敌军深入敌后"}),
      "threat": q_threat("这股敌军对本队的威胁程度?")},
     [("engage", "noul<0.5(不该打)", lambda a: a["noul"] < 0.5),
      ("action", "选撤退靠拢", lambda a: a["choice"] == "撤退靠拢"),
      ("threat", "威胁≥2.5(严重)", lambda a: a["score"] >= 2.5)]),

    ("S3建造选址",
     "家(1520,2060)。可用安全池: 北矿(1800,900)距家1200px已实控安稳, 南矿(900,2400)距家500px安稳。"
     "敌情: 敌家方位东北, 东矿(2600,1200)距敌家400px我方未控。资金3200 收入+26/s。当前阶段: 扩张经济。",
     {"where": q_choice("下一座采集器建在哪?",
                        {"北矿": "距家1200px,已实控安稳",
                         "南矿": "距家500px,安稳",
                         "东矿": "距敌家400px,我方未控"}),
      "risky": q_engage("现在应该派建造者去东矿(敌家门口未实控区)建采集器吗?")},
     [("where", "不选东矿", lambda a: a["choice"] != "东矿"),
      ("risky", "noul<0.3(不该去)", lambda a: a["noul"] < 0.3)]),

    ("S4骚扰目标",
     "本队 A1(4员满血): lightGunship×4 @敌南矿空域。"
     "可见敌: 采集器×3(无防空)@敌南矿, missileTank×2@敌家(距南矿800px)。任务: harass→敌南矿。",
     {"target": q_choice("优先攻击哪个目标?",
                         {"采集器群": "3座无防空采集器",
                          "missileTank群": "2辆防空导弹车",
                          "撤退": "放弃骚扰返航"}),
      "stay": q_engage("应该留在南矿骚扰(不转进敌家防空圈)吗?")},
     [("target", "选采集器群", lambda a: a["choice"] == "采集器群"),
      ("stay", "noul>0.5(留下)", lambda a: a["noul"] > 0.5)]),

    ("S5迷雾记忆",
     "本队 L3(10员,95%血): hoverTank×10 @中路。可见敌: 无。"
     "浅黑记忆(20秒前): c_turret_t2×4+激光塔×2@隘口B(中路前方600px,进攻敌家必经之路)。"
     "全黑: 敌家方向。任务: attack→敌家。",
     {"rush": q_engage("应该按20秒前的记忆硬闯隘口B塔阵吗?"),
      "action": q_choice("当前最合适的行动?",
                         {"硬闯隘口B": "按原计划直接冲塔阵",
                          "先侦察": "派1个单位侦察隘口B现状再定",
                          "绕道全黑区": "绕过隘口B走未探索区域"}),
      "threat": q_threat("记忆中的隘口B塔阵对本队的威胁程度?")},
     [("rush", "noul<0.5(不硬闯)", lambda a: a["noul"] < 0.5),
      ("action", "选先侦察", lambda a: a["choice"] == "先侦察"),
      ("threat", "威胁≥2(中上)", lambda a: a["score"] >= 2.0)]),
]


def q_threat_en(instr):
    return {"type": "score", "instructions": instr,
            "criteria": ["no threat", "minor threat", "moderate threat", "severe threat"]}


SCENARIOS_EN = [
    ("S1-winning-fight",
     "Squad L1 (12 units, 82% HP) at North Mine: c_tank×8, missileTank×4. Mission: attack North Mine (arrived). "
     "Nearby enemy: heavyTank×3 (60% HP) 350px east. Power: ours ground520/air180 vs theirs ground260/air40. "
     "Fog: area visible, no enemy reinforcements.",
     {"engage": q_engage("Should the squad engage the nearby enemy right now?"),
      "target": q_choice("Which target should be attacked first?",
                         {"heavyTank group": "3 damaged heavy tanks, outnumbered",
                          "hold position": "do not attack, keep holding"}),
      "threat": q_threat_en("How threatening is the nearby enemy to this squad?")},
     [("engage", "noul>0.5", lambda a: a["noul"] > 0.5),
      ("target", "heavyTank group", lambda a: a["choice"] == "heavyTank group"),
      ("threat", "score<=1.5", lambda a: a["score"] <= 1.5)]),

    ("S2-losing-fight",
     "Squad L2 (6 units, 45% HP): c_tank×5, scout×1. Mission: hold at Choke A. "
     "Nearby enemy: heavyTank×14 (90% HP) + c_artillery×4, 400px north, approaching.",
     {"engage": q_engage("The squad is badly outnumbered. Should it attack this enemy force?"),
      "action": q_choice("What is the best action now?",
                         {"stand and fight": "hold the choke to the last unit",
                          "retreat home": "fall back toward base and join defenders",
                          "flank around": "bypass the enemy and push into their rear"}),
      "threat": q_threat_en("How threatening is this enemy force to the squad?")},
     [("engage", "noul<0.5", lambda a: a["noul"] < 0.5),
      ("action", "retreat home", lambda a: a["choice"] == "retreat home"),
      ("threat", "score>=2.5", lambda a: a["score"] >= 2.5)]),

    ("S3-build-site",
     "Base at (1520,2060). Safe resource pools: North Mine (1800,900) 1200px away, secured and calm; "
     "South Mine (900,2400) 500px away, secured and calm. Enemy base is to the northeast; "
     "East Mine (2600,1200) is 400px from the enemy base, not controlled by us. Funds 3200, income +26/s. Goal: expand economy.",
     {"where": q_choice("Where should the next extractor be built?",
                        {"North Mine": "1200px from base, secured",
                         "South Mine": "500px from base, secured",
                         "East Mine": "400px from enemy base, unsecured"}),
      "risky": q_engage("Should a builder be sent to the unsecured East Mine next to the enemy base right now?")},
     [("where", "not East Mine", lambda a: a["choice"] != "East Mine"),
      ("risky", "noul<0.3", lambda a: a["noul"] < 0.3)]),

    ("S4-harass-target",
     "Squad A1 (4 units, full HP): lightGunship×4 over enemy South Mine airspace. "
     "Visible enemy: extractor×3 (no anti-air defense) at South Mine; missileTank×2 at enemy base (800px from South Mine). "
     "Mission: harass enemy South Mine.",
     {"target": q_choice("Which target should be attacked first?",
                         {"extractor group": "3 undefended extractors",
                          "missileTank group": "2 anti-air missile tanks",
                          "retreat": "abort the harassment and fly home"}),
      "stay": q_engage("Should the squad keep harassing South Mine and stay out of the enemy base AA zone?")},
     [("target", "extractor group", lambda a: a["choice"] == "extractor group"),
      ("stay", "noul>0.5", lambda a: a["noul"] > 0.5)]),

    ("S5-fog-memory",
     "Squad L3 (10 units, 95% HP): hoverTank×10 at middle lane. Visible enemy: none. "
     "Shroud memory (20s ago): c_turret_t2×4 + laser tower×2 at Choke B (600px ahead, the only route to the enemy base). "
     "Full black unexplored: enemy base direction. Mission: attack the enemy base.",
     {"rush": q_engage("Should the squad rush the Choke B turret line based on 20-second-old memory?"),
      "action": q_choice("What is the best action now?",
                         {"rush Choke B": "charge the turret line as planned",
                          "scout first": "send 1 unit to scout Choke B's current state first",
                          "detour through black": "bypass through the unexplored black area"}),
      "threat": q_threat_en("How threatening is the remembered Choke B turret line to this squad?")},
     [("rush", "noul<0.5", lambda a: a["noul"] < 0.5),
      ("action", "scout first", lambda a: a["choice"] == "scout first"),
      ("threat", "score>=2.0", lambda a: a["score"] >= 2.0)]),
]

SCENARIOS = {"zh": SCENARIOS_ZH, "en": SCENARIOS_EN}


def fmt_ans(a):
    if a["type"] == "noul":
        return f"noul={a['noul']:.2f}"
    if a["type"] == "choice":
        return f"choice={a['choice']}(conf {a.get('confidence', 0):.2f})"
    return f"score={a['score']:.2f}(conf {a.get('confidence', 0):.2f})"


def run_one(base, name, state, questions):
    payload = {"state": state, "model": "kev-latest", "questions": questions}
    return post(base, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8009")
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--lang", choices=["zh", "en"], default="en")
    args = ap.parse_args()

    print(f"== /v1/models ==  {args.base}")
    try:
        models = get(args.base, "/v1/models")
        print(json.dumps(models, ensure_ascii=False)[:600])
    except Exception as e:
        print(f"连不上服务: {e}\n先起服务: KEV_FUSED=0 uv run --extra serve python -m kev.serve --run jaredpalmer/kev-0.8b --port 8009")
        sys.exit(1)

    # ---- 质量 ----
    print("\n== 质量(5 场景) ==")
    total = passed = 0
    scenarios = SCENARIOS[args.lang]
    for name, state, questions, checks in scenarios:
        try:
            body, wall = run_one(args.base, name, state, questions)
        except Exception as e:
            print(f"[{name}] 请求失败: {e}")
            continue
        ans = body.get("answers", {})
        lat = body.get("latency_ms", 0)
        line = []
        for qid, want, judge in checks:
            a = ans.get(qid)
            if not a:
                line.append(f"{qid}:缺答")
                total += 1
                continue
            ok = judge(a)
            total += 1
            passed += ok
            line.append(f"{qid}:{fmt_ans(a)} {'✓' if ok else '✗'}({want})")
        print(f"[{name}] {lat:.0f}ms(wall {wall:.0f})  " + "  ".join(line))
    print(f"小计: {passed}/{total}")

    # ---- 延迟(缓存两档) ----
    print("\n== 延迟(同一 state 连发5次) ==")
    name, state, questions, _ = scenarios[0]
    lats = []
    for i in range(5):
        body, wall = run_one(args.base, name, state, questions)
        lats.append((body.get("latency_ms", 0), wall))
    print(f"首次(新state): {lats[0][0]:.0f}ms / wall {lats[0][1]:.0f}ms")
    cached = lats[1:]
    print(f"后续(缓存state): 模型 p50 {statistics.median(x[0] for x in cached):.0f}ms / wall p50 {statistics.median(x[1] for x in cached):.0f}ms")

    # ---- 并发 ----
    print(f"\n== 并发({args.conc} 线程×{args.rounds} 轮,模拟小队同时发问) ==")

    def job(k):
        nm, st, qs, _ = scenarios[k % len(scenarios)]
        st = st.replace("Squad", f"Squad{k}", 1) if args.lang == "en" else st.replace("本队", f"本队{k}", 1)  # 错开 prefix cache
        body, wall = run_one(args.base, nm, st, qs)
        return body.get("latency_ms", 0), wall

    all_wall, round_walls = [], []
    for r in range(args.rounds):
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=args.conc) as ex:
            res = list(ex.map(job, range(args.conc)))
        rw = (time.time() - t0) * 1000
        round_walls.append(rw)
        all_wall.extend(x[1] for x in res)
        print(f"第{r + 1}轮: 墙钟 {rw:.0f}ms, 单请求 p50 {statistics.median(x[1] for x in res):.0f}ms max {max(x[1] for x in res):.0f}ms")
    print(f"并发结论: {args.conc} 队一轮墙钟 p50 {statistics.median(round_walls):.0f}ms "
          f"({'够' if statistics.median(round_walls) < 2500 else '不够'} 2.5s 拍)")

    print("\n完成。质量行供人工判读,阈值未按本项目标定。")


if __name__ == "__main__":
    main()
