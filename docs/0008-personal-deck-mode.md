# ADR-0008: The personal deck: a mode flag over the shared pipeline, not a second pipeline

## The problem

Personal chat words are known-language lemmas, but every piece of card
machinery — cloze validation, cognate tagging, gloss grounding, the note
model, GUID discipline — is built around target-language lemmas and a single
agent whose tool arguments are the validation boundary. Personal cards need
to come out of that machinery, not beside it.

## What we tried first, and why it wasn't enough

A parallel tool surface (`poll_personal`, `submit_personal_translations`)
was the first sketch: rejected — it duplicates the validation boundary,
forks the agent's toolset, and doubles every future tool change. A parallel
content table keyed on the known-language word was the second: rejected —
it forks deck emission, audit, and lookup, and produces known-language-fronted
cards that cannot reuse the cloze model.

## What we built instead

- **Personal cards stay keyed on the target lemma.** Same note model, same
  cloze, same GUID discipline. What personalizes a card is its *selection*
  and its grounding, not its shape.
- **A pre-pass owns the known-to-target decision** (`chats/translate.py`):
  dictionary-first from `lexicon_pairs`, the LLM fills gaps, and its verdicts
  (mapped, or skipped with a reason) replace a manual review step. Skip
  reasons live in `personal_targets` and are readable via
  `chats list --status skipped` — review happens after the fact, as a log.
  The skip policy excludes function words, noise and words without a
  standalone equivalent; slang and profanity are real vocabulary and are
  never skipped.
- **Chosen words materialize into the shared `lemmas` table**, with corpus
  forms computed from the target frequency list (every listed form that
  lemmatizes to the word), so cloze validation works identically — and the
  two decks dedupe against each other through the status system: a word
  that already has a frequency card is never re-polled as personal.
- **The card-writing agent is unchanged.** `Deps.personal` flips
  `poll_vocab` to the mapping join (chat-count order, the learner's own
  word leading `suggested_known` — the existing "prefer suggested
  candidates" rule does the grounding) and `submit_translations` writes
  `kind='personal'`. No new tools, no prompt changes.
- **Deck emission** sends `kind='personal'` rows to the `::personal`
  subdeck with its own stable deck id and guid prefix.

## What this enables

- `generate --deck personal` as the only user-facing switch.
- More source languages are just more pre-pass runs over `personal_words`
  rows of that language: selection is personal, glosses stay in the known
  language, so an English stream produces Polish cards grounded in the
  words the learner says to the person they speak Polish with.
- Practice drills over the personal set with zero extra plumbing.

## What it cost us

- Review is post-hoc: a bad mapping surfaces through `chats list` and
  audit, it is not prevented up front.
- The pre-pass is a second LLM contract (structured output) beside the
  tool-using agent.
- `audit` samples `kind='lemma'` only; personal cards still need their own
  review pass.
- New personal cards sort after the frequency head in new-card order
  (public rank when available, a 50k+ fallback otherwise) — deliberate.

## Code

- `parlance/chats/translate.py` — pre-pass, verdicts, materialization
- `parlance/store.py` — `personal_targets`, poll join, pending queries
- `parlance/agent.py` — `Deps.personal`, poll branch, content kind
- `parlance/deck/build.py` — `::personal` subdeck, guid prefix
- `parlance/cli.py` — `generate --deck`
- `tests/test_personal.py` — pre-pass/poll/kind/subdeck round-trips
