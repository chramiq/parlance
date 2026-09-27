# parlance

Personal Anki card generator for *speaking* a language: you know one
language, you want to actually produce another. A pydantic-ai agent writes
validated cards from a spoken-frequency wordlist against a SQLite store,
genanki emits decks whose stable GUIDs update in place on reimport.

## Setup

```bash
uv sync
cp .env.example .env   # set GEMINI_API_KEY (https://aistudio.google.com/apikey),
                       # KNOWN_LANG (you speak), TARGET_LANG (you learn)
```

One Gemini API key drives everything (card generation, voice practice).
A billing-linked key is expected for batch generation; the free tier's
daily limits are too small.

## The loop

```bash
uv run parlance worklist build   # frequency list -> lemma queue
uv run parlance lexicon          # dictionary hint pairs + validation wordlist
uv run parlance init             # establish the language-pair profile
uv run parlance generate         # write cards (restart-loop, resumable)
uv run parlance deck build       # emit the .apkg into var/out/
uv run parlance audit            # proofread stored cards, fix defects
uv run parlance practice         # voice drills (Gemini Live)
```

Optional: import your own chat exports for a personal deck
(`parlance chats import`, then `generate --deck personal`).

## Docs

`AGENTS.md` — development rules and layout. `docs/` — architecture
decision records (why things are the way they are).
