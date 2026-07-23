# -*- coding: utf-8 -*-
"""单步 GUI 驱动：激活游戏窗口 -> 点击 -> 截屏（同一进程内完成，焦点不被终端抢走）。
用法: python tools/gui_step.py <x> <y> [out.png]
"""
import subprocess
import sys
import time

import pyautogui

x, y = int(sys.argv[1]), int(sys.argv[2])
out = sys.argv[3] if len(sys.argv) > 3 else "tools/screen.png"
subprocess.run(["powershell", "-Command",
                "(New-Object -ComObject WScript.Shell).AppActivate('Rusted Warfare')"],
               capture_output=True)
time.sleep(0.7)
pyautogui.click(x, y)
time.sleep(1.0)
subprocess.run(["powershell", "-Command",
                "(New-Object -ComObject WScript.Shell).AppActivate('Rusted Warfare')"],
               capture_output=True)
time.sleep(0.4)
pyautogui.screenshot().save(out)
print(f"clicked ({x},{y}) -> {out}")
