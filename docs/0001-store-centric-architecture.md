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

One SQLite database (`var/parlance.db`) owns everything; agents write to it;
the Anki deck and every other output are read-only projections of it.

Pipeline stages, each a small module or verb:

1. **Worklist** (`parlance worklist build`): a FrequencyWords frequency list
   (OpenSubtitles-derived, CC BY-SA, spoken register) is lemmatized with
   simplemma — kocham/kocha/kochasz collapse to one `kochać` row — ranked,
   and classified for cognateness (diacritic-stripped edit distance;
   regular-correspondence pairs are dense enough for a threshold). Rows
   already in the store are skipped by unique lemma, so the worklist is
   incremental by construction. `free` cognates become typing-only cards
   instead of full cards.
2. **Hints** (`parlance lexicon`, `parlance llm-hints`): translation candidates
   for every lemma, from edition dictionaries and from a generative model
   turned into a translation oracle. Hints are advisory grounding for
   generation, never the curriculum.
3. **Generate** (`parlance generate`): the agent takes worklist batches plus
   the pair profile plus hint suggestions, and writes validated
   `TranslationDraft` records: lemma, forms, 1–4 translation variants with
   register tags — exactly one marked preferred (the one to learn; the
   others stay visible on the record, no user picking step) — example
   sentence (+cloze, +known-language gloss), false-friend notes.
4. **Audit** (`parlance audit`): the same agent re-checks stored cards in
   random samples, fixes real defects via the same journaled upsert path,
   retires junk with a reason. This replaces "regenerate everything and
   hope". Interactive curation on top of the same tools stays deferred.
5. **Deck** (`parlance deck build`): projection to .apkg (see ADR-0003 for the
   GUID/reimport semantics, ADR-0002 for the note types).

Why SQLite and not a JSON file or an embedded graph/vector DB: the queries we
actually need are `WHERE lemma = ?`, `ORDER BY priority`, and substring grep —
stdlib sqlite3 covers them with zero dependencies, zero services, and one
file to back up.

## What this enables

- Non-overlapping translations by schema (unique lemma) and by construction
  (lemma-level worklist), not by prompting.
- Any stale or bad translation is one audit pass away from fixed, and the fix
  flows into the next deck build without losing review progress.
- Decks grow incrementally; rerunning generation only handles new lemmas.
- New content kinds and subsystems (practice sessions, hints) land as new
  tables on the same store, never as second sources of truth.

## What it cost us

- Dev build stance on schema change: no migrations. `user_version` is stamped
  and checked; on mismatch `connect()` fails loudly with "delete and
  rebuild". Real migrations return the day curated cards become
  irreplaceable — that's data loss policy, not laziness.
- The agent's editing power is unsupervised; it can fix the wrong card.
  Mitigation: every mutation is journaled in a `changes` table.

## Code

- `parlance/store.py` — schema, queries, mutation journal
- `parlance/worklist.py` — stage 1
- `parlance/lexicon.py` — stage 2
- `parlance/agent.py` — the one agent: generation, profile init, audit
- `parlance/deck/build.py` — the projection
- `tests/` — store round-trips, TestModel/FunctionModel agent tests
