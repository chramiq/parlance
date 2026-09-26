import json

from parlance import store
from parlance.practice import PracticeTools, dispatch, function_declarations


def test_session_lifecycle_and_digest(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    sid = store.open_practice_session(conn, "gemini-3.8-live")
    store.add_practice_error(
        conn, sid, "pády po záporu", "nie mam problemu", "nie mam problemu. (gen.)", "problem"
    )
    store.add_practice_error(conn, sid, "pády po záporu", "nie wiem tego", "nie wiem tego. (gen.)")
    store.close_practice_session(conn, sid, "obchod: maso u prodavačky, opakování frází")
    dig = store.practice_digest(conn)
    assert "obchod" in dig
    assert "pády po záporu (2x)" in dig
    sid2 = store.open_practice_session(conn, "gemini-3.8-live")
    assert sid2 == sid + 1


def test_tools_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    store.add_lemmas(conn, [{"lemma": "mięso", "freq_rank": 1, "freq": 10, "forms": ["mięsa"]}])
    store.set_status(conn, "mięso", "generated")
    lid = conn.execute("SELECT id FROM lemmas WHERE lemma='mięso'").fetchone()["id"]
    store.upsert_content(
        conn, lid, "lemma", "maso", json.dumps({"examples": [{"target": "Poproszę mięso."}]}), "m"
    )
    sid = store.open_practice_session(conn, "gemini-3.8-live")
    tools = PracticeTools(conn_path=db, session_id=sid)

    targets = dispatch(tools, "get_practice_targets", {"n": 5})
    assert targets == [{"lemma": "mięso", "gloss": "maso", "example": "Poproszę mięso."}]
    assert (
        dispatch(
            tools,
            "log_error",
            {"pattern": "aspekt", "wrong": "kupuję", "right": "kupię", "lemma": "kupić"},
        )
        == "logged"
    )
    assert dispatch(tools, "save_session_summary", {"summary": "drill hotov"}) == "saved"
    row = conn.execute("SELECT pattern, lemma FROM practice_errors").fetchone()
    assert (row["pattern"], row["lemma"]) == ("aspekt", "kupić")
    assert (
        conn.execute("SELECT summary FROM practice_sessions WHERE id=?", (sid,)).fetchone()[
            "summary"
        ]
        == "drill hotov"
    )


def test_declarations_match_dispatch():
    names = {d["name"] for d in function_declarations()}
    assert names == {"get_practice_targets", "log_error", "save_session_summary"}
