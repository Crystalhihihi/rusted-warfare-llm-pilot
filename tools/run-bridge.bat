@echo off
rem Launch only the bridge (pilot) in a new window. Game must already be running.
rem NOTE: ASCII only. Chinese in .bat breaks cmd.exe parsing.
start "rw-pilot bridge" cmd /k "cd /d D:\ttttttt\bridge && python -u pilot.py"
