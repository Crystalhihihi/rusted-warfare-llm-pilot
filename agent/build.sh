#!/usr/bin/env bash
# Build rw-agent.jar. Requires javac (JDK 11+) on PATH; game jars are only used
# at compile time and are NOT bundled into the agent.
set -euo pipefail
GAME_DIR="D:/SteamLibrary/steamapps/common/Rusted Warfare"
ROOT="D:/ttttttt/agent"
cd "$ROOT"
mkdir -p classes dist logs
find src -name '*.java' > sources.txt
javac --release 8 -encoding UTF-8 -cp "$GAME_DIR/game-lib.jar;$GAME_DIR/libs/*" -d classes @sources.txt
printf 'Premain-Class: rw.agent.Agent\nCan-Redefine-Classes: false\nCan-Retransform-Classes: false\n' > manifest.txt
jar cfm dist/rw-agent.jar manifest.txt -C classes .
echo "OK: dist/rw-agent.jar"
