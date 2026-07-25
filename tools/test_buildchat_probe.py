# -*- coding: utf-8 -*-
"""小模型建造快速执行层可行性探针（离线，硅基流动 Qwen3-30B-A3B）。
命题：给它建造场景+枚举动作+合法锚点表，看输出是否"能听懂"——
①JSON 合法 ②动作在枚举内 ③锚点来自合法表（不自造坐标） ④选择合乎常理。
5 个场景各有明确正确答案：常规派单/被打撤退/撞点改选/优先级/无好牌默认 hold。
用法: python tools/test_buildchat_probe.py [轮数]（每轮 5 场景×1 调用，默认 2 轮）
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import tomllib

import llm

SECRET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "secret.toml")
with open(SECRET, "rb") as fp:
    SF_KEY = tomllib.load(fp).get("siliconflow_key", "")
BASE_URL = "https://api.siliconflow.cn/v1"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"

SYSTEM = """你是 RTS 游戏建造小队的快速反应层，指挥 3 个建造者。只输出一行 JSON，不许输出任何其他文字。
合法动作四枚：build（建造）/move（移动到锚点）/repair（修理指定建筑）/hold（不动）
输出契约：{"action":"build|move|repair|hold","item":"动作表ID或建筑编号","anchor":"合法锚点表之一","reason":"≤20字"}
军规：
1. 每拍恰好一个动作，输出别的即违令
2. anchor 只能原样选自合法锚点表，禁止自造坐标、禁止编造锚点
3. build 的 item 只能原样选自动作表
4. 遇袭优先保命（move 到安全锚点），家被打了优先抢修（repair）
5. 没有好选择就 hold 并写明原因
6. reason 不超过 20 字，不许解释规则"""

SCENARIOS = [
    {
        "name": "①常规派单",
        "state": "建造者×3 空闲@家圈 | 任务：在资源池盖采集器 | 资金 5000 充足",
        "anchors": ["池A(东北400px,安全)", "池B(西南900px,有敌情)", "厂位C(家圈)", "家圈"],
        "items": ["b_extractorT1(700)", "b_c_turret_t1(750)", "b_repairbay(1000)"],
        "want_action": "build", "want_anchor": ["池A(东北400px,安全)"],
    },
    {
        "name": "②被打撤退",
        "state": "建造者#12 在池B施工，遭 2 个敌军攻击，血量 40% | 另 2 建造者在家",
        "anchors": ["池A(东北400px,安全)", "池B(西南900px,有敌情)", "厂位C(家圈)", "家圈"],
        "items": ["b_extractorT1(700)", "b_c_turret_t1(750)", "b_repairbay(1000)"],
        "want_action": "move", "want_anchor": ["家圈", "池A(东北400px,安全)"],  # 安全锚点皆可
    },
    {
        "name": "③撞点改选",
        "state": "任务：盖炮塔防线 | 厂位C 刚被盟友建筑占用 | 建造者×3 空闲",
        "anchors": ["池A(东北400px,安全)", "弧位D(东北防线,安全)", "厂位C(已被占)", "家圈"],
        "items": ["b_extractorT1(700)", "b_c_turret_t1(750)", "b_repairbay(1000)"],
        "want_action": "build", "want_anchor": ["弧位D(东北防线,安全)"],
    },
    {
        "name": "④优先级",
        "state": "主基地#9 被打掉血 60% | 任务队列：新采集器(池A) | 建造者×3 空闲 | 资金 5000",
        "anchors": ["池A(东北400px,安全)", "弧位D(东北防线,安全)", "家圈"],
        "items": ["b_extractorT1(700)", "b_c_turret_t1(750)", "b_repairbay(1000)"],
        "want_action": "repair", "want_anchor": None,  # repair 主基地#9，锚点不限
    },
    {
        "name": "⑤无好牌hold",
        "state": "全部池点都有敌情，全部槽位被占满 | 建造者×3 空闲 | 资金 200 不够任何建筑",
        "anchors": ["池B(西南900px,有敌情)", "家圈"],
        "items": ["b_extractorT1(700)", "b_c_turret_t1(750)"],
        "want_action": "hold", "want_anchor": None,
    },
]

ENUM = {"build", "move", "repair", "hold"}


def ask(sc):
    user = (f"<state>{sc['state']}</state>\n"
            f"合法锚点表: {sc['anchors']}\n"
            f"动作表: {sc['items']}")
    t0 = time.time()
    msg = llm.chat(BASE_URL, SF_KEY, MODEL,
                   [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user}], tools=[], timeout=45, max_retries=1)
    dt = time.time() - t0
    txt = (msg.get("content") or "").strip()
    return txt, dt


def judge(sc, txt):
    """返回 (解析ok, 动作合法, 锚点合法, 合乎常理, 说明)。"""
    try:
        # 容忍模型多输出文字：截第一个 { 到最后一个 }
        i, j = txt.find("{"), txt.rfind("}")
        obj = json.loads(txt[i:j + 1])
    except Exception:
        return False, False, False, False, f"JSON 解析失败: {txt[:80]}"
    act = obj.get("action")
    anchor = obj.get("anchor")
    ok_act = act in ENUM
    legal_anchors = sc["anchors"]
    ok_anchor = (anchor in legal_anchors) or (act in ("hold", "repair") and anchor in (None, "", "家圈"))
    want = sc["want_anchor"]
    if want is None:
        sane = ok_act and act == sc["want_action"]
    else:
        sane = ok_act and act == sc["want_action"] and anchor in want
    note = f"{act} anchor={anchor} item={obj.get('item')} reason={obj.get('reason')}"
    return True, ok_act, ok_anchor, sane, note


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    if not SF_KEY:
        print("FATAL: secret.toml 缺 siliconflow_key")
        return 1
    tot = {"parse": 0, "act": 0, "anchor": 0, "sane": 0, "n": 0}
    lat = []
    for r in range(rounds):
        print(f"===== 第 {r + 1} 轮 =====")
        for sc in SCENARIOS:
            try:
                txt, dt = ask(sc)
            except llm.LlmError as e:
                print(f"{sc['name']}: 调用失败 {e}")
                continue
            lat.append(dt)
            p, a, an, sane, note = judge(sc, txt)
            tot["parse"] += p; tot["act"] += a; tot["anchor"] += an; tot["sane"] += sane; tot["n"] += 1
            flag = "OK" if sane else "XX"
            print(f"{flag} {sc['name']} [{dt:.1f}s] 解析={p} 动作合法={a} 锚点合法={an} 合理={sane} | {note}")
    n = max(1, tot["n"])
    print(f"\n== 合计 {tot['n']} 次: JSON {tot['parse']}/{n} 动作合法 {tot['act']}/{n} "
          f"锚点合法 {tot['anchor']}/{n} 合理 {tot['sane']}/{n} "
          f"延迟中位 {sorted(lat)[len(lat) // 2] if lat else 0:.1f}s ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
