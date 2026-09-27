"""Translation-candidate grounding.

kaikki.org wiktextract edition dumps (CC BY-SA 3.0), both directions
(target-edition direct + known-edition inverted), landing in the
lexicon_pairs table as ADVISORY hints surfaced by poll_vocab
(suggested_known); nothing is enforced and the agent judges.

The loader is language-agnostic -- new pairs are two language codes away.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from . import store
from .worklist import SOURCE_URL_TEMPLATE

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


def build_words(conn: sqlite3.Connection, lang: str, cache_dir: Path) -> dict:
    """Validation lexicon for a language: edition headwords + frequency-list surface forms.

    Headwords alone miss real inflected usage (~21% of legit glosses in the
    measured hint table); the 50k forms union closes that hole.
    """
    words: set[str] = set()
    with gzip.open(_extract(lang, cache_dir, None), "rt", encoding="utf-8") as f:
        for line in f:
            w = (json.loads(line).get("word") or "").strip().lower()
            if w:
                words.add(w)
    forms_path = cache_dir / f"{lang}_50k.txt"
    if not forms_path.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(SOURCE_URL_TEMPLATE.format(lang=lang), forms_path)
    with open(forms_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if parts:
                words.add(parts[0].strip().lower())
    written = store.replace_words(conn, lang, words)
    return {"words_written": written, "sources": ["kaikki-headwords", "50k-forms"]}
