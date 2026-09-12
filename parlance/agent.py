"""The single pydantic-ai agent: generates translations, profiles pairs, and maintains the store via tools.

All structured data enters the store as validated tool arguments; the agent's
own output stays plain text. See docs/0005.
"""

import json
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import store
from .config import Settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "vocab.md"

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
    """Card payload accepted by submit_translation. Rejected args are retried by the model."""

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


async def strip_gateway_metadata(response: httpx2.Response) -> None:
    """Remove nonstandard gateway routing metadata the OpenAI SDK would reject (see docs/0004)."""
    if "application/json" not in response.headers.get("content-type", ""):
        return
    body = await response.aread()
    if b"weight_version" not in body:
        return
    data = json.loads(body)
    data.pop("metadata", None)
    response._content = json.dumps(data).encode()


def build_agent(s: Settings, db_path: Path, model: Model | None = None) -> Agent[Deps, str]:
    """Build the one agent with its five tools over the store.

    Args:
        s: Settings with the LiteLLM endpoint and language pair.
        db_path: Store location; tool calls open their own connections to it.
        model: Override model (tests); built from the endpoint when omitted.
    """
    if model is None:
        if not (s.litellm_base_url and s.litellm_api_key and s.litellm_model):
            raise RuntimeError(
                "LITELLM_BASE_URL, LITELLM_API_KEY and LITELLM_MODEL are required (see .env.example)"
            )
        http = httpx2.AsyncClient(event_hooks={"response": [strip_gateway_metadata]}, timeout=120)
        client = AsyncOpenAI(
            base_url=s.litellm_base_url, api_key=s.litellm_api_key, http_client=http
        )
        model = OpenAIChatModel(s.litellm_model, provider=OpenAIProvider(openai_client=client))

    known = LANG_NAMES.get(s.known_lang, s.known_lang)
    target = LANG_NAMES.get(s.target_lang, s.target_lang)
    base = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{KNOWN}", known)
        .replace("{TARGET}", target)
    )
    with closing(store.connect(db_path)) as conn:
        profile = store.meta_get(conn, "pair_profile")
    profile_block = (
        f"\n\n## Language-pair profile (JSON)\n{json.dumps(profile, ensure_ascii=False, indent=1)}"
        if profile
        else "\n\nNo pair profile saved yet; use sensible defaults for the pair."
    )
    agent = Agent[Deps, str](
        model,
        deps_type=Deps,
        instructions=base + profile_block,
        output_type=str,
        retries=5,
    )

    @agent.tool
    def poll_vocab(ctx: RunContext[Deps], n: int = 10) -> list[dict]:
        """Return up to n lemmas that have no translation yet, most frequent first, with corpus forms.

        Poll small batches (10-20), translate them, then poll again.
        """

        with closing(store.connect(ctx.deps.db_path)) as conn:
            return [
                dict(r) | {"forms": json.loads(r["forms"])}
                for r in store.poll_untranslated(conn, n)
            ]

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
    def submit_translation(ctx: RunContext[Deps], draft: TranslationDraft) -> str:
        """Save (or replace, to edit) the complete card for one lemma.

        The draft is validated; semantic violations come back as retry hints.
        Returned warnings are advisory only (e.g. gloss already used elsewhere).
        """
        with closing(store.connect(ctx.deps.db_path)) as conn:
            row = conn.execute(
                "SELECT id, forms, status FROM lemmas WHERE lemma=?", (draft.lemma,)
            ).fetchone()
            if row is None:
                raise ModelRetry(
                    f"{draft.lemma!r} is not in the vocabulary corpus; only translate lemmas from poll_vocab"
                )
            corpus_forms = list(dict.fromkeys(json.loads(row["forms"]) + [draft.lemma]))
            for ex in draft.examples:
                validate_cloze(ex, corpus_forms)
            preferred = draft.variants[draft.preferred].gloss
            duplicates = [
                r["lemma"]
                for r in conn.execute(
                    "SELECT l.lemma FROM content c JOIN lemmas l ON l.id=c.lemma_id"
                    " WHERE c.preferred_gloss=? AND c.lemma_id!=?",
                    (preferred, row["id"]),
                )
            ]
            # write order matters for crash recovery: metadata first, content
            # second, status flip last -- every window re-polls and heals.
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
            created = store.upsert_content(
                conn,
                lemma_id=row["id"],
                kind="lemma",
                preferred_gloss=preferred,
                payload=draft.model_dump_json(),
                model=ctx.deps.model_name,
            )
            if row["status"] == "worklist":
                store.set_status(conn, draft.lemma, "generated")
            return json.dumps(
                {
                    "saved": draft.lemma,
                    "created": created,
                    "warnings": [
                        f"preferred gloss {preferred!r} already used by: {', '.join(duplicates)}"
                    ]
                    if duplicates
                    else [],
                },
                ensure_ascii=False,
            )

    @agent.tool
    def set_status(
        ctx: RunContext[Deps], lemma: str, status: Literal["worklist", "generated", "retired"]
    ) -> str:
        """Set a lemma's lifecycle status. Use retired for bad or stale cards."""
        with closing(store.connect(ctx.deps.db_path)) as conn:
            ok = store.set_status(conn, lemma, status)
        if ok:
            return f"{lemma}: {status}"
        raise ModelRetry(f"unknown lemma {lemma!r}")

    @agent.tool
    def save_pair_profile(ctx: RunContext[Deps], profile: PairProfile) -> str:
        """Save the language-pair profile (pos tags, register tags, gender values, style notes)."""
        with closing(store.connect(ctx.deps.db_path)) as conn:
            store.meta_set(conn, "pair_profile", profile.model_dump())
        return "pair profile saved"

    return agent
