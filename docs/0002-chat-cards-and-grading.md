# ADR-0002: Chat-sim cards and the grading split: self-grade on mobile, existing add-ons on desktop, LLM in CLI

## The problem

The failure v1 taught us: 1000 memorized word cards, zero spoken sentences.
Word → gloss cards train recognition, and even v1's cloze card only fills one
word into a fixed sentence — nobody has to *produce* a message they actually
want to send. The learner's stated need: read a real incoming message,
understand it, and reply with an entire sentence in the target language.

The technical catch: Anki cannot grade free text. Built-in `{{type:Field}}`
is a single-line, exact-match diff and never auto-rates — fine for one word,
useless for "reply to this message however you'd naturally say it".

## What we tried first, and why it wasn't enough

Designing around `{{type:}}` anyway: pre-generate the set of acceptable
replies and exact-match against one canonical reply. Kills the pedagogy — he
correctly types an equally-valid variant and gets a red diff.

Building a custom Anki add-on with an LLM grader. Works — but add-ons are
desktop-only forever. He reviews on AnkiDroid too; a desktop-only grader is a
feature that silently vanishes on the couch.

Calling an LLM from card-template JavaScript. Template JS does run on
AnkiDroid (that is how SmarterTypeField works), but the API key would have to
ship inside the deck. Not happening.

## What we built instead

Decision (after discussion): the **primary workout loop is Anki-first
self-compare** — the learner composes a full reply, flips the card, compares
against the model replies, grades himself. Everything else stacks on top
later without throwing content away, because of one principle: **content
carries correctness, not the review app.** Every chat-sim note stores 2–3
model replies (different registers) plus a gloss of the incoming message, all
generated offline when tokens are cheap; every grading UX below consumes
those model replies as its rubric. The stack, in descending effort:

1. **Everywhere (primary): self-graded.** Card front: incoming message in
   target language + audio. He types or says his reply, flips, compares with
   the model replies, grades himself honestly. Zero infrastructure, works on
   AnkiDroid. The act of composing the full reply — not the red/green diff —
   is the exercise; self-comparison against two native variants is feedback.
2. **Everywhere, optional: SmarterTypeField-style template JS.** The
   `{{type:}}` field compares against one canonical model reply;
   template-injected JS relaxes it (ignore case/diacritics/punctuation, extra
   words allowed). Zero add-on, works on mobile — worth adding once the base
   loop exists.
3. **Desktop, optional: LLM Answer Grader add-on (AnkiWeb 365951175).**
   Already built, maintained (updated 2026-07), purpose-made for language
   production: inline text box, 0–100 score with error feedback and a
   suggested Again/Hard/Good/Easy, and — decisive for us — it accepts any
   OpenAI-compatible endpoint, i.e. the same LiteLLM gateway from `.env`.
   We document the three-step setup; we do not fork it.
   (Alternatives researched: AI scorer 357495808 hooks `{{type:}}` cards
   across six providers; MyAnswerChecker 1043318428 gives a chat window.)
4. **Terminal, optional: `parlance practice`.** A CLI chat session driven by the
   deck's chat-sim records: it prints the incoming message, he types, the
   agent scores and corrects using his own model-reply set as rubric.
   This is also where speech lands later: record, whisper-transcribe
   (faster-whisper small/int8 local, or Groq's free Whisper tier),
   grade the transcript; Azure's free-tier Pronunciation Assessment (native
   phoneme-level scoring) is the upgrade path. Deferred — see TODO.md.

Two note types in the deck:

- `lemma` note (kept from v1, fixed): lemma + forms, chosen gloss, variants,
  pos/gender, example sentence (from his profile/chats), cloze, false-friend
  note, word + sentence audio. Templates: Recognition (PL→known), Production
  (known→PL, typed, single word — exact match is legitimate here), Context
  (cloze + audio). Cognates classed `free` get production+context only — no
  recognition card for a word he already has.
- `chatline` note (new, flagship): incoming_pl, audio, context_note,
  model_replies (2–3), gloss. Templates: Read&Respond (typed/said reply), and
  for chat-sourced lines Listen&Respond (audio-only front — forces listening
  comprehension before reading).

Audio speed is a template concern, not a generation concern (user note:
learner-speed slowness is deliberate pedagogy, not a TTS defect). Audio is
generated ONCE at natural rate; card templates render a second "slow" button
that replays the same file via HTMLAudioElement.playbackRate (0.75x), so one
clip serves normal listening and careful parsing, with webview pitch
correction instead of TTS-stretched artifacts. Fallback if the AnkiDroid
webview proves unreliable with playbackRate: bake an additive slow variant
as a second content-hashed file (no schema change, just more media).

## What this enables

- Production practice on day one on every device, with the honest mobile
  ceiling stated instead of engineered around.
- Desktop users get real LLM grading with one documented add-on install and
  their existing endpoint — no code of ours to maintain.
- The CLI practice loop doubles as the test harness for the grader agent's
  prompt quality before any audio work exists.

## What it cost us

- Self-grading is only as honest as the learner; easy to grade Up when the
  answer deserved Again. The model replies mitigate but don't remove this.
- Template-JS fuzzy matching (option 2) is scored-then-reported client-side;
  it nudges but never schedules. FSRS still only hears the self-grade.
- `{{type:}}` stays off chat cards (single-line exact match would teach the
  wrong signal: chasing the canonical reply string instead of any correct
  production).

## Code

- `parlance/deck/model.py` — `lemma` and `chatline` note types + templates
- `parlance/agent.py`, terminal loop in `parlance/cli.py` (`practice`)
- `tests/` — template rendering fixtures, grader agent against FunctionModel
