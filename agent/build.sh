#!/usr/bin/env bash
# Build rw-agent.jar. Requires javac (JDK 11+) on PATH; game jars are only used
# at compile time and are NOT bundled into the agent.
set -euo pipefail
GAME_DIR="D:/SteamLibrary/steamapps/common/Rusted Warfare"
ROOT="D:/ttttttt/agent"
cd "$ROOT"
mkdir -p classes dist logs lib

# ASM（字节码 hook：聊天输入框按键拦截用）。游戏 libs 无此依赖，
# 构建期从 Maven Central 一次性下载到 lib/，shade 进 agent jar。
# 下载失败不阻断构建——hook 自动降级（OverlayInput 无回调，输入框只读）。
ASM_JAR="lib/asm-9.7.1.jar"
if [ ! -f "$ASM_JAR" ]; then
  echo "downloading ASM ..."
  curl -fsSL "https://repo1.maven.org/maven2/org/ow2/asm/asm/9.7.1/asm-9.7.1.jar" -o "$ASM_JAR" \
    || echo "WARN: ASM download failed, hooks will degrade gracefully"
fi
CP="$GAME_DIR/game-lib.jar;$GAME_DIR/libs/*"
[ -f "$ASM_JAR" ] && CP="$CP;lib/*"

find src -name '*.java' > sources.txt
javac --release 8 -encoding UTF-8 -cp "$CP" -d classes @sources.txt
if [ -f "$ASM_JAR" ]; then
  (cd classes && jar xf "../$ASM_JAR")   # shade org/objectweb/asm/** 进 agent
fi
printf 'Premain-Class: rw.agent.Agent\nCan-Redefine-Classes: false\nCan-Retransform-Classes: false\n' > manifest.txt
# OUT_JAR 可覆盖输出名：游戏运行期间 rw-agent.jar 被锁，用 OUT_JAR=dist/rw-agent-new.jar 打包新副本
OUT_JAR="${OUT_JAR:-dist/rw-agent.jar}"
jar cfm "$OUT_JAR" manifest.txt -C classes .
echo "OK: $OUT_JAR"
