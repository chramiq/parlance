# ADR-0001: SQLite as source of truth; agents mutate it, decks are projections

## The problem

v1 was one 350-line script: a frequency-list pull, one mega-prompt per batch,
regex-extracted JSON, straight into genanki. Every run started from nothing.
Consequences that hurt in practice:

- No memory of what was already generated — reruns could re-translate the same
  word into something different, and inflected forms of one lemma (kocham,
  kochasz, kocha…) each became separate cards with inconsistent glosses.
- A bad translation could not be fixed, only regenerated; the "stale bad
  translation" problem was structural.
- No prioritization: cards came out in raw frequency order, mixed with
  near-duplicate forms.
- The LLM was the scraped Gemini web UI via session cookies — brittle
  (cookies expire), and structured output was a prompt prayer plus a regex.

We wanted: non-overlapping translations, an agent that can grep and edit the
vocabulary like a codebase, incremental growth of the deck, and reproducible
prioritized generation.

## What we tried first, and why it wasn't enough

Keeping the "list in, deck out" flow but adding a JSON cache file. That fixes
re-translation cost but none of the semantics: no dedup by lemma, no edit
trail, no priority, and JSON files with concurrent-ish writes rot fast.

Prompting harder ("don't repeat yourself, avoid forms of previous words") —
the model cannot see the full accumulated vocabulary each batch; v1's prompt
already demonstrated that overlaps survive good intentions.

## What we built instead

One SQLite database (`var/parlance.db`) owns everything; agents and importers
write to it; the Anki deck is a read-only projection of it.

Pipeline stages, each a small module with its own CLI verb:

1. **Worklist** (`parlance worklist build`): FrequencyWords `pl_full.txt`
   (OpenSubtitles-derived, CC BY-SA, spoken register) is lemmatized with
   simplemma — kocham/kocha/kochasz collapse to one `kochać` row — ranked,
   and classified for Czech cognateness (diacritic-stripped edit distance;
   CZ–PL correspondences are regular enough for a threshold). Rows already in
   the store are skipped by unique lemma, so the worklist is incremental by
   construction. `free` cognates (system/system) become optional mini-cards
   instead of full cards — the learner asked to "learn only the diff".
2. **Chats (optional)** (`parlance import-chats <export dir>`): parsers for
   Instagram and Messenger "Download Your Information" JSON (identical
   Meta schema; `sender_name`, `timestamp_ms`, `content`; latin-1 mojibake
   fixer) and for `signal-export` output. His messages and hers land in
   separate tables, never uploaded anywhere beyond the configured LLM.
   Probe-first: parsers are written against real exported files.
3. **Profile** (`parlance profile build`): the agent reads a sampled slice of
   the imported chats and emits a learner profile (age, city, relationship,
   recurring topics, register, inside vocabulary) as JSON in the store. The
   profile feeds back into generation, so example sentences come from his
   life, not from a 40-year-old textbook.
4. **Generate** (`parlance generate`): the agent takes worklist batches plus
   profile plus (when present) his real messages, and writes validated
   `Flashcard` records: lemma, chosen forms, 2–4 translation
   variants with register tags — the agent marks exactly one as the
   preferred gloss (the one to learn; the others stay visible on the record,
   no user picking step) — an example sentence (+cloze, +gloss),
   false-friend notes, and optionally a chat-sim record (incoming message,
   model replies).
5. **Curate** (`parlance curate`): the same agent, driven conversationally, with
   tools over the store — search, show, edit gloss, merge
   duplicate lemmas, retire a card. This replaces "regenerate everything and
   hope".
6. **Deck** (`parlance deck build`): projection to .apkg (see ADR-0003 for the
   GUID/reimport semantics, ADR-0002 for the note types).

Why SQLite and not a JSON file or an embedded graph/vector DB: the queries we
actually need are `WHERE lemma = ?`, `ORDER BY priority`, and substring grep —
stdlib sqlite3 covers them with zero dependencies, zero services, and one
file to back up.

Priority is data, not ceremony: `priority = f(freq_rank, in_my_chats, theme)`
stored per lemma; deck build orders new cards via genanki `due=` (the only
programmatic new-card-position lever) and emits `<TargetLang>::NNN-<theme>`
subdecks so per-subdeck daily limits and FSRS stay native Anki.

## What this enables

- Non-overlapping translations by schema (unique lemma) and by construction
  (lemma-level worklist), not by prompting.
- Any stale or bad translation is one `curate` conversation away from fixed,
  and the fix flows into the next deck build without losing review progress.
- Decks grow incrementally; rerunning generation only handles new lemmas.
- Chat-sourced examples and the chat-sim card type ride the same store.

## What it cost us

- An import step the user must run manually per platform (Meta ZIP, Signal via
  linked Desktop); there is no live "sync my chats" — that is deliberate.
- Dev build stance on schema change: no migrations. `user_version` is stamped
  and checked; on mismatch `connect()` fails loudly with "delete and
  rebuild". Real migrations return the day curated cards become
  irreplaceable — that's data loss policy, not laziness.
- The curator's editing power is unsupervised; it can merge the wrong two
  lemmas. Mitigation: every mutation is journaled in a `changes` table.

## Code

- `parlance/store.py` — schema, queries, mutation journal
- `parlance/worklist.py`, `parlance/chats/`, `parlance/profile.py` — stage 1–3
- `parlance/agent.py` — the one agent: batch generation, profiling, curation tools
- `parlance/deck/build.py` — the projection
- `tests/` — store round-trips, parser fixtures, TestModel/FunctionModel agent tests
