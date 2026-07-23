#!/usr/bin/env python3
"""Minimal OpenAI-compatible chat client with tool-calling, stdlib only."""
import json
import time
import urllib.error
import urllib.request


def _logfile(line):
    """llm 调用时序同时落文件（pilot.log），并发重叠可复查。"""
    try:
        with open("pilot.log", "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


class LlmError(Exception):
    pass


def chat(base_url: str, api_key: str, model: str, messages: list, tools: list,
         timeout: int = 45, max_retries: int = 2) -> dict:
    """Returns the assistant message dict (may contain content and/or tool_calls)."""
    import threading
    t_start = time.time()
    line = f"[llm] {time.strftime('%H:%M:%S')} 调用开始 model={model} 线程={threading.current_thread().name}"
    print(line)
    _logfile(line)
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(body).encode("utf-8")
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            msg = payload["choices"][0]["message"]
            msg["_finish_reason"] = payload["choices"][0].get("finish_reason")
            msg["_usage"] = payload.get("usage") or {}
            line = f"[llm] {time.strftime('%H:%M:%S')} 调用结束 model={model} 耗时={time.time() - t_start:.1f}s"
            print(line)
            _logfile(line)
            return msg
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last_err = LlmError(f"HTTP {e.code}: {detail}")
            if e.code in (400, 401, 403, 404):
                raise last_err  # 不可重试的错误
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
            last_err = LlmError(str(e))
        if attempt < max_retries:
            time.sleep(2 * (attempt + 1))
    raise last_err
