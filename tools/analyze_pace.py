#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_pace.py — 用数据回答"时间到底花哪了"。

数据源：
  bridge/activity.jsonl   活动流（ts/kind/text；轮次结束行 = status "{icon} {name}: ..."）
  agent/logs/agent.log    游戏侧回执（cmd X ok / cmd X FAIL，毫秒级时间戳）

口径说明（与 bridge/multiagent.py、pilot.py 的打点位置一一对应）：
  - 轮次结束行：kind=status 且 text 匹配 "🔨 build: / ⚔ combat: / 📋 commander:"，
    但排除 "chat 空转，升级 reasoner"（那是轮中事件，该轮含 2 次 API 调用）。
  - LLM 下发指令：kind=cmd 且 text 以 🔨/⚔/📋 开头；以 "✗" 开头的是翻译层拦截，不计入下发。
  - 规则层指令：kind=cmd 且 text 以 ⚙ 开头。
  - 成功/失败回执：text 以 ✓/✗ 开头（✗ 行 kind=error）。
  - 轮次间隔 ≈ API 耗时 + 节拍 sleep（cadence），API 耗时估计 = 间隔 - 配置节拍。

用法：python tools/analyze_pace.py [--hours 3]
"""
import argparse
import collections
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ROLES = {"🔨": "build", "⚔": "combat", "📋": "commander"}
ROLE_LABEL = {"🔨": "🔨建造", "⚔": "⚔战斗", "📋": "📋指挥"}
ROUND_RE = re.compile(r"^([🔨⚔📋])\s+(build|combat|commander):\s*(.*)$")
TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")


def pct(sorted_vals, q):
    """线性插值百分位；sorted_vals 已排序。"""
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def fmt_dist(vals):
    if not vals:
        return "无样本"
    s = sorted(vals)
    return (f"n={len(s)} 中位={pct(s, .5):.1f}s P25={pct(s, .25):.1f}s "
            f"P75={pct(s, .75):.1f}s 最大={max(s):.1f}s 均值={sum(s)/len(s):.1f}s")


def load_activity(path, hours):
    """读 activity.jsonl，处理跨午夜回绕，返回 (rows, window_start, window_end)。"""
    rows = []
    day_off = 0
    prev_t = None
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = TS_RE.match(d.get("ts", ""))
            if not m:
                continue
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if prev_t is not None and t < prev_t - 12 * 3600:
                day_off += 86400  # 跨午夜
            prev_t = t
            rows.append({"abs": t + day_off, "kind": d.get("kind", ""),
                         "text": d.get("text", "")})
    if not rows:
        return [], None, None
    end = rows[-1]["abs"]
    start = end - hours * 3600
    win = [r for r in rows if r["abs"] >= start]
    return win, win[0]["abs"], end


def load_config(path):
    """粗解析 config.toml 里的整数/浮点配置（节拍用）。"""
    cfg = {}
    if not os.path.exists(path):
        return cfg
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\s*(\w+)\s*=\s*([0-9.]+)", line)
        if m:
            cfg[m.group(1)] = float(m.group(2))
    return cfg


def norm_action(desc):
    """动作归一化：去数字/坐标，把"同一动作不同参数"聚到一起。"""
    s = re.sub(r"[\d.,@()#x ]+", "", desc)
    return s[:40]


def hms(sec):
    sec = int(sec) % 86400
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"


def analyze_activity(win, start, end, cfg):
    span_min = max((end - start) / 60.0, 1e-9)
    print(f"【数据窗口】activity.jsonl 实际覆盖 {hms(start)} → {hms(end)}，"
          f"共 {span_min:.1f} 分钟、{len(win)} 行")
    if span_min < 30:
        print(f"  ⚠ 注意：文件只保留了当前对局（新对局开始时会重置），"
              f"不足 --hours 指定的窗口；以下按实际 {span_min:.1f} 分钟统计。")
    print()

    # ---- 轮次切分 ----
    rounds = collections.defaultdict(list)   # role -> [abs_ts]
    round_texts = collections.defaultdict(list)
    escalations = collections.Counter()      # chat 空转升级 reasoner 次数
    llm_fail = collections.Counter()
    for r in win:
        if r["kind"] != "status":
            if r["kind"] == "error":
                m = re.match(r"^([🔨⚔📋])\s+\w+ LLM失败", r["text"])
                if m:
                    llm_fail[m.group(1)] += 1
            continue
        m = ROUND_RE.match(r["text"])
        if not m:
            continue
        icon, _, rest = m.groups()
        if "空转，升级" in rest:
            escalations[icon] += 1
            continue
        rounds[icon].append(r["abs"])
        round_texts[icon].append(rest)

    # ---- 1. 轮次间隔分布 ----
    print("═══ 1. 轮次间隔分布（同角色相邻两轮结束行的时间差）═══")
    cadence_cfg = {"🔨": cfg.get("build_interval_sec", 10),
                   "⚔": cfg.get("combat_interval_sec", 6),
                   "📋": cfg.get("commander_interval_sec", 50)}
    api_est = {}
    for icon in ("🔨", "⚔", "📋"):
        ts = rounds[icon]
        iv = [b - a for a, b in zip(ts, ts[1:])]
        print(f"  {ROLE_LABEL[icon]}: 轮数={len(ts)}  间隔 {fmt_dist(iv)}"
              f"  （配置节拍 {cadence_cfg[icon]:.0f}s）")
        api_est[icon] = [max(v - cadence_cfg[icon], 0.0) for v in iv]
    print()

    # ---- 2. 有效动作吞吐 ----
    print("═══ 2. 有效动作吞吐（LLM 翻译后真正下发的指令）═══")
    issued = collections.Counter()
    blocked_tl = collections.Counter()   # 翻译层拦截（✗ 开头）
    skip_unchanged = collections.Counter()
    issued_descs = collections.defaultdict(list)  # (icon, abs, desc)
    rule_cmds = 0
    ok_receipts = 0
    fail_receipts = 0
    for r in win:
        t = r["text"]
        if r["kind"] == "cmd":
            if t.startswith("⚙"):
                rule_cmds += 1
            elif t.startswith("✓"):
                ok_receipts += 1
            elif t[:1] in ROLES:
                icon = t[0]
                desc = t[1:].strip()
                if desc.startswith("✗"):
                    blocked_tl[icon] += 1
                else:
                    if "未变，跳过" in desc:
                        skip_unchanged[icon] += 1
                    issued[icon] += 1
                    issued_descs[icon].append((r["abs"], desc))
        elif r["kind"] == "error" and t.startswith("✗"):
            fail_receipts += 1
    for icon in ("🔨", "⚔", "📋"):
        print(f"  {ROLE_LABEL[icon]}: 下发 {issued[icon]} 条 / {span_min:.1f} 分钟 "
              f"= {issued[icon]/span_min:.2f} 条/分钟"
              f"（其中未变跳过 {skip_unchanged[icon]} 条、翻译层拦截 {blocked_tl[icon]} 条）")
    print(f"  ⚙规则层: {rule_cmds} 条 = {rule_cmds/span_min:.2f} 条/分钟")
    print(f"  游戏回执: ✓ {ok_receipts} 条 ({ok_receipts/span_min:.2f}/分钟)，"
          f"✗ {fail_receipts} 条")
    print()

    # ---- 3. 浪费率 ----
    print("═══ 3. 浪费率 ═══")
    tot_receipts = ok_receipts + fail_receipts
    print(f"  3a. 失败回执占比: ✗ {fail_receipts}/{tot_receipts} "
          f"= {fail_receipts/tot_receipts*100 if tot_receipts else 0:.1f}%（activity 窗口）")
    fail_msgs = collections.Counter()
    for r in win:
        if r["kind"] == "error" and r["text"].startswith("✗"):
            fail_msgs[re.sub(r"\d+", "N", r["text"])[:60]] += 1
    for msg, c in fail_msgs.most_common(5):
        print(f"      ✗ x{c}: {msg}")
    print("  3b. 无动作轮次占比:")
    for icon in ("🔨", "⚔", "📋"):
        noact = sum(1 for t in round_texts[icon] if "无动作" in t)
        n = len(round_texts[icon])
        print(f"      {ROLE_LABEL[icon]}: {noact}/{n} = {noact/n*100 if n else 0:.0f}%")
    n_skip = sum(skip_unchanged.values())
    n_issued = sum(issued.values())
    print(f"  3c. 「未变，跳过」: {n_skip}/{n_issued} 条下发 "
          f"= {n_skip/n_issued*100 if n_issued else 0:.1f}%")
    print("  3d. 同一动作 60s 内重复下发 TOP5（按归一化动作、滑动窗内最大次数）:")
    rep = []
    for icon, items in issued_descs.items():
        by_key = collections.defaultdict(list)
        for abs_t, desc in items:
            by_key[norm_action(desc)].append((abs_t, desc))
        for key, occ in by_key.items():
            occ.sort()
            best, sample = 1, occ[0][1]
            for i, (a, d) in enumerate(occ):
                j = i
                while j < len(occ) and occ[j][0] - a <= 60:
                    j += 1
                if j - i > best:
                    best, sample = j - i, d
            if best > 1:
                rep.append((best, len(occ), icon, key, sample))
    for best, total, icon, key, sample in sorted(rep, reverse=True)[:5]:
        print(f"      {ROLE_LABEL[icon]} {sample[:50]!r}: 60s 内最多 x{best}，窗口内共 x{total}")
    if not rep:
        print("      （无 60s 内重复下发）")
    print()

    # ---- 4. LLM 调用耗时拆分 ----
    print("═══ 4. LLM 调用耗时（轮次间隔 - 配置节拍；≥0 截断）═══")
    print("  模型分层：🔨=deepseek-chat 单层；⚔=chat 起步、空转升 reasoner；📋=reasoner")
    for icon in ("🔨", "⚔", "📋"):
        est = sorted(api_est[icon])
        if not est:
            print(f"  {ROLE_LABEL[icon]}: 无样本")
            continue
        print(f"  {ROLE_LABEL[icon]}: 估计 API 耗时 中位={pct(est,.5):.1f}s "
              f"P25={pct(est,.25):.1f}s P75={pct(est,.75):.1f}s 最大={max(est):.1f}s")
    for icon, c in escalations.items():
        print(f"  {ROLE_LABEL[icon]} 「chat 空转，升级 reasoner」{c} 次"
              f"（这些轮含 2 次 API 调用，耗时约为双倍）")
    for icon, c in llm_fail.items():
        print(f"  {ROLE_LABEL[icon]} LLM 调用失败 {c} 次")
    print()

    # ---- 5. 规则层占比 ----
    print("═══ 5. 规则层 vs LLM 指令占比 ═══")
    tot = rule_cmds + n_issued
    print(f"  ⚙ {rule_cmds} 条 vs LLM(🔨⚔📋) {n_issued} 条 → "
          f"规则层占 {rule_cmds/tot*100 if tot else 0:.1f}%")
    print()

    return {
        "span_min": span_min, "rounds": rounds, "round_texts": round_texts,
        "api_est": api_est, "issued": issued, "escalations": escalations,
        "ok": ok_receipts, "fail": fail_receipts, "rule_cmds": rule_cmds,
        "skip_unchanged": skip_unchanged, "cadence": cadence_cfg,
    }


def analyze_agentlog(path, hours):
    """agent.log 最近 hours 小时窗口：cmd ok/FAIL 按类型统计。"""
    if not os.path.exists(path):
        print("（agent.log 不存在，跳过）")
        return None
    lines = open(path, encoding="utf-8", errors="replace").readlines()
    ts_re = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    cmd_re = re.compile(r"cmd (\S+) (ok|FAIL)(?::\s*(.*))?")
    n = len(lines)

    def tsec(i):
        m = ts_re.match(lines[i])
        if not m:
            return None
        return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                + int(m.group(3)) + int(m.group(4)) / 1000.0)

    # 绝对时间轴：顺序扫描，跨午夜（时间戳倒退 >12h）日偏移 +86400
    abs_ts = []          # 与 lines 对齐，无时间戳的行为 None
    day_off, prev = 0.0, None
    for i in range(n):
        t = tsec(i)
        if t is None:
            abs_ts.append(None)
            continue
        if prev is not None and t < prev - 12 * 3600:
            day_off += 86400.0
        prev = t
        abs_ts.append(t + day_off)
    valid = [(i, a) for i, a in enumerate(abs_ts) if a is not None]
    if not valid:
        return None
    last_i, last_abs = valid[-1]
    cut = last_abs - hours * 3600
    start_idx = next(i for i, a in valid if a >= cut)
    first_t = abs_ts[start_idx] % 86400
    last_t = last_abs % 86400
    seg = lines[start_idx:]
    dur_min = max((last_abs - abs_ts[start_idx]) / 60.0, 1e-9)

    ok_c, fail_c = collections.Counter(), collections.Counter()
    fail_msgs = collections.Counter()
    for l in seg:
        m = cmd_re.search(l)
        if not m:
            continue
        verb, res, msg = m.group(1), m.group(2), m.group(3) or ""
        if res == "ok":
            ok_c[verb] += 1
        else:
            fail_c[verb] += 1
            fail_msgs[re.sub(r"\d+", "N", f"{verb}: {msg}")[:70]] += 1
    ok_t, fail_t = sum(ok_c.values()), sum(fail_c.values())
    print(f"═══ 附：agent.log 最近 {hours}h 回执（游戏侧，含规则层+LLM 全部指令）═══")
    print(f"  窗口 {hms(first_t)} → {hms(last_t)}（{dur_min:.1f} 分钟）")
    print(f"  ✓ ok {ok_t} 条（{ok_t/dur_min:.2f}/分钟）  ✗ FAIL {fail_t} 条"
          f"（{fail_t/dur_min:.2f}/分钟）  失败率 {fail_t/(ok_t+fail_t)*100:.1f}%")
    verbs = sorted(set(ok_c) | set(fail_c))
    for v in verbs:
        o, f = ok_c[v], fail_c[v]
        print(f"    {v:12s} ok={o:4d} FAIL={f:4d}  失败率 {f/(o+f)*100 if o+f else 0:5.1f}%")
    print("  FAIL 原因 TOP5:")
    for msg, c in fail_msgs.most_common(5):
        print(f"    x{c}: {msg}")
    print()
    return {"ok": ok_t, "fail": fail_t, "dur_min": dur_min,
            "ok_c": ok_c, "fail_c": fail_c}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3)
    ap.add_argument("--activity", default=os.path.join(ROOT, "bridge", "activity.jsonl"))
    ap.add_argument("--agentlog", default=os.path.join(ROOT, "agent", "logs", "agent.log"))
    ap.add_argument("--config", default=os.path.join(ROOT, "bridge", "config.toml"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    print(f"配置节拍: build={cfg.get('build_interval_sec',10):.0f}s "
          f"combat={cfg.get('combat_interval_sec',6):.0f}s "
          f"commander={cfg.get('commander_interval_sec',50):.0f}s "
          f"fast={cfg.get('fast_interval_sec',2.5)}s\n")

    win, start, end = load_activity(args.activity, args.hours)
    if not win:
        print("activity.jsonl 无数据")
        return
    st = analyze_activity(win, start, end, cfg)
    ag = analyze_agentlog(args.agentlog, args.hours)

    # ---- 6. 结论 ----
    print("═══ 6. 结论（按数据）═══")
    parts = []
    for icon in ("🔨", "⚔", "📋"):
        est = sorted(st["api_est"][icon])
        if est:
            parts.append((pct(est, .5), f"{ROLE_LABEL[icon]} API 中位 {pct(est,.5):.0f}s/轮"))
    noact_total = sum(sum(1 for t in st["round_texts"][i] if "无动作" in t)
                      for i in ("🔨", "⚔", "📋"))
    rounds_total = sum(len(st["round_texts"][i]) for i in ("🔨", "⚔", "📋"))
    if rounds_total:
        parts.append((noact_total / rounds_total * 10,
                      f"空转轮 {noact_total}/{rounds_total} "
                      f"({noact_total/rounds_total*100:.0f}%)"))
    tot_r = st["ok"] + st["fail"]
    if tot_r:
        parts.append((st["fail"] / tot_r * 10,
                      f"失败回执率 {st['fail']}/{tot_r} ({st['fail']/tot_r*100:.0f}%)"))
    esc = sum(st["escalations"].values())
    if esc:
        parts.append((esc, f"chat 空转升 reasoner {esc} 次（双倍 API 耗时）"))
    parts.sort(reverse=True)
    print("  慢的主要成分排序: " + "；".join(p[1] for p in parts))
    med_api = max((pct(sorted(st["api_est"][i]), .5) for i in ("🔨", "⚔", "📋")
                   if st["api_est"][i]), default=0)
    if med_api >= st["cadence"]["⚔"]:
        print("  → 主因是 API 延迟：单次 LLM 调用耗时已超过节拍本身，"
              "轮次间隔被 API 拖长，不是节拍配置问题。")
    elif rounds_total and noact_total / rounds_total > 0.3:
        print("  → 主因是空转轮：LLM 大量轮次无实质动作，节拍在空耗。")
    if tot_r and st["fail"] / tot_r > 0.3:
        print("  → 失败重试占比过高：指令被游戏反复判失败，有效吞吐被吃掉。")
    if ag:
        print(f"  （3h 长窗口佐证：游戏侧失败率 "
              f"{ag['fail']/(ag['ok']+ag['fail'])*100:.1f}%，"
              f"其中 build FAIL {ag['fail_c'].get('build',0)} 次、"
              f"train FAIL {ag['fail_c'].get('train',0)} 次）")


if __name__ == "__main__":
    main()
