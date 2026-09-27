import json

from parlance import store
from parlance.practice import PracticeTools, build_practice_agent


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

    targets = tools.get_practice_targets(n=5)
    assert targets == [{"lemma": "mięso", "gloss": "maso", "example": "Poproszę mięso."}]
    assert tools.log_error("aspekt", "kupuję", "kupię", lemma="kupić") == "logged"
    assert tools.save_session_summary("drill hotov") == "saved"
    row = conn.execute("SELECT pattern, lemma FROM practice_errors").fetchone()
    assert (row["pattern"], row["lemma"]) == ("aspekt", "kupić")
    assert (
        conn.execute("SELECT summary FROM practice_sessions WHERE id=?", (sid,)).fetchone()[
            "summary"
        ]
        == "drill hotov"
    )


def test_agent_tools_wired_to_store(tmp_path):
    from pydantic_ai.models.test import TestModel

    from parlance.practice import PracticeDeps

    db = tmp_path / "t.db"
    conn = store.connect(db)
    sid = store.open_practice_session(conn, "gemini-3.8-live")
    agent = build_practice_agent("You are a drill coach. Use your tools.")
    with agent.override(model=TestModel()):
        # TestModel calls every registered tool once; the store must reflect
        # each side effect for the wiring to be real
        result = agent.run_sync("drill me", deps=PracticeDeps(db_path=db, session_id=sid))
        assert result.output
    assert conn.execute("SELECT COUNT(*) c FROM practice_errors").fetchone()["c"] == 1
    assert (
        conn.execute("SELECT summary != '' s FROM practice_sessions WHERE id=?", (sid,)).fetchone()[
            "s"
        ]
        == 1
    )


def test_transcript_turns_aggregates_chunks(tmp_path):
    from parlance.practice import transcript_turns

    msgs = [
        {"server_content": {"input_transcription": {"text": "Dzień do"}}},
        {"server_content": {"input_transcription": {"text": "bry."}}},
        {"server_content": {"output_transcription": {"text": "Ahoj, "}}},
        {"server_content": {"output_transcription": {"text": "co trénujeme?"}}},
        {"server_content": {"turn_complete": True}},
        {"server_content": {"input_transcription": {"text": "Kawę."}}},
        {"server_content": {"model_turn": {"parts": [{"inline_data": {"data": "eA=="}}]}}},
    ]
    path = tmp_path / "1.jsonl"
    path.write_text("\n".join(__import__("json").dumps(m) for m in msgs), encoding="utf-8")
    assert transcript_turns(path) == [
        ("you", "Dzień dobry."),
        ("coach", "Ahoj, co trénujeme?"),
        ("you", "Kawę."),
    ]


def test_session_lookup_helpers(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert store.get_practice_session(conn) is None
    sid = store.open_practice_session(conn, "m")
    store.add_practice_error(conn, sid, "p", "w", "r")
    sess = store.get_practice_session(conn)
    assert sess["id"] == sid
    assert store.session_errors(conn, sid)[0]["pattern"] == "p"
