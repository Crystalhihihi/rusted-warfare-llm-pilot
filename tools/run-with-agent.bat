@echo off
rem Launch Rusted Warfare with rw-agent attached. Does not modify any game file.
cd /d "D:\SteamLibrary\steamapps\common\Rusted Warfare"
if not exist "D:\ttttttt\agent\logs" mkdir "D:\ttttttt\agent\logs"
jvm64\bin\java -javaagent:"D:\ttttttt\agent\dist\rw-agent.jar"=log="D:\ttttttt\agent\logs\agent.log" -Xmx1000M -Dfile.encoding=UTF-8 -Djava.library.path=. -cp "game-lib.jar;libs/*" com.corrodinggames.rts.java.Main
pause
