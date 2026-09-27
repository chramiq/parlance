"""The single pydantic-ai agent: generates translations, profiles pairs, and maintains the store via tools.

All structured data enters the store as validated tool arguments; the agent's
own output stays plain text.
"""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from . import store
from .cognates import known_vocab, similar_known
from .config import Settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "vocab.md"
AUDIT_PROMPT_PATH = Path(__file__).parent / "prompts" / "audit.md"

LANG_NAMES = {"cs": "Czech", "pl": "Polish", "en": "English", "de": "German", "sk": "Slovak"}


class GlossVariant(BaseModel):
    """One possible translation and its register tag."""

    gloss: str
    style: str = "neutral"


class Example(BaseModel):
    """One usage example: sentence, its translation, and the cloze form."""

    target: str = Field(description="Short natural sentence in the TARGET language")
    known: str = Field(description="Translation of target into the KNOWN language")
    cloze: str = Field(description="target with exactly one form of the lemma replaced by [...]")
    meaning: str = Field(
        default="",
        description="Which sense this example illustrates. Required when more than one example is given.",
    )


class TranslationDraft(BaseModel):
    """Card payload accepted by submit_translations. Rejected args are retried by the model."""

    lemma: str
    pos: str = Field(description="Part of speech, written in the KNOWN language")
    gender: str = Field(
        default="", description="Noun gender in the KNOWN language; empty for non-nouns"
    )
    variants: list[GlossVariant] = Field(min_length=1, max_length=4)
    preferred: int = Field(
        description="Index into variants: the one gloss the learner should learn"
    )
    examples: list[Example] = Field(
        min_length=1,
        max_length=3,
        description="One example per card; more only when the lemma has genuinely distinct senses",
    )
    false_friend_note: str = Field(
        default="",
        description="Warning when the lemma resembles a KNOWN-language word but differs in meaning",
    )

    @model_validator(mode="after")
    def _preferred_in_range(self):
        if not 0 <= self.preferred < len(self.variants):
            raise ValueError(f"preferred={self.preferred} outside variants range")
        if len(self.examples) > 1 and any(not e.meaning for e in self.examples):
            raise ValueError("multiple examples must each state their meaning (sense)")
        return self


class StatusChange(BaseModel):
    """One lifecycle change accepted by set_statuses."""

    lemma: str
    status: Literal["worklist", "generated", "retired", "skipped"]
    reason: str = Field(
        default="",
        description="Why this change is made. Required when status is 'skipped' (e.g. \"free cognate of 'systém'\")",
    )


class PairProfile(BaseModel):
    """Language-pair conventions established once at init and rendered into prompts."""

    pos_tags: list[str] = Field(
        description="Part-of-speech vocabulary, written in the KNOWN language"
    )
    register_tags: list[str] = Field(
        description="Style tags for translation variants (e.g. neutral, colloquial)"
    )
    gender_values: list[str] = Field(
        default_factory=list,
        description="Noun genders of the TARGET language, in the KNOWN language",
    )
    gloss_style: str = Field(
        default="",
        description="How to write the preferred gloss: bare word vs parenthetical disambiguators, length, disambiguation policy",
    )
    sentence_style: str = Field(
        default="", description="How example sentences should read (length, register, topics)"
    )
    false_friend_policy: str = Field(
        default="", description="When and how to flag false friends for this pair"
    )
    notes: str = Field(
        default="", description="Anything else a translator for this pair should know"
    )


@dataclass
class Deps:
    """Run-time dependencies for the agent's tools.

    Tools receive a path, not a connection: pydantic-ai runs sync tools in a
    threadpool and sqlite connections are thread-bound, so every tool call
    opens its own short-lived connection.
    """

    db_path: Path
    model_name: str
    randomize: bool = False
    personal: bool = False


def validate_cloze(ex: Example, corpus_forms: list[str]) -> None:
    """Check that exactly one corpus form of the lemma appears in target and the cloze matches.

    Raises:
        ModelRetry: with a fix hint when the sentence/cloze contract is broken.
    """
    hits = [f for f in corpus_forms if re.search(rf"\b{re.escape(f)}\b", ex.target, re.IGNORECASE)]
    if len(hits) != 1:
        raise ModelRetry(
            f"sentence must contain exactly one form of the lemma, found {hits!r}; adjust the sentence"
        )
    cloze, _ = re.subn(
        rf"\b{re.escape(hits[0])}\b", "[...]", ex.target, count=1, flags=re.IGNORECASE
    )
    if ex.cloze != cloze:
        raise ModelRetry(
            f"cloze must be exactly {cloze!r} (sentence with {hits[0]!r} replaced by [...])"
        )


def unknown_gloss_token(conn: sqlite3.Connection, lang: str, gloss: str) -> str | None:
    """First token of the gloss missing from the language's validation lexicon, if any.

    Anti-fabrication tier: invented words ('úteka', 'prda', 'dovozit') are not
    in any lexicon; real but obscure/gloss-style choices pass because they are
    words. Every token of a multiword gloss must check out ('je mi líto').
    """
    for tok in (t.strip('.,!?"()') for t in gloss.lower().split()):
        if tok and not tok.isdigit() and not store.lexicon_contains(conn, lang, tok):
            return tok
    return None


def _cognate_class(lemma: str, gloss: str, false_friend_note: str) -> str:
    """Classify the lemma/gloss pair by normalized similarity.

    High similarity is only a candidate signal: the false-friend note (agent's
    semantic judgment) decides between false_friend and free.
    """
    import difflib
    import unicodedata

    def norm(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
        )

    ratio = difflib.SequenceMatcher(None, norm(lemma), norm(gloss)).ratio()
    if ratio >= 0.65:
        return "false_friend" if false_friend_note else "free"
    return "none"


def build_model(s: Settings) -> GoogleModel:
    """Build the Gemini model (one API key, one model name)."""
    if not s.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required (see .env.example)")
    from google.genai.types import HttpRetryOptions

    return GoogleModel(
        s.gemini_model,
        provider=GoogleProvider(
            api_key=s.gemini_api_key,
            retry_options=HttpRetryOptions(
                attempts=4,
                initial_delay=2.0,
                max_delay=30.0,
                # capacity spikes surface as 503, quota bursts as 429; both heal on retry
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        ),
    )


def build_agent(
    s: Settings,
    db_path: Path,
    model: Model | None = None,
    extra_prompt: str = "",
    audit: bool = False,
) -> Agent[Deps, str]:
    """Build the one agent with its five tools over the store.

    Args:
        s: Settings with the Gemini credentials and language pair.
        db_path: Store location; tool calls open their own connections to it.
        model: Override model (tests); built from the API key when omitted.
        extra_prompt: Optional user persona/task note appended to the
            instructions for this run (clis: --prompt).
        audit: Load the proofreader instructions (review_sample available)
            instead of the generator instructions.
    """
    if model is None:
        model = build_model(s)

    known = LANG_NAMES.get(s.known_lang, s.known_lang)
    target = LANG_NAMES.get(s.target_lang, s.target_lang)
    base = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{KNOWN}", known)
        .replace("{TARGET}", target)
    )
    with closing(store.connect(db_path)) as conn:
        profile = store.meta_get(conn, "pair_profile")
    vocab_map = known_vocab(s.known_lang)
    profile_block = (
        f"\n\n## Language-pair profile (JSON)\n{json.dumps(profile, ensure_ascii=False, indent=1)}"
        if profile
        else "\n\nNo pair profile saved yet; use sensible defaults for the pair."
    )
    if extra_prompt:
        profile_block += f"\n\n## User note for this run\n{extra_prompt}"
    agent = Agent[Deps, str](
        model,
        deps_type=Deps,
        instructions=base + profile_block,
        output_type=str,
        retries=5,
    )

    if audit:

        @agent.tool
        def review_sample(ctx: RunContext[Deps], n: int = 10) -> list[dict]:
            """Return a random sample of up to n existing cards with full payloads, for auditing."""

            with closing(store.connect(ctx.deps.db_path)) as conn:
                return store.sample_generated(conn, n)

    @agent.tool
    def poll_vocab(ctx: RunContext[Deps], n: int = 10) -> list[dict]:
        """Return up to n lemmas that have no translation yet, most frequent first, with corpus forms.

        Poll small batches (10-20), translate them, then poll again.
        similar_known names the KNOWN-language word a lemma resembles (when
        any): judge it yourself -- same meaning = free cognate, different
        meaning = false friend, unrelated = ignore the tag.
        """

        with closing(store.connect(ctx.deps.db_path)) as conn:
            out = []
            if ctx.deps.personal:
                rows = store.poll_personal_untranslated(conn, n)
            else:
                rows = store.poll_untranslated(conn, n, ctx.deps.randomize)
            for r in rows:
                row = dict(r) | {"forms": json.loads(r["forms"])}
                row["similar_known"] = similar_known(r["lemma"], vocab_map)
                hints = store.pair_suggestions(conn, r["lemma"], s.known_lang, s.target_lang)
                if ctx.deps.personal:
                    # the learner's own word leads the grounding: it is the
                    # reason this card exists
                    hints = [r["personal_known"]] + [h for h in hints if h != r["personal_known"]]
                if hints:
                    row["suggested_known"] = hints
                out.append(row)
            return out

    @agent.tool
    def lookup(
        ctx: RunContext[Deps],
        text: str,
        where: Literal["all", "lemmas", "glosses", "forms"] = "all",
    ) -> list[dict]:
        """Search the dictionary: lemmas, corpus forms, and/or submitted glosses matching a substring.

        An exact lemma hit returns that word's full existing card payload (if any) —
        use it to review/edit previous work and to avoid reusing glosses.
        """
        with closing(store.connect(ctx.deps.db_path)) as conn:
            return store.find_lexicon(conn, text, where)

    @agent.tool
    def submit_translations(ctx: RunContext[Deps], drafts: list[TranslationDraft]) -> str:
        """Save (or replace, to edit) complete cards for a batch of lemmas.

        Submit a whole polled batch in one call. Each draft is validated on
        its own: valid cards are saved, invalid ones are reported back in
        rejected with the reason (they stay untranslated and will reappear in
        poll_vocab). Returned warnings are advisory only.
        """
        if not 1 <= len(drafts) <= 20:
            raise ModelRetry("submit 1-20 drafts per call")
        saved, rejected, warnings = [], [], []
        with closing(store.connect(ctx.deps.db_path)) as conn:
            for draft in drafts:
                try:
                    row = conn.execute(
                        "SELECT id, forms, status FROM lemmas WHERE lemma=?", (draft.lemma,)
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "not in the vocabulary corpus; only lemmas from poll_vocab exist"
                        )
                    corpus_forms = list(dict.fromkeys(json.loads(row["forms"]) + [draft.lemma]))
                    for ex in draft.examples:
                        validate_cloze(ex, corpus_forms)
                    preferred = draft.variants[draft.preferred].gloss
                    hints = store.pair_suggestions(conn, draft.lemma, s.known_lang, s.target_lang)
                    if hints and preferred.lower() not in {h.lower() for h in hints}:
                        warnings.append(
                            f"{draft.lemma}: preferred gloss {preferred!r} is not among the suggested"
                            f" candidates {hints!r}; kept, but the off-list choice needs justification"
                        )
                    # anti-fabrication tier: inert until the validation wordlist is built
                    if conn.execute(
                        "SELECT 1 FROM lexicon_words WHERE lang = ? LIMIT 1", (s.known_lang,)
                    ).fetchone():
                        for v in draft.variants:
                            bad = unknown_gloss_token(conn, s.known_lang, v.gloss)
                            if bad:
                                raise ValueError(
                                    f"gloss token {bad!r} (in {v.gloss!r}) is not a Czech word;"
                                    f" check spelling or pick a real one (see suggested_known)"
                                )
                    duplicates = [
                        r["lemma"]
                        for r in conn.execute(
                            "SELECT l.lemma FROM content c JOIN lemmas l ON l.id=c.lemma_id"
                            " WHERE c.preferred_gloss=? AND c.lemma_id!=?",
                            (preferred, row["id"]),
                        )
                    ]
                    if duplicates:
                        warnings.append(
                            f"{draft.lemma}: gloss {preferred!r} already used by {', '.join(duplicates)}"
                        )
                    # write order matters for crash recovery: metadata first,
                    # content second, status flip last -- every window heals on re-poll.
                    kind = "personal" if ctx.deps.personal else "lemma"
                    conn.execute(
                        "UPDATE lemmas SET pos=?, cognate_class=?, updated_at=? WHERE id=?",
                        (
                            draft.pos,
                            _cognate_class(draft.lemma, preferred, draft.false_friend_note),
                            store.now(),
                            row["id"],
                        ),
                    )
                    conn.commit()
                    store.upsert_content(
                        conn,
                        lemma_id=row["id"],
                        kind=kind,
                        preferred_gloss=preferred,
                        payload=draft.model_dump_json(),
                        model=ctx.deps.model_name,
                    )
                    if row["status"] == "worklist":
                        store.set_status(conn, draft.lemma, "generated")
                    saved.append(draft.lemma)
                except (ValueError, ModelRetry) as e:
                    rejected.append({"lemma": draft.lemma, "error": str(e)})
        if not saved:
            raise ModelRetry(f"every draft was rejected: {rejected}")
        return json.dumps(
            {"saved": saved, "rejected": rejected, "warnings": warnings}, ensure_ascii=False
        )

    @agent.tool
    def set_statuses(ctx: RunContext[Deps], changes: list[StatusChange]) -> str:
        """Set lifecycle status for a batch of lemmas. retired = bad/stale card;
        skipped = deliberately not learned (reason required), e.g. a free cognate.
        """
        applied, rejected = [], []
        with closing(store.connect(ctx.deps.db_path)) as conn:
            for ch in changes:
                if ch.status == "skipped" and not ch.reason:
                    rejected.append({"lemma": ch.lemma, "error": "skipped requires a reason"})
                    continue
                if store.set_status(conn, ch.lemma, ch.status, reason=ch.reason):
                    applied.append(ch.lemma)
                else:
                    rejected.append({"lemma": ch.lemma, "error": "unknown lemma"})
        return json.dumps({"applied": applied, "rejected": rejected}, ensure_ascii=False)

    @agent.tool
    def save_pair_profile(ctx: RunContext[Deps], profile: PairProfile) -> str:
        """Save the language-pair profile (pos tags, register tags, gender values, style notes)."""
        with closing(store.connect(ctx.deps.db_path)) as conn:
            store.meta_set(conn, "pair_profile", profile.model_dump())
        return "pair profile saved"

    return agent
