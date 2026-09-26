# AGENTS.md

parlance: an generator of personal Anki cards for speaking a
language, built for a learner who knows one language (KNOWN_LANG) and wants to
actually produce another (TARGET_LANG). Language-pair agnostic; 
the pair is runtime config from `.env`. Cards are built
from the subtitle-frequency wordlist; pydantic-ai agents write, grade 
and maintain the content; genanki emits decks with stable
GUIDs so reimports update in place.

## Repo layout

| Path | What lives there |
|---|---|
| `parlance/cli.py` | typer CLI: `worklist`, `init`, `generate`, `deck build`, `audit`, `lexicon`, `llm-hints`, `chats`, `practice`, `practice-log` |
| `parlance/config.py` | pydantic-settings from `.env`: LLM endpoint, Gemini key, language pair, paths |
| `parlance/store.py` | SQLite source of truth: lemmas, content, changes journal, lexicon hints/words, practice sessions |
| `parlance/worklist.py` | frequency list -> simplemma lemma queue, dedup vs store |
| `parlance/cognates.py` | known-language vocab map + orthographic similarity tags; the model owns all cognate judgments |
| `parlance/agent.py` | the single pydantic-ai agent + store tools (poll_vocab, lookup, submit_translations, set_statuses, save_pair_profile; review_sample in audit mode); tool-arg schemas are the validation boundary |
| `parlance/lexicon.py` | hint grounding: kaikki edition pairs, gemini-live spoken-JSON hints, validation wordlist |
| `parlance/practice.py` | voice drill session over Gemini Live: digest, tools, session lifecycle, transcript |
| `parlance/deck/` | Anki emission: `model.py` (note types + templates), `build.py` (genanki, GUID/due/media) |
| `parlance/tts.py` | TTS providers + content-hash audio cache (edge-tts default) |
| `parlance/chats/` | Meta DIY export ingestion (`meta.py`): the owner's own words -> gated, diacritic-restored, deduped personal wordlist |
| `parlance/prompts/` | agent instruction files (`vocab.md`, `audit.md`, `practice.md`); profile JSON is rendered in at build time |
| `docs/` | architecture decision records (`INDEX.md` + one file per ADR) — read these for why |
| `tests/` | pytest; agents run offline against TestModel/FunctionModel |
| `scripts/` | probe scripts that ran against real exports/APIs; kept as the reference the parsers were written against |
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
cp .env.example .env
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
  review -> docs. The probe captures real API/file output; code is written
  against the probe, never from docs alone. Probe scripts live in `scripts/`
  and persist after use — they are the reference the parser was written
  against.
- New external-format integrations are probe-first: a temp probe script runs
  against a real export file and the parser is written from the captured
  structure
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
- Hard rule: never reference documents, probes, dates, or "verified-on"
  markers in docs or comments. Facts are stated on their own; git history
  owns the when. No personal details about the user anywhere in the repo.

### Research

- Always confirm current APIs before writing code against libraries.
  Pydantic ecosystem: fetch `https://pydantic.dev/docs/ai/llms.txt`, then
  individual pages as `.md`; Context7 as fallback.
- `gh` is authenticated for GitHub research.
