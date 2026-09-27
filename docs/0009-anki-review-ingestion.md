# ADR-0009: Anki review ingestion — desktop Anki as a read-only relay

## The problem

The voice practice coach drills "recent vocabulary", but recency in the store
means generation order — the pipeline's bookkeeping, not the learner's day.
The learner's actual exposure happens in Anki on the phone: which words were
reviewed today is knowledge that lives in Anki's revlog, and the coach needs
it to drill what was just seen, not what was just generated.

## What we tried first, and why it wasn't enough

Reading the phone: AnkiDroid exposes no API to other apps, and it has no
add-on surface that could host one. There is nothing to integrate with.

Reading AnkiWeb directly: no public API exists. The only consumer of
AnkiWeb state is a synced desktop collection.

The built-in search (`rated:1` in the phone's card browser): fine for
eyeballs, useless for a pipeline — nothing can consume it programmatically.

A mobile plugin: the platform forbids it; AnkiConnect is a Python add-on
running an HTTP server inside Anki desktop and desktop is where it stays.

## What we built instead

A one-directional relay: reviews happen on the phone, AnkiWeb carries them,
and desktop Anki — running the AnkiConnect add-on, listening on localhost —
is the only readable end of the chain. parlance reads; it never writes to
Anki.

- **Transport:** stdlib HTTP against AnkiConnect. Each pull first triggers
  the AnkiWeb sync (so phone reviews since the last desktop sync arrive),
  then one notesInfo query. The query is scoped to the deck's note types
  (parlance-lemma, parlance-lemma-cognate — fixed genanki constants) plus
  rated:1, which is Anki's "answered today" with its own day cutoff. This
  keeps any other deck out of practice targets in any language pair; deck
  names never appear in queries.
- **Identity:** the note's Lemma field is the store lemma verbatim (the deck
  is a projection of the store), read by field name — never by position.
- **Store:** an additive anki_reviews(lemma, reviewed_on) table; only
  lemmas with a generated card are recorded, and drift — a deck word with no
  store card — is reported, never silently dropped.
- **Session flow:** practice pulls automatically at session open; a closed
  Anki or a signed-out profile degrades to one warning line and the session
  continues without today's reviews. Failures are phrased as operator
  instructions ("is Anki desktop running?", "press Sync and sign in"), not
  raw exceptions. get_practice_targets returns today's reviewed lemmas
  first, topped up with recent cards.
- **CLI:** a standalone anki-sync command runs the same pull for manual use
  and inspection.

One-time setup fact: a desktop profile that has never synced performs a
full download from AnkiWeb on first login, replacing local collection
state. Because decks are projections (ADR-0001), the recovery is always
rebuild and reimport; scheduling history of locally-imported, never-synced
decks does not survive that path.

## What this enables

The coach opens every session already knowing which words the learner saw in
Anki today — recognition practice (Anki) and production practice (voice)
meet on the same day's material, and the review data joins the same store
that feeds generation, auditing and deck building.

## What it cost us

- Desktop Anki must be running when practice starts; it can sit in the
  tray, and no reviewing ever needs to happen there — it is a relay.
- The pull stamps the local calendar date while Anki buckets by its
  day-cutoff; sessions in the small hours can mis-bucket a day.
- Every pull triggers an AnkiWeb sync: instant when current, minutes-long
  after a first login (300 s timeout).

## Code

- `parlance/anki.py` — AnkiConnect client: sync, scoped rated:1 query, Lemma extraction
- `parlance/store.py` — anki_reviews table, record_anki_reviews
- `parlance/practice.py` — auto-pull at session open, get_practice_targets prioritization
- `parlance/cli.py` — anki-sync command
- `tests/test_anki.py` — stub AnkiConnect server, store round-trip, target prioritization
