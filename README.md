# NewEra

A multiplayer text adventure (a MUD) in the browser, where players type whatever they want in plain Chinese and an LLM narrates what happens. The difference from "ChatGPT as a dungeon master": **the model never owns the game state.** Every number, item, hit and death is decided by a deterministic rules engine; the model only interprets what the player meant and describes what the engine says happened.

> The game itself is in Chinese. This README is in English for portfolio readers; code comments and design notes are in Chinese.

<!-- Screenshot / short gameplay video goes here -->

## Why this design

Letting an LLM run a game directly breaks quickly. It forgets an item a player picked up three turns ago, lets a clever sentence ("I swing so hard the dragon dies instantly") win a fight, and invents doors that don't exist. Multiplayer makes it worse, because two players can be told two different versions of the same room.

So the model gets two narrow jobs and no authority:

1. **Understanding** — turn free text into structured actions (`attack n2`, `stunt` with a requested damage tier and difficulty, `give i3 to n1`).
2. **Describing** — write a short narration from a list of facts the engine produced, and nothing else.

Everything in between is plain Python with row-level locks in Postgres.

## How a turn works

```
player text
   │
   ▼
1. Parse      commands.py — hand-written rules cover common phrasings (fast, free)
              ai.parse_intent — LLM fallback with structured output for everything else
   │          → list of typed actions (pydantic models in schema.py)
   ▼
2. Execute    engine.execute_all — validates every action against the database,
              rolls dice, applies damage, moves items; anything the AI asked for
              is clamped (an "instant kill" request becomes whatever the rules allow)
   │          → list of facts ("Goblin takes 31 damage, HP 189/220")
   ▼
3. Decide     ai.decide_give — separate small call: does the NPC hand something over?
              (asked before narration so the story can never promise an item
              the engine didn't move)
   ▼
4. Narrate    ai.narrate — writes 2–4 sentences from the facts only; NPC dialogue is a
              separate role-play call wrapped into the narration verbatim
```

A few things that took real iteration:

- **Facts-only narration.** Player names are swapped for placeholders before the model sees them (a player called "寒风", *cold wind*, kept getting written into the weather), and swapped back afterwards.
- **Combat rounds for groups.** In a fight, each party member's command is queued; when everyone has acted, enemies act once. Rounds that get stuck (a deploy mid-fight, a dropped connection) are closed automatically on startup and after a timeout.
- **Stealth.** Detection chance grows each action, depends on light, cover, monster perception and how many enemies are watching; sneak attacks, hiding mid-fight and "keen" monsters that can't be sneaked past.

## What's in the game

- A small village (tavern, smithy, general store) with five NPCs who remember each player, track affinity, sell, upgrade, refine gems and react to gifts.
- A procedurally generated dungeon: each floor is a 3×3 grid with combat, treasure, event and rest rooms, a stairs guardian, teleport stones every five floors and a gate boss every 25.
- 13 dungeon themes with their own rules, including:
  - **Forge**: constant heat damage, cooled by spring water;
  - **Crystal caverns**: glare that blinds unless you close your eyes;
  - **Silent temple**: a noise meter that wakes statues and summons wanderers;
  - **Desert**: shifting exits and quicksand;
  - **Dream corridor**: drifting light and scrambled distances;
  - **Fog sea**: monsters appear at arm's length and a ferryman charges a fare;
  - **Astral expanse**: a light cycle and edges you can be knocked off.
- 60+ monsters, 35+ events, 200+ items, gems with sockets, upgrade paths, damage-over-time effects, crowd control with immunity windows, bosses with telegraphed attacks, combos and phase changes.
- Two gate bosses built around player choices rather than raw stats:
  - **Anubis**: drinking potions or dodging adds "sin" to the scales he weighs you with;
  - **Perseus**: he vanishes, flies and has to be pulled down with a rope, and petrifies anyone who doesn't close their eyes.

All game content lives in YAML (`world.yaml`, `dungeon.yaml`, `items_dungeon.yaml`, `loot.yaml`, `deep_*.yaml`) and is loaded by the engine; adding a monster or an event is data, not code.

## Balancing with a simulator

`balance/sim.py` is a Monte Carlo model of the same rules: it plays thousands of runs through 30 floors for different builds (sword and shield, dual wield, stealth, ranged) and gear tiers, including the in-game economy (gold, upgrades, potions). Every combat change is checked against per-floor survival targets before it ships; `balance/result.md` is the current baseline. `balance/bosses.py` tunes individual bosses, and `balance/calibrate.py` compares the model against real players' logs.

## Tech

- **Backend:** Python 3.14, FastAPI, psycopg 3 (connection pool, explicit transactions, `SELECT … FOR UPDATE` where players can race)
- **Database:** Postgres on Supabase (`schema.sql`)
- **LLMs:** provider-agnostic wrapper with structured output, retries with feedback and model fallback (Zhipu GLM by default; Anthropic and Gemini supported), token and latency logging per call
- **Frontend:** a single static HTML page plus an admin panel (`static/`), no framework
- **Hosting:** Render

## Project layout

| File | What it does |
|---|---|
| `server.py` | HTTP API, turn pipeline, combat round scheduling |
| `engine.py` | the rules engine: every action handler, combat, stealth, effects, NPC services |
| `rules.py` | pure formulas (damage, hit chance, scaling by depth, upgrade odds), shared with the simulator |
| `dungeon.py` | floor generation, themes, spawning, loot |
| `commands.py` | rule-based parser for common commands |
| `ai.py` | all LLM calls: parsing, giving decisions, role-play, narration |
| `schema.py` / `schema.sql` | action and state models / database schema |
| `seed.py` | loads the world from YAML into the database |
| `balance/` | simulator and balance reports |

## Running it locally

You need Python 3.14, a Postgres database and an API key for one of the supported LLM providers.

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in DATABASE_URL and an AI key
psql "$DATABASE_URL" -f schema.sql                    # create tables
python seed.py                                        # load the world (resets it)
uvicorn server:app --reload
```

Then open http://localhost:8000. Without an AI key the game still runs on the rule-based parser, with plain fact lines instead of narration.

## Status

A working prototype played by a handful of friends. It's a personal project: the code is published to show how it's built, not as a reusable library.

## License

Copyright © 2026 HFbise. All rights reserved. The source is public for viewing only; no permission is granted to use, copy, modify or distribute it. See [LICENSE](LICENSE).
