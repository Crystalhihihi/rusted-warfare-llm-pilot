# Launch Rusted Warfare with rw-agent, fully detached (survives the caller).
# ASCII only.
$game = "D:\SteamLibrary\steamapps\common\Rusted Warfare"
$proj = "D:\ttttttt"
Start-Process -FilePath "$game\jvm64\bin\java.exe" `
  -ArgumentList @(
    "-javaagent:$proj\agent\dist\rw-agent.jar=log=$proj\agent\logs\agent.log",
    "-Xmx1000M",
    "-Dfile.encoding=UTF-8",
    "-Djava.library.path=.",
    "-cp", "game-lib.jar;libs/*",
    "com.corrodinggames.rts.java.Main"
  ) `
  -WorkingDirectory $game
Write-Output "launched"
