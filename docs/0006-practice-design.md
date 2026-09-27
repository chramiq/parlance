# ADR-0006: Practice subsystem — voice conversation drills with persistent learner memory

## The problem

The vocabulary pipeline (generate + hints + audit + deck) trains recognition
and typed recall. The stated goal is sentence *production* under context:
"come to the store, have to say something, say it as best I can, get
corrected in my known language, retry." Anki/AnkiDroid can never host speech
grading; the skill loop must live outside the memory loop. An earlier
standalone voice experiment died of having no memory — every session started
from zero.

## What we tried first, and why it wasn't enough

A pipelined voice stack (speech-to-text, chat model, TTS): workable
text-only scaffolding, but half-duplex latency kills conversation feel,
and it offers no realtime voice model.

A typed-first version of the drill to shake out session logic before audio.
It found the real bugs cheap (turn discipline, transcription aggregation,
per-turn stream semantics), then got retired as the surface: the product is
speech.

## What we built instead

One provider: the Gemini API runs both the batch content pipeline and the
realtime voice loop (Gemini 3.8 Live, voice-native, generous free tier) —
one API key for everything; session content is synthetic language
practice, non-sensitive by construction.

- **Drill flavor: situated simulation.** The coach sets a scene ("you walk
  into a shop and need…"), the learner attempts the target language or asks
  for help in the known language; correction happens in the known language,
  followed by a forced retry. Directives on language split, pacing (slower
  than natural), and brevity live in `prompts/practice.md`.
- **Correction style: adaptive.** Interrupt-and-fix when it helps, collect
  when flow matters — the coach's judgment, not a config flag.
- **Voice I/O: hands-free default.** Continuous mic stream (16 kHz PCM),
  automatic server-side voice activity detection, barge-in flushes the local
  playout queue. The SDK's own examples stream identically; server-side VAD
  carries open-speaker use. `--text` gives a typed transport for scripted
  use. Client-side VAD configuration (sensitivities, `proactive_audio`,
  push-to-talk via manual activity events) stays in reserve behind flags.
  Context stays lean over long sessions via
  `turn_coverage = TURN_INCLUDES_ONLY_ACTIVITY`.
- **Memory = our SQLite store, not an agent-memory product.** The agent
  memory landscape (extraction pipelines, temporal graphs, self-editing
  runtimes) solves persistence for sprawling general chats at the price of
  vector/graph infrastructure and LLM calls on every write. Our memory need
  is small and structured: per-session rows, per-error rows, retrieval is
  "recent + aggregate by pattern". Verdict: sqlite tables plus per-session
  JSON transcripts; revisit semantic recall only if free-form notes
  accumulate.
- **Continuity without model memory.** Each session opens with a digest
  built from the store (recent summaries, recurring error patterns, target
  vocabulary) injected into instructions, and closes with a summary
  journaled via tool — persistence is owned locally, not by the provider.

## Architecture

- `parlance practice` runs the session: mic/speaker/websocket lifecycle,
  bridging Live **function calls** to local tools over the store.
- Tools in-session: `get_practice_targets(n)`, `log_error(pattern, wrong,
  right, lemma?)`, `save_session_summary(text)`. The coach writes its own
  memory through them; the loop enforces a closing summary and otherwise
  marks the session transcript-only.
- Schema (additive DDL, no version bumps): `practice_sessions`, `practice_errors`,
  `lexicon_words`; transcripts under `var/practice/`.
- `parlance practice-log` replays a session: aggregated you/coach turns,
  logged errors, summary.
- Speaker rate is persona instruction, not an API knob: the speech config
  surface offers voice and language selection only, so "slower, learner
  pace" is a prompt directive evaluated by ear.

## What this enables

- Production drilling at conversation latency with corrections that survive
  across days — every session starts from yesterday's error patterns.
- Error data grows into the same store that feeds vocab auditing and deck
  building; one journal for all of it.

## What it cost us

- Sessions live and die with a hosted realtime API; voice has no offline
  mode.
- Auto-VAD quality is room-dependent; tuning knobs exist but are empirical.

## Live API facts

- Audio-out only: no TEXT response modality; text arrives as output
  transcription, learner speech as input transcription — any plan for a
  separate speech-to-text step is obsolete on this path.
- Text input works via `send_realtime_input(text=...)`; per-turn latency is
  dominated by audio generation, so Live is never a batch surface —
  translations are consumed in-session only.
- `session.receive()` yields a per-turn stream that ends at `turn_complete`
  and must be re-called; a single persistent read loop goes deaf after the
  first turn.

## Code

- `parlance/practice.py` — session runner (voice + typed transports), tools,
  transcript, lifecycle
- `parlance/prompts/practice.md` — coach persona and drill rules
- `parlance/store.py` — practice tables, digest
- `tests/test_practice.py` — tools, session lifecycle, transcript aggregation
