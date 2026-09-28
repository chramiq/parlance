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
| `parlance/cli.py` | typer CLI: `worklist`, `init`, `generate`, `deck build`, `audit`, `lexicon`, `chats`, `profile`, `practice`, `practice-log`, `anki-sync` |
| `parlance/config.py` | pydantic-settings from `.env`: Gemini key/model, language pair, paths |
| `parlance/store.py` | SQLite source of truth: lemmas, content, changes journal, lexicon hints/words, practice sessions |
| `parlance/worklist.py` | frequency list -> simplemma lemma queue, dedup vs store |
| `parlance/cognates.py` | known-language vocab map + orthographic similarity tags; the model owns all cognate judgments |
| `parlance/agent.py` | the single pydantic-ai agent + store tools (poll_vocab, lookup, submit_translations, set_statuses, save_pair_profile; review_sample in audit mode); tool-arg schemas are the validation boundary |
| `parlance/lexicon.py` | hint grounding: kaikki edition pairs + validation wordlist |
| `parlance/practice.py` | voice drill session as a pydantic-ai realtime agent over Gemini Live: digest, store tools, session lifecycle, transcript |
| `parlance/anki.py` | AnkiConnect client: today's reviewed words off desktop Anki (read-only relay: phone -> AnkiWeb -> desktop) |
| `parlance/deck/` | Anki emission: `model.py` (note types + templates), `build.py` (genanki, GUID/due/media) |
| `parlance/tts.py` | TTS providers + content-hash audio cache (edge-tts default) |
| `parlance/chats/` | Meta DIY export ingestion (`meta.py`) + personal pre-pass (`translate.py`): the owner's words -> gated wordlist -> target lemmas |
| `parlance/profile.py` | pair-profile show/edit (the stored document that seeds generation style) |
| `parlance/prompts/` | agent instruction files (`vocab.md`, `audit.md`, `translate.md`, `practice.md`): language-pair-agnostic workflow text, `{KNOWN}`/`{TARGET}` placeholders; profile JSON is rendered in at build time |
| `docs/` | architecture decision records (`INDEX.md` + one file per ADR) — read these for why |
| `tests/` | pytest; agents run offline against TestModel/FunctionModel |
| `var/` | all runtime state (gitignored): `parlance.db`, audio cache, chat exports |
| `TODO.md` | deferred ideas and tasks |

## Stack

| Component | Purpose |
|---|---|
| pydantic-ai | agents, realtime sessions (Gemini Live practice), structured output (ToolOutput/NativeOutput/PromptedOutput), TestModel/FunctionModel for offline tests |
| Gemini API | one API key + one model name in `.env`; `GoogleModel` + `GoogleProvider` (google-genai under the hood); same key serves Live (practice) |
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
  against the probe, never from docs alone. Probes are throwaway: run from
  a scratch dir, never committed, deleted after the finding is recorded.
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
- No hardcoded languages. Wordlists, URLs, prompts, error text and
  tokenizers take the language from config or a parameter; nothing is
  scoped to one language. The only allowed language table is the
  display-name map (LANG_NAMES) with a code fallback.
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
- Design rationale lives in `docs/` ADRs, not in code comments. Code never
  references ADRs the other way either — no `see docs/NNNN` in comments or
  docstrings.
- Docstrings and comments describe only how the code works NOW.
- Hard rule: a docstring states what the method, function or class does
  as it works right now, concisely. Never how it worked before, what
  happened during development, how the code got here, or what changed —
  that history belongs to git and the ADRs.
- No Markdown in docstrings or comments: no backticks, no emphasis, no
  links. Identifiers are written bare.
- Hard rule: never reference documents, probes, scripts, dates, or
  "verified-on" markers in docs or comments. Facts are stated on their
  own; git history owns the when. No personal details about the user
  anywhere in the repo.

### Research

- Always confirm current APIs before writing code against libraries.
  Pydantic ecosystem: fetch `https://pydantic.dev/docs/ai/llms.txt`, then
  individual pages as `.md`; Use Context7 extensively, same for websearch.
- `gh` is authenticated for GitHub research.
