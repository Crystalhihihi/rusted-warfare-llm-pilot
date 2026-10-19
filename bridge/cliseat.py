#!/usr/bin/env python3
"""CLI 席位骨架（架构二期）：角色=独立 `kimi` CLI 会话，spawn 模式（实测地板 ~12s/拍、带读文件 ~18s）。

- 跨角色通信=留言板文件（board.md），席位间不许直接对话；席位只读写局文件夹。
- 总指挥一席（60s+ 拍，产物 plan.md）；中层四席（land/navy/air/build，25s 拍，
  产物 tasks/<branch>.json）——config cli_middle_enabled 总闸 + cli_middle_seats 名单（默认 land,build）。
- **分席会话**：每席首拍全新 spawn（不带 -c——同目录下 -c 续的是"该目录最近会话"，
  五席会互相续进同一个会话串味），从 stream-json 里解析本席 session_id 存 Seat.sid，
  之后每拍 `kimi -S <sid> -p ...` 续本席自己的会话（-S=--session 按 id 续接，kimi --help 口径；
  -r 是其隐藏别名，若实证 -S 与 -p 不兼容就换 -r）。换局=新 Seat 对象=sid 自然重置。
- stream-json=逐行 JSON 事件流（assistant content / tool_calls / tool 返回），机器可解析；
  解析取最终 assistant content。总指挥若自己用 CLI 工具写了 plan.md 就以文件为准，否则代写。
- spawn 留手动验证路径：进程在后台线程跑，桥接 2s 拍不阻塞；kimi 不在 PATH 记事件后停摆。
"""
import json
import os
import re
import subprocess
import threading
import time

COMMANDER_PROMPT = """你是《Rusted Warfare》AI 总指挥（架构二期慢层，约60s一拍）。只看大局出布局，不微操——施工调度/产兵/保命归系统。
本目录=对局档案：state.json=战局快照（2s覆写：资金收入/建筑/小队/可见敌情/资源池实控/迷雾）、board.md=留言板（中层/快层上报，需处置的请读）、tasks/=任务单（opening.json=开局任务单，已被执行层消费；中层各席会写 <军种>.json）、plan.md=你的布局产物（若已存在，在其基础上修订）、ref/units.md=单位手册（单位/建筑合法 ID 全在里面，别发明 ID）。
请审阅 state.json 与 board.md，然后用文件工具把《作战布局》写入 plan.md，固定五小节：
【阶段】开局/扩张/中盘/决战 之一+一句话依据
【经济】采集器目标数、建造重点（厂型与顺序）、是否上海军/实验厂
【配兵】军队构成方针（主战/防空/远程比例与具体兵种，针对可见敌构成）
【攻防】姿态(expand/defend/attack)与主攻方向（坐标或方位），何时转攻
【布局】坐标级可执行：厂区方位、防线（锚点+座数）、后勤安排、扩张方向
文末必须附 ```json 参数块（机器消费，字段必填）：
{"阶段":"扩张","采集器目标":5,"建造者目标":4,"厂型清单":["landFactory","airFactory"],"主攻":[1200,800],"防线":{"锚点":[1830,1100],"座数":8},"姿态":"expand"}
只写 plan.md，别改其他文件；没有实质局势变化只微调。以稳为先。"""

# 中层 prompt = 通用段 + 分域段（席位 prompt 即任务词汇表，schema/词表/纪律全在里面）
_MIDDLE_COMMON = """你是《Rusted Warfare》AI 的{cn}中层指挥（架构二期中层席，约25s一拍）。
本目录=对局档案：state.json=战局快照（2s覆写）、plan.md=总指挥布局（照它执行，冲突时在 board.md 指出）、board.md=留言板、tasks/opening.json=开局任务单（schema 范例）、ref/units.md=单位手册（单位/建筑合法 ID 见它，别发明 ID）。
【产物】把本军种详细任务单覆盖写入 tasks/{branch}.json：
{{"tasks":[{{"id":"唯一串（保持稳定，别每拍换名）","branch":"{branch}","task":"一句话","do":{{...}},"trigger":{{"when":[谓词...],"then":"任务id|abort|null"}},"status":"pending"}}]}}
【谓词词表（只有这些合法）】at_point(sid,x,y,r)/built(action,count)/unit_count(type,op,n)/enemy_visible(any|home|x,y,r)/squad_wiped(sid)/task_stuck(任务id,秒)。
【留言板礼仪】需上报或跨席协调的，追加写 board.md，段落开头 ## [HH:MM:SS] {branch}。
【纪律】局势没变别重写任务单（每次覆写都扰动执行层）；任务 id 保持稳定；只派本军种（陆=land/海=navy/空=air/建造=build，绝不越界）；不许微操——撤退/集火等现场判断归 Jev 快层，你出的是任务级意图（打哪/守哪/盖哪类，不是逐单位走位）。
善用 trigger 链：harass 队到点转 attack（when at_point(队,x,y,r) → then 下一任务 id）；unit_count 达标转攻。"""

_MIDDLE_BRANCH = {
    "land": """【陆席读本】读 state.json 小队段+可见敌情 + plan.md 攻防节。
产物：mission 任务（attack/hold/harass/defend + 目标坐标 + 编成提示 + trigger 链）、
composition 产兵构成（照 plan.md 配兵节；plan 没写就按可见敌方构成的克制关系自定，兵种要混编主战+防空）、
rally 集结点（朝交火方向、别蹲厂里）。do 键：mission/composition/rally/board。""",
    "build": """【建造席读本】读 state.json 经济段（资金/收入/建筑计数/资源池实控）+ plan.md 经济节。
产物：work_order 建造工单。slot 词汇：pool:k=第k近安全池 / factory:k=厂区槽位 / misc:k=后勤排 / defense:k=防线弧；
精确坐标少用（你没有逐格地形/占位数据）；防线一律阵地单 {{"action":"position","kind":"defense_mixed","anchor":[x,y],"toward":[x,y],"count":座数}}（系统按密集阵展开）。
do 键：work_order/board。经济目标调整（采集器/建造者目标数）不归你改 plan_params——写 board.md 上报总指挥。
建造者/采集器的即时调度归系统（2s 拍）；你管"接下来盖什么、盖哪类"。工单 4~10 项覆盖未来 2~3 分钟，序列即规划：先经济再产能再防线。""",
    "air": """【空席读本】读 state.json 小队段（A 队）+可见敌情 + plan.md 攻防节。
本军种未成型（无空军厂/无本军种单位）时：只在 board.md 留一句"待 airFactory 建成后激活"，不产出任务单（省 token，别写空 tasks 文件）。
成型后同陆席形态：mission（空军骚扰/制空）+ composition（只下本军种厂型 airFactory 的动作 ID）。""",
    "navy": """【海席读本】读 state.json 小队段（N 队）+资源池实控（水池）+ plan.md 攻防节。
本军种未成型（无 seaFactory/无船）时：只在 board.md 留一句"待 seaFactory 建成后激活"，不产出任务单（省 token）。
成型后同陆席形态：mission（水域控制/骚扰敌池）+ composition（只下 seaFactory 的动作 ID）；目标点必须在水域。""",
}


def _middle_prompt(branch):
    return _MIDDLE_COMMON.format(cn=_BRANCH_CN.get(branch, branch), branch=branch) \
        + "\n\n" + _MIDDLE_BRANCH.get(branch, "")

_BRANCH_CN = {"land": "陆军", "navy": "海军", "air": "空军", "build": "建造"}

_PARAMS_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_stream_json(text):
    """kimi --output-format stream-json 逐行事件流 → 最终 assistant 文本。
    事件形态（文档口径）：assistant 消息（content 文本块或 tool_calls）→ tool 消息 → 后续 assistant。
    取最后一条非空 assistant 文本；兼容 result 事件兜底；全不是 JSON 时回退原文。"""
    final = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
        if ev.get("type") == "assistant" or msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                final = content
            elif isinstance(content, list):
                t = "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
                if t.strip():
                    final = t
        elif ev.get("type") == "result" and isinstance(ev.get("result"), str):
            final = ev["result"]
    return final.strip() or (text or "").strip()


def extract_params_block(text):
    """plan.md 文末 ```json 参数块 → dict（机器消费）；没有/解析坏=None。"""
    m = _PARAMS_RE.search(text or "")
    if not m:
        return None
    try:
        v = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


_RESUME_RE = re.compile(r"kimi\s+-r\s+(session_[A-Za-z0-9_-]+)")


def extract_session_id(text):
    """从输出抓本席 session_id：stream-json meta 事件（type=session.resume_hint 带
    session_id 字段）优先；文本尾部 'To resume this session: kimi -r session_xxx' 兜底。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "session.resume_hint":
            sid = ev.get("session_id") or (ev.get("meta") or {}).get("session_id")
            if sid:
                return str(sid)
    m = _RESUME_RE.search(text or "")
    return m.group(1) if m else None


class Seat:
    """一个 CLI 席位：按节拍在局文件夹 spawn kimi 会话，产物落盘。
    on_plan(text, params)：总指挥专用回调（参数块消费归 cliloop）。
    chat_fn(who, text)：游戏内聊天台推送（cliloop.push_chat 注入；None=不推）。"""

    def __init__(self, name, match_dir, cfg, event_fn, board_fn, on_plan=None,
                 ai_enabled=None, timeout=240, chat_fn=None):
        self.name = name
        self.match_dir = match_dir
        self.cfg = cfg
        self.event_fn = event_fn
        self.board_fn = board_fn
        self.on_plan = on_plan
        self.ai_enabled = ai_enabled or (lambda: True)
        self.chat_fn = chat_fn
        self.kimi = str(cfg.get("kimi_bin", "kimi"))
        self.timeout = timeout
        self.sid = None  # 本席会话 id（首拍后从输出解析；换局=新 Seat=自然重置）
        if name == "commander":
            self.interval = float(cfg.get("commander_interval_sec", 60))
            self.enabled = True
        else:
            self.interval = float(cfg.get("middle_interval_sec", 25))
            # 中层四席：cli_middle_enabled 总闸 × cli_middle_seats 名单（默认 land,build）
            seats = {s.strip() for s in str(cfg.get("cli_middle_seats", "land,build")).split(",")
                     if s.strip()}
            self.enabled = bool(cfg.get("cli_middle_enabled", False)) and name in seats
        self.last = 0.0
        self.busy = False
        self.broken = False  # kimi 不在 PATH：记一次事件后停摆（别每拍刷屏）
        self.last_out = ""   # 最近一拍最终文本（dashboard 指挥舱"在干什么"直读，_run 里更新）

    def maybe_fire(self, now):
        if not self.enabled or self.broken or self.busy:
            return
        if now - self.last < self.interval:
            return
        if not self.ai_enabled():
            return
        self.last = now
        self.busy = True
        threading.Thread(target=self._run, daemon=True,
                         name=f"cli-seat-{self.name}").start()

    def _prompt(self):
        if self.name == "commander":
            return COMMANDER_PROMPT
        return _middle_prompt(self.name)

    def _cmd(self):
        """本席 spawn 命令：首拍全新会话（不带 -c——同目录 -c 续"该目录最近会话"，五席互串）；
        拿到 sid 后 `kimi -S <sid>` 续本席自己的会话。"""
        cmd = [self.kimi]
        if self.sid:
            cmd += ["-S", self.sid]
        return cmd + ["--output-format", "stream-json", "-p", self._prompt()]

    def _run(self):
        fired = time.time()
        t0 = fired
        try:
            proc = subprocess.run(
                self._cmd(),
                cwd=self.match_dir, capture_output=True, timeout=self.timeout,
                encoding="utf-8", errors="replace")
            if not self.sid:  # 首拍：抓本席 session_id（stdout 事件流 + stderr 提示都试）
                self.sid = extract_session_id((proc.stdout or "") + "\n" + (proc.stderr or ""))
                self.event_fn({"ev": "seat_session", "seat": self.name, "sid": self.sid})
        except FileNotFoundError:
            self.broken = True
            self.event_fn({"ev": "seat_down", "seat": self.name,
                           "why": f"{self.kimi} 不在 PATH（kimi_bin 可配）"})
            print(f"[cliseat] {self.name}: {self.kimi} 不在 PATH，席位停摆")
            return
        except subprocess.TimeoutExpired:
            self.event_fn({"ev": "seat_timeout", "seat": self.name,
                           "timeout": self.timeout})
            return
        except OSError as e:
            self.event_fn({"ev": "seat_down", "seat": self.name, "why": str(e)[:120]})
            return
        finally:
            self.busy = False
        ms = int((time.time() - t0) * 1000)
        content = parse_stream_json(proc.stdout)
        self.last_out = (content or "")[:600]  # 指挥舱席位卡直读（截断防爆面板）
        if self.name == "commander":
            self._land_commander(content, fired, ms)
        else:
            # 中层：产物=tasks/<branch>.json（状态机每拍自装载）+ board 由 CLI 自写；
            # 它没写板就把最终文本代贴留言板（保底可见）
            if content:
                self.board_fn(self.name, content[:800])
            self.event_fn({"ev": "seat_done", "seat": self.name, "ms": ms})
            print(f"[cliseat] {self.name} 一拍完成 {ms}ms（验证 tasks/{self.name}.json 与 board.md）")

    def _land_commander(self, content, fired, ms):
        """总指挥产物落盘：它自己写了 plan.md 就以文件为准，否则用最终 content 代写。"""
        plan_path = os.path.join(self.match_dir, "plan.md")
        text = None
        try:
            if os.path.exists(plan_path) and os.path.getmtime(plan_path) >= fired - 1:
                with open(plan_path, encoding="utf-8") as fp:
                    text = fp.read()  # CLI 工具亲写（首选）
        except OSError:
            pass
        if text is None and content:
            text = content
            try:
                with open(plan_path, "w", encoding="utf-8") as fp:
                    fp.write(text)  # 代写（保底）
            except OSError:
                pass
        if not text:
            self.event_fn({"ev": "seat_empty", "seat": self.name, "ms": ms})
            return
        params = extract_params_block(text)
        if self.on_plan:
            try:
                self.on_plan(text, params)
            except Exception as e:
                self.event_fn({"ev": "seat_plan_err", "seat": self.name, "why": str(e)[:120]})
        # 总指挥每拍完成后推一行摘要上聊天台（who="指挥"）：阶段+姿态+主攻+一句话方针
        if self.chat_fn:
            try:
                first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
                if params:
                    summ = (f"【{params.get('阶段', '?')}】姿态{params.get('姿态', '?')}"
                            f" 主攻{params.get('主攻') or '-'}｜{first[:40]}")
                else:
                    summ = first[:60]
                self.chat_fn("指挥", summ)
            except Exception:
                pass
        self.event_fn({"ev": "seat_done", "seat": self.name, "ms": ms,
                       "params": bool(params)})
        print(f"[cliseat] 总指挥一拍完成 {ms}ms，plan.md 已更新"
              f"（参数块{'已' if params else '未'}解析）——见 {plan_path}")
