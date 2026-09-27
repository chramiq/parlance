"""Frequency list -> simplemma lemmas -> cognate classes -> dedup vs store -> ranked queue.

The source list is hermitdave/FrequencyWords: spoken-register forms with raw counts.
Surface forms collapse to lemmas via simplemma, counts are summed per lemma, and the
queue is ranked by summed frequency. Cognate classification happens later, at
generation time, when a gloss pair actually exists to compare.
"""

import os
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import simplemma

from . import store

SOURCE_URL = (
    "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/pl/pl_50k.txt"
)
MAX_FORMS_PER_LEMMA = 20


@dataclass
class LemmaQueueItem:
    """One worklist entry: a lemma with its summed corpus weight.

    Attributes:
        forms: Surface forms seen in the corpus, most frequent first.
    """

    lemma: str
    freq: int
    forms: list[str] = field(default_factory=list)


def ensure_source(path: Path) -> None:
    """Download the frequency list to path when missing, no-op otherwise."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with httpx.stream("GET", SOURCE_URL, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                f.writelines(r.iter_bytes())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def iter_forms(path: Path, scan_limit: int) -> list[tuple[str, int]]:
    """Read up to scan_limit (form, count) pairs from the source file."""
    forms = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                forms.append((parts[0], int(parts[1])))
            if len(forms) >= scan_limit:
                break
    return forms


def build_queue(forms: list[tuple[str, int]]) -> list[LemmaQueueItem]:
    """Collapse surface forms into lemmas ranked by summed count.

    Returns:
        Items sorted by summed frequency descending.
    """
    freq: dict[str, int] = {}
    seen_forms: dict[str, list[str]] = {}
    for form, count in forms:
        lemma = simplemma.lemmatize(form, lang="pl")
        freq[lemma] = freq.get(lemma, 0) + count
        bucket = seen_forms.setdefault(lemma, [])
        if len(bucket) < MAX_FORMS_PER_LEMMA:
            bucket.append(form)
    ranked = sorted(freq.items(), key=lambda kv: -kv[1])
    return [LemmaQueueItem(lemma=lem, freq=c, forms=seen_forms[lem]) for lem, c in ranked]


def run(db_path: Path, source_path: Path, scan_limit: int) -> tuple[int, int, int]:
    """Build the worklist: fetch source if needed, lemmatize, seed the store.

    Returns:
        (total_lemmas_in_queue, inserted, skipped_as_known).
    """
    ensure_source(source_path)
    forms = iter_forms(source_path, scan_limit)
    if not forms:
        raise ValueError(f"source {source_path} yielded 0 forms (empty or corrupt file?)")
    queue = build_queue(forms)
    rows = [
        {"lemma": it.lemma, "freq_rank": rank, "freq": it.freq, "forms": it.forms}
        for rank, it in enumerate(queue, start=1)
    ]
    with closing(store.connect(db_path)) as conn:
        inserted, skipped = store.add_lemmas(conn, rows)
    return len(queue), inserted, skipped
