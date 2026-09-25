#!/usr/bin/env python3
"""系统谓词词表 + 任务单状态机（架构二期评审定论）。

- 任务单触发条件只能用本文件词表，2s 拍判定，零 LLM。
- 谓词串语法：name(arg,arg,...)，参数逗号分隔不加引号；词表外/参数非法一律 False + 记事件。
- 谓词函数吃 world 视图（cliloop 每拍重建）返回 bool：
    world.snap    最新快照 dict
    world.squads  {sid: {"members": set, "mission": ...}}（系统编队层维护）
    world.wiped   已团灭 sid 集合
    world.tasks   {任务id: task dict}（TaskEngine 持有，运行时字段带 "_" 前缀）
    world.home    (x, y) 主基地；None=未找到
    world.now     time.time()

任务单 schema（tasks/*.json，中层/开局生成器产物）：
  {"id", "branch", "task", "units"|"squad"(可选), "do": {...},
   "trigger": {"when": [谓词...], "then": "任务id"|"abort"|预案名},   # trigger 可省略
   "status": "pending|active|done|aborted"}                            # 仅首次载入生效

状态机语义（2s 拍）：
  pending：无 trigger=立即激活；有 trigger=when 全真才激活。激活=执行 do（do_fn），
           do_fn 返回 False=条件不足（如小队未成型），留 pending 下拍重试。
           无 trigger 的任务激活即 done（移交类：工单/目标移交 executor 后完成判定归系统）。
  active ：when 全真=触发——then="abort"→aborted；其余→done；
           then 是任务 id 且该任务 pending→链式激活它（预案接力）。
"""
import json
import math
import os
import re
import time

# ---------- 谓词 ----------

def _own(w):
    return (w.snap or {}).get("own", [])


def _cmp(v, op, n):
    return {">=": v >= n, "<=": v <= n, ">": v > n,
            "<": v < n, "==": v == n, "=": v == n}.get(op, False)


def at_point(w, sid, x, y, r):
    """小队质心是否在 (x,y) 半径 r 内。"""
    sq = (w.squads or {}).get(str(sid))
    if not sq or not sq.get("members"):
        return False
    ms = [u for u in _own(w) if u["id"] in sq["members"]]
    if not ms:
        return False
    cx = sum(u["x"] for u in ms) / len(ms)
    cy = sum(u["y"] for u in ms) / len(ms)
    return math.hypot(cx - float(x), cy - float(y)) <= float(r)


def built(w, action, count):
    """已完工建筑计数：action 可带 b_/c_/u_ 前缀（剥离后按类型名匹配）。"""
    btype = re.sub(r"^(b_|u_|c_)", "", str(action))
    n = sum(1 for u in _own(w)
            if u.get("bld") and u["t"] == btype and u.get("prog", 1) >= 1)
    return n >= int(count)


def unit_count(w, utype, op, n):
    """非建筑单位计数比较：unit_count(c_tank,>=,6)。类型可带 u_ 前缀。"""
    t = re.sub(r"^u_", "", str(utype))
    c = sum(1 for u in _own(w) if not u.get("bld") and u["t"] == t)
    return _cmp(c, str(op), int(n))


def enemy_visible(w, *where):
    """敌情出现：enemy_visible(any)=任一可见敌；enemy_visible(home)=敌入家圈1200px；
    enemy_visible(x,y,r)=坐标圈内可见敌。"""
    enemies = (w.snap or {}).get("enemy", [])
    if not where or where[0] in ("any", ""):
        return bool(enemies)
    if where[0] == "home":
        if not w.home:
            return False
        hx, hy = w.home
        return any((e["x"] - hx) ** 2 + (e["y"] - hy) ** 2 <= 1200 ** 2 for e in enemies)
    if len(where) == 3:
        x, y, r = float(where[0]), float(where[1]), float(where[2])
        return any((e["x"] - x) ** 2 + (e["y"] - y) ** 2 <= r * r for e in enemies)
    return False


def squad_wiped(w, sid):
    """小队团灭（系统编队层记账：有员小队成员清零即入 wiped 集）。"""
    return str(sid) in (w.wiped or set())


def task_stuck(w, task_id, sec):
    """任务卡死：active 且超过 sec 秒无进展（进展由 cliloop 记账：mission 距离逼近/工单完成数）。"""
    t = (w.tasks or {}).get(str(task_id))
    if not t or t.get("_status") != "active":
        return False
    last = t.get("_prog_ts") or t.get("_act_ts") or 0.0
    return (w.now - last) >= float(sec)


PREDICATES = {
    "at_point": at_point,
    "built": built,
    "unit_count": unit_count,
    "enemy_visible": enemy_visible,
    "squad_wiped": squad_wiped,
    "task_stuck": task_stuck,
}

_PRED_RE = re.compile(r"^\s*(\w+)\s*\((.*)\)\s*$")


def parse(pred):
    """谓词串 → (name, [args])；非法形态返回 (None, [])。"""
    m = _PRED_RE.match(str(pred))
    if not m:
        return None, []
    args = [a.strip() for a in m.group(2).split(",") if a.strip() != ""]
    return m.group(1), args


def evaluate(world, pred, on_error=None):
    """单谓词判定。词表外/异常一律 False（保守不触发），on_error 记事件用。"""
    name, args = parse(pred)
    fn = PREDICATES.get(name)
    if fn is None:
        if on_error:
            on_error(f"未知谓词 {pred!r}（词表: {'/'.join(sorted(PREDICATES))}）")
        return False
    try:
        return bool(fn(world, *args))
    except (TypeError, ValueError) as e:
        if on_error:
            on_error(f"谓词参数非法 {pred!r}: {e}")
        return False


def eval_when(world, task, on_error=None):
    """trigger.when 全真判定（空表=True）。"""
    when = ((task or {}).get("trigger") or {}).get("when") or []
    return all(evaluate(world, p, on_error) for p in when)


# ---------- 任务单状态机 ----------

GIVE_UP_SEC = 600.0  # do 条件不足（如小队认不到）的放弃时限：超时 aborted+记事件，
#                      防"永远不会有该军种的图"上 30s 节流重试到天涯海角；任务单可写 give_up_sec 覆盖


class TaskEngine:
    """任务单装载 + 2s 拍状态迁移。文件是内容源（中层热写），运行态（_status）归本引擎。
    do_fn(task)->bool|None：激活执行（False=条件不足留 pending 重试）。
    event_fn(dict)：状态迁移/异常落 logs/events.jsonl。"""

    def __init__(self, tasks_dir, do_fn, event_fn):
        self.tasks_dir = tasks_dir
        self.do_fn = do_fn
        self.event_fn = event_fn
        self.tasks = {}          # id -> task（文件字段 + 运行时 "_" 前缀字段）
        self._mtime = {}         # path -> mtime（变更重读）
        self._retry_note = {}    # id -> 上次"留单重试"事件时刻（30s 节流）

    # ---- 装载 ----
    def _load(self):
        try:
            names = [n for n in os.listdir(self.tasks_dir) if n.endswith(".json")]
        except OSError:
            return
        for name in names:
            path = os.path.join(self.tasks_dir, name)
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            if self._mtime.get(path) == mt:
                continue
            self._mtime[path] = mt
            try:
                with open(path, encoding="utf-8") as fp:
                    payload = json.load(fp)
            except (OSError, json.JSONDecodeError) as e:
                self._event({"ev": "task_file_bad", "file": name, "err": str(e)[:120]})
                continue
            items = payload.get("tasks") if isinstance(payload, dict) else payload
            if isinstance(items, dict):
                items = [items]
            for it in items or []:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                tid = str(it["id"])
                if tid in self.tasks:
                    # 内容热更新（中层改单）：文件字段覆盖，运行态保留
                    self.tasks[tid].update({k: v for k, v in it.items()
                                            if not k.startswith("_") and k != "status"})
                    continue
                t = dict(it)
                t["_status"] = str(it.get("status") or "pending")
                self.tasks[tid] = t
                self._event({"ev": "task_loaded", "id": tid, "file": name,
                             "status": t["_status"]})

    # ---- 拍 ----
    def tick(self, world, now):
        self._load()
        world.tasks = self.tasks
        for t in list(self.tasks.values()):
            st = t.get("_status")
            trig = t.get("trigger") or {}
            when = trig.get("when") or []
            if st == "pending":
                if when and not eval_when(world, t, self._pred_err):
                    continue
                ok = self.do_fn(t)
                if ok is False:
                    t.setdefault("_fail0", now)  # 首次失败起算（内容热更新不动 "_" 运行字段）
                    if now - t["_fail0"] >= float(t.get("give_up_sec") or GIVE_UP_SEC):
                        self._event({"ev": "task_give_up", "id": t["id"],
                                     "why": f"do 条件不足超 {float(t.get('give_up_sec') or GIVE_UP_SEC):.0f}s"})
                        self._transit(t, "aborted", now, then="give_up")
                        continue
                    if now - self._retry_note.get(t["id"], 0) > 30:
                        self._retry_note[t["id"]] = now
                        self._event({"ev": "task_wait", "id": t["id"],
                                     "why": "do 条件不足，留 pending 重试"})
                    continue
                t["_act_ts"] = t["_prog_ts"] = now
                if trig:  # 有触发器：激活后转入 active 等触发/完成
                    self._transit(t, "active", now)
                else:     # 无触发器：移交即完成（完成判定归 executor/谓词层）
                    self._transit(t, "active", now)
                    self._transit(t, "done", now)
            elif st == "active" and when:
                if not eval_when(world, t, self._pred_err):
                    continue
                then = trig.get("then")
                if then == "abort":
                    self._transit(t, "aborted", now, then="abort")
                    continue
                self._transit(t, "done", now, then=then)
                nt = self.tasks.get(str(then)) if isinstance(then, str) else None
                if nt is not None and nt.get("_status") == "pending":
                    ok = self.do_fn(nt)
                    if ok is False:
                        continue  # 接力条件不足：留 pending 等自己的 when/下拍
                    nt["_act_ts"] = nt["_prog_ts"] = now
                    self._transit(nt, "active", now)
                    self._event({"ev": "task_chain", "from": t["id"], "to": nt["id"]})
                    if not (nt.get("trigger")):
                        self._transit(nt, "done", now)

    def _transit(self, t, to, now, then=None):
        old = t.get("_status")
        t["_status"] = to
        ev = {"ev": "task", "id": t.get("id"), "from": old, "to": to}
        if then:
            ev["then"] = str(then)
        self._event(ev)

    def _pred_err(self, text):
        self._event({"ev": "pred_bad", "text": text})

    def _event(self, ev):
        ev.setdefault("ts", time.strftime("%H:%M:%S"))
        try:
            self.event_fn(ev)
        except Exception:
            pass
