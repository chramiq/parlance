"""Translation-candidate grounding.

Two provider families, both landing in the `lexicon_pairs` table as ADVISORY
hints surfaced by poll_vocab (`suggested_known`); nothing is enforced and
the agent judges:

- dictionary: kaikki.org wiktextract edition dumps (CC BY-SA 3.0), both
  directions (target-edition direct + known-edition inverted)
- generative: gemini-3.8-live spoken-JSON turns over the Live transcription
  channel (unlimited free tier), parsed into candidate slots

The loader is language-agnostic -- new pairs are two language codes away.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import sqlite3
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from . import store

KAIKKI_URL = "https://kaikki.org/dictionary/downloads/{lang}/{lang}-extract.jsonl.gz"
LIVE_HINT_MODEL = "gemini-3.8-live"
LIVE_HINT_SOURCE = "gemini-live"


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
    }


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
    except json.JSONDecodeError:
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


def lemmas_missing_hints(conn: sqlite3.Connection, lemmas: list[str]) -> list[str]:
    """Filter lemma list to those with no gemini-live hint row yet."""
    if not lemmas:
        return []
    marks = ",".join("?" * len(lemmas))
    done = {
        r["target"]
        for r in conn.execute(
            f"SELECT DISTINCT target FROM lexicon_pairs WHERE sources LIKE ? AND target IN ({marks})",
            (f"%{LIVE_HINT_SOURCE}%", *lemmas),
        )
    }
    return [lemma for lemma in lemmas if lemma not in done]


async def _hint_turn(settings, words: list[str]) -> list[list[str]] | None:
    """One fresh Live session, one numbered batch, parsed candidate slots. None on failure."""
    from google import genai
    from google.genai import types

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
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        async with asyncio.timeout(60):
            async with client.aio.live.connect(model=LIVE_HINT_MODEL, config=cfg) as session:
                await session.send_realtime_input(
                    text=", ".join(f"{j + 1}. {w}" for j, w in enumerate(words))
                )
                out: list[str] = []
                async for msg in session.receive():
                    sc = msg.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        out.append(sc.output_transcription.text)
                    if sc and sc.turn_complete:
                        break
                return parse_hint_json("".join(out))
    except Exception:  # noqa: BLE001 -- flaky live sessions/timeouts/parse noise all mean "skip batch"
        return None


async def hint_lemmas(
    conn: sqlite3.Connection, settings, lemmas: list[str], batch: int = 20, log=print
) -> int:
    """Hint exactly these lemmas (single worker). Returns rows written."""
    written = 0
    for i in range(0, len(lemmas), batch):
        words = lemmas[i : i + batch]
        slots = await _hint_turn(settings, words)
        if not slots:
            log(f"llm-hints: batch @{i} failed, skipped (stays pending)")
            continue
        rows = [
            (word, gloss, LIVE_HINT_SOURCE)
            for word, glosses in zip(words, slots)
            for gloss in glosses
        ]
        written += store.merge_pairs(conn, settings.target_lang, settings.known_lang, rows)
    return written


async def build_llm_hints(
    conn: sqlite3.Connection,
    settings,
    batch: int = 20,
    limit: int | None = None,
    workers: int = 4,
    log=print,
) -> dict:
    """Parallel resumable backfill of gemini-live hints (probe: 4/4 concurrent sessions OK, 8 -> quota)."""
    all_lemmas = [r["lemma"] for r in conn.execute("SELECT lemma FROM lemmas ORDER BY freq_rank")]
    pending = lemmas_missing_hints(conn, all_lemmas)
    if limit:
        pending = pending[:limit]
    if not pending:
        log("llm-hints: nothing pending")
        return {"lemmas": 0, "turns": 0}

    queue: asyncio.Queue[list[str]] = asyncio.Queue()
    for i in range(0, len(pending), batch):
        queue.put_nowait(pending[i : i + batch])
    stats = {"turns": 0, "rows": 0, "failed_batches": 0}

    async def worker(worker_id: int) -> None:
        from google import genai
        from google.genai import types

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
        client = genai.Client(api_key=settings.gemini_api_key)
        session = None
        session_cm = None
        while True:
            try:
                words = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            slots: list[list[str]] | None = None
            for attempt in range(4):
                if session is None:
                    try:
                        session_cm = client.aio.live.connect(model=LIVE_HINT_MODEL, config=cfg)
                        session = await session_cm.__aenter__()
                    except Exception as e:  # noqa: BLE001 -- quota/connect churn: back off, retry
                        wait = 15 * (attempt + 1)
                        log(f"llm-hints: w{worker_id} connect failed ({str(e)[:40]}), wait {wait}s")
                        await asyncio.sleep(wait)
                        continue
                try:
                    async with asyncio.timeout(60):
                        await session.send_realtime_input(
                            text=", ".join(f"{j + 1}. {w}" for j, w in enumerate(words))
                        )
                        out: list[str] = []
                        async for msg in session.receive():
                            sc = msg.server_content
                            if sc and sc.output_transcription and sc.output_transcription.text:
                                out.append(sc.output_transcription.text)
                            if sc and sc.turn_complete:
                                break
                    slots = parse_hint_json("".join(out))
                    break
                except Exception as e:  # noqa: BLE001 -- turn failed: drop session, reconnect next try
                    session = None
                    session_cm = None
                    wait = 15 * (attempt + 1)
                    log(f"llm-hints: w{worker_id} turn failed ({str(e)[:40]}), wait {wait}s")
                    await asyncio.sleep(wait)
            if not slots:
                stats["failed_batches"] += 1
                log(f"llm-hints: batch gave up ({' / '.join(words[:3])}...), stays pending")
                continue
            stats["turns"] += 1
            rows = [
                (word, gloss, LIVE_HINT_SOURCE)
                for word, glosses in zip(words, slots)
                for gloss in glosses
            ]
            stats["rows"] += store.merge_pairs(
                conn, settings.target_lang, settings.known_lang, rows
            )
        if session_cm is not None:
            await session_cm.__aexit__(None, None, None)

    await asyncio.gather(*(worker(w) for w in range(workers)))
    return {"lemmas": len(pending), **stats}
