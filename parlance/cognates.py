"""Cognate tooling: known-language vocab map + orthographic similarity tags.

The map answers 'which known-language word does this target lemma resemble',
used only to TAG lemmas for the model. The model owns every judgment
(free cognate, false friend, unrelated) — nothing here decides.
"""

import unicodedata
from functools import lru_cache

from simplemma.strategies.dictionaries import DefaultDictionaryFactory


def strip_diacritics(s: str) -> str:
    """Lowercase and remove combining marks, for cross-language orthographic comparison."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


@lru_cache(maxsize=4)
def known_vocab(lang: str) -> dict[str, list[str]]:
    """Map of diacritic-stripped known-language form to the actual lemma(s), max 3 each."""
    mapping = DefaultDictionaryFactory().get_dictionary(lang)
    out: dict[str, list[str]] = {}
    for lemma in mapping.values():
        key = strip_diacritics(lemma)
        bucket = out.setdefault(key, [])
        if len(bucket) < 3 and lemma not in bucket:
            bucket.append(lemma)
    return out


def similar_known(lemma: str, vocab: dict[str, list[str]]) -> str | None:
    """The known-language word(s) the target lemma orthographically resembles, or None."""
    hits = vocab.get(strip_diacritics(lemma))
    return ", ".join(hits) if hits else None
