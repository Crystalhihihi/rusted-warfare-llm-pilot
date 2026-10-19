# -*- coding: utf-8 -*-
"""沙盒探针：设置游戏速度（agent Commands.java 的 cmd=speed，0.1-64x）。

用法: python tools/set_speed.py 8        # 设 8x
      python tools/set_speed.py 1        # 恢复
游戏未运行时连接失败即退出（exit 1）。回执打印一行。"""
import json
import socket
import sys

HOST, PORT = "127.0.0.1", 41234


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    try:
        x = float(sys.argv[1])
    except ValueError:
        print("速度需为数字（0.1-64）")
        return 1
    x = max(0.1, min(64.0, x))
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((HOST, PORT))
    except OSError as e:
        print(f"连不上 {HOST}:{PORT}（游戏/agent 未起？）: {e}")
        return 1
    try:
        s.sendall((json.dumps({"cmd": "speed", "x": x}) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        for line in buf.decode("utf-8", "replace").splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "cmdResult":
                print(("OK " if m.get("ok") else "FAIL ") + str(m.get("msg", "")))
                return 0 if m.get("ok") else 1
        print("未收到 cmdResult（agent 版本旧？需重打包 jar）")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
