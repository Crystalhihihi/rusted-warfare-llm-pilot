# Rusted Warfare LLM Pilot

[中文](README.md) | **English**

> ## ⚠️ WARNING
>
> **Experimental project — DO NOT use in multiplayer / online matches.**
> Automated control violates fair-play principles, and AI mistakes will drag down
> teammates. **Single-player, local reverse-engineering research only.
> You are solely responsible for any consequences of misuse.**
>
> Known limitations (the layered v2 architecture has fixed most early control defects):
> - The executor's build order is a fixed template (land maps mainly); water maps and
>   exotic starts are not well tested
> - Stalled constructions (e.g. builder died) are not auto-resumed — a status line
>   reports them for manual completion
> - Strategic quality depends on the LLM you plug in; weak models still make bad calls

An experiment that connects large language models to the real-time strategy game
Rusted Warfare: it extracts live battlefield state in a non-intrusive way, lets an
LLM make decisions, and injects the resulting orders back to command your forces.

## How it works (three layers)

```
Rusted Warfare (JVM)
  └─ javaagent: rw-agent.jar ──TCP 127.0.0.1:41234 / NDJSON──▶ bridge (Python)
                                                                 │
   ┌─ Strategy layer (LLM, ~every 25s: attack/defend/expand/build_army only)
   │      token budget ≈ 1/30 of a pure-LLM micro-control design
   ├─ Executor (deterministic FSM: build order / production / upgrades / builders)
   └─ Reflex (pure rules, millisecond: retreat under fire, defend the base)
                                                                 ▼
                                     atomic orders injected via the game's normal command queue
```

- **agent/**: a Java `javaagent` attached at game launch (modifies no game files),
  emitting periodic battlefield snapshots (own/ally/visible-enemy units, funds,
  build progress, production queues, ASCII map and resource points).
- **bridge/executor.py**: deterministic execution layer — default build order,
  action-availability self-learning, parallel builders, stalled-construction
  detection; runs the economy with zero LLM calls (`LLM_OFF=1`).
- **bridge/pilot.py**: strategy layer — distills the battlefield into a 12×12
  strategic grid plus executor status, asks an OpenAI-compatible API (DeepSeek by
  default) for one strategic verb every ~25s, and translates it into goals or orders.
- **bridge/reflex.py**: pure-rule reflexes — retreat when the squad's HP crashes,
  pull back when the base is threatened.
- **tools/**: an analysis probe and a launcher that starts the game with the agent.

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
   - `python bridge/main.py` — battlefield summary printout (bring-up check)
   - `LLM_OFF=1 python bridge/pilot.py` — executor-only mode (zero tokens, verify auto-economy)
   - `python bridge/pilot.py` — full mode (executor economy + reflexes + LLM strategy)

   In-game, delegate units first: click "control all" on the overlay panel, or box-select
   units and click "delegate selection".

## Compliance

- **No multiplayer use** (see the warning above): known control defects would
  drag down teammates and violate fair play.
- Single-player, local reverse-engineering research only.
- This repository contains no game files and no decompiled code. Please support
  the official game.
- This project is not affiliated with Corroding Games in any way.

## License

[MIT](LICENSE)
