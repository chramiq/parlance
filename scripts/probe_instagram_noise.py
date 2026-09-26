"""Follow-up probe: the three noise classes in the owner wordlist.

1. localized system strings ("posíláte přílohu") polluting token counts
2. diacritic-less texting ("mam", "mas", "ja") mis-lemmatizing
   ("mámit", "masa") or splitting lemmas ("ja"/"já")
3. English leakage through the cs_50k gate ("you", "me", "call")

Output is aggregate counts plus: exact content strings repeated 100+ times
(necessarily generic: system templates and one-word replies, never personal
sentences) and restored public word forms. Nothing else is printed.
"""

import collections
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

import simplemma

IN = Path("var/in/your_instagram_activity/messages")
WORDLIST = Path("var/wordlist")
TOKEN_RE = re.compile(r"[a-záčďéěíňóřšťúůýžó-]+")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EN_50K_URL = (
    "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/en/en_50k.txt"
)


def fix(s):
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def strip_diacritics(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def load_public(path):
    """Return {form: rank} plus the diacritic restoration map {stripped: (form, rank)}."""
    public = {}
    if not path.exists():
        urllib.request.urlretrieve(
            f"https://raw.githubusercontent.com/hermitdave/FrequencyWords/"
            f"master/content/2018/{path.stem.split('_')[0]}/{path.name}",
            path,
        )
    with open(path, encoding="utf-8") as f:
        for rank, line in enumerate(f, start=1):
            parts = line.split()
            if not parts:
                continue
            form = parts[0].lower()
            public.setdefault(form, rank)
    restore = {}
    for form, rank in public.items():
        key = strip_diacritics(form)
        if key not in restore or rank < restore[key][1]:
            restore[key] = (form, rank)
    return public, restore


def main():
    print("=" * 70)
    print("Structure and aggregate counts only. Exact strings shown are")
    print("repeated 100+ times -- system templates, never personal text.")
    print("=" * 70)

    files = sorted(IN.glob("*/*/message_*.json"))
    threads = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    part = collections.Counter()
    for t in threads:
        for p in t.get("participants", []):
            part[fix(p.get("name", ""))] += 1
    owner = next(p for p, n in part.items() if n == len(files))

    # ---- 1. system templates: exact strings repeated 100+ times ----
    exact = collections.Counter()
    form_counts = collections.Counter()
    for t in threads:
        for m in t.get("messages", []):
            if fix(m.get("sender_name", "")) != owner:
                continue
            c = m.get("content")
            if not c or m.get("is_unsent"):
                continue
            c = fix(c)
            exact[c] += 1
            low = URL_RE.sub(" ", c.lower())
            for tok in TOKEN_RE.findall(low):
                if len(tok) > 1:
                    form_counts[tok] += 1
    templates = {s: n for s, n in exact.items() if n >= 100}
    total_owner = sum(exact.values())
    sys_share = sum(templates.values())
    print(f"\nowner content messages: {total_owner}")
    print(f"messages matching a 100+ repetition template: {sys_share}")
    print("templates:")
    for s, n in sorted(templates.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {n:>5}x  {s!r}")

    # ---- 2. diacritic restoration ----
    public, restore = load_public(WORDLIST / "cs_50k.txt")
    restored_forms = 0
    improved_tokens = 0
    examples = {}
    for form, cnt in form_counts.items():
        if not form.isascii():
            continue
        key = strip_diacritics(form)
        best, best_rank = restore.get(key, (form, public.get(form, 10**9)))
        own_rank = public.get(form, 10**9)
        if best != form and best_rank < own_rank:
            restored_forms += 1
            improved_tokens += cnt
            if len(examples) < 8:
                examples[form] = best
    print(f"\ndiacritic restoration: {restored_forms} distinct ascii forms restore to a")
    print(f"higher-frequency public form ({improved_tokens} token occurrences)")
    print("examples:", examples)

    # effect on the suspect lemmas: re-lemmatize with restoration applied
    lemma_counts = collections.Counter()
    for form, cnt in form_counts.items():
        key = strip_diacritics(form)
        best, best_rank = restore.get(key, (form, public.get(form, 10**9)))
        token = best if (form.isascii() and best_rank < public.get(form, 10**9)) else form
        lemma_counts[simplemma.lemmatize(token, lang="cs")] += cnt
    for suspect in ("mámit", "masa", "mít", "ja", "já", "uz", "už"):
        print(f"  lemma {suspect!r}: {lemma_counts.get(suspect, 0)} occurrences")
    survived = sum(c for l, c in lemma_counts.items() if any(f in public for f in (l,)))
    print(f"lemma-level gate survival (naive lemma-in-public check): {survived}")

    # ---- 3. english leakage through the cs gate ----
    en_public, _ = load_public(WORDLIST / "en_50k.txt")
    en_top = {w for w, r in en_public.items() if r <= 2000}
    leak = [
        (l, c)
        for l, c in lemma_counts.most_common()
        if l in en_top and simplemma.lemmatize(l, lang="cs") == l
    ]
    print(f"\nlemmas also in english top-2000: {len(leak)}, occurrences: {sum(c for _, c in leak)}")
    print("top:", leak[:12])


if __name__ == "__main__":
    main()
