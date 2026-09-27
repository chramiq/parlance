# ADR-0007: Personal wordlist from chat exports: word-bag ingestion behind a public-frequency gate

## The problem

The subtitle-frequency worklist teaches the vocabulary of the target language
at large. But a learner's *active* vocabulary in their known language is
what they will actually need to produce: the words they type daily are the
words they will want to say. The raw material — personal chat exports — is
also the most sensitive data the project could ever touch: complete personal
history, every conversation, every name.

## What we tried first, and why it wasn't enough

The original plan ingested chats and translated sentences into tailored
scenario cards. Two fatal flaws: sentence-level translation ships raw personal
text through LLM pipelines (a privacy non-starter), and cards generated from
personal sentences describe the user's life rather than train the user's
vocabulary. PII-scrubbing libraries (Presidio, NER models) were considered as
a gate: heavyweight, English-centric, model dependencies, and probabilistic —
a miss leaks a name.

## What we built instead

A word-bag with a structural privacy guarantee:

- **Ingestion keeps only single words.** Sentences are tokenized and
  discarded inside the parser; nothing sentence-shaped is ever stored or
  sent anywhere.
- **The gate is a public frequency list** (OpenSubtitles `cs_50k`, the same
  source and license as the target-language worklist). A token survives
  only if it is a common public word of the gate language. This one check is
  simultaneously the privacy filter (names, handles, slang, dialect and
  typos are not public words), the stopword filter (public rank), and the
  language filter (English words drop under the Czech gate; an English
  stream is the same pipeline run with `--lang en` and `en_50k`).
- **Meta DIY export realities**: recursive
  `message_*.json` discovery across export layouts; the owner is the
  participant listed in every thread; message content carries Meta's double
  encoding (UTF-8 bytes JSON-escaped as Latin-1) repaired by a latin-1
  round-trip; localized system templates ("Posíláte přílohu.") are
  exact-match dropped; diacritic-less texting ("mam", "mas") is restored
  against the gate list before lemmatizing, because bare simplemma
  mis-lemmatizes diacritic-less forms into wrong words ("mam" → "mámit",
  not "mít").
- **Idempotent accumulation**: each message is hashed
  (thread_path|sender|timestamp|content) into `chat_seen` keyed by
  (language, hash). Re-importing an export changes nothing; importing a
  second platform's export (Facebook ships the same JSON shape, detected
  owner is per-export-root so display names may differ) adds to the counts.
- The CLI warns the user not to read through their own export; the tool
  does the looking.

Survivors land in `personal_words (lang, lemma, count, status='pending')`,
ranked by the user's own usage frequency.

## What this enables

- A `personal` subdeck: cards for words the learner actually says, selected
  by their own frequency rather than the corpus's.
- Per-language streams of the same chats (Czech words for the cz→pl pair;
  the English said to a specific person, later, via `--lang en`).
- Practice drills and the error→card pipeline over the learner's real
  vocabulary.

## What it cost us

- The gate deliberately drops Moravian dialect and personal coinages; a
  review step can re-add, at the price of building that review surface.
- English leaks through cs_50k (loanwords and subtitle English): ~1k lemmas
  overlap the English top-2000, mostly Czech homographs ("ten", "no", "do")
  but some genuine English ("you", "call") — needs review-step judgment,
  not automatic dropping.
- The system-string denylist is locale- and provenance-specific and must
  grow with each new export vintage.
- `chat_seen` stores hashes, not content: debugging a miscount requires the
  original export still on disk.

## Code

- `parlance/chats/meta.py` — parser, gate, restore, dedupe
- `parlance/store.py` — `personal_words`, `chat_seen`, upserts
- `parlance/cli.py` — `chats import`, `chats list`
- `tests/test_chats.py` — gate/idempotency/accumulation round-trips
