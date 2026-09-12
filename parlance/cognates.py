"""Cognate tooling: known-language vocab membership + false-friends guard.

Membership answers 'is this target lemma also a word in the known language'
using simplemma's bundled dictionaries (offline). The guard stops the one
fatal class of error: words that exist in BOTH languages but mean different
things (szukać = hledat, NOT šukat) must never count as free cognates.
"""

import json
import sqlite3
import unicodedata
from functools import lru_cache
from pathlib import Path

from simplemma.strategies.dictionaries import DefaultDictionaryFactory

from .store import now

FF_DIR = Path(__file__).parent / "data"


def strip_diacritics(s: str) -> str:
    """Lowercase and remove combining marks, for cross-language orthographic comparison."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


@lru_cache(maxsize=4)
def known_lemmas(lang: str) -> frozenset[str]:
    """All diacritic-stripped LEMMAS of the known language, from simplemma's bundled dictionary.

    Lemmas, not forms: matching against arbitrary forms wrongly equates a
    target lemma with some obscure known-language inflection (Polish kochać
    vs Czech kochám-from-kochat-se).
    """
    mapping = DefaultDictionaryFactory().get_dictionary(lang)
    return frozenset(strip_diacritics(v) for v in mapping.values())


@lru_cache(maxsize=8)
def ff_guard(target_lang: str, known_lang: str) -> frozenset[str]:
    """Target lemmas that look like known-language words but mean something else, for this pair.

    Returns an empty set for pairs without a curated guard file.
    """
    path = FF_DIR / f"false_friends_{target_lang}_{known_lang}.json"
    if not path.exists():
        return frozenset()
    return frozenset(json.loads(path.read_text(encoding="utf-8"))["guards"])


def mark_free_cognates(conn: sqlite3.Connection, *, target_lang: str, known_lang: str) -> list[str]:
    """Mark untranslated lemmas that are also known-language words and not false friends.

    Matched rows get cognate_class='free' and status='skipped', excluding them
    from generation. Guarded (false-friend) lemmas are never touched.
    """
    vocab = known_lemmas(known_lang)
    guard = ff_guard(target_lang, known_lang)
    rows = list(
        conn.execute(
            "SELECT id, lemma FROM lemmas WHERE status='worklist' AND (cognate_class IS NULL OR cognate_class='')"
        )
    )
    skipped = []
    for r in rows:
        if strip_diacritics(r["lemma"]) in vocab and r["lemma"] not in guard:
            conn.execute(
                "UPDATE lemmas SET cognate_class='free', status='skipped', updated_at=? WHERE id=?",
                (now(), r["id"]),
            )
            skipped.append(r["lemma"])
    conn.commit()
    return skipped
