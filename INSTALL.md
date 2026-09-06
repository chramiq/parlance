## Anki setup for parlance decks

Recommended starting preset (FSRS, Anki 23.10+ / AnkiDroid 2.17+); retune
after the first month of real retention data, desired retention above all:

- Desired retention: 90% (do not exceed ~95%; workload explodes)
- Learning steps: `10m` (all under one day)
- New card gather order: Deck (top subdecks first — this is how
  `<TargetLang>::NNN-<theme>` ordering becomes priority)
- New card sort order: Card type, then order gathered
- Bury new siblings: ON
- Bury review siblings: ON
- Bury interday learning siblings: ON

Notes from the deck lifecycle (docs/0003):

- Re-importing a newer .apkg updates notes in place (same GUIDs), review
  progress intact. Never delete and re-import instead.
- Retired cards are omitted from new builds; deleting them from the
  collection is a manual act (browse by guid) if ever wanted.
- Desktop only, optional: LLM Answer Grader add-on (AnkiWeb 365951175),
  pointed at the same OpenAI-compatible endpoint from `.env`, grades typed
  replies in-review. Not available on AnkiDroid — see docs/0002.
