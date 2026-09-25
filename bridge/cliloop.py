#!/usr/bin/env python3
"""架构二期新决策循环（config cli_mode=true）：总指挥 CLI 慢层 + 确定性 2s 快层 + Jev 事件查询。

评审定论划界：
- 地基全保留：AgentLink 协议 / executor 工单调度与选址 / connmap / fog / powergrid。
- 决策层三层：总指挥 1 个 CLI 会话（60s+ 拍，只看大局出布局，产物 plan.md）→
  中层四席（陆/海/空/建造各 1 个 CLI 会话，25s 拍，产物 tasks/<branch>.json——本阶段 stub，
  cli_middle_enabled=false 先关）→ 快速执行层（executor 2s 拍裁剪段 + 谓词状态机 + Jev 事件查询）。
- 跨角色通信=留言板文件（board.md 追加段落），角色间不许直接对话。
- 本循环不接 reflex.py：撤退/保命判断全归 Jev（旧 multiagent 循环里的 reflex 不动）。
- Jev 是驾驶位不是顾问：领了 active 任务的小队由 Jev 连续开车（菜单题点菜，
  系统只翻译执行）；事件急查（受击/敌情突现）并入同一菜单，与直驾共用去重。
- 局文件夹 bridge/matches/<开局时间戳>/：state.json（2s 覆写）/board.md/plan.md/
  tasks/*.json（任务单）/logs/events.jsonl（状态迁移+事件）。换局=换文件夹。

注意：本模块严禁 import pilot（pilot 以 __main__ 运行，import 会重执行 main）。
"""
import json
import math
import os
import re
import threading
import time
from collections import Counter, deque
from types import SimpleNamespace

import danger as danger_mod
import executor as exec_mod
import fog
import multiagent  # 复用 squad_assign/_map_size_class/_sid_branch/_home（旧三循环本体不动）
import spawns

import cliseat
import jev as jev_mod
import opening
import predicates

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
MATCHES_DIR = os.path.join(BRIDGE_DIR, "matches")
TICK_SEC = 2.0
HP_WINDOW = 6.0      # 受击检测：6s 窗口血量跌 >15%
HP_DROP = 0.15
ENEMY_POP_R = 1100   # 敌情突现：新敌入队 1100px 内
JEV_CD = 20.0        # 每队 Jev 查询冷却（不盯梢：只在事件触发时问）
STUCK_SEC = 90.0     # 任务卡死阈值（active 无进展）
STUCK_JEV_CD = 120.0
# ---- Jev 直驾（config jev_drive=true，仅 cli_mode）----
JEV_DRIVE_ENEMY_R = 1600  # "有现场"半径：可见敌入队 1600px 内才问（平静的队不问，省 token/显存）
DRIVE_CMD_DEDUP = 25.0    # 同队同指令去重秒数（与旧循环 25s 同目标去重同口径）；
#                           驱动循环与事件流共用 cmd_dedup，同一拍不对同一队重复发指令
DRIVE_DOWN_LOG = 30.0     # jev_down 事件节流秒数
UNSURE_LOG_CD = 15.0      # unsure 日志节流：同队同结论 15s 内不重复记（events.jsonl/控制台）
TASK_CHAT_CD = 10.0       # 任务单状态迁移推聊天台节流：同任务 10s 内不重复推


# ---------- Jev 置信阈值（config 可配，仅 cli_mode 读；0.8B 档绝对置信度系统性偏低） ----------

def _th(ctx):
    """阈值包：菜单题=focus_top/focus_margin（地板+相对 margin）；engage_hi/lo 现仅用于
    卡死放弃题（noul）；retreat_hi 菜单化后不再被读（撤退并入菜单项，键保留备用）。"""
    cfg = ctx["cfg"]
    return {"engage_hi": float(cfg.get("jev_engage_hi", 0.60)),
            "engage_lo": float(cfg.get("jev_engage_lo", 0.40)),
            "retreat_hi": float(cfg.get("jev_retreat_hi", 0.60)),
            # retreat 的"留下"下沿取镜像（1-hi）：撤退题没有独立 lo 键
            "retreat_lo": 1.0 - float(cfg.get("jev_retreat_hi", 0.60)),
            "focus_top": float(cfg.get("jev_focus_top", 0.35)),
            "focus_margin": float(cfg.get("jev_focus_margin", 1.5))}


def _unsure_ok(match, key, now):
    """unsure 节流闸：同键 15s 内只放一次（events.jsonl/board/控制台共用）。"""
    last = match.unsure_ts.get(key, 0)
    if now - last < UNSURE_LOG_CD:
        return False
    match.unsure_ts[key] = now
    return True


# ---------- 游戏内聊天台推送（overlay chat 通道，与旧循环 multiagent._push_chat 同款） ----------

_CHAT_LAST = {}  # 各路限速 1s
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")  # 游戏字体无 emoji 字形（推送前剥掉）


def push_chat(ctx, who, text):
    """往游戏内聊天台推一行（NDJSON overlay chat 通道）。限速 1s/路，断连静默。"""
    now = time.time()
    if now - _CHAT_LAST.get(who, 0) < 1.0:
        return
    _CHAT_LAST[who] = now
    try:
        ctx["link"].send({"cmd": "overlay", "who": who, "chat": _EMOJI_RE.sub("", str(text))})
    except Exception:
        pass


def _push_task(ctx, match, ev):
    """任务单状态迁移推聊天台（who="任务"，同任务 10s 节流）。"""
    tid = ev.get("id")
    now = time.time()
    if now - match.task_chat_ts.get(tid, 0) < TASK_CHAT_CD:
        return
    match.task_chat_ts[tid] = now
    push_chat(ctx, "任务", f"{tid} {ev.get('from')}→{ev.get('to')}")


# ---------- 局文件夹读写 ----------

def post_board(match_dir, who, text):
    """留言板追加段落（跨角色唯一通信通道）。"""
    try:
        with open(os.path.join(match_dir, "board.md"), "a", encoding="utf-8") as fp:
            fp.write(f"\n## [{time.strftime('%H:%M:%S')}] {who}\n\n{text}\n")
    except OSError:
        pass


def _write_event(match, ev):
    ev.setdefault("ts", time.strftime("%H:%M:%S"))
    try:
        with open(os.path.join(match.dir, "logs", "events.jsonl"), "a", encoding="utf-8") as fp:
            fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass
    kind = {"task": "status", "task_chain": "status", "squad_wiped": "alert",
            "jev_retreat": "alert", "jev_down": "error", "pred_bad": "error"}.get(ev.get("ev"))
    if kind:
        match.log(kind, "⚙ " + json.dumps(ev, ensure_ascii=False)[:200])


# ---------- state.json 快照 ----------

def _state_json(ctx, match, snap, now):
    """压缩战局（JSON 不是散文；取材参照旧 _user_* 拼 prompt 的逻辑）。"""
    team = snap.get("team") or {}
    own = snap.get("own", [])
    ex = ctx["exec_holder"].get("ex")
    bld_done = Counter(u["t"] for u in own if u.get("bld") and u.get("prog", 1) >= 1)
    bld_prog = [{"t": u["t"], "x": u["x"], "y": u["y"], "prog": round(u.get("prog", 0), 2)}
                for u in own if u.get("bld") and u.get("prog", 1) < 1][:8]
    builders = [u for u in own if exec_mod._is_builder(u)]
    squads = []
    for sid, sq in sorted(match.squad_st.squads.items()):
        ms = [u for u in own if u["id"] in sq["members"]]
        if not ms:
            continue
        cx = sum(u["x"] for u in ms) / len(ms)
        cy = sum(u["y"] for u in ms) / len(ms)
        hp = sum(u["hp"] for u in ms)
        mhp = sum(u.get("maxHp", u["hp"]) for u in ms) or 1
        m = sq.get("mission") or {}
        squads.append({"id": sid, "n": len(ms), "hp_pct": round(hp / mhp, 2),
                       "pos": [int(cx), int(cy)], "comp": dict(Counter(u["t"] for u in ms)),
                       "mission": {"任务": m.get("任务"), "目标": m.get("目标")} if m else None})
    enemies = [{"t": e["t"], "x": e["x"], "y": e["y"], "hp": e.get("hp")}
               for e in snap.get("enemy", [])[:12]]
    pools = []
    for p in (match.map_msg.get("resources") or [])[:24]:
        if any(abs(u["x"] - p["x"]) < 40 and abs(u["y"] - p["y"]) < 40 for u in own if u.get("bld")):
            st = "ours"
        elif any(abs(e["x"] - p["x"]) < 200 and abs(e["y"] - p["y"]) < 200 for e in snap.get("enemy", [])):
            st = "hostile"
        elif match.danger_map.danger(p["x"], p["y"], snap):
            st = "hostile"  # 记忆威胁（敌人离开视野仍危险）
        else:
            st = "free"
        pools.append({"x": p["x"], "y": p["y"], "w": bool(p.get("w")), "state": st})
    fog_part = None
    fm = match.fog_map
    if fm.fresh():
        fr = fm.fracs()
        fog_part = {"mode": fm.mode_text(),
                    "fracs": {"可见": round(fr[0], 2), "浅黑": round(fr[1], 2),
                              "全黑": round(fr[2], 2)} if fr else None,
                    "black_zones": fm.black_zones(),
                    "ghosts": [{"t": g.get("t"), "x": int(g.get("x", 0)), "y": int(g.get("y", 0))}
                               for g in (fm.ghosts or [])[:8]]}
    wo = []
    if ex:
        wo = [{"action": t["action"], "slot": t.get("slot"), "status": t["status"],
               "note": t.get("note", "")[:60]} for t in ex.work_order[:16]]
    return {
        "ts": time.strftime("%H:%M:%S"), "tick": snap.get("tick", 0),
        "funds": round(team.get("funds", 0)),
        "income": team.get("income"), "expense": team.get("expense"),
        "home": [int(match.home_xy[0]), int(match.home_xy[1])] if match.home_xy else None,
        "buildings": {"done": dict(bld_done), "constructing": bld_prog},
        "builders": {"total": len(builders),
                     "idle": sum(1 for u in builders if not exec_mod._busy(u))},
        "squads": squads,
        "enemy_visible": enemies, "enemy_count": len(snap.get("enemy", [])),
        "pools": pools, "fog": fog_part,
        "econ_goal": dict(ex.econ_goal) if ex else None,
        "stance": ex.stance if ex else None,
        "composition": (ex.army_goal.get("composition") if ex else None),
        "work_order": wo,
        "wo_progress": getattr(ex, "wo_progress", None) if ex else None,
        "tasks": {tid: t.get("_status") for tid, t in match.engine.tasks.items()},
        "defeated": match.defeated,
    }


# ---------- 任务单 do 派发 ----------

def _do_fn(ctx, match, task):
    """激活执行任务单的 do 段。返回 False=条件不足留 pending（30s 节流记事件）。"""
    d = task.get("do") or {}
    ex = ctx["exec_holder"].get("ex")
    snap = match.world.snap if match.world else None
    if "work_order" in d and ex is not None:
        known = ctx.get("known_actions") or set()
        ok = []
        for t in (d.get("work_order") or [])[:24]:
            a = str((t or {}).get("action", ""))
            if not a:
                continue
            if a == "position":  # 阵地单：几何展开归 executor
                if isinstance((t or {}).get("anchor"), list):
                    ok.append({"action": "position", "kind": str(t.get("kind", "defense_mixed")),
                               "anchor": [float(t["anchor"][0]), float(t["anchor"][1])],
                               "toward": t.get("toward"), "count": t.get("count"),
                               "note": str(t.get("note", ""))})
                continue
            if not a.startswith(("b_", "u_", "c_")):
                a = "b_" + a
            if known and a not in known:
                _write_event(match, {"ev": "task_do_bad", "id": task.get("id"),
                                     "why": f"非法动作 {a} 已丢弃"})
                continue
            ok.append({"action": a, "slot": t.get("slot"), "note": str(t.get("note", ""))})
        if ok:
            with ex.lock:
                ex.set_work_order(ok, snap)
        task["_wo_done"] = 0  # 进展记账基线（工单 done 数）
        match.log("status", f"⚙ 任务单 {task.get('id')} 工单已下发（{len(ok)} 项）")
    if "plan_params" in d and ex is not None:
        with ex.lock:
            ex.apply_plan_params(d["plan_params"])
    comp = d.get("composition")
    rally = d.get("rally")
    if (comp or rally) and ex is not None:
        with ex.lock:
            ex.set_army_goal(composition=comp if isinstance(comp, dict) else None,
                             rally=rally if isinstance(rally, list) else None)
    if "mission" in d:
        return _do_mission(ctx, match, task, d["mission"], snap)
    if "board" in d:
        post_board(match.dir, f"任务单/{task.get('id')}", str(d["board"]))
    return True


def _do_mission(ctx, match, task, mission, snap):
    """小队任务：认领执行器（系统的活）——按军种挑无任务小队，一次性派发，不盯梢。"""
    branch = str(task.get("branch") or "land")
    sid = _claim_squad(match, branch)
    if sid is None:
        return False  # 无可用小队（兵未成型）：留 pending 下拍重试
    sq = match.squad_st.squads[sid]
    m = {"任务": str(mission.get("任务", "hold")), "编成": str(mission.get("编成", "")),
         "备注": str(mission.get("备注", "")), "目标": None}
    tgt = mission.get("目标")
    if isinstance(tgt, list) and len(tgt) == 2:
        m["目标"] = [float(tgt[0]), float(tgt[1])]
    sq["mission"] = m
    sq["mission_ts"] = time.time()
    task["squad"] = sid
    # 一次性派发（快层不盯梢，异常归 Jev/谓词报警）
    if snap and m["目标"] is not None and m["任务"] in ("attack", "harass", "hold", "defend"):
        ms = [u for u in snap.get("own", []) if u["id"] in sq["members"]]
        if ms:
            ids = ",".join(str(u["id"]) for u in ms)
            cmd = {"cmd": "attackmove" if m["任务"] in ("attack", "harass") else "move",
                   "x": float(m["目标"][0]), "y": float(m["目标"][1]), "units": ids}
            ctx["link"].send(cmd)
            # 进展记账基线：当前质心距目标
            cx = sum(u["x"] for u in ms) / len(ms)
            cy = sum(u["y"] for u in ms) / len(ms)
            task["_best_d"] = math.hypot(cx - m["目标"][0], cy - m["目标"][1])
    match.log("status", f"⚙ 任务单 {task.get('id')} 认领 {sid}: {m['任务']}"
                        + (f"→({int(m['目标'][0])},{int(m['目标'][1])})" if m["目标"] else ""))
    return True


def _claim_squad(match, branch):
    """按军种挑队：只认领无任务小队（有任务不抢，避免覆盖派发），人少的优先；
    军种绝不越界。无空闲小队返回 None（任务留 pending 等兵力扩充）。"""
    free = [(sid, sq) for sid, sq in match.squad_st.squads.items()
            if multiagent._sid_branch(sid) == branch and not sq.get("mission")]
    if not free:
        return None
    return min(free, key=lambda kv: len(kv[1]["members"]))[0]


# ---------- 任务进展记账（task_stuck 谓词的数据源） ----------

def _progress_tick(match, snap, now):
    ex = match.ex
    own = snap.get("own", [])
    for t in match.engine.tasks.values():
        if t.get("_status") != "active":
            continue
        d = t.get("do") or {}
        if "mission" in d and t.get("squad"):
            sq = match.squad_st.squads.get(t["squad"])
            tgt = (sq.get("mission") or {}).get("目标") if sq else None
            if sq and tgt:
                ms = [u for u in own if u["id"] in sq["members"]]
                if ms:
                    cx = sum(u["x"] for u in ms) / len(ms)
                    cy = sum(u["y"] for u in ms) / len(ms)
                    dist = math.hypot(cx - tgt[0], cy - tgt[1])
                    if dist < t.get("_best_d", 1e18) - 50:  # 逼近 50px 才算进展
                        t["_best_d"] = dist
                        t["_prog_ts"] = now
        if "work_order" in d and ex is not None:
            done = sum(1 for w in ex.work_order if w["status"] == "done")
            if done > t.get("_wo_done", 0):
                t["_wo_done"] = done
                t["_prog_ts"] = now


# ---------- Jev 事件查询（撤退/保命全归这里；不盯梢） ----------

def _jev_tick(ctx, match, snap, now):
    if match.jev is None or not match.jev.enabled or match.defeated:
        return
    own = snap.get("own", [])
    enemies = snap.get("enemy", [])
    enemy_ids = {e["id"] for e in enemies}
    for sid, sq in list(match.squad_st.squads.items()):
        ms = [u for u in own if u["id"] in sq["members"]]
        if not ms:
            continue
        hp = sum(u["hp"] for u in ms)
        hist = match.hp_hist.setdefault(sid, deque())
        hist.append((now, hp))
        while hist and now - hist[0][0] > HP_WINDOW:
            hist.popleft()
        reasons = []
        if len(hist) > 1 and hist[0][1] > 0:
            drop = (hist[0][1] - hp) / hist[0][1]
            if drop > HP_DROP:
                reasons.append(f"受击：血量{HP_WINDOW:.0f}s跌{drop * 100:.0f}%")
        cx = sum(u["x"] for u in ms) / len(ms)
        cy = sum(u["y"] for u in ms) / len(ms)
        pop = [e for e in enemies if e["id"] not in match.seen_enemies
               and (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2 < ENEMY_POP_R ** 2]
        if pop:
            reasons.append(f"敌情突现：{len(pop)}个新敌入队{ENEMY_POP_R}px内")
        if not reasons or now < match.jev_cd.get(sid, 0):
            continue
        match.jev_cd[sid] = now + JEV_CD
        push_chat(ctx, "警戒", f"{sid} {'；'.join(reasons)}")  # 受击/敌情突现上聊天台
        _jev_event_ask(ctx, match, sid, sq, ms, snap, "；".join(reasons), now)
    match.seen_enemies |= enemy_ids
    # 任务卡死（mission 类）：问 Jev 是否放弃
    for t in match.engine.tasks.values():
        if t.get("_status") != "active" or "mission" not in (t.get("do") or {}):
            continue
        last = t.get("_prog_ts") or t.get("_act_ts") or 0
        if now - last < STUCK_SEC or now < t.get("_jev_ts", 0):
            continue
        t["_jev_ts"] = now + STUCK_JEV_CD
        _ask_jev_stuck(ctx, match, t, now)


def _send_squad_cmd(ctx, match, sid, cmd, now):
    """小队指令统一下发口：同队同指令 25s 去重（直驾循环与事件流共用——同一拍/邻拍
    不对同一队重复发同目标指令，与旧循环 25s 同目标去重同口径）。units 不参与去重键。"""
    key = json.dumps({k: v for k, v in cmd.items() if k != "units"}, sort_keys=True)
    last = match.cmd_dedup.get(sid)
    if last and last[0] == key and now - last[1] < DRIVE_CMD_DEDUP:
        return False
    if ctx["link"].send(cmd):
        match.cmd_dedup[sid] = (key, now)
        return True
    return False


def _jev_event_ask(ctx, match, sid, sq, ms, snap, reason, now):
    """事件急查（受击/敌情突现）：同一套菜单题立即问（不等直驾节拍节拍）。
    取舍注记：顾问时代的"撤退 noul + 集火 choice"两题并入菜单（"撤退回家"就是菜单项）——
    只有一条问答/翻译/标定路径，比保留两套题省一份翻译逻辑与置信口径（用户定调"Jev 接取并执行"）。
    拿不准（不置信）要上板请中层裁决（15s 节流）；服务 down=不动+记事件。"""
    cx = sum(u["x"] for u in ms) / len(ms)
    cy = sum(u["y"] for u in ms) / len(ms)
    state, questions = _drive_menu(sid, sq, ms, snap, cx, cy, reason=reason)
    ans = match.jev.ask(state, questions)
    if ans is None:
        _write_event(match, {"ev": "jev_down", "squad": sid, "why": reason,
                             "act": "不动（服务不可用降级）"})
        return
    match.drive_mem[sid] = (state, ans, now)  # 与直驾共用缓存/去重（同 state 不重答）
    act, chat_txt, category = _drive_act(ctx, match, sid, sq, ms, ans, snap, now,
                                         event_why=reason)
    if chat_txt:
        push_chat(ctx, "Jev", chat_txt)
    ch = (ans.get("action") or {}).get("choice")
    print(f"[jev-event] {sid} 事件[{reason}] 菜单={ch} → {act}")
    _write_event(match, {"ev": "jev_event", "squad": sid, "choice": ch,
                         "act": act, "why": reason})


def _ask_jev_stuck(ctx, match, task, now):
    """任务卡死（active 超阈值无进展）→ Jev：放弃还是继续等。"""
    sid = task.get("squad") or "?"
    state = (f"任务单 {task.get('id')}（{task.get('task', '')}）已激活超过 "
             f"{STUCK_SEC:.0f}s 无进展。认领小队 {sid}。当前资金/兵力见系统快照不在此列。")
    ans = match.jev.ask(state, {"abort": {"type": "noul",
                                          "instructions": "该任务已长时间无进展，应该放弃(abort)吗？"}})
    if ans is None:
        _write_event(match, {"ev": "jev_down", "task": task.get("id"), "why": "任务卡死"})
        return
    a = ans.get("abort") or {}
    v = a.get("noul")
    th = _th(ctx)
    if v is None or jev_mod.unsure_noul(a, lo=th["engage_lo"], hi=th["engage_hi"]):
        if _unsure_ok(match, ("stuck", task.get("id")), now):
            post_board(match.dir, "jev", f"任务 {task.get('id')} 卡死，Jev 拿不准（noul={v}）——请中层裁决。")
            _write_event(match, {"ev": "jev_unsure", "task": task.get("id"), "noul": v})
        return
    if v >= th["engage_hi"]:
        task["_status"] = "aborted"
        _write_event(match, {"ev": "task", "id": task.get("id"), "from": "active",
                             "to": "aborted", "why": f"jev 判放弃(noul={v:.2f})"})
    else:
        task["_prog_ts"] = now  # 判继续：重置进展起点（下个 STUCK_SEC 再问）
        _write_event(match, {"ev": "jev_keep", "task": task.get("id"), "noul": round(v, 3)})


# ---------- Jev 驾驶位（config jev_drive=总开关，仅 cli_mode） ----------
# 用户定调"Jev 接取并执行"：领了 active 任务的小队由 Jev 连续开车。
# 问法=一题菜单（"系统出菜单 Jev 点菜"），翻译执行归系统（不自作主张）。

MENU_PUSH = "执行任务/推进"   # 朝 mission 目标（attack/harass=attackmove，hold/defend=move）
MENU_HOLD = "原地驻守"        # stop
MENU_RETREAT = "撤退回家"     # move 回主基地（撤退语义并入菜单，独立 retreat 题已删）
MENU_FOCUS = "集火"           # 前缀：集火<类型#id> → attack id（#id 尾码沿用）


def _drive_menu(sid, sq, ms, snap, cx, cy, reason=None):
    """驾驶位问答的紧凑战况串 + 菜单题。敌人按类型分组（类型×数量×均血×距离）。
    候选：三个基础项（推进/驻守/撤退）恒定 + 每个可见敌组一个"集火<类型#id>"——
    恒 ≥3 个候选（少于 3 个的菜单对 0.8B 档没有区分度）。
    state 串供 drive_mem 去重；事件急查时 reason 拼进 state（Jev 知道为什么被问）。"""
    comp = ",".join(f"{k}x{v}" for k, v in Counter(u["t"] for u in ms).most_common(6))
    hp = sum(u["hp"] for u in ms)
    mhp = sum(u.get("maxHp", u["hp"]) for u in ms) or 1
    m = sq.get("mission") or {}
    groups = {}
    for e in snap.get("enemy", []):
        groups.setdefault(e["t"], []).append(e)
    glines = []
    focuses = {}
    for t, es in sorted(groups.items(),
                        key=lambda kv: min((e["x"] - cx) ** 2 + (e["y"] - cy) ** 2 for e in kv[1])):
        ne = min(es, key=lambda e: (e["x"] - cx) ** 2 + (e["y"] - cy) ** 2)
        dm = math.hypot(ne["x"] - cx, ne["y"] - cy)
        ah = sum(e["hp"] for e in es) / max(1, sum(e.get("maxHp", e["hp"]) for e in es))
        glines.append(f"{t}x{len(es)}({ah:.0%}血,{dm:.0f}px)")
        if len(focuses) < 4:
            focuses[f"{MENU_FOCUS}{t}#{ne['id']}"] = f"{t}x{len(es)} 均血{ah:.0%} 距{dm:.0f}px"
    tgt = m.get("目标")
    tgt_txt = (f"→({int(tgt[0])},{int(tgt[1])}) 距{math.hypot(tgt[0] - cx, tgt[1] - cy):.0f}px"
               if tgt else "")
    criteria = {MENU_PUSH: (f"朝任务目标{tgt_txt or ''}推进/执行" if tgt
                            else "按任务意图推进/执行"),
                MENU_HOLD: "原地驻守，不主动行动"}
    criteria.update(focuses)
    criteria[MENU_RETREAT] = "撤回主基地保命"
    state = (f"本队 {sid}({len(ms)}员,{hp / mhp:.0%}血)@({cx:.0f},{cy:.0f}): {comp}。"
             f"任务: {m.get('任务', '无')}{tgt_txt}。"
             f"可见敌: {'; '.join(glines) or '无'}。"
             + (f"事件: {reason}。" if reason else ""))
    question = {"action": {"type": "choice",
                           "instructions": "下一步行动？（执行任务=朝任务目标推进；"
                                           "集火=盯住该组敌打；拿不准就选原地驻守）",
                           "criteria": criteria}}
    return state, question


def _drive_act(ctx, match, sid, sq, ms, ans, snap, now, event_why=None):
    """菜单答案 → 指令（系统只做翻译，不自作主张）。
    返回 (控制台描述, 聊天台文本 or None, 类别)——chat 文本只在实际发了指令时非 None
    （unsure/去重跳过/无目标都不推，聊天台不刷噪音）。
    event_why 非 None=事件急查路径：拿不准要上板请中层裁决（15s 节流）。"""
    ids = ",".join(str(u["id"]) for u in ms)
    a = (ans or {}).get("action") or {}
    th = _th(ctx)
    if jev_mod.unsure_choice(a, top=th["focus_top"], margin=th["focus_margin"]):
        # 不置信 → 维持上一动作；事件急查的拿不准上板（直驾的只记节流日志）
        if event_why and _unsure_ok(match, ("menu", sid), now):
            post_board(match.dir, "jev",
                       f"{sid} {event_why}，Jev 拿不准"
                       f"（{json.dumps(a.get('probabilities') or {}, ensure_ascii=False)}），"
                       "未动——请中层裁决。")
            _write_event(match, {"ev": "jev_unsure", "squad": sid,
                                 "choice": a.get("choice"), "why": event_why})
        return f"unsure(choice={a.get('choice')})→维持上一动作", None, "unsure"
    ch = str(a.get("choice") or "")
    if ch == MENU_RETREAT:
        if not match.home_xy:
            return "菜单撤退→无家坐标，不动", None, "noop"
        hx, hy = match.home_xy
        sent = _send_squad_cmd(ctx, match, sid,
                               {"cmd": "move", "x": float(hx), "y": float(hy), "units": ids}, now)
        if sent:
            match.log("alert", f"⚔ Jev 撤退：{sid}（菜单）→ 回主基地({hx},{hy})")
        return "菜单撤退→回家" + ("" if sent else "（去重跳过）"), \
            f"{sid} 撤回家" if sent else None, "retreat"
    if ch == MENU_HOLD:
        sent = _send_squad_cmd(ctx, match, sid, {"cmd": "stop", "units": ids}, now)
        return "菜单驻守→stop" + ("" if sent else "（去重跳过）"), \
            f"{sid} 原地驻守" if sent else None, "stop"
    pick = re.search(r"#(\d+)$", ch) if ch.startswith(MENU_FOCUS) else None
    if pick:
        sent = _send_squad_cmd(ctx, match, sid,
                               {"cmd": "attack", "id": int(pick.group(1)), "units": ids}, now)
        if sent:
            match.log("cmd", f"⚔ Jev 集火：{sid} → {ch}")
        return f"菜单集火→attack {ch}" + ("" if sent else "（去重跳过）"), \
            f"{sid} 打 {ch}" if sent else None, "attack"
    if ch == MENU_PUSH:
        m = sq.get("mission") or {}
        tgt = m.get("目标")
        if not tgt:
            return "菜单推进→无任务目标点，不动", None, "noop"
        verb = "attackmove" if m.get("任务") in ("attack", "harass") else "move"
        sent = _send_squad_cmd(ctx, match, sid,
                               {"cmd": verb, "x": float(tgt[0]), "y": float(tgt[1]),
                                "units": ids}, now)
        chat_txt = (f"{sid} 压向 ({int(tgt[0])},{int(tgt[1])})" if verb == "attackmove"
                    else f"{sid} 前往 ({int(tgt[0])},{int(tgt[1])})")
        return (f"菜单推进→{verb}({int(tgt[0])},{int(tgt[1])})" + ("" if sent else "（去重跳过）"),
                chat_txt if sent else None, "push")
    return f"未知选项({ch})→维持（幻觉兜底）", None, "unsure"


def _jev_drive_tick(ctx, match, snap, now):
    """Jev 驾驶位循环：每 jev_drive_interval_sec（默认 3s）一拍。
    接敌（1600px 内有可见敌）=每拍问；行军/驻防（有任务无近敌）=每 jev_drive_calm_every
    拍（默认 5=15s）或 state 变了才问（drive_mem 去重）；没领任务的小队不问。"""
    interval = float(ctx["cfg"].get("jev_drive_interval_sec", 3.0))
    if now - match.drive_last < interval:
        return
    match.drive_last = now
    calm_sec = interval * float(ctx["cfg"].get("jev_drive_calm_every", 5))
    own = snap.get("own", [])
    enemies = snap.get("enemy", [])
    for sid, sq in list(match.squad_st.squads.items()):
        ms = [u for u in own if u["id"] in sq["members"]]
        if not ms:
            continue
        m = sq.get("mission")
        if not m:
            continue  # 没领任务的队不问（认领/分配归系统，Jev 只开领了任务的）
        cx = sum(u["x"] for u in ms) / len(ms)
        cy = sum(u["y"] for u in ms) / len(ms)
        engaged = any((e["x"] - cx) ** 2 + (e["y"] - cy) ** 2 <= JEV_DRIVE_ENEMY_R ** 2
                      for e in enemies)
        state, questions = _drive_menu(sid, sq, ms, snap, cx, cy)
        mem = match.drive_mem.get(sid)
        if not engaged and mem and mem[0] == state and now - mem[2] < calm_sec:
            continue  # 平静队：state 未变且心跳未到点
        # 接敌队每拍问（敌动=state 变=新现场）；平静队到心跳点或 state 变才问
        ans = match.jev.ask(state, questions)
        if ans is None:
            if now - match.drive_down > DRIVE_DOWN_LOG:
                match.drive_down = now
                _write_event(match, {"ev": "jev_down", "src": "drive",
                                     "act": "本轮跳过（服务不可用）"})
            continue
        match.drive_mem[sid] = (state, ans, now)
        act, chat_txt, category = _drive_act(ctx, match, sid, sq, ms, ans, snap, now)
        if chat_txt:
            push_chat(ctx, "Jev", chat_txt)  # 实际执行动作才推聊天台（unsure/去重不推）
        # unsure 且结论连续相同 → 控制台/事件流 15s 节流
        if category == "unsure" and not _unsure_ok(match, ("drive-unsure", sid), now):
            continue
        ch = (ans.get("action") or {}).get("choice")
        print(f"[jev-drive] {sid} 菜单={ch} → {act}")
        _write_event(match, {"ev": "jev_drive", "squad": sid, "choice": ch, "act": act})


# ---------- 对局装配 ----------

def _setup_match(ctx, gen):
    """新对局：建局文件夹 + 重建执行内核/迷雾/威胁/编队 + 开局任务单物化。"""
    link = ctx["link"]
    map_msg = link.map_message()
    if not map_msg:
        return None
    snap = link.snapshot()
    if not snap:
        return None  # 快照没到，下拍重试
    # 开局物化等快照有效性闸：map 消息先到、单位快照后进——快照里没有我方 CC（或任意
    # 我方建筑）就下拍重试。否则开局工单在热身期物化：安全池/槽位全冷启动（首拍
    # safe_pools=0），pool 单会撞热身期（现虽有 45s 宽限兜底，能不等就不等）
    if not any(u.get("bld") for u in snap.get("own", [])):
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    mdir = os.path.join(MATCHES_DIR, ts)
    os.makedirs(os.path.join(mdir, "tasks"), exist_ok=True)
    os.makedirs(os.path.join(mdir, "logs"), exist_ok=True)
    match = SimpleNamespace(
        gen=gen, dir=mdir, map_msg=map_msg, log=ctx["log_activity"],
        fog_map=fog.FogMap(map_msg), danger_map=danger_mod.DangerMap(),
        squad_st=SimpleNamespace(squads={}, move_map={}, mission_stale=False),
        wiped=set(), hp_hist={}, seen_enemies=set(), jev_cd={},
        drive_last=0.0, drive_mem={}, cmd_dedup={}, drive_down=0.0,
        unsure_ts={}, task_chat_ts={},
        defeated=False, defeated_since=None, home_xy=None, world=None, ex=None,
        jev=None, seats=[], engine=None)
    match.squad_st.move_map = {u["name"]: u.get("move")
                               for u in (ctx.get("pedia") or {}).get("units", [])}
    # 出生点/威胁种子
    own_cc0 = next((u for u in snap.get("own", []) if u["t"] == "commandCenter"), None)
    mine, enemies = spawns.friendly_spawns(
        map_msg.get("path", ""), (own_cc0["x"], own_cc0["y"]) if own_cc0 else None)
    match.spawns = (mine, enemies or [])
    if enemies:
        match.danger_map.seed_bases(enemies)
    match.danger_map.fog = match.fog_map
    # 执行内核重建（cli 裁剪段；旧路径语义不动）
    ex = exec_mod.Executor(map_msg, ctx["pedia"], ctx["log_activity"])
    ex.autopilot = False
    ex.routine = True          # 2s 拍走 _routine_tick（cli_mode 裁剪）
    ex.cli_mode = True
    ex.auto_production = True  # ⑦ 显式构成生产保留
    ex.dmap = match.danger_map
    ex.control.fog = match.fog_map
    ctx["exec_holder"]["ex"] = ex
    match.ex = ex
    # 谓词引擎 + 任务单状态机（状态迁移同步推聊天台：who="任务"，同任务 10s 节流）
    match.engine = predicates.TaskEngine(
        os.path.join(mdir, "tasks"),
        do_fn=lambda t: _do_fn(ctx, match, t),
        event_fn=lambda ev: (_write_event(match, ev),
                             _push_task(ctx, match, ev) if ev.get("ev") == "task" else None))
    # Jev 客户端
    cfg = ctx["cfg"]
    if cfg.get("jev_enabled", True):
        match.jev = jev_mod.Jev(cfg.get("jev_url", "http://127.0.0.1:8009"), enabled=True)
    # 开局任务单
    tw = (map_msg.get("tilesW", 0) or 0) * (map_msg.get("tileW", 0) or 0)
    th = (map_msg.get("tilesH", 0) or 0) * (map_msg.get("tileH", 0) or 0)
    size_cls, _hint = multiagent._map_size_class(tw, th)
    op = opening.generate_opening(snap, ctx.get("pedia"), match.spawns,
                                  ex.is_naval_map(), size_cls, map_msg)
    if op:
        _write_opening(ctx, match, op, size_cls)
    try:  # 参考资料：ref/units.md（席位合法 ID 词汇表）
        _write_units_ref(mdir, ctx.get("pedia"))
    except Exception as e:
        print(f"[cliloop] ref/units.md 写入失败: {e}")
    # CLI 席位：总指挥一席（60s 拍）+ 中层四席 stub（配置驱动，默认关）
    def on_plan(text, params):
        if params and match.ex is not None:
            with match.ex.lock:
                match.ex.apply_plan_params(params)
            _write_event(match, {"ev": "plan_params", "params": params})
        post_board(match.dir, "commander", "plan.md 已更新"
                   + (f"，参数块已消费: {json.dumps(params, ensure_ascii=False)}" if params else "（无参数块）"))
        ds = ctx.get("dash_st")  # 面板同步（cli 最小接入：计划书/参数/时间戳可读）
        if ds is not None:
            ds.plan, ds.plan_params = text, params or {}
            ds.plan_ts = time.time()
    chat_fn = lambda who, txt: push_chat(ctx, who, txt)  # AI 的嘴：游戏内聊天台
    ai_on = ctx.get("ai_enabled") or (lambda: True)
    match.seats.append(cliseat.Seat("commander", mdir, cfg,
                                    event_fn=lambda ev: _write_event(match, ev),
                                    board_fn=lambda who, txt: post_board(mdir, who, txt),
                                    on_plan=on_plan, ai_enabled=ai_on, chat_fn=chat_fn))
    for br in ("land", "navy", "air", "build"):
        match.seats.append(cliseat.Seat(br, mdir, cfg,
                                        event_fn=lambda ev: _write_event(match, ev),
                                        board_fn=lambda who, txt: post_board(mdir, who, txt),
                                        ai_enabled=ai_on, chat_fn=chat_fn))
    # 面板状态同步（cli 最小接入）：地图/迷雾/小队/阵亡随局换绑
    ds = ctx.get("dash_st")
    if ds is not None:
        ds.map_msg = map_msg
        ds.generation = gen
        ds.squads = match.squad_st.squads
        ds.fog_map = match.fog_map
        ds.defeated = False
        ds.plan = ""      # 新局清旧计划书（总指挥首拍重写）
        ds.plan_params = {}
        ds.plan_ts = 0.0
    print(f"[cliloop] 新对局 {mdir}（图 {tw:.0f}x{th:.0f} {size_cls}）")
    print(f"[cliloop] 手动验证路径：state.json 每 2s 覆写；tasks/opening.json 已被消费见 "
          f"logs/events.jsonl 与 executor 工单；总指挥每 {cfg.get('commander_interval_sec', 60)}s "
          f"更新 plan.md（需 {cfg.get('kimi_bin', 'kimi')} 在 PATH）")
    ctx["log_activity"]("status", f"⚙ 架构二期新对局：{mdir}")
    return match


def _write_units_ref(match_dir, pedia):
    """局文件夹参考资料 ref/units.md：紧凑单位手册（生产者/建造者 → 可产动作清单+价格，
    本局 pedia 快照；口径与 pilot.pedia_production_table 同源：builds 字段分组 b_/u_/升级）。
    席位 prompt 指认它为合法 ID 唯一来源（治 LLM 编造动作 ID）。"""
    units = (pedia or {}).get("units", [])
    price = {u["name"]: u.get("price") for u in units}
    lines = ["# 单位手册（本局 pedia 快照）", "",
             "用法：composition/生产用 u_ 动作 ID；work_order/建造用 b_ 动作 ID；升级动作用于对建筑 train。",
             "只用本表列出的 ID，别发明；ID 区分大小写。", ""]
    for u in units:
        builds = list(dict.fromkeys(u.get("builds", [])))  # 去重保序
        if not builds:
            continue
        lines.append(f"## {u['name']}（${u.get('price', '?')}）")
        for b in builds[:24]:
            bt = b[2:] if b.startswith(("b_", "u_")) else b
            p = price.get(bt)
            lines.append(f"- {b}" + (f"（${p}）" if p is not None else ""))
        lines.append("")
    ref = os.path.join(match_dir, "ref")
    os.makedirs(ref, exist_ok=True)
    with open(os.path.join(ref, "units.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))


def _write_opening(ctx, match, op, size_cls):
    """开局物化：写 tasks/opening.json + plan.md 种子 + board 首段；执行由状态机激活。"""
    tasks = [
        {"id": "op-econ", "branch": "build", "task": "开局经济链（工单+经济目标）",
         "do": {"work_order": op["work_order"], "plan_params": op["plan_params"]},
         "status": "pending"},
    ]
    # 构成/集结任务只在开局表显式给了构成时才挂（v2 起开局表不带兵——造什么兵归 AI 层）
    if op["composition"]:
        tasks.append(
            {"id": "op-army", "branch": "land", "task": "开局构成+集结点",
             "do": {"composition": op["composition"],
                    "rally": list(op["plan_params"]["主攻"]) if op["plan_params"].get("主攻") else None},
             "status": "pending"})
    for tpl in op["mission_templates"]:
        # mission 任务无激活门槛：认领不到小队（兵未成型）时 do_fn 返回 False 留 pending 重试；
        # 激活后保持 active（when 空=不自动完成）——完成/中止归 Jev 卡死查询或中层改派
        tasks.append({
            "id": f"op-mission-{tpl['branch']}-{tpl['seq']}", "branch": tpl["branch"],
            "squad": "auto", "task": tpl["mission"].get("备注", "开局任务"),
            "do": {"mission": tpl["mission"]},
            "trigger": {"when": [], "then": None},
            "status": "pending"})
    with open(os.path.join(match.dir, "tasks", "opening.json"), "w", encoding="utf-8") as fp:
        json.dump({"kind": "opening", "size_class": size_cls, "tasks": tasks},
                  fp, ensure_ascii=False, indent=1)
    with open(os.path.join(match.dir, "plan.md"), "w", encoding="utf-8") as fp:
        fp.write(op["plan_text"] + "\n")
    post_board(match.dir, "系统", "开局任务单已挂载（确定性生成，总指挥上线后修订）。")
    match.log("status", f"⚙ 开局任务单已挂载（{len(tasks)} 项，{size_cls}）")


# ---------- 主循环 ----------

def _update_defeated(ctx, match, snap, now):
    """阵亡判定：own 全空 15s（与旧循环同口径）；新对局由 _setup_match 重建清旗。"""
    if snap.get("own"):
        match.defeated_since = None
        match.defeated = False
        return
    if match.defeated_since is None:
        match.defeated_since = now
        return
    if not match.defeated and now - match.defeated_since >= 15.0:
        match.defeated = True
        ds = ctx.get("dash_st")
        if ds is not None:
            ds.defeated = True
        post_board(match.dir, "系统", "☠ 已阵亡：全部单位与建筑损失，席位与 Jev 转入待机")
        ctx["log_activity"]("alert", "☠ 已阵亡：cliloop 转入待机（新对局自动恢复）")


def _tick(ctx, match, now):
    link = ctx["link"]
    snap = link.snapshot()
    # 回执喂执行层学习（失败封禁/可用表——事件触发不归 LLM 节拍）
    res = link.drain_cmd_results()
    if res and match.ex is not None:
        with match.ex.lock:
            match.ex.on_cmd_results(res)
    fog_snap = getattr(link, "fog_snap", None)
    if fog_snap or snap:
        match.fog_map.update(fog_snap or snap)
    if not snap or not snap.get("team") or snap["team"].get("ai"):
        return
    home = multiagent._home(snap)
    match.home_xy = (home["x"], home["y"]) if home else None
    match.danger_map.update(snap, match.home_xy)
    _update_defeated(ctx, match, snap, now)
    if not snap.get("delegated"):
        return
    # 小队归队（单位分配是系统的活：编队/认领执行器，中层只调参数）
    before = set(match.squad_st.squads)
    multiagent.squad_assign({"cfg": ctx["cfg"], "log_activity": ctx["log_activity"]},
                            match.squad_st, snap)
    for sid in before - set(match.squad_st.squads):
        match.wiped.add(sid)
        _write_event(match, {"ev": "squad_wiped", "squad": sid})
    # 谓词世界视图（每拍重建）
    match.world = SimpleNamespace(snap=snap, squads=match.squad_st.squads,
                                  wiped=match.wiped, tasks=match.engine.tasks,
                                  home=match.home_xy, now=now)
    # 任务单状态机（2s 拍，零 LLM）
    match.engine.tick(match.world, now)
    _progress_tick(match, snap, now)
    # state.json 覆写（席位/面板/人工共用观察面）
    try:
        st = _state_json(ctx, match, snap, now)
        with open(os.path.join(match.dir, "state.json"), "w", encoding="utf-8") as fp:
            json.dump(st, fp, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[cliloop] state.json 写入异常: {e}")
    # Jev 事件查询 + CLI 席位
    _jev_tick(ctx, match, snap, now)
    # Jev 直驾（config jev_drive=true）：有现场的小队每 3s 问一轮三题并翻译执行
    if (match.jev is not None and match.jev.enabled and ctx["cfg"].get("jev_drive")
            and not match.defeated and (ctx.get("ai_enabled") or (lambda: True))()):
        _jev_drive_tick(ctx, match, snap, now)
    for seat in match.seats:
        seat.maybe_fire(now)
    # 玩家留言 → 留言板（优先级最高的外部输入）
    while True:
        with link.lock:
            if not link.user_msgs:
                break
            msg = link.user_msgs.popleft()
        post_board(match.dir, "玩家", msg)
        ctx["log_activity"]("chat", f"💬 玩家: {msg}")


def run(ctx):
    """cliloop 主循环（阻塞）。pilot cli_mode=true 时替代 multiagent.run。"""
    link = ctx["link"]
    os.makedirs(MATCHES_DIR, exist_ok=True)
    print(f"[cliloop] 架构二期新循环启动：局文件夹 {MATCHES_DIR}")
    print("[cliloop] 快层=确定性 2s 拍（executor 裁剪段+谓词状态机）+ Jev 驾驶位（菜单题）；"
          "慢层=总指挥/中层 CLI（spawn 模式）")
    # 监控面板（cli 最小接入，cfg["dashboard"] 闸照旧）：能打开、活动流/战况/计划书可读；
    # dash_st 是轻量命名空间，换局由 _setup_match 换绑（地图/迷雾/小队/计划）
    dash_st = SimpleNamespace(
        lock=threading.Lock(), map_msg=None, generation=0, squads={},
        plan="", plan_params={}, plan_ts=0.0, defeated=False,
        fog_map=fog.FogMap(None),  # 空迷雾模型（非 None，面板 /api/map 开局前不崩）
        move_map={u["name"]: u.get("move") for u in (ctx.get("pedia") or {}).get("units", [])},
        bgroups={},
        broadcast=lambda text: ctx["log_activity"]("chat", f"🖥 面板: {text}"))
    ctx["dash_st"] = dash_st
    try:
        import dashboard
        dashboard.start(ctx, dash_st)
    except Exception as e:
        print(f"[dashboard] 启动失败（不影响桥接）: {e}")
    match = None
    while True:
        mv = getattr(link, "map_version", 0)
        if match is None or mv != match.gen:
            m = _setup_match(ctx, mv)
            if m is None:
                time.sleep(1.0)
                continue
            match = m
        try:
            _tick(ctx, match, time.time())
        except Exception as e:
            print(f"[cliloop] tick 异常: {e}")  # tick 异常要暴露，不能裸吞
        time.sleep(TICK_SEC)
