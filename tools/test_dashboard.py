# -*- coding: utf-8 -*-
"""监控面板离线验证：mock ctx/st 起真实 HTTP 服务，断言状态接口/指令下发/开关覆盖层。"""
import json
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "bridge")
import dashboard
import fog as fog_mod


class FakeLink:
    def __init__(self):
        self.sent = []

    def snapshot(self):
        return {"tick": 12345, "timeMs": 754000,
                "team": {"id": 1, "funds": 4321, "income": 26, "expense": 3, "ai": False},
                "own": [{"id": 0, "t": "commandCenter", "bld": True, "x": 100, "y": 200}]
                       + [{"id": i} for i in range(1, 45)],
                "allies": [], "enemy": [{"id": 99}],
                "delegated": list(range(30))}

    def ai_control_state(self):
        return {"enabled": True, "cmdCommander": True, "cmdBuild": True, "cmdCombat": True}

    def send(self, msg):
        self.sent.append(msg)


class FakeExecutor:
    work_order = [
        {"idx": 0, "action": "b_extractorT1", "slot": "pool:1", "note": "二池", "status": "done", "x": 100, "y": 200},
        {"idx": 1, "action": "b_landFactory", "slot": "factory:0", "note": "", "status": "assigned", "x": None, "y": None},
    ]
    wo_version = 3
    econ_goal = {"builders": 6}


class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.generation = 2
        self.map_msg = {"path": "maps/ice_lake.tmx", "tileW": 20, "tileH": 20,
                        "tilesW": 4, "tilesH": 2,
                        "rows": ["....", "~~~~"]}
        self.plan = "【阶段】中期\n【经济】五池全开"
        self.plan_params = {"阶段": "mid", "池数": 5}
        self.plan_ts = time.time() - 90
        self.squads = {
            "S1": {"mission": {"任务": "defend", "编成": "tank", "备注": "守家", "规模": 10, "目标": None},
                   "members": {1, 2, 3}},
            "S2": {"mission": None, "members": set()},
        }
        self.broadcasts = []
        self.fog_map = fog_mod.FogMap(self.map_msg)
        self.fog_map.update({"fogGrid": {"w": 4, "h": 2, "e": True, "f": True, "g": False,
                                         "rows": ["0x2;1x2", "2x4"]},
                             "ghosts": [{"t": "turret", "x": 60, "y": 10, "team": 2}]})

    def broadcast(self, text):
        self.broadcasts.append(text)


def main():
    logs = []
    link = FakeLink()
    ctx = {"link": link, "cfg": {"dashboard": True, "dashboard_port": 0},
           "exec_holder": {"ex": FakeExecutor()},
           "log_activity": lambda k, t: logs.append((k, t)),
           "ai_enabled": lambda: True,
           "role_enabled": lambda name: True}
    st = FakeState()
    srv = dashboard.start(ctx, st)
    assert srv is not None, "面板应启动"
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def post(path, obj):
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # 1. 页面
    code, html = get("/")
    assert code == 200 and "总控台" in html, f"首页异常 {code}"

    # 2. 状态接口：三角色在想什么 + 战况
    code, body = get("/api/state")
    d = json.loads(body)
    assert code == 200 and d["ok"], "状态接口异常"
    assert d["game"]["funds"] == 4321 and d["game"]["own"] == 45 and d["game"]["delegated"] == 30
    assert "中期" in d["commander"]["plan"] and d["commander"]["params"]["池数"] == 5
    assert d["commander"]["age_sec"] >= 89
    assert d["build"]["version"] == 3 and d["build"]["builders_target"] == 6
    assert len(d["build"]["tasks"]) == 2 and d["build"]["tasks"][1]["status"] == "assigned"
    sq = {s["id"]: s for s in d["combat"]["squads"]}
    assert sq["S1"]["mission"]["任务"] == "defend" and sq["S1"]["members"] == 3
    assert sq["S2"]["mission"] is None

    # 3. 面板指令 → broadcast + 活动流 + 游戏聊天台
    code, r = post("/api/command", {"text": "转龟缩防守，空军回家"})
    assert code == 200 and r["ok"], "指令下发失败"
    assert any("转龟缩防守" in b and "优先级最高" in b for b in st.broadcasts), st.broadcasts
    assert any("🖥 面板" in t for _, t in logs)
    assert any(m.get("cmd") == "overlay" and "转龟缩防守" in m.get("chat", "") for m in link.sent)
    code, r = post("/api/command", {"text": "   "})
    assert code == 400 and not r["ok"], "空指令应拒绝"

    # 4. 开关覆盖层：强制关 combat → role_enabled 立即变 False；恢复跟随 → 回 True
    code, r = post("/api/switches", {"role": "combat", "state": False})
    assert code == 200 and r["ok"]
    assert ctx["role_enabled"]("combat") is False
    assert ctx["role_enabled"]("commander") is True
    assert r["switches"]["roles"]["combat"]["effective"] is False
    code, r = post("/api/switches", {"role": "combat", "state": None})
    assert ctx["role_enabled"]("combat") is True and r["switches"]["roles"]["combat"]["override"] is None

    # 5. 总开关覆盖 + 非法参数
    code, r = post("/api/switches", {"role": "all", "state": False})
    assert ctx["ai_enabled"]() is False
    post("/api/switches", {"role": "all", "state": None})
    assert ctx["ai_enabled"]() is True
    code, r = post("/api/switches", {"role": "hacker", "state": True})
    assert code == 400 and not r["ok"], "非法角色应拒绝"

    # 6. 地图理解图层：地形按需下发/迷雾 RLE 转发/幽灵/家/单位层
    code, body = get("/api/map")
    m = json.loads(body)
    assert code == 200 and m["ok"] and m["w"] == 4 and m["h"] == 2
    assert m["rows"] is None, "默认不发病重地形（前端按 generation 缓存）"
    assert m["tileW"] == 20
    assert m["fog"] and m["fog"]["rows"] == ["0x2;1x2", "2x4"]
    assert "LOS" in m["modeText"]
    assert len(m["ghosts"]) == 1 and m["ghosts"][0]["t"] == "turret"
    assert m["home"] == [100, 200]
    assert m["units"]["own"][0] == [100, 200, 1, 0]  # commandCenter 建筑标记+军种(陆=0)
    assert m["units"]["enemy"] and m["units"]["allies"] == []
    code, body = get("/api/map?full=1")
    m2 = json.loads(body)
    assert m2["rows"] == ["....", "~~~~"], "full=1 发病重地形"

    # 7. 军种中层：开关自动带出 + /api/squads 覆盖层
    code, r = get("/api/switches")
    sw = json.loads(r)
    assert "land" in sw["roles"] and "air" in sw["roles"] and "navy" in sw["roles"]
    code, r = post("/api/squads", {"land": 5, "air": 0})
    assert r["ok"] and r["caps"]["land"] == 5 and r["override"].get("land") == 5
    assert r["caps"]["air"] == 2 and "air" not in r["override"], "0=非法输入忽略(默认2)"
    code, r = post("/api/squads", {"land": None})
    assert r["caps"]["land"] == 3 and "land" not in r["override"], "None=恢复跟随 config"

    # 8. cli 指挥舱接线：mode 切换 + 席位/任务单/Jev 事件流/board 尾部/静音横幅
    import collections
    import os
    import tempfile
    mdir = tempfile.mkdtemp(prefix="dash-cli-")
    with open(os.path.join(mdir, "board.md"), "w", encoding="utf-8") as fp:
        fp.write("commander: plan.md 已更新\nland: 收到，等第一波\n\nbuild: 池三开建\n")

    class FakeSeat:
        def __init__(self, name, **kw):
            self.name = name
            self.enabled = True; self.busy = False; self.broken = False
            self.last = time.time() - 10; self.interval = 60
            self.sid = "s-1"; self.last_out = "plan.md 已更新\n【阶段】开局"
            for k, v in kw.items():
                setattr(self, k, v)

    fake_match = type("M", (), {})()
    fake_match.dir = mdir
    fake_match.seats = [FakeSeat("commander"), FakeSeat("land", enabled=False)]
    fake_match.engine = type("E", (), {"tasks": {
        "t1": {"id": "t1", "branch": "build", "task": "开局经济", "_status": "active",
               "squad": "", "trigger": {"when": [{"池": ">=4"}]}, "note": ""},
        "t2": {"id": "t2", "branch": "land", "task": "集结", "_status": "pending",
               "squad": "L1", "trigger": {}, "note": ""}}})()
    fake_match.ev_tail = collections.deque([
        {"ts": "01:00:01", "ev": "jev_drive", "squad": "L1", "act": "advance",
         "probs": {"advance": 0.61}},
        {"ts": "01:00:02", "ev": "task", "id": "t1", "status": "active"},
        {"ts": "01:00:03", "ev": "jev_unsure", "squad": "L2", "noul": 0.31},
    ], maxlen=10)
    st.cli_match = fake_match
    code, body = get("/api/state")
    d = json.loads(body)
    assert d["mode"] == "cli", d["mode"]
    c = d["cli"]
    assert len(c["seats"]) == 2 and c["seats"][0]["name"] == "commander"
    assert c["seats"][1]["enabled"] is False and c["seats"][0]["next_in"] is not None
    assert "plan.md" in c["seats"][0]["last_out"]
    assert len(c["tasks"]) == 2
    t1 = next(t for t in c["tasks"] if t["id"] == "t1")
    assert t1["status"] == "active" and "池" in t1["when"], t1
    assert len(c["jev"]) == 2 and c["jev"][0]["ev"] == "jev_drive", c["jev"]
    assert "land: 收到" in c["board"]
    assert c["muted"] is False
    fake_match.seats = []
    code, body = get("/api/state")
    d = json.loads(body)
    assert d["cli"]["muted"] is True, "无席位=静音模式横幅"
    del st.cli_match
    code, body = get("/api/state")
    d = json.loads(body)
    assert d["mode"] == "legacy" and d["cli"] is None, "拔掉 cli_match 回 legacy"

    srv.shutdown()
    print("PASS test_dashboard：页面/状态/指令/开关覆盖层/地图图层/CLI 指挥舱 全部符合预期")


if __name__ == "__main__":
    main()
