import json

import pytest

from parlance import store


def test_seed_and_dedupe(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    rows = [
        {"lemma": "być", "freq_rank": 1, "freq": 340, "forms": ["jest", "jestem", "są"]},
        {"lemma": "kochać", "freq_rank": 2, "freq": 150, "forms": ["kocha", "kocham"]},
    ]
    assert store.add_lemmas(conn, rows) == (2, 0)
    assert store.add_lemmas(conn, rows) == (0, 2)


def test_list_persists_rank_and_forms(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_lemmas(
        conn,
        [
            {"lemma": "kochać", "freq_rank": 2, "freq": 150, "forms": ["kocha"]},
            {"lemma": "być", "freq_rank": 1, "freq": 340, "forms": ["jest"]},
        ],
    )
    rows = store.list_lemmas(conn, limit=10)
    assert [r["lemma"] for r in rows] == ["być", "kochać"]
    assert json.loads(rows[0]["forms"]) == ["jest"]


def test_status_transition_and_journal(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_lemmas(conn, [{"lemma": "być", "freq_rank": 1, "freq": 340, "forms": []}])
    assert store.set_status(conn, "być", "generated")
    assert store.list_lemmas(conn, status="worklist") == []
    assert store.stats(conn) == {"generated": 1}
    assert not store.set_status(conn, "nieistnieje", "retired")
    actions = {r["action"] for r in conn.execute("SELECT action FROM changes")}
    assert "worklist_seed" in actions and "status:generated" in actions


def test_add_lemmas_rejects_garbage_rows(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.add_lemmas(conn, [{"lemma": None, "freq_rank": 1, "freq": 1, "forms": []}])
    assert store.stats(conn) == {}


def test_set_status_validates_and_skips_noops(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_lemmas(conn, [{"lemma": "być", "freq_rank": 1, "freq": 1, "forms": []}])
    with pytest.raises(ValueError):
        store.set_status(conn, "być", "bogus")
    before = conn.execute("SELECT COUNT(*) n FROM changes").fetchone()["n"]
    assert store.set_status(conn, "być", "worklist")  # already worklist: no-op
    assert conn.execute("SELECT COUNT(*) n FROM changes").fetchone()["n"] == before


def test_pair_stamp_refuses_foreign_pair(tmp_path, monkeypatch):
    """One store serves one pair: .env pointing at a different pair fails loudly."""
    import sqlite3

    from parlance.config import Settings

    monkeypatch.setattr(
        "parlance.config.get_settings", lambda: Settings(known_lang="cs", target_lang="pl")
    )
    db = tmp_path / "t.db"
    conn = store.connect(db)
    assert store.meta_get(conn, "language_pair") == {"pair": "cs>pl"}
    conn.close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE meta SET value=? WHERE key='language_pair'", ('{"pair": "de>en"}',))
    raw.commit()
    with pytest.raises(RuntimeError, match="belongs to de>en"):
        store.connect(db)
