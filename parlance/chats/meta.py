"""Meta DIY chat-export ingestion: the owner's own words become a personal wordlist.

The privacy model is structural: only single words that also appear in a
public frequency list of the language ever reach the store, in shuffled
count form -- sentences never leave this module, and names, handles, slang
and dialect drop out because they are not public words. Message hashes make
re-imports idempotent and imports from several exports accumulate
(Instagram and Facebook ship the same JSON shape).
"""

import hashlib
import json
import re
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path

import simplemma

from .. import store
from ..worklist import SOURCE_URL_TEMPLATE

# Localized template strings Meta writes as message content. Matched after
# the mojibake fix; extend when a new export vintage/provenance adds templates.
SYSTEM_STRINGS = {
    "Posíláte přílohu.",
    "Audio call ended",
    "You started an audio call",
}

TOKEN_RE = re.compile(r"[a-záčďéěíňóřšťúůýž-]+")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _fix(s: str) -> str:
    """Repair Meta's double encoding (UTF-8 bytes JSON-escaped as Latin-1).

    'chuÅ¥' -> 'chuť'. Strings that are not Latin-1 round-trippable (already
    proper UTF-8) pass through unchanged.
    """
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def ensure_wordlist(path: Path, lang: str) -> None:
    """Download the public frequency list for lang when missing, no-op otherwise."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE_URL_TEMPLATE.format(lang=lang), path)


def load_gate(path: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Read the public frequency list into (form -> best rank, stripped -> form).

    The restore map picks the most frequent public form per diacritics-stripped
    key, recovering diacritic-less texting ('mam' -> 'mám') before lemmatizing.
    """
    public: dict[str, int] = {}
    restore: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for rank, line in enumerate(f, start=1):
            parts = line.split()
            if not parts:
                continue
            form = parts[0].lower()
            public.setdefault(form, rank)
            key = _strip_diacritics(form)
            if key not in restore or rank < public[restore[key]]:
                restore[key] = form
    return public, restore


def _find_owner(threads: list[dict]) -> str:
    """The participant listed in every thread who also sends messages.

    Participants include the owner in these exports; everyone else appears
    only in their own conversations.

    Raises:
        ValueError: when owner detection is ambiguous or empty.
    """
    n = len(threads)
    part = Counter(_fix(p.get("name", "")) for t in threads for p in t.get("participants", []))
    senders = {_fix(m.get("sender_name", "")) for t in threads for m in t.get("messages", [])}
    candidates = [p for p, c in part.items() if c == n and p in senders]
    if len(candidates) != 1:
        raise ValueError(
            f"owner detection failed: {len(candidates)} participants appear in all "
            f"{n} threads and send messages; expected exactly one. Pass an export "
            f"directory of a single account."
        )
    return candidates[0]


def _msg_hash(thread: dict, m: dict, content: str) -> str:
    raw = "|".join(
        str(x)
        for x in (thread.get("thread_path"), m.get("sender_name"), m.get("timestamp_ms"), content)
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    text = URL_RE.sub(" ", text.lower())
    return [t for t in TOKEN_RE.findall(text) if len(t) > 1]


def _gate(form: str, public: dict[str, int], restore: dict[str, str]) -> str | None:
    """Restore diacritics, then keep the form only if it is a public word."""
    if form.isascii():
        fixed = restore.get(form, form)
        if public.get(fixed, 10**9) < public.get(form, 10**9):
            form = fixed
    return form if form in public else None


def import_chats(conn, roots: list[Path], lang: str, wordlist_path: Path) -> dict[str, int]:
    """Parse Meta DIY exports and accumulate the owner's word counts in the store.

    Args:
        conn: Store connection from store.connect().
        roots: Extracted export directories (any nesting; message_*.json is
            searched recursively). Each root's owner is detected independently,
            so Instagram and Facebook exports combine.
        lang: Gate language; the export's text is filtered to this language's
            public words (other languages' words silently drop).
        wordlist_path: Public frequency list for lang, downloaded when missing.

    Returns:
        Counts: files, new_messages, skipped_seen, system_strings, tokens,
        forms_kept, lemmas_new, lemmas_updated.
    """
    ensure_wordlist(wordlist_path, lang)
    public, restore = load_gate(wordlist_path)
    seen = store.chat_seen_hashes(conn, lang)
    lemma_counts: Counter[str] = Counter()
    new_hashes: list[str] = []
    stats = {
        k: 0
        for k in (
            "files",
            "new_messages",
            "skipped_seen",
            "system_strings",
            "tokens",
            "forms_kept",
            "lemmas_new",
            "lemmas_updated",
        )
    }

    for root in roots:
        files = sorted(root.glob("**/message_*.json"))
        if not files:
            raise ValueError(f"no message_*.json under {root} (is this an extracted Meta export?)")
        threads = [json.loads(p.read_text(encoding="utf-8")) for p in files]
        owner = _find_owner(threads)
        stats["files"] += len(files)
        for thread in threads:
            for m in thread.get("messages", []):
                if _fix(m.get("sender_name", "")) != owner:
                    continue
                content = m.get("content")
                if not content or m.get("is_unsent"):
                    continue
                content = _fix(content)
                if content in SYSTEM_STRINGS:
                    stats["system_strings"] += 1
                    continue
                h = _msg_hash(thread, m, content)
                if h in seen:
                    stats["skipped_seen"] += 1
                    continue
                seen.add(h)
                new_hashes.append(h)
                stats["new_messages"] += 1
                for tok in _tokens(content):
                    stats["tokens"] += 1
                    form = _gate(tok, public, restore)
                    if form:
                        stats["forms_kept"] += 1
                        lemma_counts[simplemma.lemmatize(form, lang=lang)] += 1

    new, updated = store.upsert_personal_words(conn, lang, dict(lemma_counts))
    store.mark_chat_seen(conn, lang, new_hashes)
    conn.commit()
    stats["lemmas_new"], stats["lemmas_updated"] = new, updated
    return dict(stats)
