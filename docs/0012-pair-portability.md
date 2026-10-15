# ADR-0012: Pair portability: card text from the profile, one store per pair, picked chat templates

## The problem

The repo's rule is that nothing is scoped to one language: wordlists,
URLs, prompts and tokenizers take the language from config. An audit of
what survives a pair switch found the last hardcoded bits: target-language
card-front tags sat as Polish literals in the note templates; the store
tables for lemmas and reviews carry no language column, so a switched
pair silently mixed into the existing database; the TTS voice silently
defaulted to a Polish voice; and the chat importer needed Czech and
English template strings baked into Python because they came from one
specific phone's UI language.

## What we tried first, and why it wasn't enough

- Card tags could become more config variables, but they are not
  operator settings: they are pair data with exactly one right answer
  per pair, and the pair profile already exists to carry that.
- Chat templates could be caught structurally (attachment-bearing
  messages), but real exports show attachment-bearing contents are
  mostly user-pasted links, while the robot templates themselves have no
  reliable structural marker. Dropping the filter entirely sends
  thousands of robot strings through the gate and distorts the personal
  stream's frequencies.

## What we built instead

- **Card-front tags are pair-profile fields** (`card_tag_discover`,
  `card_tag_type`), seeded by the init agent in the target language and
  editable through `parlance profile`. The note models take the tags as
  parameters; when they are empty, the tag element is omitted.
- **A store is stamped with its pair** on first open (`meta` key
  `language_pair`); `connect` refuses a database stamped with a
  different pair than the configured one, with a message naming the fix
  (a fresh DATA_DIR). Schema-adjacent tables whose rows are already
  language-scoped are untouched.
- **TTS_VOICE has no default**: the deck build fails loudly when it is
  unset, instead of rendering another language's audio in the current
  pair's voice.
- **Chat templates are picked, not hardcoded**: `chats templates`
  counts exact repeated owner contents in an export, proposes the ones
  containing a gate-passing word, and stores the owner's picks in the
  store; `chats import` skips exactly those contents.
- The chat review filter also covers the extend and listen note types,
  so reviews of those cards feed practice targets. (Unrelated to the
  pair rule; it fell out of the audit and had no other home.)

## What this enables

- A new language pair needs: `.env` values, a `DATA_DIR` of its own,
  `parlance init`, one `chats templates` pass per export — and nothing
  in Python.
- The profile document now owns every piece of per-pair card text, so
  prompt, gloss and face overrides travel together.

## What it cost us

- One extra command per chat export (templates before import) and one
  extra profile edit for stores that predate the card-tag fields (they
  render without tags until set).
- The first pair change on an existing store is a loud error where it
  used to be silent cross-contamination; switching deliberately means a
  fresh DATA_DIR.

## Code

- `parlance/deck/model.py` — note-type builders take the front tags
- `parlance/deck/build.py` — tags read from the stored pair profile
- `parlance/agent.py` — PairProfile card-tag fields
- `parlance/store.py` — pair stamp in `connect`, chat stop list
- `parlance/chats/meta.py` — template candidate detection, stop-aware import
- `parlance/cli.py` — `chats templates`
- `parlance/config.py`, `parlance/tts.py` — no default voice, loud guard
- `parlance/anki.py` — review filter covers extend and listen models
- `tests/test_deck.py`, `tests/test_store.py`, `tests/test_chats.py`, `tests/test_anki.py` — the behavior
