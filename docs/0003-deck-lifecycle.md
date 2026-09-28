# ADR-0003: Deck lifecycle: stable GUIDs update in place; retirement is explicit

## The problem

Regenerating a language deck is not a one-shot export, it is a maintenance
loop: curate fixes a gloss, the profile freshens examples, a batch of stale
cards gets retired. v1 ignored this entirely — every run minted a fresh
`DECK_ID = time()` and a new deck, so Anki saw total strangers each time and
"update" meant re-importing duplicates.

The loop must answer three questions: how do fixed content edits reach
existing notes without resetting their review progress; how do new cards join
in priority order; how do dead cards leave.

## What we tried first, and why it wasn't enough

v1's approach — new IDs per run — worked exactly once and polluted the
collection ever after. Text-file (TSV/CSV) imports match on the first field
and offer update choices, but that path is manual, fragile about which field
is the key, and second-class for media. CrowdAnki JSON round-trips are the
git-friendly answer to syncing a deck, but even CrowdAnki cannot delete
notes, and adding a desktop-plugin dependency to the happy path contradicts
our mobile-first constraint.

## What we built instead

**Identity is content-derived.** Every note gets `guid = stable hash of
(type, lemma-or-chatline-id)` — genanki `Note(guid=...)` accepts it. REIMPORT
RULE (Anki import semantics): importing an .apkg whose GUIDs match
existing notes updates those notes' fields in place; scheduling/review
progress is untouched because genanki files carry no scheduling data; field
count of the note type must stay unchanged or updates silently skip. Note-type
field lists are therefore frozen per model ID; a schema change mints a new
model ID on purpose, never edits fields in place.

**New-card order is generated, not configured after the fact.**
`Note(due=priority_position)` sets the new-card position at build time; we
combine it with gather order "Deck", sort "card type, then gathered", and
bury-siblings on (one note spawns 2–3 cards; without burying they camp
together in a session). The
package emits two subdecks under an uppercase target-language root,
`<TARGET>::COMMON` and `<TARGET>::PERSONAL`; new-card pacing is a single
deck-options number in Anki, not subdeck structure, so frequency bands are
gone.

**Retirement is data, not import magic.** .apkg cannot delete, by design of
the format. So: `curate retire <lemma>` flips state in the store; the deck
build omits retired notes and prints their GUIDs. Deletion in the collection
is a deliberate manual act (Anki browser → find by guid → delete) for the
rare times it matters; an AnkiConnect cleanup script is a documented
optional extra on desktop (TODO.md), never on the happy path.

**Media is immutable.** Audio filenames are content hashes (`sha1(text+voice)`
), referenced by basename only. genanki does NOT dedupe
by content — the same file passed twice ships twice in the .apkg; uniqueness
comes from our content-hash naming, and AnkiWeb sync delivers the basenames
to AnkiDroid. Retext a sentence and old audio orphans harmlessly; the
store's audio table records what exists so the deck build never regenerates
unchanged clips.

**FSRS, one preset.** Desired retention 90%; learning steps under a day (10m);
bury new siblings; gather order Deck; sort card-type-then-gathered. A
starting preset, to be retuned against real retention data.

Scope note: the collection starts from zero — there is deliberately no
migration path for the old deck. The lifecycle rules above exist for the
generate-curate-rebuild loop going forward, not to accommodate anything that
already exists.

## What this enables

- `curate` → `deck build` → import over the existing deck: gloss edits,
  fresher examples and audio swaps land in place, progress intact.
- Priority changes (chats re-imported, frequency re-ranked) just change
  `due=` on the next build.
- Increasing the deck is append-only and safe; shrinking it is always
  explicit.

## What it cost us

- Note-type fields can never be reordered/extended silently; the discipline
  is enforced by a unit test on the field list hash, not by tooling.
- True deletion stays manual on mobile-only setups. Accepted: retirement is
  rare and the browser flow is two minutes on desktop.
- GUID stability means a regenerated note with the *same* sort-field but new
  collateral (new example, new audio) must reuse the same guid value — the
  hash therefore keys on identity (lemma id), never on content.

## Code

- `parlance/deck/build.py` — guid/due emission, subdeck assembly, media list
- `parlance/deck/model.py` — frozen field lists (+ hash test)
- `parlance/store.py` — retired state, audio table
- `tests/` — guid stability across rebuilds, field-list hash guard
