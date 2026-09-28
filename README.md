# parlance

Personal Anki card generator for *speaking* a language: you know one
language (KNOWN_LANG), you want to actually produce another
(TARGET_LANG). Language-pair agnostic — the pair is runtime config.

A pydantic-ai agent writes validated cards against a SQLite store:
glosses are checked against a real dictionary wordlist, sentences must
contain the word, clozes must match. genanki emits decks with stable
GUIDs, so reimporting a rebuilt deck updates cards in place instead of
duplicating them. Your own chat vocabulary can become a second, private
deck. Voice practice drills what you reviewed in Anki that day.

## Setup

```bash
uv sync
cp .env.example .env
```

Set in `.env`: `GEMINI_API_KEY` (https://aistudio.google.com/apikey),
`KNOWN_LANG`, `TARGET_LANG` (ISO 639-1 codes). One key drives card
generation and voice practice. Batch generation needs a billing-linked
key; the free tier's daily limits are too small. Expect roughly one US
cent per generated card.

## The loop

Each phase writes to the store (`var/parlance.db`); every later phase
reads from it. All generation runs are resumable: killing a run loses
at most one batch.

### 1. Build the frequency worklist

```bash
uv run parlance worklist build
```

Downloads the spoken-frequency list for the target language
(hermitdave/FrequencyWords), collapses surface forms into lemmas, and
seeds a ranked queue. Cards are generated most-frequent-first.

### 2. Ground the lexicon

```bash
uv run parlance lexicon
```

Downloads kaikki.org dictionary editions for both languages and builds
two things: translation hints the agent sees when writing a card, and
the known-language validation wordlist that rejects made-up glosses.
Both are cached in `var/lexicon/`; reruns are offline.

### 3. Create the pair profile

```bash
uv run parlance init
```

A one-time LLM conversation about you (register, interests, style)
produces the language-pair profile that seeds every card's sentence
style. Refine it any time, without a model:

```bash
uv run parlance profile show
uv run parlance profile edit    # opens $EDITOR, validated on save
```

### 4. Generate cards

```bash
uv run parlance generate --count 100
```

The agent polls unlearned lemmas in frequency order, writes cards
(gloss, variants, example sentence with translation and cloze,
false-friend notes where relevant), and validates every draft. Invalid
drafts are rejected with the reason and retried in the same run.
Generate a small sample first, review it, then scale up. `--deck
personal` (below) writes cards for your own words instead.

### 5. Build the deck

```bash
uv run parlance deck build
```

Renders TTS audio (edge-tts, cached by content hash) and emits
`var/out/pl-vocab.apkg` — one subdeck per source: `<TARGET>::COMMON`
(frequency core) and `<TARGET>::PERSONAL` (your words). Import it into
Anki; later rebuilds reimport as in-place updates. `var/out/preview.html`
renders one card through every template for a quick look without
importing.

Words with a recorded Anki review also get a listen card — the word's
audio only, no text: ear training that exists only for words already in
learning. Reviews reach the store through `parlance anki-sync` (or
automatically at practice session open); the next deck build mints the
listen cards, and reimport adds them.

### 6. Learn from your own messages (optional)

```bash
uv run parlance chats import <export-dir> --lang cs
uv run parlance chats import <export-dir> --lang en
uv run parlance generate --deck personal --words 100 --count 20
```

Meta DIY exports (Instagram, Facebook) work directly. The privacy model
is structural: only single words that also appear in a public frequency
list of the language ever reach the store — sentences are never stored,
names and handles drop out. One export can be imported under several
source languages; each gets its own gate. The pre-pass maps your words
to target lemmas (dictionary first, LLM for gaps), skipping function
words and chat noise with a recorded reason. `--words N` caps the
pre-pass to the N most-used still-unmapped words per run.

### 7. Review and audit

```bash
uv run parlance audit
```

A proofreading pass over a random sample of stored cards; the agent
fixes defects it finds. Run it occasionally as the deck grows.

### 8. Practice speaking

```bash
uv run parlance practice        # voice (Gemini Live)
uv run parlance practice --text # typed
uv run parlance anki-sync       # pull today's Anki reviews manually
```

Voice drills with a Live coach that opens every session knowing which
words you reviewed in Anki today — reviews happen on the phone, AnkiWeb
carries them, and desktop Anki with the AnkiConnect add-on (code
2055492159) is the readable end of that chain. Desktop Anki must be
running when the session opens; if it is not, practice continues
without today's reviews. Practice errors are logged to the store as
error patterns.

## Anki setup (one-time, per collection)

The .apkg carries decks, note types, templates and new-card order —
but scheduling settings belong to your Anki collection and cannot ship
in a deck file. Set these once on desktop *or* AnkiDroid (they sync
with the collection), in deck options for `<TARGET>`:

| Section | Setting | Value |
|---|---|---|
| Daily Limits | New cards/day | 10–25 (your pace; ~2 cards per word) |
| Daily Limits | Maximum reviews/day | ≥ 10x your new limit — see pitfalls |
| New Cards | Learning steps | `1m 10m` |
| Display Order | New card gather order | Deck |
| Display Order | New card sort order | Card type, then order gathered |
| Display Order | New/review order | Show after reviews |
| Display Order | Review sort order | Ascending retrievability |
| Burying | Bury new/review/interday siblings | all ON |
| Audio | Don't play audio automatically | OFF (default) |
| FSRS | Enable + desired retention | ON, 0.90 |

Then give each subdeck its own daily new-card slots (deck options →
Daily Limits → **This deck**), because subdeck gathering is alphabetical
and `COMMON` sorts before `PERSONAL` — with a shared limit, the frequency
core would starve the personal deck for months:

| Deck | New cards/day |
|---|---|
| `<TARGET>` (preset, the total) | 12 |
| `<TARGET>::COMMON` | 10 |
| `<TARGET>::PERSONAL` | 2 |

Behavioral rules that keep the scheduler healthy:

- **Never cap reviews below steady state.** Every new card becomes
  several future reviews; hiding them does not make them go away, and
  once the review cap is reached new cards stop appearing too (they
  respect the review limit). If you fall behind, pause new cards — do
  not lower the review cap.
- **Press Hard only for recalled-with-effort, never for forgotten** —
  FSRS treats Hard as a pass, and misuse silently inflates intervals.
- Run FSRS parameter optimization after a few hundred reviews, then
  roughly monthly. Before that, defaults are better than noise.

## Docs

`AGENTS.md` — development rules and repo layout. `docs/` — architecture
decision records: the why behind every design choice.
