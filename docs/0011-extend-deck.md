# ADR-0011: The extend deck: gloss collisions become routing, not defects

## The problem

Many target words share one preferred gloss: Polish "czy" and "jeśli" are
both "jestli" in Czech, "iść" and "pójść" are both "jít", and roughly five
percent of generated cards collide this way. The recognition card does not
care. The typed card does: Anki's native type-in-the-answer check compares
against a single field, so a prompt showing only the gloss cannot say which
variant is wanted — the learner produces a correct word for that gloss and
is marked wrong for it.

## What we tried first, and why it wasn't enough

- Accepting multiple answers per typed card: Anki's type check is
  single-answer by construction; faking it with scripted grading is
  desktop-add-on territory and breaks the mobile review path. It also
  grades the unlearned distinction (czy vs jeśli) as correct, which is the
  one thing the card exists to teach.
- Disambiguating the gloss itself ("jestli (v otázce)"): workable, but it
  annotates every face of both cards, and the pair profile deliberately
  mandates bare glosses — one word, one meaning, no parentheses. Every
  collision would need a manual wording decision forever.

## What we built instead

Collisions are not a defect to prevent but a fact to route on:

- At deck build, generated cards are grouped by preferred gloss across
  both kinds (frequency and personal — the typing prompt is the gloss
  regardless of which deck carries the card).
- The most frequent word per group (rows are frequency-rank ordered)
  keeps the bare gloss and stays in its normal deck. The gloss is now
  unique among the non-extend cards, which is what makes the typed prompt
  unambiguous.
- Siblings route to a new "::extend" subdeck with a new note type: a typed
  card whose front shows the gloss with the other group members struck
  through — no label word, so the template stays language-pair agnostic.
  The exclusion lists every other member of the group, since all of them
  are wrong answers for that prompt.
- Extend routing takes precedence over the free-cognate model and over the
  kind-based deck: the struck-through exclusions are the disambiguator,
  so an extend sibling is always the extend note type. GUIDs stay
  kind:lemma, so reimports keep updating in place. Listen cards follow the
  existing review-history rule unchanged.

Generation, the store schema, and the collision warning in
submit_translations are untouched: the store keeps tolerating collisions,
and the deck projection decides what they mean.

## What this enables

- A deck where every typed prompt has exactly one right answer, without
  giving up any vocabulary.
- Vocabulary expansion as a separate lane: the extend deck is exactly the
  "same meaning, different word" set, learned after the workhorse word.
- The audit agent can pull a wrongly demoted card (aspect pairs, sense
  splits) back into the main deck by re-submitting it with a reworded
  gloss that no longer collides; retiring a junk sibling shrinks its group
  and frees the primary. The projection recomputes on every build.

## What it cost us

- Anki does not move cards between decks when an updated note is
  reimported: existing sibling cards stay in their old deck until moved
  once by hand (filter by the extend note type in the desktop browser).
- The primary is mechanical (frequency rank). When a future lower-rank
  card joins a group, the old primary becomes an extend sibling in the
  build while the reviewed copy in Anki keeps its deck until moved.
- Aspect pairs and sense splits are demoted by the same rule as register
  synonyms; recovering them needs an audit edit, which the warning
  machinery already supports.
- An import that shrinks a note type's template set leaves orphan cards
  behind: a card whose template slot no longer exists renders the
  fallback face (fields and exclusions correct, but no live type-answer
  input), and Tools > Empty Cards does not flag it. Check Database may
  remove such cards; the reliable fix is deleting the affected notes and
  reimporting, which restarts their scheduling history.

## Code

- `parlance/deck/model.py` — extend note type: Excludes field, struck-through front
- `parlance/deck/build.py` — collision grouping, routing, ::extend deck
- `tests/test_deck.py` — group routing, exclusions, deck placement round-trip
