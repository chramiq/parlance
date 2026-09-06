# AGENTS.md

parlance: an offline-first generator of personal Anki material for speaking a
language, built for a learner who knows one language (KNOWN_LANG) and wants to
actually produce another (TARGET_LANG) — not just recognize it. Language-pair
agnostic; the pair is runtime config from `.env`. Cards are built
from the learner's own chat exports (optional) and a subtitle-frequency
wordlist; pydantic-ai agents write, grade and maintain the content; a local
SQLite DB is the single source of truth; genanki emits decks with stable
GUIDs so reimports update in place.

The flagship card is a chat simulation: an incoming message arrives (text +
audio), the learner reads, understands and replies with an entire sentence.
Success = production, not memorization.

## Repo layout

| Path | What lives there |
|---|---|
| `parlance/cli.py` | typer CLI: `worklist`, `import-chats`, `profile`, `generate`, `curate`, `deck`, `practice` |
| `parlance/config.py` | pydantic-settings from `.env`: LLM endpoint, language pair, paths |
| `parlance/store.py` | SQLite source of truth: lemmas, glosses, variants, sources, card state, priority score |
| `parlance/worklist.py` | frequency list -> simplemma lemma queue, cognate classification, dedup vs store |
| `parlance/profile.py` | learner-profile builder from imported chats (topics, register, recurring situations) |
| `parlance/chats/` | export parsers: `meta.py` (Instagram + Messenger DYI json), `signal.py` (signal-export output) |
| `parlance/agent.py` | the single pydantic-ai agent: generates translations, profiles chats, maintains the store via tools (batch prompts or conversation) |
| `parlance/deck/` | Anki emission: `model.py` (note types + templates), `build.py` (genanki, GUID/due/media) |
| `parlance/tts.py` | pluggable TTS with content-hash audio cache (edge-tts default, Google Chirp3-HD optional) |
| `docs/` | architecture decision records (`INDEX.md` + one file per ADR) — read these for why |
| `tests/` | pytest; agents run offline against TestModel/FunctionModel |
| `var/` | all runtime state (gitignored): `parlance.db`, audio cache, chat exports |
| `TODO.md` | deferred ideas and tasks |

## Stack

| Component | Purpose |
|---|---|
| pydantic-ai | agents, structured output (ToolOutput/NativeOutput/PromptedOutput), TestModel/FunctionModel for offline tests |
| LiteLLM gateway | OpenAI-compatible LLM endpoint; base_url + api_key + one model name in `.env`; `OpenAIChatModel` + `OpenAIProvider` |
| genanki | .apkg emission, deterministic `guid=`, `due=` new-card ordering, `::` subdecks |
| simplemma | lemmatization for both languages (dictionary-based, offline, pl + cs) |
| typer / pydantic-settings | CLI / `.env` config |
| edge-tts | default free TTS; target-language voice from `.env` |
| sqlite3 (stdlib) | the database; no ORM |
| ruff + ty / pytest | lint + format + typecheck / tests |

## Setup

```bash
uv sync
cp .env.example .env  # fill in LiteLLM base_url + api key
```

## Develop

```bash
uv run ruff check --fix .
uv run ruff format .
uv run ty check .
uv run pytest
uv run parlance <command>
```

## Rules

### Workflow

- Features land in a fixed order: research -> probe -> implement -> test ->
  review -> docs. The probe is a throwaway script capturing real
  API/file output; code is written against the probe, never from docs alone.
- New external-format integrations are probe-first: a temp probe script runs
  against a real export file and the parser is written from the captured
  structure (see docs/0001).
- When the user states a durable decision or preference, record it:
  behavior/rules here in AGENTS.md, task deferrals in TODO.md.
- After completing a change and running the checks, ask whether to spawn the
  reviewer subagent before committing. Respect the answer.

### Code

- Use `uv add` / `uv remove` for dependencies, never hand-edit pyproject deps.
- No `__init__.py` files. Use implicit namespace packages (PEP 420).
- New features and structural changes get an ADR in `docs/`: one file per
  decision, appended to `docs/INDEX.md`. Amend the ADR when the decision changes.
- Python 3.13 target.
- Run all three checks after every change: `ruff check`, `ruff format --check`,
  `ty check`.
- Tool failures surface as explicit, structured error text the model can act
  on — never silent empty results, never raw third-party exceptions.
- Tests must exercise real functionality: SQLite round-trips, parser fixtures
  from real (scrubbed) export snippets, genanki output structure. Agent tests
  run against TestModel/FunctionModel — never the network.
- The user reviews on AnkiDroid and desktop. Anything built must degrade
  sanely on mobile: no desktop-add-on dependency in the happy path.

### Documentation

- Comments are for non-obvious invariants and the why of a surprising
  construction. Never restate code, never narrate past behavior.
- Docstrings are Google-style: one-line summary, blank line, body,
  `Args:`/`Returns:`. A fact lives in exactly one place.
- Agent tool docstrings are LLM input; examples go inline in the body, never
  in sections after `Args:`.
- Design rationale lives in `docs/` ADRs, not in code comments.

### Research

- Confirm current APIs before writing code against fast-moving libraries.
  Pydantic ecosystem: fetch `https://pydantic.dev/docs/ai/llms.txt`, then
  individual pages as `.md`; Context7 as fallback.
- `gh` is authenticated for GitHub research.
