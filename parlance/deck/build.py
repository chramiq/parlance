"""Projection: store -> .apkg. Reads only; the store stays the truth.

GUIDs are derived from identity (never content), deck/model IDs are fixed
constants, and new-card position follows frequency rank -- reimport updates
in place.
"""

import json
from contextlib import closing
from pathlib import Path

import genanki

from ..config import Settings
from ..store import connect
from ..tts import render_lines
from .model import cognate_model, lemma_model, listen_model

COMMON_DECK_ID = 1715500005
PERSONAL_DECK_ID = 1715500004


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
        # ear training follows learning: listen cards exist only for words
        # with a recorded Anki review
        reviewed = {r["lemma"] for r in conn.execute("SELECT DISTINCT lemma FROM anki_reviews")}
    if not rows:
        raise RuntimeError("no generated cards in the store; run generate first")

    examples = {r["lemma"]: json.loads(r["payload"])["examples"][0] for r in rows}
    texts = [r["lemma"] for r in rows] + [examples[r["lemma"]]["target"] for r in rows]
    audio_dir = s.data_dir / "audio"
    audio = await render_lines(texts, s.tts_voice, audio_dir, s.tts_rate)

    model = lemma_model()
    cognate = cognate_model()
    listen = listen_model()
    decks: dict[int, genanki.Deck] = {}
    root = s.target_lang.upper()
    package_decks = []
    first_fields = None
    for r in rows:
        payload = json.loads(r["payload"])
        if r["kind"] == "personal":
            suffix, deck_id = "PERSONAL", PERSONAL_DECK_ID
        else:
            suffix, deck_id = "COMMON", COMMON_DECK_ID
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
        if r["lemma"] in reviewed:
            decks[deck_id].add_note(
                genanki.Note(
                    model=listen,
                    fields=field_values,
                    guid=f"listen:{r['lemma']}",
                    due=r["freq_rank"],
                )
            )

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
        "cards": sum(1 if r["cognate_class"] == "free" else 2 for r in rows)
        + sum(1 for r in rows if r["lemma"] in reviewed),
        "listen": sum(1 for r in rows if r["lemma"] in reviewed),
        "media": len(package.media_files),
        "out": str(out_path),
    }
    return out_path, counts
