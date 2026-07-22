@echo off
rem ============================================
rem  Rusted Warfare + LLM AI one-click launcher
rem  1. start game with rw-agent  2. start Python bridge
rem  NOTE: ASCII only. Chinese in .bat breaks cmd.exe parsing.
rem ============================================
cd /d %~dp0

rem Check for an already-running agent (port 41234 in use)
netstat -ano | findstr ":41234" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [WARN] port 41234 already in use - an agent instance may already be running.
  echo        Close the old game/bridge first, or press any key to continue anyway.
  pause >nul
)

echo [1/2] Launching Rusted Warfare with rw-agent ...
start "Rusted Warfare + rw-agent" cmd /c "tools\run-with-agent.bat"

echo [2/2] Waiting 10s for the agent, then launching the bridge ...
timeout /t 10 /nobreak >nul
start "rw-pilot bridge (LLM)" cmd /k "cd /d %~dp0bridge && python -u pilot.py"

echo.
echo Done. In game: start a skirmish, then click the [control all] button
echo on the AI overlay panel (or box-select units and click [delegate]).
echo Chat command: /s your message to the AI (English only).
pause
