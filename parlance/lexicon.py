"""Translation-candidate grounding from kaikki.org wiktextract edition dumps (CC BY-SA 3.0).

For a language pair (known, target) two directions combine:

- direct:  the TARGET edition's entries carry translation sections into KNOWN
- inverse: the KNOWN edition's entries carry translation sections into TARGET,
           inverted so pl word -> Czech candidates works either way

These pairs are HINTS ONLY: poll_vocab surfaces them as `suggested_known`
and the agent judges them. Nothing is enforced; ground truth stays the model
plus its validators. The whole loader is language-agnostic -- new pairs are
two language codes away.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from . import store

KAIKKI_URL = "https://kaikki.org/dictionary/downloads/{lang}/{lang}-extract.jsonl.gz"


def _extract(lang: str, cache_dir: Path, source_dir: Path | None) -> Path:
    """Return the gzipped edition extract for lang, downloading into cache_dir if absent."""
    if source_dir is not None and (src := source_dir / f"{lang}-extract.jsonl.gz").exists():
        return src
    dest = cache_dir / f"{lang}-extract.jsonl.gz"
    if not dest.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(KAIKKI_URL.format(lang=lang), dest)
    return dest


def iter_translations(path: Path, lang_code: str) -> Iterator[tuple[str, str]]:
    """Yield (entry_word, translated_word) for every translation into lang_code."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            word = (e.get("word") or "").strip().lower()
            if not word:
                continue
            for t in e.get("translations") or []:
                if t.get("lang_code") == lang_code and t.get("word"):
                    yield word, t["word"].strip()


def parse_hint_json(text: str) -> list[list[str]] | None:
    """Extract the [[...], ...] hint array from a spoken-transcription payload.

    Survives code fences, narration around the array, and newlines. None when
    no parseable array exists (caller skips the batch instead of guessing).
    """
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(arr, list):
        return None
    out = []
    for slot in arr:
        if isinstance(slot, str):
            slot = [slot]
        if not isinstance(slot, list):
            return None
        out.append([str(g).strip() for g in slot if str(g).strip()])
    return out


LIVE_HINT_MODEL = "gemini-3.8-live"
LIVE_HINT_SOURCE = "gemini-live"


async def build_llm_hints(
    conn: sqlite3.Connection,
    settings,
    batch: int = 20,
    limit: int | None = None,
    session_every: int = 200,
    log=print,
) -> dict:
    """Backfill gemini-live hint rows for every lemma lacking one (resumable).

    One Live session, chunked to dodge server-side session limits; each turn
    carries `batch` numbered lemmas and expects a spoken JSON array of Czech
    candidates in the same order (probe: 20/20 slots in ~4 s).
    """
    from google import genai
    from google.genai import types

    done = {
        r["target"]
        for r in conn.execute(
            "SELECT DISTINCT target FROM lexicon_pairs WHERE sources LIKE ?",
            (f"%{LIVE_HINT_SOURCE}%",),
        )
    }
    pending = [
        r["lemma"]
        for r in conn.execute("SELECT lemma FROM lemmas ORDER BY freq_rank")
        if r["lemma"] not in done
    ]
    if limit:
        pending = pending[:limit]
    if not pending:
        log("llm-hints: nothing pending")
        return {"lemmas": 0, "turns": 0}

    client = genai.Client(api_key=settings.gemini_api_key)
    written = turns = 0
    for chunk_start in range(0, len(pending), session_every):
        chunk = pending[chunk_start : chunk_start + session_every]
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=(
                f"Jsi prekladac {settings.target_lang}->{settings.known_lang}. "
                f"Na ocislovany seznam slov odpovez POUZE JSON polem ve stejnem poradi: "
                f'[["preklad1","preklad2"], ...]. 1-3 hovorove preklady, zadny jiny text.'
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            temperature=0.2,
        )
        async with client.aio.live.connect(model=LIVE_HINT_MODEL, config=cfg) as session:
            for i in range(0, len(chunk), batch):
                words = chunk[i : i + batch]
                out = []
                try:
                    import asyncio

                    async with asyncio.timeout(60):
                        await session.send_realtime_input(
                            text=", ".join(f"{j + 1}. {w}" for j, w in enumerate(words))
                        )
                        async for msg in session.receive():
                            sc = msg.server_content
                            if sc and sc.output_transcription and sc.output_transcription.text:
                                out.append(sc.output_transcription.text)
                            if sc and sc.turn_complete:
                                break
                except TimeoutError:
                    log(f"llm-hints: turn timeout @{i}; rotating session, lemmas stay pending")
                    break
                turns += 1
                slots = parse_hint_json("".join(out))
                if not slots:
                    log(f"llm-hints: batch @{i} unparsed, skipped")
                    continue
                rows = [
                    (word, gloss, LIVE_HINT_SOURCE)
                    for word, glosses in zip(words, slots)
                    for gloss in glosses
                ]
                store.merge_pairs(conn, settings.target_lang, settings.known_lang, rows)
                written += len(rows)
        log(
            f"llm-hints: {min(chunk_start + len(chunk), len(pending))}/{len(pending)} lemmas, {written} rows"
        )
    return {"lemmas": len(pending), "turns": turns, "rows": written}


def build_pairs(
    conn: sqlite3.Connection,
    known: str,
    target: str,
    cache_dir: Path,
    source_dir: Path | None = None,
) -> dict:
    """Load both editions, merge directions, replace the pair table for this pair."""
    merged: dict[str, dict[str, set[str]]] = {}
    for path, code, tag, invert in (
        (_extract(target, cache_dir, source_dir), known, f"{target}wikt", False),
        (_extract(known, cache_dir, source_dir), target, f"{known}wikt-inv", True),
    ):
        for word, translated in iter_translations(path, code):
            tgt, knw = (translated, word) if invert else (word, translated)
            merged.setdefault(tgt, {}).setdefault(knw, set()).add(tag)
    rows = [
        (target, tgt, known, knw, json.dumps(sorted(srcs), ensure_ascii=False))
        for tgt, cand in merged.items()
        for knw, srcs in cand.items()
    ]
    replaced = store.replace_pairs(conn, target, known, rows)
    return {
        "lemmas_with_hints": len(merged),
        "pairs_written": replaced,
        "extracts": {
            target: str(_extract(target, cache_dir, source_dir)),
            known: str(_extract(known, cache_dir, source_dir)),
        },
    }
