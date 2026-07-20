# Rusted Warfare LLM Pilot

[中文](README.md) | **English**

> ## ⚠️ WARNING
>
> **This project is at an early experimental stage. The AI's control of the game
> has many known defects, including but not limited to:**
>
> - Building on resource points may fail (placement/occupancy checks are incomplete)
> - Production and construction lack variety; upgrading is effectively broken
> - Disorientation in some openings (cannot locate the HQ, wanders to faraway resource points)
> - Buildings may be wrongly included in move orders; macro control is essentially serial
>
> **DO NOT use this project in multiplayer / online matches.**
> The AI's mistakes will drag down teammates and ruin other players' experience,
> and automated control violates fair-play principles in any multiplayer setting.
> **This project is for single-player, local reverse-engineering research only.
> You are solely responsible for any consequences of misuse.**

An experiment that connects large language models to the real-time strategy game
Rusted Warfare: it extracts live battlefield state in a non-intrusive way, lets an
LLM make decisions, and injects the resulting orders back to command your forces.

## How it works

```
Rusted Warfare (JVM)
  └─ javaagent: rw-agent.jar ──TCP 127.0.0.1:41234 / NDJSON──▶ bridge (Python)
                                                                   │  OpenAI-compatible API (DeepSeek by default)
                                                                   ▼
                                        LLM decisions → orders injected via the game's normal command queue
```

- **agent/**: a Java `javaagent` attached at game launch (it modifies no game
  files) that periodically emits battlefield snapshots (own/ally/visible-enemy
  unit counts, funds, team state, downsampled ASCII map, etc.).
- **bridge/**: a Python bridge (standard library only, Python ≥ 3.11) that turns
  snapshots into prompts for an OpenAI-compatible API and translates the model's
  tool calls back into game orders. Includes adaptive tick rates (faster under
  threat, slower when calm), an APM cap with human-like pacing, and hot-reloadable
  squad delegation.
- **tools/**: an analysis probe and a launcher script that starts the game with
  the agent attached.

Fair-play by design: only information within the team's own vision is forwarded;
orders go through the game's normal command queue; everything is rate-limited.

## Quick start

1. **Build the agent** (JDK 11+): point `GAME_DIR` in `agent/build.sh` at your
   game installation, then run `bash agent/build.sh` — output: `agent/dist/rw-agent.jar`.
   (Game jars are referenced at compile time only; nothing from the game is bundled.)
2. **Configure the model**: copy `bridge/secret.toml.example` to
   `bridge/secret.toml` and fill in your DeepSeek API key (or set the
   `DEEPSEEK_API_KEY` environment variable). `bridge/config.toml` holds
   `base_url`, `model`, tick rates, APM cap, etc.
3. **Launch the game**: edit paths in `tools/run-with-agent.bat` and run it —
   the game starts with the agent attached.
4. **Run the bridge**:
   - `python bridge/main.py` — prints battlefield summaries (useful for bring-up)
   - `python bridge/pilot.py` — the LLM commander loop

## Compliance

- **No multiplayer use** (see the warning above): known control defects would
  drag down teammates and violate fair play.
- Single-player, local reverse-engineering research only.
- This repository contains no game files and no decompiled code. Please support
  the official game.
- This project is not affiliated with Corroding Games in any way.

## License

[MIT](LICENSE)
