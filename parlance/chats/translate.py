"""Pre-pass: personal words -> target lemmas. Dictionary-first, LLM fills gaps.

The card-writing agent stays deck-agnostic: it receives target-language
lemmas either way. This module owns the known->target decision and the
skip-with-reason verdicts, materializing chosen words into the shared
lemma table so both decks dedupe against each other through the status
system.
"""

import json
from collections import Counter
from itertools import batched
from pathlib import Path

import simplemma
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from .. import store, worklist
from ..agent import LANG_NAMES, build_model
from ..config import Settings

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "translate.md"


class WordTarget(BaseModel):
    """One pre-pass verdict: the chosen target lemma, or a skip reason."""

    known: str = Field(description="the input word, echoed exactly as given")
    target: str = Field(
        default="", description="the everyday target-language lemma; empty when skipped"
    )
    skip_reason: str = Field(
        default="", description="one short line why the word is not worth a card"
    )


class WordTargets(BaseModel):
    """A batch of pre-pass verdicts."""

    words: list[WordTarget]


def _target_forms(path, lang: str) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Group the target frequency list: lemma -> corpus forms, form -> rank."""
    forms_by_lemma: dict[str, list[str]] = {}
    ranks: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for rank, line in enumerate(f, start=1):
            parts = line.split()
            if not parts:
                continue
            form = parts[0].lower()
            ranks.setdefault(form, rank)
            forms_by_lemma.setdefault(simplemma.lemmatize(form, lang=lang), []).append(form)
    return forms_by_lemma, ranks


def _dict_candidates(
    conn, known_lang: str, target_lang: str, words: list[str]
) -> dict[str, list[str]]:
    """Dictionary translation candidates per word, from the lexicon hint pairs."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(words), 50):
        chunk = words[i : i + 50]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT known, target FROM lexicon_pairs"
            f" WHERE target_lang=? AND known_lang=? AND known IN ({marks})",
            [target_lang, known_lang, *chunk],
        ):
            bucket = out.setdefault(r["known"], [])
            if r["target"] not in bucket and len(bucket) < 6:
                bucket.append(r["target"])
    return out


def _materialize(
    conn, word: str, chat_count: int, forms_by_lemma, ranks, fallback: list[int], lang: str
) -> tuple[int, bool]:
    """Insert the chosen word as a lemma (or reuse the existing row).

    The chosen word is normalized to its simplemma lemma so the target
    frequency-list form grouping applies; corpus forms come from that
    grouping, making cloze validation work exactly as for frequency-worklist
    lemmas.

    Returns:
        (lemma_id, newly created).
    """
    lemma = simplemma.lemmatize(word.strip().lower(), lang=lang)
    row = conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()
    if row is not None:
        return row["id"], False
    rank = ranks.get(lemma)
    if rank is None:
        fallback[0] += 1
        rank = fallback[0]
    ts = store.now()
    cur = conn.execute(
        "INSERT INTO lemmas (lemma, freq_rank, freq, forms, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            lemma,
            rank,
            chat_count,
            json.dumps(forms_by_lemma.get(lemma) or [lemma], ensure_ascii=False),
            ts,
            ts,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid, True


def translate_pending(
    conn, s: Settings, batch: int = 40, model: Model | None = None, words: int | None = None
) -> dict[str, int]:
    """Run the pre-pass over pending personal words (any language).

    Idempotent and resumable: only words without a verdict are sent; words
    the model omits or echoes wrongly stay pending and are retried next run.
    A failed LLM batch is logged and skipped the same way.

    Args:
        conn: Store connection.
        s: Settings (target wordlist is downloaded when missing).
        batch: Words per LLM request.
        model: Override model (tests).
        words: Only the N most-used words (None = all pending).

    Returns:
        Counts: pending, mapped, skipped, new_lemmas, existing_lemmas,
        unmatched, omitted, errors.
    """
    pending = store.personal_pending(conn, words)
    stats: Counter[str] = Counter(pending=len(pending))
    if not pending:
        return dict(stats)
    worklist.ensure_source(s.wordlist_path, s.target_lang)
    forms_by_lemma, ranks = _target_forms(s.wordlist_path, s.target_lang)
    model = model or build_model(s)
    fallback = [50_000]
    last_error = ""

    template = PROMPT_PATH.read_text(encoding="utf-8")
    for lang in sorted({r["lang"] for r in pending}):
        known = LANG_NAMES.get(lang, lang)
        target = LANG_NAMES.get(s.target_lang, s.target_lang)
        instructions = template.replace("{KNOWN}", known).replace("{TARGET}", target)
        agent = Agent(model, output_type=WordTargets, retries=5, instructions=instructions)
        for chunk in batched([r for r in pending if r["lang"] == lang], batch):
            words_in_chunk = [r["lemma"] for r in chunk]
            candidates = _dict_candidates(conn, lang, s.target_lang, words_in_chunk)
            prompt = "\n".join(
                f"{w}  [candidates: {', '.join(candidates.get(w, []))}]" for w in words_in_chunk
            )
            try:
                result = agent.run_sync(prompt)
            except Exception as e:  # noqa: BLE001 -- one failed batch must not kill the run; its words stay pending
                stats["errors"] += 1
                last_error = f"{type(e).__name__}: {e}"[:200]
                continue
            by_word = {r["lemma"]: r for r in chunk}
            verdicts = []
            for t in result.output.words:
                w = by_word.pop(t.known, None)
                if w is None:
                    stats["unmatched"] += 1
                    continue
                lemma_id = None
                if t.target:
                    lemma_id, is_new = _materialize(
                        conn,
                        t.target,
                        w["count"],
                        forms_by_lemma,
                        ranks,
                        fallback,
                        s.target_lang,
                    )
                    stats["new_lemmas" if is_new else "existing_lemmas"] += 1
                verdicts.append(
                    {"known": t.known, "lemma_id": lemma_id, "skip_reason": t.skip_reason}
                )
            stats["omitted"] += len(by_word)
            mapped, skipped = store.apply_personal_targets(conn, lang, verdicts)
            stats["mapped"] += mapped
            stats["skipped"] += skipped
    store.journal(
        conn,
        "personal:prepass",
        None,
        {k: v for k, v in stats.items() if k != "pending"}
        | ({"last_error": last_error} if last_error else {}),
    )
    conn.commit()
    return dict(stats)
