# ADR-0002: Chat-sim cards and the grading split: self-grade on mobile, existing add-ons on desktop, LLM in CLI

## The problem

The failure v1 taught us: 1000 memorized word cards, zero spoken sentences.
Word → gloss cards train recognition, and even v1's cloze card only fills one
word into a fixed sentence — nobody has to *produce* a message they actually
want to send. The stated need: read a real incoming message, understand it,
and reply with an entire sentence in the target language.

The technical catch: Anki cannot grade free text. Built-in `{{type:Field}}`
is a single-line, exact-match diff and never auto-rates — fine for one word,
useless for "reply to this message however you'd naturally say it".

## What we tried first, and why it wasn't enough

Designing around `{{type:}}` anyway: pre-generate the set of acceptable
replies and exact-match against one canonical reply. Kills the pedagogy — he
correctly types an equally-valid variant and gets a red diff.

Building a custom Anki add-on with an LLM grader. Works — but add-ons are
desktop-only forever. Reviews happen on AnkiDroid too; a desktop-only grader
is a feature that silently vanishes on the couch.

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
   target language + audio. The learner types or says a reply, flips, compares
   with the model replies, grades themselves honestly. Zero infrastructure,
   works on AnkiDroid. The act of composing the full reply — not the
   red/green diff — is the exercise; self-comparison against two native
   variants is feedback.
2. **Everywhere, optional: SmarterTypeField-style template JS.** The
   `{{type:}}` field compares against one canonical model reply;
   template-injected JS relaxes it (ignore case/diacritics/punctuation, extra
   words allowed). Zero add-on, works on mobile — worth adding once the base
   loop exists.
3. **Desktop, optional: LLM Answer Grader add-on (AnkiWeb 365951175).**
   Purpose-made for language production: inline text box, 0–100 score with
   error feedback and a suggested Again/Hard/Good/Easy, and — decisive for
   us — it accepts any OpenAI-compatible endpoint, and the Gemini API
   publishes one, so the same key from `.env` works. We document the
   three-step setup; we do not fork it.
   (Alternatives researched: AI scorer 357495808 hooks `{{type:}}` cards
   across six providers; MyAnswerChecker 1043318428 gives a chat window.)
4. **Voice coach: `parlance practice`.** A live conversation loop over a
   realtime speech API — scenario drills, corrections in the known language,
   forced retries, errors journaled into the store. Fully specified in
   ADR-0006; this bullet records that the terminal/voice loop was always the
   intended alternative to app-bound grading, not an afterthought.

Two note types in the deck:

- `lemma` note: lemma + forms, chosen gloss, variants,
  pos/gender, example sentence, cloze, false-friend
  note, word + sentence audio. Templates: Discover (PL→known), Type it
  (known→PL, typed, single word — exact match is legitimate here).
  Cognates classed `free` get the typing card only.

  Mechanism note: suppression of the recognition card is STRUCTURAL — a
  separate `parlance-lemma-cognate` model with only the Type-it template —
  not the blank-front trick. Anki's required-fields rule is conjunctive on
  non-empty values, so 'generate only when field X is empty' is not
  expressible; a qfmt wrapped in an inverted mustache section makes Anki
  treat every referenced field as required and cards silently vanish
  whenever any field is empty.
- `chatline` note (new, flagship): incoming_pl, audio, context_note,
  model_replies (2–3), gloss. Templates: Read&Respond (typed/said reply), and
  for chat-sourced lines Listen&Respond (audio-only front — forces listening
  comprehension before reading).

Audio speed is a template concern, not a generation concern: learner-speed
slowness is deliberate pedagogy, not a TTS defect.

Anki's desktop webview (Qt WebEngine) ships no MP3 codec, so `<audio>`
plays on no desktop: buttons clicked, silence. Anki only plays audio
through its own native pipeline, i.e. `[sound:]` tags. Therefore: hidden
native `[sound:]` rows — one learner-slow file per utterance (slowed at
render time, content-hashed and cached like any other clip) — and the
custom glyph buttons merely click the hidden native replay buttons by
index. Works on desktop AND AnkiDroid with zero codec questions.

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
