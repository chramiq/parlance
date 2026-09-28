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
        "cards": 3,  # system is a free cognate -> Recognition card suppressed
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
