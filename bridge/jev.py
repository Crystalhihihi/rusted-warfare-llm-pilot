#!/usr/bin/env python3
"""Jev/Kev 快层客户端（架构二期）：POST /v1/systemone 薄封装，stdlib urllib（与 llm.py 同款）。

- 超时 1.5s（快层 2s 拍耗不起）；失败/超时/解析坏 → 返回 None，调用方降级=不动+记事件。
- 请求形态（tools/test_kev_probe.py 实测口径）：
    {"state": str, "questions": {"qid": {"type":"noul","instructions":"..."}
                                  | {"type":"choice","instructions":"...","criteria":{"选项":"描述",...}}}}
  应答 body["answers"][qid]：noul→.noul；choice→.choice/.probabilities（.confidence 有的版本有）。
- 置信度低带（0.8B 本地档概率挤 0.47~0.72，hedged 是常态）：noul∈[0.35,0.65]、
  choice 顶端概率 <0.6 → 视为"拿不准"，调用方不动+上报中层。
"""
import json
import time
import urllib.request

NOUL_LOW, NOUL_HIGH = 0.35, 0.65   # noul 置信带：带内=拿不准
CHOICE_FLOOR = 0.6                 # choice 顶端概率地板：低于=拿不准


class Jev:
    def __init__(self, url="http://127.0.0.1:8009", enabled=True, timeout=1.5):
        self.url = (url or "").rstrip("/")
        self.enabled = enabled
        self.timeout = timeout
        self.down_since = None   # 连续失败起点（调用方节流/诊断用）

    def ask(self, state, questions):
        """→ answers dict | None（任何失败都 None，绝不抛）。"""
        if not self.enabled or not self.url or not questions:
            return None
        payload = {"state": str(state), "questions": questions}
        req = urllib.request.Request(
            self.url + "/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json",
                     "authorization": "Bearer local"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception:
            self.down_since = self.down_since or time.time()
            return None
        ans = body.get("answers")
        if not isinstance(ans, dict):
            return None
        self.down_since = None
        return ans


def unsure_noul(ans, lo=NOUL_LOW, hi=NOUL_HIGH):
    """noul 答案置信度低（拿不准→不动+上报）。lo/hi 可配（cli 模式读 config 阈值）。"""
    v = (ans or {}).get("noul")
    return v is None or lo <= v <= hi


def unsure_choice(ans, top=CHOICE_FLOOR, margin=None):
    """choice 答案置信度低。两闸：顶概率 ≥ top；margin 开启时再要 顶概率 ≥ margin×次高概率
    （相对 margin 治 0.8B 档绝对置信度系统性偏低——实测顶概率上限 ~0.47，0.6 一刀切永远不够格）。
    margin=None=旧行为（只看顶概率地板）；无概率数据=拿不准（保守不动）。"""
    ans = ans or {}
    probs = ans.get("probabilities")
    if isinstance(probs, dict) and probs:
        try:
            vals = sorted((float(v) for v in probs.values()), reverse=True)
        except (TypeError, ValueError):
            return True
        if not vals or vals[0] < top:
            return True
        return bool(margin) and len(vals) > 1 and vals[0] < margin * vals[1]
    conf = ans.get("confidence")
    return conf is None or float(conf) < top
