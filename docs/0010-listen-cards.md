# ADR-0010: Listen cards — ear training gated on review history

## The problem

Recognition and production cards teach the word through the eye. The
learner's goal is speaking, and the missing retrieval angle is the ear:
hear the target word, recall its meaning. But listening practice on
unknown words is weak — listening-only input acquires fewer new words
than reading — so ear-training cards are reinforcement, not first
contact, and must not exist for words the learner has not started
learning.

## The decision

A third note type, parlance-listen: the front carries only the word
audio (auto-plays on show; the native replay button, styled as a large
round button, is the entire interface), the back is the standard full
card. The build emits a listen note only for lemmas with a recorded
Anki review (the anki_reviews relay, ADR-0009): ear training exists only
for words already in learning.

A separate note type instead of a conditional field on the shared
model: field lists are frozen per model ID (ADR-0003), and a flag field
would force a model migration on every existing note for a card only
some notes should have. A separate note with its own GUID namespace
(listen:) appears and updates through the normal reimport path with no
migration at all.

## What it costs

- Burying does not span note types: a word's listen card can appear the
  same day as its recognition card. The modality differs enough that
  this is acceptable.
- Listen notes duplicate the base note's fields; the deck is a
  projection, so the store stays the single truth.
- AnkiConnect must have recorded reviews before any listen card exists:
  the feature is invisible until the review loop runs.

## Code

- `parlance/deck/model.py` — listen_model, LISTEN_MODEL_ID
- `parlance/deck/build.py` — reviewed-lemma gate, listen emission
- `tests/test_deck.py` — emission only for reviewed words
