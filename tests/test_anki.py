"""AnkiConnect client against a stub server; review ingestion into the store."""

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from parlance import anki, store
from parlance.practice import PracticeTools


def _note(lemma):
    return {
        "noteId": 1,
        "modelName": "parlance-lemma",
        "tags": [],
        "fields": {"Lemma": {"value": lemma, "order": 0}},
    }


def make_stub(responses):
    """Start a stub AnkiConnect on a free port; returns (server, recorded requests)."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            result, error = responses.get(body["action"], (None, "unsupported action"))
            resp = json.dumps({"result": result, "error": error}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def test_fetch_reviewed_today_syncs_then_reads_lemmas():
    notes = [_note("mięso"), _note("kawa"), {"noteId": 2, "modelName": "other", "fields": {}}]
    server, requests = make_stub({"sync": (None, None), "notesInfo": (notes, None)})
    try:
        lemmas = anki.fetch_reviewed_today(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
    assert lemmas == ["mięso", "kawa"]
    actions = [r["action"] for r in requests]
    assert actions == ["sync", "notesInfo"]
    query = requests[1]["params"]["query"]
    assert 'note:"parlance-lemma"' in query and 'note:"parlance-lemma-cognate"' in query
    assert "rated:1" in query


def test_fetch_reviewed_today_without_sync():
    server, requests = make_stub({"notesInfo": ([_note("kawa")], None)})
    try:
        lemmas = anki.fetch_reviewed_today(
            f"http://127.0.0.1:{server.server_address[1]}", sync_first=False
        )
    finally:
        server.shutdown()
        server.server_close()
    assert lemmas == ["kawa"]
    assert [r["action"] for r in requests] == ["notesInfo"]


def test_unreachable_endpoint_is_actionable():
    try:
        anki.fetch_reviewed_today("http://127.0.0.1:1", sync_first=False)
    except anki.AnkiConnectError as e:
        assert "unreachable" in str(e)
        assert "2055492159" in str(e)
    else:
        raise AssertionError("expected AnkiConnectError")


def test_mid_response_failure_is_actionable():
    """A listener that hangs up before sending a body must not leak a raw exception."""
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(
        target=lambda: srv.accept()[0].close(),
        daemon=True,  # hang up immediately
    ).start()
    try:
        anki.fetch_reviewed_today(f"http://127.0.0.1:{port}", sync_first=False)
    except anki.AnkiConnectError as e:
        assert "unreachable" in str(e)
    else:
        raise AssertionError("expected AnkiConnectError")
    finally:
        srv.close()


def test_open_degrades_when_anki_is_closed(tmp_path, capsys):
    """A practice session must open even when the Anki pull cannot reach AnkiConnect."""
    from parlance.config import Settings
    from parlance.practice import _open

    s = Settings(data_dir=tmp_path, anki_connect_url="http://127.0.0.1:1")
    _, session_id, _agent, _deps, transcript = _open(s, tmp_path / "tr")
    assert session_id == 1
    assert transcript.parent.is_dir()
    assert "pull skipped" in capsys.readouterr().out


def test_auth_error_maps_to_signin_hint():
    server, _ = make_stub({"sync": (None, "sync: auth not configured")})
    try:
        anki.fetch_reviewed_today(f"http://127.0.0.1:{server.server_address[1]}")
    except anki.AnkiConnectError as e:
        assert "AnkiWeb" in str(e)
        assert "sign in" in str(e)
    else:
        raise AssertionError("expected AnkiConnectError")
    finally:
        server.shutdown()
        server.server_close()


def test_other_errors_pass_through():
    server, _ = make_stub({"notesInfo": (None, "boom")})
    try:
        anki.fetch_reviewed_today(f"http://127.0.0.1:{server.server_address[1]}", sync_first=False)
    except anki.AnkiConnectError as e:
        assert "notesInfo failed: boom" in str(e)
    else:
        raise AssertionError("expected AnkiConnectError")
    finally:
        server.shutdown()
        server.server_close()


def _generated_lemma(conn, lemma, rank, gloss="g", example="e"):
    store.add_lemmas(conn, [{"lemma": lemma, "freq_rank": rank, "freq": 1, "forms": []}])
    store.set_status(conn, lemma, "generated")
    lid = conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"]
    store.upsert_content(
        conn, lid, "lemma", gloss, json.dumps({"examples": [{"target": example}]}), "m"
    )


def test_record_anki_reviews_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    _generated_lemma(conn, "mięso", 1)
    stored, unknown = store.record_anki_reviews(conn, ["mięso", "nigdy"], "2026-09-27")
    assert (stored, unknown) == (1, ["nigdy"])
    # idempotent re-record on the same day
    assert store.record_anki_reviews(conn, ["mięso"], "2026-09-27") == (1, [])
    rows = conn.execute("SELECT lemma, reviewed_on FROM anki_reviews").fetchall()
    assert [(r["lemma"], r["reviewed_on"]) for r in rows] == [("mięso", "2026-09-27")]


def test_get_practice_targets_prioritizes_todays_reviews(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    _generated_lemma(conn, "kawa", 1, "káva", "Poproszę kawę.")
    _generated_lemma(conn, "mięso", 2, "maso", "Poproszę mięso.")
    store.record_anki_reviews(conn, ["mięso"], date.today().isoformat())  # noqa: DTZ011 -- must match the tool's own "today"

    targets = PracticeTools(conn_path=db, session_id=1).get_practice_targets(2)
    assert [t["lemma"] for t in targets] == ["mięso", "kawa"]
    assert targets[0]["gloss"] == "maso"


def test_get_practice_targets_falls_back_when_no_reviews(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    _generated_lemma(conn, "kawa", 1)
    targets = PracticeTools(conn_path=db, session_id=1).get_practice_targets(5)
    assert [t["lemma"] for t in targets] == ["kawa"]
