from parlance import store
from parlance.cognates import ff_guard, known_lemmas, mark_free_cognates, strip_diacritics


def test_strip_diacritics():
    assert strip_diacritics("Systém") == "system"
    assert strip_diacritics("pieniądze") == "pieniadze"


def test_known_lemmas_and_guard_real_data():
    vocab = known_lemmas("cs")
    assert len(vocab) > 30_000
    assert "system" in vocab and "hotel" in vocab and "kochac" not in vocab
    guards = ff_guard("pl", "cs")
    assert {"szukać", "list", "zawód"} <= guards
    assert ff_guard("pl", "xx") == frozenset()


def test_mark_free_cognates_respects_guard(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    store.add_lemmas(
        conn,
        [
            {"lemma": "system", "freq_rank": 1, "freq": 10, "forms": []},
            {"lemma": "list", "freq_rank": 2, "freq": 10, "forms": []},
            {"lemma": "kochać", "freq_rank": 3, "freq": 10, "forms": []},
        ],
    )
    import parlance.cognates as cg

    monkeypatch.setattr(cg, "known_lemmas", lambda lang: frozenset({"system", "list"}))
    skipped = mark_free_cognates(conn, target_lang="pl", known_lang="cs")
    assert skipped == ["system"]
    got = {
        r["lemma"]: (r["status"], r["cognate_class"])
        for r in conn.execute("SELECT lemma, status, cognate_class FROM lemmas")
    }
    assert got["system"] == ("skipped", "free")
    assert got["list"][0] == "worklist"  # false friend: guarded, never auto-skipped
    assert got["kochać"][0] == "worklist"
