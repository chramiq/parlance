"""Throwaway probe: Instagram DYI export -> structure capture + personal wordlist gate.

Probe-first rule (AGENTS.md): the future parser gets written against what this
captures, never from docs. Output is structure and aggregate stats only; no
message text is printed or written. The only text that leaves this probe is
the gated candidate list: single common public Czech words.
"""

import collections
import json
import re
import sys
import urllib.request
from pathlib import Path

import simplemma

IN = Path("var/in/your_instagram_activity/messages")
OUT = Path("var/probe")
CS_50K_URL = (
    "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/cs/cs_50k.txt"
)
WORDLIST = Path("var/wordlist/cs_50k.txt")
TOKEN_RE = re.compile(r"[a-záčďéěíňóřšťúůýžó-]+")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def mojibake_fix(s: str) -> str:
    """Round-trip UTF-8 bytes that were JSON-escaped as Latin-1; keep original on failure."""
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def mask(s: str) -> str:
    return (s[:2] + "***") if s else "<empty>"


def load_threads():
    files = sorted(IN.glob("*/*/message_*.json"))
    if not files:
        sys.exit(f"no message_*.json under {IN}")
    return files, [json.load(open(f, encoding="utf-8")) for f in files]


def detect_owner(files, threads):
    sender_files = collections.Counter()
    participant_files = collections.Counter()
    for t in threads:
        senders = {mojibake_fix(m.get("sender_name", "")) for m in t.get("messages", [])}
        participants = {mojibake_fix(p.get("name", "")) for p in t.get("participants", [])}
        for s in senders:
            sender_files[s] += 1
        for p in participants:
            participant_files[p] += 1
    # owner: the participant listed in EVERY thread (everyone else appears
    # only in their own conversations), who also sends messages
    candidates = [
        (s, n) for s, n in sender_files.most_common() if participant_files.get(s) == len(files)
    ]
    return candidates, sender_files, participant_files


def ensure_wordlist():
    if not WORDLIST.exists():
        WORDLIST.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(CS_50K_URL, WORDLIST)


def main():
    print("=" * 70)
    print("WARNING: This export is your full chat history. Do NOT open these")
    print("files and start reading -- you will find things you did not want")
    print("to remember. Let the tool do its job: it extracts nothing but")
    print("single common public words. Curiosity stays outside.")
    print("=" * 70)

    OUT.mkdir(parents=True, exist_ok=True)
    files, threads = load_threads()

    # ---- structure capture (keys/counts only) ----
    thread_keys = set()
    msg_keys = collections.Counter()
    participants_per_thread = collections.Counter()
    for t in threads:
        thread_keys.update(t.keys())
        participants_per_thread[len(t.get("participants", []))] += 1
        for m in t.get("messages", []):
            msg_keys.update(m.keys())
    n_msgs = sum(len(t.get("messages", [])) for t in threads)

    # ---- owner detection ----
    candidates, sender_files, _participant_files = detect_owner(files, threads)
    if len(candidates) != 1:
        print(f"\nowner detection ambiguous: {[(mask(s), n) for s, n in candidates]}")
        sys.exit("refusing to continue with ambiguous owner")
    owner, owner_files = candidates[0]
    print(f"\nowner detected: {mask(owner)} (sends in {owner_files}/{len(files)} files)")
    print("owner is the participant listed in every thread; participants includes the owner")

    # ---- mojibake stats on owner content (aggregate, no text) ----
    fixed = unchanged = failed = 0
    owner_msgs = 0
    for t in threads:
        for m in t.get("messages", []):
            if mojibake_fix(m.get("sender_name", "")) != owner:
                continue
            c = m.get("content")
            if not c or m.get("is_unsent"):
                continue
            owner_msgs += 1
            try:
                c.encode("latin-1")
            except UnicodeEncodeError:
                failed += 1  # chars > U+00FF: string is already proper UTF-8
                continue
            if mojibake_fix(c) != c:
                fixed += 1
            else:
                unchanged += 1
    print(
        f"\nmojibake (owner content strings): fixed={fixed} unchanged={unchanged} "
        f"already-utf8={failed} of {owner_msgs}"
    )

    # ---- tokenize owner text, count surface forms ----
    form_counts = collections.Counter()
    for t in threads:
        for m in t.get("messages", []):
            if mojibake_fix(m.get("sender_name", "")) != owner:
                continue
            c = m.get("content")
            if not c or m.get("is_unsent"):
                continue
            c = mojibake_fix(c)
            c = URL_RE.sub(" ", c.lower())
            for tok in TOKEN_RE.findall(c):
                if len(tok) > 1 or tok in ("a", "i", "o", "u", "e"):
                    form_counts[tok] += 1
    total_tokens = sum(form_counts.values())
    print(
        f"\nowner text messages: {owner_msgs}, tokens: {total_tokens}, "
        f"distinct surface forms: {len(form_counts)}"
    )

    # ---- gate against public cs_50k ----
    ensure_wordlist()
    public = {}  # form -> best rank
    with open(WORDLIST, encoding="utf-8") as f:
        for rank, line in enumerate(f, start=1):
            parts = line.split()
            if parts:
                public.setdefault(parts[0].lower(), rank)
    survived = {f: c for f, c in form_counts.items() if f in public}
    print(
        f"gate (surface form in public cs_50k): {len(survived)}/{len(form_counts)} "
        f"distinct forms survive ({100 * len(survived) // max(len(form_counts), 1)}%)"
    )

    # ---- lemmatize survivors -> candidate personal wordlist ----
    lemma_counts = collections.Counter()
    for form, c in survived.items():
        lemma = simplemma.lemmatize(form, lang="cs")
        lemma_counts[lemma] += c
    print(f"distinct lemmas after lemmatize: {len(lemma_counts)}")

    rows = []
    for lemma, c in lemma_counts.most_common():
        best_rank = min(
            (public[f] for f in survived if simplemma.lemmatize(f, lang="cs") == lemma),
            default=10**9,
        )
        rows.append((lemma, c, best_rank))

    for cut in (0, 50, 100, 200, 500):
        kept = sum(c for _, c, r in rows if r > cut)
        print(
            f"  stopword-cut at public rank >{cut}: {kept} token occurrences kept "
            f"({100 * kept // max(total_tokens, 1)}% of owner tokens)"
        )

    # ---- write outputs: gated public words only ----
    with open(OUT / "personal_cs_candidates.txt", "w", encoding="utf-8") as f:
        for lemma, c, r in rows:
            f.write(f"{lemma}\t{c}\t{r}\n")
    with open(OUT / "structure.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "n_files": len(files),
                "n_messages": n_msgs,
                "thread_top_level_keys": sorted(thread_keys),
                "message_key_inventory": dict(msg_keys.most_common()),
                "participants_per_thread_size": dict(sorted(participants_per_thread.items())),
                "n_distinct_senders": len(sender_files),
                "owner_msgs_with_content": owner_msgs,
                "total_owner_tokens": total_tokens,
                "distinct_owner_forms": len(form_counts),
                "distinct_public_forms": len(survived),
                "distinct_lemmas": len(lemma_counts),
            },
            f,
            indent=1,
            ensure_ascii=False,
        )
    print(f"\nwrote {OUT / 'personal_cs_candidates.txt'} (lemma, chat_count, public_rank)")
    print(f"wrote {OUT / 'structure.json'} (structure/stats only, no message text)")


if __name__ == "__main__":
    main()
