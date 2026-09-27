"""Projection: store -> .apkg. Reads only; the store stays the truth.

GUIDs are derived from identity (never content), deck/model IDs are fixed
constants, and new-card position follows frequency rank -- reimport updates
in place.
"""

import json
from contextlib import closing
from pathlib import Path

import genanki

from ..agent import LANG_NAMES
from ..config import Settings
from ..store import connect
from ..tts import render_lines
from .model import cognate_model, lemma_model

# (low, high, suffix) with stable deck ids
BANDS = [
    (1, 500, "001-Top500", 1715500001),
    (501, 1500, "010-501-1500", 1715500002),
    (1501, None, "020-1501plus", 1715500003),
]
PERSONAL_DECK_ID = 1715500004


def _band(rank: int) -> tuple[str, int]:
    for low, high, suffix, deck_id in BANDS:
        if rank >= low and (high is None or rank <= high):
            return suffix, deck_id
    return BANDS[-1][2], BANDS[-1][3]


def _variants(payload: dict, preferred_idx: int) -> str:
    others = [v["gloss"] for i, v in enumerate(payload["variants"]) if i != preferred_idx]
    return " / ".join(others)


def _meta(payload: dict) -> str:
    meta = payload["pos"]
    if payload.get("gender"):
        meta += f" ({payload['gender']})"
    return meta


def _write_preview(model, fields: list[str], css: str, path: Path) -> None:
    """Render every template front+back for one real note into a standalone HTML file.

    An approximation of the card faces for eyeball review without importing
    into Anki (browser render, not a webview; sound tags stripped).
    """
    import chevron

    from .model import FIELDS

    data = dict(zip(FIELDS, fields))
    data["type:Lemma"] = f'<div id="typeans">{data["Lemma"]}</div>'
    import re

    for k in ("WordAudioFile", "ExAudioFile"):
        data[k] = re.sub(r"\[sound:[^\]]+\]", "", data[k])
    parts = [
        '<!doctype html><meta charset="utf-8"><title>parlance card preview</title>',
        f"<style>{css}</style>",
        "<body class='card' style='background:#0e0e10'>",
    ]
    for label, tmpl in [("FRONT", "qfmt"), ("BACK", "afmt")]:
        for t in model.templates:
            parts.append(
                f"<hr style='border-color:#2a2c31'><div class='tag'>{t['name']} / {label}</div>"
            )
            parts.append(chevron.render(t[tmpl], data))
    parts.append("</body>")
    path.write_text("\n".join(parts), encoding="utf-8")


async def build(s: Settings, limit: int | None = None) -> tuple[Path, dict]:
    """Render audio and write the vocab .apkg.

    Returns:
        (output path, counts dict).
    """
    with closing(connect(s.db_path)) as conn:
        query = """
            SELECT l.lemma, l.freq_rank, l.forms, l.cognate_class, c.kind, c.preferred_gloss,
                   c.payload
            FROM content c JOIN lemmas l ON l.id = c.lemma_id
            WHERE c.kind IN ('lemma', 'personal') AND l.status = 'generated'
            ORDER BY l.freq_rank
        """
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = list(conn.execute(query))
    if not rows:
        raise RuntimeError("no generated cards in the store; run generate first")

    examples = {r["lemma"]: json.loads(r["payload"])["examples"][0] for r in rows}
    texts = [r["lemma"] for r in rows] + [examples[r["lemma"]]["target"] for r in rows]
    audio_dir = s.data_dir / "audio"
    audio = await render_lines(texts, s.tts_voice, audio_dir, s.tts_rate)

    model = lemma_model()
    cognate = cognate_model()
    decks: dict[int, genanki.Deck] = {}
    root = LANG_NAMES.get(s.target_lang, s.target_lang)
    package_decks = []
    first_fields = None
    for r in rows:
        payload = json.loads(r["payload"])
        if r["kind"] == "personal":
            suffix, deck_id = "personal", PERSONAL_DECK_ID
        else:
            suffix, deck_id = _band(r["freq_rank"])
        if deck_id not in decks:
            decks[deck_id] = genanki.Deck(deck_id, f"{root}::{suffix}")
            package_decks.append(decks[deck_id])
        ex = examples[r["lemma"]]
        forms = ", ".join(json.loads(r["forms"])[:4])
        field_values = [
            r["lemma"],
            r["preferred_gloss"],
            _variants(payload, payload["preferred"]),
            _meta(payload),
            forms,
            ex["target"],
            ex["known"],
            payload.get("false_friend_note") or "",
            f"[sound:{audio[r['lemma']]}]",
            f"[sound:{audio[ex['target']]}]",
        ]
        if first_fields is None:
            first_fields = field_values
        note = genanki.Note(
            model=cognate if r["cognate_class"] == "free" else model,
            fields=field_values,
            # personal notes get their own guid prefix: kinds can never share
            # a lemma (status dedup), but a collision would silently corrupt
            # reimports -- a visible duplicate is the recoverable failure
            guid=f"{r['kind']}:{r['lemma']}",
            due=r["freq_rank"],
            tags=["personal"] if r["kind"] == "personal" else [],
        )
        decks[deck_id].add_note(note)

    out_dir = s.data_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{s.target_lang}-vocab.apkg"
    package = genanki.Package(deck_or_decks=package_decks)
    package.media_files = [str(audio_dir / audio[t]) for t in texts]
    package.write_to_file(str(out_path))
    if first_fields is not None:
        _write_preview(model, first_fields, model.css, out_dir / "preview.html")
    counts = {
        "notes": len(rows),
        "personal": sum(1 for r in rows if r["kind"] == "personal"),
        "cards": sum(1 if r["cognate_class"] == "free" else 2 for r in rows),
        "media": len(package.media_files),
        "out": str(out_path),
    }
    return out_path, counts
