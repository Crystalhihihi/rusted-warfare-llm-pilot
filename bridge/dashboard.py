# -*- coding: utf-8 -*-
"""外部监控面板（总控台）：内嵌在桥接进程里的 HTTP 服务，纯标准库。

第一屏=三角色在想什么：总指挥《作战计划书》全文+参数块、建造指挥《建造工单》进度、
战斗指挥《小队任务书》；支持发高层指令（走玩家留言同款 broadcast 通道，优先级最高）、
三角色启停开关（覆盖层：缺省跟随游戏内设置面板，面板可强制开/关/恢复跟随）。

由 multiagent.run() 拉起（daemon 线程，随桥接退出）。config.toml：
  dashboard = true        # 开关
  dashboard_port = 8321   # 监听 127.0.0.1，浏览器打开 http://127.0.0.1:8321
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ACTIVITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.jsonl")

# 开关覆盖层：None=跟随游戏内设置面板；True/False=面板强制（改 ctx 里的判定函数即生效）
_overrides = {"enabled": None, "commander": None, "build": None, "combat": None,
              "land": None, "air": None, "navy": None}  # 军种中层（无游戏面板键，视同 cmdCombat）
_ROLE_PANEL_KEY = {"commander": "cmdCommander", "build": "cmdBuild", "combat": "cmdCombat",
                   "land": None, "air": None, "navy": None}

# 游戏字体无 emoji 字形，推游戏聊天台前剥掉（与 multiagent._push_chat 同规）
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")


def start(ctx, st):
    """包开关判定 + 起 HTTP 服务（daemon）。返回 server；cfg 关闭则返回 None。"""
    cfg = ctx.get("cfg") or {}
    if not cfg.get("dashboard", True):
        return None
    port = int(cfg.get("dashboard_port", 8321))

    orig_role = ctx.get("role_enabled")
    if callable(orig_role):
        def _role_wrapped(name, _orig=orig_role):
            ov = _overrides.get(name)
            return _orig(name) if ov is None else bool(ov)
        ctx["role_enabled"] = _role_wrapped
    orig_ai = ctx.get("ai_enabled")
    if callable(orig_ai):
        def _ai_wrapped(_orig=orig_ai):
            ov = _overrides.get("enabled")
            return _orig() if ov is None else bool(ov)
        ctx["ai_enabled"] = _ai_wrapped

    handler = _make_handler(ctx, st)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="rw-dashboard").start()
    ctx["log_activity"]("status", f"🖥 监控面板已上线 http://127.0.0.1:{port}")
    print(f"[dashboard] 监控面板 http://127.0.0.1:{port}")
    return srv


# ---------- 状态采集 ----------

def _state(ctx, st):
    link = ctx["link"]
    snap = link.snapshot() or {}
    team = snap.get("team") or {}
    ex = (ctx.get("exec_holder") or {}).get("ex")

    game = None
    if snap:
        game = {
            "tick": snap.get("tick", 0),
            "time_sec": int((snap.get("timeMs", 0) or 0) / 1000),
            "funds": team.get("funds"),
            "income": team.get("income"),
            "expense": team.get("expense"),
            "ai_controlled": bool(team.get("ai")),
            "own": len(snap.get("own", []) or []),
            "allies": len(snap.get("allies", []) or []),
            "enemy_visible": len(snap.get("enemy", []) or []),
            "delegated": len(snap.get("delegated", []) or []),
            "map": os.path.basename((st.map_msg or {}).get("path", "") or ""),
            "generation": st.generation,
        }

    wo = []
    if ex:
        for t in list(getattr(ex, "work_order", []) or []):
            wo.append({k: t.get(k) for k in ("idx", "action", "slot", "note", "status", "x", "y")})
    build = {"version": getattr(ex, "wo_version", 0) if ex else 0,
             "builders_target": (ex.econ_goal.get("builders") if ex else None),
             "tasks": wo}

    with st.lock:  # members 是 set，squad manager 会并发改，锁内取快照
        squad_items = [(sid, (m.copy() if (m := sq.get("mission")) else None),
                        len(sq.get("members") or ())) for sid, sq in st.squads.items()]
    squads = [{"id": sid, "members": n, "mission": m} for sid, m, n in squad_items]

    return {"ok": True, "ts": time.strftime("%H:%M:%S"),
            "defeated": bool(getattr(st, "defeated", False)),
            "mode": ("cli" if getattr(st, "cli_match", None) is not None else "legacy"),
            "cli": _cli_payload(st),
            "game": game,
            "commander": {"plan": st.plan or "", "params": st.plan_params or {},
                          "ts": st.plan_ts, "age_sec": (int(time.time() - st.plan_ts) if st.plan_ts else None)},
            "build": build,
            "combat": {"squads": squads},
            "switches": _switches(ctx),
            "activity": _activity_tail(30)}


def _cli_payload(st):
    """cli 指挥舱数据源（cliloop 活局直读）：席位/任务单/事件流/board 尾部。
    无 cli_match（旧循环或主菜单）返回 None，前端走 legacy 卡集。"""
    cm = getattr(st, "cli_match", None)
    if cm is None:
        return None
    now = time.time()
    seats = []
    for s in list(getattr(cm, "seats", []) or []):
        nxt = None
        if s.enabled and not s.broken:
            nxt = max(0, int(s.interval - (now - s.last)))
        seats.append({"name": s.name, "enabled": bool(s.enabled), "busy": bool(s.busy),
                      "broken": bool(s.broken), "next_in": nxt, "interval": s.interval,
                      "session": bool(s.sid), "last_out": s.last_out or ""})
    tasks = []
    eng = getattr(cm, "engine", None)
    if eng is not None:
        for tid, t in list(eng.tasks.items()):
            trig = ((t.get("trigger") or {}).get("when")) or []
            tasks.append({"id": tid, "branch": t.get("branch", ""),
                          "task": str(t.get("task") or "")[:40],
                          "status": t.get("_status"),
                          "squad": t.get("squad") or "",
                          "when": " & ".join(str(p)[:26] for p in trig)[:80],
                          "note": str(t.get("note") or "")[:60]})
    board = ""
    try:
        with open(os.path.join(cm.dir, "board.md"), encoding="utf-8") as fp:
            lines = [ln.rstrip() for ln in fp.readlines() if ln.strip()]
        board = "\n".join(lines[-14:])
    except OSError:
        pass
    evs = list(getattr(cm, "ev_tail", []) or [])
    jev = [e for e in evs if str(e.get("ev", "")).startswith("jev")]
    task_evs = [e for e in evs if str(e.get("ev", "")).startswith(("task", "plan", "seat", "squad", "opening", "pred"))]
    return {"match": os.path.basename(getattr(cm, "dir", "") or ""),
            "muted": not bool(getattr(cm, "seats", None)),
            "seats": seats, "tasks": tasks, "board": board,
            "jev": jev[-14:], "events": task_evs[-14:]}


def _switches(ctx):
    try:
        panel = ctx["link"].ai_control_state() or {}
    except Exception:
        panel = {}

    def one(panel_on, ov):
        return {"panel": bool(panel_on), "override": ov,
                "effective": (bool(panel_on) if ov is None else bool(ov))}

    return {
        "all": one(panel.get("enabled", True), _overrides.get("enabled")),
        "roles": {name: one(panel.get(key or "cmdCombat", True), _overrides.get(name))
                  for name, key in _ROLE_PANEL_KEY.items()},
    }


def _map_state(ctx, st, full=False):
    """地图+战场图层负载（前端 2.5s 轮询）：单位=快照原样（敌人已被 agent 迷雾过滤，即 AI 所见）。
    地形行沉重，仅 full=True（首载/换图）时下发，前端按 generation 缓存。"""
    fm = st.fog_map
    m = st.map_msg or {}
    snap = ctx["link"].snapshot() or {}
    home = None
    for u in snap.get("own", []):
        if u.get("t") == "commandCenter" and u.get("bld"):
            home = [u["x"], u["y"]]
            break
    fresh = fm.fresh()
    from multiagent import _branch_of as _bof  # 军种着色（陆0/空1/海2；模块级循环安全：run() 先完成加载）
    move_map = getattr(st, "move_map", {})
    bidx = {"land": 0, "air": 1, "navy": 2}
    return {"ok": True, "generation": st.generation,
            "rows": (m.get("rows", []) if full else None),
            "resources": (m.get("resources", []) if full else None),
            "tileW": m.get("tileW", 20), "tileH": m.get("tileH", 20),
            "w": m.get("tilesW") or 0, "h": m.get("tilesH") or 0,
            "fog": fm.raw if fresh else None,
            "modeText": fm.mode_text() if fm.mode else None,
            "ghosts": fm.ghosts if fresh else [],
            "home": home,
            "units": {
                "own": [[u.get("x", 0), u.get("y", 0), 1 if u.get("bld") else 0,
                         bidx.get(_bof(u.get("t", ""), move_map), 0)]
                        for u in snap.get("own", [])],
                "allies": [[u.get("x", 0), u.get("y", 0)] for u in snap.get("allies", [])],
                "enemy": [[u.get("x", 0), u.get("y", 0)] for u in snap.get("enemy", [])],
            }}


def _bchat(ctx, st, body=None):
    """建造小队 chat 状态与参数：GET 读（开关/每组上限/各组成员数+最近动作+回退态）；
    POST 改 enabled/cap（运行中改下拍生效）。"""
    import multiagent as ma  # 组名中文化
    cfg = ctx["cfg"]
    if body is not None:
        v = body.get("cap")
        if isinstance(v, int) and 1 <= v <= 12:
            cfg["builder_chat_cap"] = v
            ctx["log_activity"]("status", f"🖥 面板调整建造组上限: {v}")
        en = body.get("enabled")
        if isinstance(en, bool):
            cfg["builder_chat_enabled"] = en
            ctx["log_activity"]("status", f"🖥 面板{'开启' if en else '关闭'}建造小队")
    groups = {}
    for gid, bg in (st.bgroups or {}).items():
        groups[gid] = {"name": ma._bchat_gname(gid),
                       "members": len(bg["members"]),
                       "online": bool(bg["thread"] and bg["thread"].is_alive()),
                       "fallback": time.time() < bg["fallback_until"],
                       "actions": list(bg["actions"])}
    return {"ok": True, "enabled": bool(cfg.get("builder_chat_enabled")),
            "cap": int(cfg.get("builder_chat_cap", 4)), "groups": groups}


def _squads_caps(ctx, body=None):
    """军种小队数上限：GET 读当前（覆盖层+生效值），POST 改（None=恢复跟随 config）。
    覆盖层存 multiagent._CAPS_OVERRIDE，squad_assign 下拍即生效。"""
    import multiagent as ma
    if body is not None:
        for br in ("land", "air", "navy"):
            v = body.get(br)
            if v is None:
                ma._CAPS_OVERRIDE.pop(br, None)
            elif isinstance(v, (int, float)) and 1 <= int(v) <= 12:
                ma._CAPS_OVERRIDE[br] = int(v)
        ctx["log_activity"]("status", f"🖥 面板调整军种上限: {ma._CAPS_OVERRIDE or '（恢复跟随 config）'}")
    return {"ok": True, "caps": ma._current_caps(ctx), "override": dict(ma._CAPS_OVERRIDE)}


def _activity_tail(n):
    """activity.jsonl 末 n 行（只读文件尾 64KB，大文件不整读）。"""
    try:
        with open(_ACTIVITY_FILE, "rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - 65536))
            lines = fp.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _command(ctx, st, text):
    """面板高层指令：走玩家留言同款 broadcast（三路都收，优先级最高）。"""
    text = (text or "").strip()
    if not text:
        return False
    st.broadcast(f"【面板指令】（优先级最高，照做并说明怎么执行）: {text}")
    ctx["log_activity"]("chat", f"🖥 面板: {text}")
    try:  # 同步上游戏内聊天台（断连静默）
        ctx["link"].send({"cmd": "overlay", "who": "player", "chat": _EMOJI_RE.sub("", "🖥 " + text)})
    except Exception:
        pass
    return True


def _set_switch(role, state):
    """state: True/False=强制开关；None=恢复跟随游戏面板。返回 False=参数非法。"""
    key = "enabled" if role == "all" else role
    if key not in _overrides:
        return False
    if state is not None and not isinstance(state, bool):
        return False
    _overrides[key] = state
    return True


# ---------- HTTP 层 ----------

def _make_handler(ctx, st):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默（不刷 pilot 控制台）
            pass

        def _send(self, body, ctype="application/json; charset=utf-8", code=200):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(_PAGE, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(json.dumps(_state(ctx, st), ensure_ascii=False))
            elif path == "/api/map":
                full = "full=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
                self._send(json.dumps(_map_state(ctx, st, full), ensure_ascii=False))
            elif path == "/api/switches":
                self._send(json.dumps(_switches(ctx), ensure_ascii=False))
            elif path == "/api/squads":
                self._send(json.dumps(_squads_caps(ctx), ensure_ascii=False))
            elif path == "/api/bchat":
                self._send(json.dumps(_bchat(ctx, st), ensure_ascii=False))
            else:
                self._send('{"ok":false,"err":"404"}', code=404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._body_json()
            if path == "/api/command":
                ok = _command(ctx, st, str(body.get("text", "")))
                self._send(json.dumps({"ok": ok}, ensure_ascii=False),
                           code=200 if ok else 400)
            elif path == "/api/switches":
                ok = _set_switch(str(body.get("role", "")), body.get("state"))
                self._send(json.dumps({"ok": ok, "switches": _switches(ctx)},
                                      ensure_ascii=False), code=200 if ok else 400)
            elif path == "/api/squads":
                self._send(json.dumps(_squads_caps(ctx, body), ensure_ascii=False))
            elif path == "/api/bchat":
                self._send(json.dumps(_bchat(ctx, st, body), ensure_ascii=False))
            else:
                self._send('{"ok":false,"err":"404"}', code=404)

    return Handler


# ---------- 页面（纯原生 JS，无外部依赖，离线可用） ----------

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>铁锈战争 · AI 总控台</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3; --dim:#8b949e;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --acc:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif; }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; }
  #dot { width:10px; height:10px; border-radius:50%; background:var(--bad); display:inline-block; }
  #dot.on { background:var(--ok); }
  #battle { color:var(--dim); font-size:13px; }
  .bar { padding:8px 16px; border-bottom:1px solid var(--line); display:flex; gap:10px; align-items:center;
         flex-wrap:nowrap; overflow-x:auto; white-space:nowrap; }
  .sw { display:flex; align-items:center; gap:4px; flex:0 0 auto; }
  .sw .nm { color:var(--dim); font-size:12px; }
  .sw button { background:var(--card); color:var(--dim); border:1px solid var(--line); border-radius:6px;
               padding:1px 8px; cursor:pointer; font-size:12px; }
  .sw button.on { color:#fff; border-color:var(--acc); background:#1f6feb33; }
  .sw .eff { font-size:11px; }
  .eff.y { color:var(--ok); } .eff.n { color:var(--bad); }
  main { display:flex; gap:12px; padding:12px 16px; align-items:flex-start; }
  .col { display:flex; flex-direction:column; gap:12px; min-width:0; flex:1 1 0; }
  #col-l { flex:0 0 370px; }   /* 决策·慢层 */
  #col-r { flex:0 0 460px; }   /* 战场 */
  @media (max-width: 1180px) {
    main { flex-direction:column; }
    #col-l, #col-r { flex:1 1 auto; }
  }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; min-width:0; }
  .card h2 { font-size:14px; margin:0 0 8px; display:flex; justify-content:space-between; }
  .card h2 .meta { color:var(--dim); font-weight:normal; font-size:12px; }
  pre { white-space:pre-wrap; word-break:break-word; margin:0; font:13px/1.7 "Microsoft YaHei",sans-serif; }
  .empty { color:var(--dim); }
  .chips { margin-bottom:8px; display:flex; gap:6px; flex-wrap:wrap; }
  .chip { background:#1f6feb22; border:1px solid #1f6feb55; color:var(--acc); border-radius:10px; padding:0 8px; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { border-bottom:1px solid var(--line); padding:3px 6px; text-align:left; }
  th { color:var(--dim); font-weight:normal; }
  td.wrap { word-break:break-all; }
  .st-pending { color:var(--dim); } .st-assigned { color:var(--warn); }
  .st-done { color:var(--ok); } .st-failed { color:var(--bad); }
  .sq { border:1px solid var(--line); border-radius:8px; padding:8px; margin-bottom:8px; }
  .sq .hd { display:flex; gap:8px; align-items:center; margin-bottom:4px; }
  .capin { width:34px; background:var(--bg); border:1px solid var(--line); border-radius:4px;
           color:var(--fg); padding:1px 4px; font-size:12px; }
  .capbtn { background:var(--card); color:var(--dim); border:1px solid var(--line); border-radius:6px;
            padding:1px 10px; cursor:pointer; font-size:12px; margin-left:4px; }
  .capbtn:hover { color:#fff; border-color:var(--acc); }
  .badge { border-radius:6px; padding:0 8px; font-size:12px; }
  .b-attack { background:#f8514922; color:var(--bad); } .b-defend { background:#3fb95022; color:var(--ok); }
  .b-hold { background:#8b949e22; color:var(--dim); } .b-harass { background:#d2992222; color:var(--warn); }
  .b-produce { background:#58a6ff22; color:var(--acc); }
  #cmd { display:flex; gap:8px; padding:10px 16px; border-top:1px solid var(--line); }
  #cmd input { flex:1; background:var(--card); border:1px solid var(--line); border-radius:8px;
               color:var(--fg); padding:8px 12px; font-size:14px; outline:none; }
  #cmd input:focus { border-color:var(--acc); }
  #cmd button { background:#238636; color:#fff; border:0; border-radius:8px; padding:8px 18px; cursor:pointer; }
  #feedwrap { padding:0 16px 16px; }
  #feed { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:8px 12px;
          max-height:220px; overflow-y:auto; font:12px/1.8 Consolas,monospace; }
  #feed .k-status { color:#8ab4f8; } #feed .k-cmd { color:#9ece6a; } #feed .k-error { color:var(--bad); }
  #feed .k-note { color:var(--dim); } #feed .k-chat { color:#e0af68; } #feed .k-alert { color:#ff9e64; }
  /* —— cli 指挥舱 —— */
  .seat { display:flex; gap:8px; align-items:baseline; border-bottom:1px solid var(--line); padding:5px 0; }
  .seat:last-child { border-bottom:0; }
  .seat .nm { min-width:64px; font-weight:bold; }
  .seat .st { min-width:86px; font-size:12px; }
  .seat .out { color:var(--dim); font-size:12px; word-break:break-all; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:4px; }
  .dot.g { background:var(--ok); } .dot.y { background:var(--warn); } .dot.r { background:var(--bad); }
  .dot.dim { background:var(--dim); }
  .ev { font:12px/1.8 Consolas,monospace; border-bottom:1px dashed #21262d; padding:1px 0; }
  .ev:last-child { border-bottom:0; }
  .ev .et { color:var(--dim); margin-right:6px; }
  .jev-q { color:var(--acc); } .jev-a { color:var(--ok); } .jev-u { color:var(--warn); }
  .muted-banner { background:#d2992222; border:1px solid #d2992255; color:var(--warn);
                  border-radius:8px; padding:4px 10px; font-size:12px; margin-bottom:8px; }
</style>
</head>
<body>
<header>
  <span id="dot"></span><h1>铁锈战争 · AI 总控台</h1><span id="battle">连接中…</span>
</header>
<div class="bar" id="switches"></div>
<main>
 <div class="col" id="col-l">
  <div class="card cli-only"><h2>🪑 CLI 席位 <span class="meta" id="seats-meta"></span></h2>
    <div id="muted-banner" style="display:none" class="muted-banner">LLM_OFF 静音模式：席位未启用，快层+任务机照跑</div>
    <div id="seats"></div></div>
  <div class="card"><h2>📋 总指挥 · 作战计划书 <span class="meta" id="plan-age"></span></h2>
    <div class="chips" id="plan-params"></div><pre id="plan" class="empty">（尚无计划书）</pre></div>
  <div class="card cli-only"><h2>🧠 Jev 驾驶位 <span class="meta">最近决策</span></h2>
    <div id="jev"></div></div>
  <div class="card cli-only"><h2>💬 留言板 board.md <span class="meta">跨席异步</span></h2>
    <pre id="board" class="empty">（board.md 为空）</pre></div>
 </div>
 <div class="col" id="col-m">
  <div class="card cli-only"><h2>📜 任务单状态机 <span class="meta" id="tasks-meta"></span></h2>
    <div id="tasks"></div></div>
  <div class="card"><h2>🔨 建造指挥 · 建造工单 <span class="meta" id="wo-meta"></span></h2>
    <div id="wo"></div></div>
  <div class="card"><h2>⚔ 战斗指挥 · 小队任务书 <span class="meta"></span></h2>
    <div class="legacy-only" style="margin-bottom:8px;color:var(--dim);font-size:12px">军种上限:
      陆<input class="capin" id="cap-land"> 空<input class="capin" id="cap-air"> 海<input class="capin" id="cap-navy">
      <button class="capbtn" onclick="setCaps()">应用</button><span id="cap-note" style="margin-left:6px"></span></div>
    <div id="squads"></div></div>
  <div class="card cli-only"><h2>⚡ 事件流 <span class="meta">任务/席位生命周期</span></h2>
    <div id="clev"></div></div>
  <div class="card legacy-only"><h2>🧰 建造小队 · 现场执行层 <span class="meta" id="bchat-meta"></span></h2>
    <div style="margin-bottom:8px;color:var(--dim);font-size:12px">
      <button class="capbtn" id="bchat-toggle" onclick="toggleBchat()">开/关</button>
      每组上限<input class="capin" id="bchat-cap">
      <button class="capbtn" onclick="setBchatCap()">应用</button></div>
    <div id="bchat"></div></div>
 </div>
 <div class="col" id="col-r">
  <div class="card"><h2>🗺 地图理解 <span class="meta" id="map-meta"></span></h2>
    <canvas id="mapcanvas" style="width:100%;image-rendering:pixelated;border-radius:6px"></canvas>
    <div style="color:var(--dim);font-size:12px;margin-top:6px">军种着色：亮绿=陆军 · 天蓝=空军 · 深蓝=海军 · 深绿块=建筑 · 淡紫=盟友 · 红点=可见敌（AI 所见） · 空心红框=记忆敌建筑 · 黄=浅黑(记忆区) · 黑=未探索（AI 盲区） · 绿圈=家</div></div>
 </div>
</main>
<div id="cmd">
  <input id="cmdtext" placeholder="输入高层指令，回车发送（如：转龟缩防守 / 集火北面矿点 / 空军全部撤回）">
  <button onclick="sendCmd()">发送</button>
</div>
<div id="feedwrap"><div id="feed"></div></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const STATUS_CN = {pending:"待派", assigned:"在干", done:"完成", failed:"失败"};
const ago = sec => sec == null ? "" : sec < 60 ? sec + "秒前" : Math.floor(sec/60) + "分钟前";

async function refresh() {
  let d;
  try { d = await (await fetch("/api/state")).json(); $("dot").className = "on"; }
  catch (e) { $("dot").className = ""; $("battle").textContent = "桥接未连接（pilot.py 未启动？）"; return; }

  const g = d.game;
  $("battle").textContent = !g ? "未检测到对局（游戏未连接 / 在主菜单）" :
    (d.defeated ? "☠ 已阵亡 · AI 待机中 · " : "")
    + `🕐 ${String(Math.floor(g.time_sec/60)).padStart(2,"0")}:${String(g.time_sec%60).padStart(2,"0")}`
    + (g.funds != null ? ` · 💰 ${g.funds}（收入+${g.income ?? "?"}/s 支出-${g.expense ?? "?"}/s）` : "")
    + ` · 己方 ${g.own} · 委托 ${g.delegated} · 可见敌 ${g.enemy_visible}`
    + (g.map ? ` · 地图 ${g.map}` : "") + ` · 第${g.generation}代`;

  // 开关（角色动态渲染：军种中层 land/air/navy 自动带出）
  const sw = d.switches, mk = (label, cur, role) => {
    const eff = `<span class="eff ${cur.effective ? "y" : "n"}">${cur.effective ? "生效:开" : "生效:关"}</span>`;
    const btn = (txt, st, on) => `<button class="${on ? "on" : ""}" onclick="setSw('${role}',${st})">${txt}</button>`;
    return `<span class="sw"><span class="nm">${label}</span>`
      + btn("开", true, cur.override === true) + btn("关", false, cur.override === false)
      + btn("跟随游戏", null, cur.override === null) + eff + `</span>`;
  };
  const RN = {commander: "总指挥", build: "建造指挥", combat: "战斗总闸", land: "陆军", air: "空军", navy: "海军"};
  $("switches").innerHTML = mk("AI 总开关", sw.all, "all")
    + Object.entries(sw.roles).map(([k, v]) => mk(RN[k] || k, v, k)).join("");

  // 模式切换：cli 指挥舱 / legacy 三角色（一壳两身，按运行循环自动切）
  const mode = d.mode || "legacy";
  document.querySelectorAll(".cli-only").forEach(e => e.style.display = mode === "cli" ? "" : "none");
  document.querySelectorAll(".legacy-only").forEach(e => e.style.display = mode === "legacy" ? "" : "none");
  if (mode === "cli") renderCli(d.cli || {});

  // 计划书
  const c = d.commander;
  $("plan-age").textContent = c.age_sec != null ? "更新于 " + ago(c.age_sec) : "";
  $("plan-params").innerHTML = Object.entries(c.params || {})
    .map(([k, v]) => `<span class="chip">${esc(k)}=${esc(typeof v === "object" ? JSON.stringify(v) : v)}</span>`).join("");
  $("plan").textContent = c.plan || "（尚无计划书）";
  $("plan").className = c.plan ? "" : "empty";

  // 工单
  const b = d.build;
  $("wo-meta").textContent = `v${b.version}` + (b.builders_target ? ` · 建造者目标 ${b.builders_target}` : "");
  $("wo").innerHTML = !b.tasks.length ? `<span class="empty">（工单为空）</span>` :
    `<table><tr><th>#</th><th>动作</th><th>槽位</th><th>状态</th><th>备注</th></tr>` + b.tasks.map(t =>
      `<tr><td>${t.idx + 1}</td><td>${esc(t.action)}</td><td>${esc(t.slot ?? "")}</td>`
      + `<td class="st-${esc(t.status)}">${STATUS_CN[t.status] || esc(t.status)}</td><td>${esc(t.note || "")}</td></tr>`
    ).join("") + `</table>`;

  // 小队
  $("squads").innerHTML = d.combat.squads.map(s => {
    const m = s.mission || {}, task = m["任务"];
    if (!task) return `<div class="sq"><div class="hd"><b>${s.id}</b><span class="empty">未派任务 · 在役 ${s.members}</span></div></div>`;
    const bits = [];
    if (m["编成"]) bits.push("编成 " + esc(m["编成"]));
    if (m["规模"]) bits.push("规模 " + m["规模"]);
    bits.push("在役 " + s.members);
    if (m["目标"]) bits.push("目标 (" + Math.round(m["目标"][0]) + "," + Math.round(m["目标"][1]) + ")");
    if (task === "produce") bits.push(esc(m["动作"] || "") + " ×" + (m["数量"] ?? "") + (m["_done"] ? " ✓完成" : ""));
    if (m["备注"]) bits.push(esc(m["备注"]));
    return `<div class="sq"><div class="hd"><b>${s.id}</b><span class="badge b-${esc(task)}">${esc(task)}</span></div>`
      + `<div>${bits.join(" · ")}</div></div>`;
  }).join("");

  // 活动流
  const feed = $("feed");
  feed.innerHTML = (d.activity || []).map(e =>
    `<div class="k-${esc(e.kind)}">${esc(e.ts)} ${esc(e.text)}</div>`).join("");
  feed.scrollTop = feed.scrollHeight;
}

function renderCli(c) {
  const SEAT_CN = {commander: "总指挥", land: "陆军席", navy: "海军席", air: "空军席", build: "建造席"};
  // 席位
  $("seats-meta").textContent = c.match ? `局 ${c.match}` : "";
  $("muted-banner").style.display = c.muted ? "" : "none";
  $("seats").innerHTML = (c.seats || []).map(s => {
    let dot, st;
    if (s.broken) { dot = "r"; st = "停摆"; }
    else if (!s.enabled) { dot = "dim"; st = "停用"; }
    else if (s.busy) { dot = "y"; st = "思考中…"; }
    else { dot = "g"; st = s.next_in != null && s.next_in > 0 ? `${s.next_in}s 后拍` : "待拍"; }
    const out = s.last_out ? s.last_out.split("\\n").filter(x => x.trim()).slice(-2).join(" ⏎ ") : "（还没拍过）";
    return `<div class="seat"><span class="nm">${SEAT_CN[s.name] || esc(s.name)}</span>` +
      `<span class="st"><span class="dot ${dot}"></span>${st}</span>` +
      `<span class="out">${esc(out.slice(0, 160))}</span></div>`;
  }).join("") || `<span class="empty">（无席位）</span>`;
  // 任务单
  const TK_CN = {pending: "待激活", active: "执行中", done: "完成", aborted: "中止", failed: "失败"};
  $("tasks-meta").textContent = `${(c.tasks || []).length} 张`;
  $("tasks").innerHTML = (c.tasks || []).length ? `<table><tr><th>id</th><th>军种</th><th>任务</th><th>状态</th><th>小队</th><th>谓词 when</th></tr>` +
    c.tasks.map(t => `<tr><td>${esc(t.id)}</td><td>${esc(t.branch)}</td><td>${esc(t.task)}</td>` +
      `<td class="st-${esc(t.status)}">${TK_CN[t.status] || esc(t.status)}</td><td>${esc(t.squad)}</td>` +
      `<td style="color:var(--dim);font-size:12px">${esc(t.when || (t.note ? "备注:" + t.note : ""))}</td></tr>`).join("") + `</table>`
    : `<span class="empty">（任务机为空）</span>`;
  // Jev
  $("jev").innerHTML = (c.jev || []).length ? c.jev.map(e => {
    const k = e.ev || "";
    let line;
    if (k === "jev_drive" || k === "jev_event") {
      line = `<span class="jev-q">${esc(e.squad || e.task || "")}</span> 选 <span class="jev-a">${esc(e.act || e.choice || "")}</span>`;
      if (e.probs) line += ` <span style="color:var(--dim)">${esc(JSON.stringify(e.probs).slice(0, 80))}</span>`;
    } else if (k === "jev_unsure") {
      line = `<span class="jev-q">${esc(e.squad || e.task || "")}</span> <span class="jev-u">unsure</span> ${esc(e.noul || e.why || "")}`;
    } else {
      line = `<span class="jev-u">${esc(e.why || k)}</span>`;
    }
    return `<div class="ev"><span class="et">${esc(e.ts || "")}</span>${line}</div>`;
  }).join("") : `<span class="empty">（Jev 无事件——未启用/静音/平静期）</span>`;
  // 留言板
  $("board").textContent = c.board || "（board.md 为空）";
  $("board").className = c.board ? "" : "empty";
  // 任务/席位事件流（按类型格式化，不吐原始 JSON）
  const evTxt = e => {
    const k = e.ev || "";
    if (k === "task") return `任务 <b>${esc(e.id)}</b> ${esc(e.from)}→${esc(e.to)}` + (e.then ? ` 接力:${esc(e.then)}` : "");
    if (k === "task_loaded") return `装载任务 <b>${esc(e.id)}</b>（${esc(e.file)}）`;
    if (k === "task_chain") return `接力 <b>${esc(e.from)}</b>→<b>${esc(e.to)}</b>`;
    if (k === "task_wait") return `任务 <b>${esc(e.id)}</b> 条件不足，pending 重试`;
    if (k === "task_give_up") return `任务 <b>${esc(e.id)}</b> 放弃（${esc(e.why || "")}）`;
    if (k === "seat_session") return `席位 <b>${esc(e.seat)}</b> 会话建立`;
    if (k === "seat_done") return `席位 <b>${esc(e.seat)}</b> 一拍完成 ${e.ms ?? "?"}ms`;
    if (k === "seat_timeout") return `席位 <b>${esc(e.seat)}</b> 超时（${e.timeout ?? "?"}s）`;
    if (k === "seat_down") return `席位 <b>${esc(e.seat)}</b> 停摆：${esc(e.why || "")}`;
    if (k === "plan_params") return `计划书参数块已消费`;
    if (k === "pred_bad") return `谓词错误：${esc(e.why || JSON.stringify(e).slice(0, 100))}`;
    if (k === "squad_wiped") return `小队 <b>${esc(e.squad || e.id || "")}</b> 团灭`;
    return esc(k) + " " + esc(JSON.stringify({...e, ev: undefined, ts: undefined}).slice(0, 100));
  };
  $("clev").innerHTML = (c.events || []).length ? c.events.map(e =>
    `<div class="ev"><span class="et">${esc(e.ts || "")}</span>${evTxt(e)}</div>`
  ).join("") : `<span class="empty">（无事件）</span>`;
}

async function sendCmd() {  const box = $("cmdtext"), text = box.value.trim();
  if (!text) return;
  const r = await fetch("/api/command", {method:"POST", headers:{"Content-Type":"application/json"},
                                         body: JSON.stringify({text})});
  if ((await r.json()).ok) { box.value = ""; refresh(); }
}
async function setSw(role, state) {
  await fetch("/api/switches", {method:"POST", headers:{"Content-Type":"application/json"},
                                body: JSON.stringify({role, state})});
  refresh();
}
async function refreshCaps() {
  try {
    const c = await (await fetch("/api/squads")).json();
    for (const br of ["land", "air", "navy"]) {
      const el = $("cap-" + br);
      if (document.activeElement !== el) el.value = c.caps[br];
    }
    $("cap-note").textContent = Object.keys(c.override).length ? "（面板覆盖中）" : "（跟随 config）";
  } catch (e) {}
}
async function setCaps() {
  const body = {};
  for (const br of ["land", "air", "navy"]) {
    const v = parseInt($("cap-" + br).value, 10);
    body[br] = (v >= 1 && v <= 12) ? v : null;
  }
  await fetch("/api/squads", {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify(body)});
  refreshCaps();
}
async function refreshBchat() {
  try {
    const b = await (await fetch("/api/bchat")).json();
    $("bchat-meta").textContent = b.enabled ? "运行中" : "已关闭";
    $("bchat-toggle").textContent = b.enabled ? "关闭建造小队" : "开启建造小队";
    const el = $("bchat-cap");
    if (document.activeElement !== el) el.value = b.cap;
    $("bchat").innerHTML = Object.entries(b.groups || {}).map(([gid, g]) =>
      `<div class="sq"><b>${esc(g.name)}(${gid})</b> ${g.online ? "🟢在线" : "⚪待员"} · 在组 ${g.members}` +
      (g.fallback ? ` · <span style="color:#ff9e64">规则层接管中</span>` : "") +
      `<div class="meta">${(g.actions || []).map(esc).join(" | ") || "（暂无动作）"}</div></div>`
    ).join("") || `<div class="meta">（尚无分组——委托建造者后自动归队）</div>`;
  } catch (e) {}
}
async function toggleBchat() {
  const b = await (await fetch("/api/bchat")).json();
  await fetch("/api/bchat", {method:"POST", headers:{"Content-Type":"application/json"},
                             body: JSON.stringify({enabled: !b.enabled})});
  refreshBchat();
}
async function setBchatCap() {
  const v = parseInt($("bchat-cap").value, 10);
  if (!(v >= 1 && v <= 12)) return;
  await fetch("/api/bchat", {method:"POST", headers:{"Content-Type":"application/json"},
                             body: JSON.stringify({cap: v})});
  refreshBchat();
}
function decodeRow(s) {
  const out = [];
  for (const p of String(s).split(";")) {
    if (!p) continue;
    const [v, n] = p.split("x");
    for (let i = 0; i < +n; i++) out.push(+v);
  }
  return out;
}

const TERRAIN = {".":"#1d2b1d","~":"#14324a","=":"#4a4a4a","L":"#5a2d1a","^":"#3a3a22","#":"#333"," ":"#000"};
let mapCache = {gen: -1, rows: null};  // 地形底图缓存：只在换图时重拉
async function refreshMap() {
  let m;
  try { m = await (await fetch("/api/map")).json(); } catch (e) { return; }
  if (!m.ok || !m.w || !m.h) { $("map-meta").textContent = "无地图数据"; return; }
  if (m.generation !== mapCache.gen || !mapCache.rows) {  // 换图/首载：重拉地形
    try { m = await (await fetch("/api/map?full=1")).json(); } catch (e) { return; }
    mapCache = {gen: m.generation, rows: m.rows};
  }
  const cv = $("mapcanvas"), c2 = cv.getContext("2d");
  const cs = Math.max(1, Math.floor(512 / Math.max(m.w, m.h)));
  cv.width = m.w * cs; cv.height = m.h * cs;
  const rows = mapCache.rows || [];
  for (let y = 0; y < m.h; y++) {
    const row = rows[y] || "";
    for (let x = 0; x < m.w; x++) {
      c2.fillStyle = TERRAIN[row[x]] || "#111";
      c2.fillRect(x * cs, y * cs, cs, cs);
    }
  }
  if (m.fog && m.fog.rows) {  // 迷雾层：黄=浅黑(记忆区) 黑=未探索（即 AI 的盲区）
    for (let y = 0; y < Math.min(m.fog.rows.length, m.h); y++) {
      const fr = decodeRow(m.fog.rows[y]);
      for (let x = 0; x < Math.min(fr.length, m.w); x++) {
        if (fr[x] === 1) c2.fillStyle = "rgba(210,153,34,0.35)";
        else if (fr[x] === 2) c2.fillStyle = "rgba(0,0,0,0.85)";
        else continue;
        c2.fillRect(x * cs, y * cs, cs, cs);
      }
    }
    $("map-meta").textContent = (m.modeText || "") + (m.ghosts.length ? ` · 记忆敌建筑 ${m.ghosts.length} 座` : "");
  } else {
    $("map-meta").textContent = m.modeText || "迷雾数据未同步（游戏未开局/老 jar）";
  }
  const px = u => [u[0] / m.tileW * cs, u[1] / m.tileH * cs];
  const dot = (u, color, r) => { const [x, y] = px(u); c2.fillStyle = color;
    c2.beginPath(); c2.arc(x, y, r, 0, 7); c2.fill(); };
  const u = m.units || {own: [], allies: [], enemy: []};
  const BCOL = ["#7ee787", "#79c0ff", "#1f6feb"];  // 军种着色：陆=亮绿 空=天蓝 海=深蓝
  for (const g of (m.ghosts || [])) {  // 记忆敌建筑：空心红方块（不是现状，是上次所见）
    c2.strokeStyle = "#f85149"; c2.lineWidth = 1.5;
    c2.strokeRect(g.x / m.tileW * cs - 3, g.y / m.tileH * cs - 3, 6, 6);
  }
  for (const a of u.allies) dot(a, "#b987ff", 2);            // 盟友=淡紫
  for (const e of u.enemy) dot(e, "#f85149", 2.5);           // 可见敌人=红点（迷雾过滤后，AI 所见）
  for (const o of u.own) o[2] ? dot(o, "#2ea043", 3.5)       // 我方建筑=深绿块
                              : dot(o, BCOL[o[3] || 0], 2);  // 单位按军种着色
  if (m.home) {
    c2.strokeStyle = "#3fb950"; c2.lineWidth = 2;
    c2.beginPath(); c2.arc(m.home[0] / m.tileW * cs, m.home[1] / m.tileH * cs, 6, 0, 7); c2.stroke();
  }
}

$("cmdtext").addEventListener("keydown", e => { if (e.key === "Enter") sendCmd(); });
refresh();
refreshMap();
refreshCaps();
refreshBchat();
setInterval(refresh, 2500);
setInterval(refreshMap, 2500);  // 单位/迷雾动态刷新（快照 0.5s 一跳，面板 2.5s 跟拍）
setInterval(refreshCaps, 5000);
setInterval(refreshBchat, 5000);
</script>
</body>
</html>
"""
