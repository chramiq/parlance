# ADR-0005: The agent: tool-argument schemas as the validation boundary, restart-loop driver, pair-profile init

## The problem

Generation has to be an agent loop, not a batch prompt: translation N must
look at translations 1..N-1 (gloss collisions, near-synonyms, false friends),
the queue must be pollable, and previously written cards must be editable —
all inside one coherent actor. Meanwhile the corpus (8.6k lemmas) will never
fit one context, and any invalid card that reaches the deck poisons Anki.

The design questions were: where the loop lives (code vs model), where
validation lives (output parsing vs write path), how a long job survives
context limits, and how language-pair-specific conventions (POS vocabulary,
style tags, gender rules) get established for a pair we hardcode nothing
about.

## What we tried first, and why it wasn't enough

v1's pattern: one mega-prompt per 20 words, regex-extracted JSON array, no
cross-batch memory. That gave overlapping glosses, no edits, no crash
safety, and validation limited to "did the regex match".

A per-batch outer loop in Python (discussed first): safe but clunky —
batching in code duplicates scheduling logic that the agent's own tool loop
already has, and every restart needs code to re-derive "where was I".

## What we built instead

**One agent, text output, all structure through tool arguments.** The agent
(loop owned by pydantic-ai via function tools) has five tools in vocab mode:

- `poll_vocab(n)` — untranslated lemmas by frequency rank (the queue
  cursor: "untranslated" = lemma still in status worklist; it flips to
  generated when its first card commits).
- `lookup(text, where)` — one scoped search over lemmas, corpus forms,
  submitted glosses, or all; returns the full previous card payload on an
  exact lemma hit. This is the dictionary that prevents overlap.
- `submit_translations(drafts)` — the batch upsert (1–20 cards per call).
  Arguments are pydantic `TranslationDraft`s (variants 1–4, `preferred`
  index into them, examples 1–3 with cloze, false-friend note). Validation
  is layered: pydantic shape checks run for free, semantic rules (sentence
  contains exactly one corpus form of the lemma; cloze is mechanically
  derived from the sentence; multiple examples must each name their sense —
  the polysemy guarantee) reject that single draft — valid batchmates still
  land, rejected ones stay untranslated and resurface in the next poll.
  Gloss collisions are advisory warnings, never rejections. Editing ==
  submit again for the same lemma: upsert, journaled, model-stamped.
- `set_statuses(changes)` — batch lifecycle (worklist / generated /
  retired / skipped). skipped requires a reason and is the model-arbitrated
  end for free cognates: poll_vocab tags each lemma with the
  known-language word it resembles (similar_known) and the prompt puts the
  judgment on the model — same meaning = eligible skip, different meaning =
  false friend = card with a note. No curated false-friends list exists
  anywhere in the system; the model's own knowledge is the arbiter.
- `save_pair_profile(profile)` — init mode below.

Output type stays `str`: the run ends with a short report (counts, notable
conflicts). There is no output-schema gymnastics anywhere in the system.

**Driver = fresh-run restart loop.** `parlance generate --count N` loops:
one `agent.run` bounded by `UsageLimits(request_limit)` (the honest proxy
for context), cards commit as they land, and when the run hits the cap the
driver starts a FRESH run with empty history — `poll_vocab` returns the
words still untranslated, so continuation is idempotent by construction.
Translating the whole corpus is "the loop runs longer"; nothing needs to
fit a context window.

**Pair-profile init.** Language-pair conventions are data, not code:
`parlance init` runs the same agent, whose `save_pair_profile` tool persists
a `PairProfile` (POS tag vocabulary written in the known language, register
tags, target-language noun genders, false-friend duty, sentence-style
rules, free-form notes for a translator) into a `meta` table — optionally
after a short interactive exchange with the user. The generation prompt
renders the profile at run time; profiles are editable JSON.

## What this enables

- Invalid cards cannot reach the store: the write path is the validator.
- 8.6k-lemma generation is an afternoon of restart-loop iterations, with
  resume-after-crash free at every granularity.
- One agent covers vocab generation, pair init, chat imports (later, as a
  parallel content kind), and curation — new abilities mean new tools, not
  new agents.
- The whole flow runs against TestModel/FunctionModel on a tmp DB in tests.

## What it cost us

- Runaway risk is capped, not eliminated: `request_limit` bounds spend per
  run; a misbehaving model can still waste its budget on lookups.
- Advisory warnings (collisions) fire per submit; noisy-but-harmless is
  accepted over silent overlap.
- Schema-discipline: the `TranslationDraft` contract lives in the tool args;
  renaming a field is a card-format migration for old payloads.

## Code

- `parlance/agent.py` — models, validators, tools, build_agent
- `parlance/prompts/vocab.md` — generation instructions (profile-rendered)
- `parlance/cli.py` — `init`, `generate` (restart-loop driver)
- `parlance/store.py` — schema v2 (content, meta), upsert/lookup/meta API
- `tests/test_agent.py`, `tests/test_store.py` — FunctionModel flows, migration
