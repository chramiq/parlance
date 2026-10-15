import json
import sqlite3
import zipfile

from parlance import store
from parlance.config import Settings
from parlance.deck import build as deckbuild
from parlance.deck.model import FIELDS
from parlance.tts import audio_name

VOICE = "pl-PL-ZofiaNeural"


def seeded_db(tmp_path):
    db = tmp_path / "parlance.db"
    conn = store.connect(db)
    store.add_lemmas(
        conn,
        [
            {
                "lemma": "kochać",
                "freq_rank": 1,
                "freq": 150,
                "forms": ["kochać", "kocha", "kocham"],
            },
            {"lemma": "system", "freq_rank": 480, "freq": 99, "forms": ["system"]},
        ],
    )
    store.set_status(conn, "kochać", "generated")
    store.set_status(conn, "system", "generated")

    def pk(lemma: str) -> int:
        return conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"]

    store.upsert_content(
        conn,
        pk("kochać"),
        "lemma",
        "milovat",
        json.dumps(
            {
                "lemma": "kochać",
                "pos": "sloveso",
                "gender": "",
                "variants": [
                    {"gloss": "milovat", "style": "neutral"},
                    {"gloss": "mít rád", "style": "neutral"},
                ],
                "preferred": 0,
                "examples": [
                    {
                        "target": "Kocham cię, kochanie.",
                        "known": "Miluju tě.",
                        "cloze": "[...] cię, kochanie.",
                        "meaning": "",
                    }
                ],
                "false_friend_note": "",
            }
        ),
        "testmodel",
    )
    store.upsert_content(
        conn,
        pk("system"),
        "lemma",
        "systém",
        json.dumps(
            {
                "lemma": "system",
                "pos": "podstatné jméno",
                "gender": "mužský",
                "variants": [{"gloss": "systém", "style": "neutral"}],
                "preferred": 0,
                "examples": [
                    {
                        "target": "System działa.",
                        "known": "Systém funguje.",
                        "cloze": "[...] działa.",
                        "meaning": "",
                    }
                ],
                "false_friend_note": "",
            }
        ),
        "testmodel",
    )
    conn.execute("UPDATE lemmas SET cognate_class='free' WHERE lemma='system'")
    conn.commit()
    return db, conn


def test_field_list_frozen():
    # accidental reorder/rename corrupts reimports; change this hash means a
    # deliberate new MODEL_ID is required
    import hashlib

    digest = hashlib.sha1("|".join(FIELDS).encode()).hexdigest()
    assert digest == "dfab2cb9e2c5af7b8b1330c613aa803eafa453a7"
    assert FIELDS[0] == "Lemma" and FIELDS[-1] == "ExAudioFile"


async def _build(db, tmp_path, monkeypatch):
    monkeypatch.setattr(deckbuild, "render_lines", _fake_render)
    s = Settings(data_dir=tmp_path, tts_voice=VOICE)
    return await deckbuild.build(s)


async def _fake_render(texts, voice, out_dir, rate):
    out_dir.mkdir(parents=True, exist_ok=True)
    assert voice == VOICE
    for t in texts:
        (out_dir / audio_name(t, voice, rate)).write_bytes(b"FAKE_MP3")
    return {t: audio_name(t, voice, rate) for t in texts}


async def test_build_apkg_structure(tmp_path, monkeypatch):
    db, _ = seeded_db(tmp_path)
    out_path, counts = await _build(db, tmp_path, monkeypatch)
    assert counts == {
        "notes": 2,
        "personal": 0,
        "extend": 0,
        "cards": 3,  # system is a free cognate -> Recognition card suppressed
        "listen": 0,  # no recorded reviews in this fixture
        "media": 4,  # 2 texts x 2 notes, one learner-slow rate each
        "out": str(out_path),
    }
    z = zipfile.ZipFile(out_path)
    media_map = json.loads(z.read("media"))
    basenames = set(media_map.values())
    assert audio_name("kochać", VOICE, "-25%") in basenames
    assert audio_name("Kocham cię, kochanie.", VOICE, "-25%") in basenames
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        z.extract("collection.anki2", td)
        con = sqlite3.connect(f"{td}/collection.anki2")
        notes = {r[0]: r[1] for r in con.execute("SELECT guid, flds FROM notes")}
        assert "lemma:kochać" in notes and "lemma:system" in notes
        k_fields = notes["lemma:kochać"].split("\x1f")
        assert k_fields[0] == "kochać" and k_fields[1] == "milovat"
        assert k_fields[2] == "mít rád"
        assert k_fields[5] == "Kocham cię, kochanie."
        assert k_fields[8].startswith("[sound:") and k_fields[9].endswith("]")
        s_fields = notes["lemma:system"].split("\x1f")
        assert len(s_fields) == len(FIELDS)
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        names = {d["name"] for d in decks.values()}
        assert "PL::COMMON" in names


def test_audio_name_deterministic_and_voice_and_rate_scoped():
    assert audio_name("Kocham cię.", "v", "+0%") == audio_name("Kocham cię.", "v", "+0%")
    assert audio_name("Kocham cię.", "v", "+0%") != audio_name("Kocham cię.", "v", "-20%")
    assert audio_name("Kocham cię.", "a", "+0%") != audio_name("Kocham cię.", "b", "+0%")


def _card_payload(lemma: str, gloss: str, target: str, known: str) -> str:
    return json.dumps(
        {
            "lemma": lemma,
            "pos": "spojka",
            "gender": "",
            "variants": [{"gloss": gloss, "style": "neutral"}],
            "preferred": 0,
            "examples": [{"target": target, "known": known, "cloze": "", "meaning": ""}],
            "false_friend_note": "",
        }
    )


async def test_gloss_collisions_route_to_extend_deck(tmp_path, monkeypatch):
    """The most frequent word per gloss keeps the bare gloss in COMMON;
    siblings route to ::EXTEND with the other group members struck through."""
    db = tmp_path / "parlance.db"
    conn = store.connect(db)
    store.add_lemmas(
        conn,
        [
            {"lemma": "by", "freq_rank": 30, "freq": 900, "forms": ["by"]},
            {"lemma": "żeby", "freq_rank": 60, "freq": 400, "forms": ["żeby", "żebym"]},
            {"lemma": "aby", "freq_rank": 90, "freq": 200, "forms": ["aby"]},
        ],
    )
    examples = {
        "by": ("Bałby się.", "Bál by se."),
        "żeby": ("Żebyś był zdrów.", "Ať jsi zdravý."),
        "aby": ("Aby działało.", "Aby to fungovalo."),
    }
    for lemma, (target, known) in examples.items():
        store.set_status(conn, lemma, "generated")
        pk = conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"]
        store.upsert_content(
            conn, pk, "lemma", "aby", _card_payload(lemma, "aby", target, known), "testmodel"
        )
    # a free-cognate sibling still routes to extend: the exclusions are the point
    conn.execute("UPDATE lemmas SET cognate_class='free' WHERE lemma='aby'")
    conn.commit()
    stored, unknown = store.record_anki_reviews(conn, ["żeby"], "2026-10-15")
    assert (stored, unknown) == (1, [])

    monkeypatch.setattr(deckbuild, "render_lines", _fake_render)
    s = Settings(data_dir=tmp_path, tts_voice=VOICE)
    out_path, counts = await deckbuild.build(s)
    assert counts["extend"] == 2
    assert counts["listen"] == 1
    assert counts["cards"] == 5  # by: Discover+Type, żeby/aby: extend Type, listen:żeby

    import tempfile
    import zipfile

    z = zipfile.ZipFile(out_path)
    with tempfile.TemporaryDirectory() as td:
        z.extract("collection.anki2", td)
        con = sqlite3.connect(f"{td}/collection.anki2")
        flds = {r[0]: r[1].split("\x1f") for r in con.execute("SELECT guid, flds FROM notes")}
        assert flds["lemma:by"][1] == "aby"
        assert len(flds["lemma:by"]) == 10
        assert flds["lemma:żeby"][10] == "by · aby"
        assert flds["lemma:aby"][10] == "by · żeby"
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        names = {d["name"] for d in decks.values()}
        assert "PL::COMMON" in names and "PL::EXTEND" in names
        assert "PL::PERSONAL" not in names
        name_by_id = {d["id"]: d["name"] for d in decks.values()}
        guid_by_nid = {r[0]: r[1] for r in con.execute("SELECT id, guid FROM notes")}
        card_decks = {
            guid_by_nid[r[0]]: name_by_id[r[1]] for r in con.execute("SELECT nid, did FROM cards")
        }
        assert card_decks["lemma:by"] == "PL::COMMON"
        assert card_decks["lemma:żeby"] == "PL::EXTEND"
        assert card_decks["lemma:aby"] == "PL::EXTEND"
        assert card_decks["listen:żeby"] == "PL::EXTEND"
    preview = (tmp_path / "out" / "preview-extend.html").read_text(encoding="utf-8")
    assert "by · aby" in preview and "text-decoration: line-through" in preview


def test_listen_cards_only_for_reviewed_words(tmp_path):
    """A word with a recorded Anki review gets a listen note; unreviewed words do not."""
    import asyncio

    db, _ = seeded_db(tmp_path)
    with store.connect(db) as conn:
        stored, unknown = store.record_anki_reviews(conn, ["kochać", "neexistuje"], "2026-09-28")
    assert stored == 1 and unknown == ["neexistuje"]
    s = Settings(data_dir=tmp_path, tts_voice=VOICE)
    out_path, counts = asyncio.run(deckbuild.build(s))
    z = zipfile.ZipFile(out_path)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        z.extract("collection.anki2", td)
        con = sqlite3.connect(f"{td}/collection.anki2")
        guids = {r[0] for r in con.execute("SELECT guid FROM notes")}
        assert "listen:kochać" in guids
        assert "listen:system" not in guids
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        names = {d["name"] for d in decks.values()}
        assert "PL::COMMON" in names
    assert counts["listen"] == 1
