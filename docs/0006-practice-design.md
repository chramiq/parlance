# ADR-0006: Practice subsystem — voice conversation drills with persistent learner memory

Status: proposed (2026-09-26)

## Context

The vocabulary pipeline (generate + hints + audit + deck) trains recognition and
typed recall. The learner's stated goal is sentence *production* under context:
"come to the store, have to say something, say it as best I can, get corrected
in my known language, retry." Anki/AnkiDroid can never host speech grading;
the skill loop must live outside the memory loop. A previous standalone project
("echo", Gemini Live, deleted) proved the conversation concept but died of
having no memory — every session started from zero.

Two-provider reality is accepted: school gateway = batch content pipeline
(unlimited-ish, private), Google Gemini 3.8 Live = the realtime voice loop
(free student tier, native full-duplex audio; session content is synthetic
language practice, non-sensitive by construction).

## Decisions

- **Drill flavor: situated simulation.** The drill-master sets a scene
  ("jsteś v obchodě, potřebuješ…"), the learner attempts Polish or says
  "cannot"; correction happens in Czech, followed by a forced retry.
- **Correction style: adaptive.** Interrupt-and-fix when it helps, collect
  when flow matters — drill-master's judgment, not a config flag.
- **Voice I/O: both modes supported, configurable.** The google-genai SDK
  natively provides (a) server-side automatic VAD with tunable sensitivities
  and (b) manual mode where the client sends activity_start/activity_end —
  which *is* push-to-talk. Spacebar-PTT v1, auto-VAD flag later. Audio is
  16kHz PCM in / 24kHz out via sounddevice. Barge-in is native to Live.
- **Batch translation stays on the gateway.** Gemini Live is session-oriented
  realtime; it is the wrong tool for batch card generation and its free tier
  is rate-limited. Not pursued.
- **Memory = our SQLite store, not an agent-memory product.** Research
  (mem0 / Zep / Letta, 2026 landscape): these sell RAG-with-LLM-extraction
  over general chat agents, want vector/graph infra, and put an LLM call on
  every write. Our needs are small and structured: per-session rows, per-error
  rows, a learner-profile KV — retrieval is "recent + aggregate by lemma/
  pattern," not semantic search over thousands of vague memories. Verdict:
  sqlite tables + JSON transcript files for human review; revisit semantic
  recall via sqlite-vec only if free-form notes accumulate (one extension
  file, zero new infra).

## Architecture

- `parlance practice` verb: session manager in Python (asyncio), owns mic /
  speaker / websocket lifecycle, bridges Live **function calls** to local
  tools over the store.
- Tools exposed in-session: `get_practice_targets(n)` (due-ish words +
  example sentences + recent error adjacency), `log_error(lemma?, pattern,
  wrong, right)`, `lookup(text)` (reused), `save_session_summary(text)`,
  `update_learner_profile(notes)`. The drill-master thereby writes its own
  memory — the Letta pattern, minus adopting Letta.
- Session lifecycle: open → build digest from store (last summaries, error
  counts by pattern, due vocab) → inject as system context → drill → close
  requires summary + error flush; otherwise transcript-only, flagged.
- Schema (additive DDL, no version bump):
  `practice_sessions(id, started_at, ended_at, model, summary)`,
  `practice_errors(id, session_id, ts, lemma TEXT NULL, pattern, wrong, right)`,
  meta key `learner_profile`; transcripts `var/practice/<id>.jsonl`.
- Deck interplay: practice never writes Anki state. Persistent errors may
  mint vocab cards later (via audit/generate), never inside the live loop v1.

## Non-goals (v1)

No mobile, no Anki-write-back, no real-chat ingestion (privacy wall from the
vocab discussion stands), no multi-learner, no semantic/vector memory.

## Verification

Spike: one scripted scenario run end-to-end (PTT → drill → forced retry →
close); assert session row, ≥1 error row, summary present; audio round-trip
manual check. Then a week of real use before anything beyond flag-level
changes.
