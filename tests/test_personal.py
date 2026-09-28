"""The personal deck path: pre-pass translation, poll join, submit kind, subdeck.

The card-writing agent must stay deck-agnostic: personal mode is a data
switch (Deps.personal) plus content kind, never a second agent.
"""

import json
import sqlite3
import zipfile
from typing import Any

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from typer.testing import CliRunner

from parlance import cli, store
from parlance.agent import Deps, build_agent
from parlance.chats import translate
from parlance.config import Settings


def seeded_settings(tmp_path):
    """Settings rooted at tmp_path with a tiny pl frequency wordlist on disk."""
    wl = tmp_path / "wordlist" / "pl_50k.txt"
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("kochać 300\nkocha 200\nkocham 150\njestem 90\n", encoding="utf-8")
    return Settings(data_dir=tmp_path), wl


def add_personal_word(conn, lang, lemma, count):
    conn.execute(
        "INSERT INTO personal_words (lang, lemma, count, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(lang, lemma) DO UPDATE SET count=excluded.count",
        (lang, lemma, count, store.now()),
    )
    conn.commit()


def scripted_prepass(payload: dict) -> Any:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(json.dumps(payload))])

    return fn


def test_translate_pending_maps_and_skips(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    add_personal_word(conn, "cs", "milovat", 10)
    add_personal_word(conn, "cs", "nicméně", 5)
    store.merge_pairs(conn, "pl", "cs", [("kochać", "milovat", "cswikt-inv")])

    model = FunctionModel(
        scripted_prepass(
            {
                "words": [
                    {"known": "milovat", "target": "kochać", "skip_reason": ""},
                    {"known": "nicméně", "target": "", "skip_reason": "function word"},
                ]
            }
        )
    )
    stats = translate.translate_pending(conn, s, model=model)

    assert stats["pending"] == 2
    assert stats["mapped"] == 1
    assert stats["skipped"] == 1
    assert stats["new_lemmas"] == 1
    # chosen lemma materialized with corpus forms from the pl wordlist
    lemma = conn.execute("SELECT * FROM lemmas WHERE lemma='kochać'").fetchone()
    assert lemma["forms"].count("kocham") == 1
    assert lemma["status"] == "worklist"
    assert lemma["freq"] == 10  # the learner's chat count
    # verdicts recorded, statuses flipped
    assert store.personal_words_stats(conn, "cs") == {"mapped": 1, "skipped": 1}
    assert store.personal_skip_reasons(conn, "cs") == {"nicméně": "function word"}

    # idempotent: nothing pending, the model is never called again
    stats2 = translate.translate_pending(
        conn, s, model=FunctionModel(lambda m, i: (_ for _ in ()).throw(AssertionError()))
    )
    assert stats2["pending"] == 0


def test_translate_pending_omitted_and_unmatched_stay_pending(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    add_personal_word(conn, "cs", "milovat", 10)
    add_personal_word(conn, "cs", "kámo", 8)

    model = FunctionModel(
        scripted_prepass(
            {
                "words": [
                    {"known": "kámo", "target": "koleś", "skip_reason": ""},
                    {"known": "nebylo-v-inputu", "target": "x", "skip_reason": ""},
                ]
            }
        )
    )
    stats = translate.translate_pending(conn, s, model=model)

    assert stats["unmatched"] == 1
    assert stats["omitted"] == 1  # milovat was never echoed -> stays pending
    assert len(store.personal_pending(conn)) == 1
    assert store.personal_pending(conn)[0]["lemma"] == "milovat"
    # LLM form normalized to its lemma: kolesia-family forms would group, but
    # the chosen word goes through simplemma before insert
    lemma = conn.execute("SELECT lemma FROM lemmas WHERE lemma='koleś'").fetchone()
    assert lemma is not None


def test_translate_prompt_carries_dictionary_candidates(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    add_personal_word(conn, "cs", "milovat", 10)
    store.merge_pairs(conn, "pl", "cs", [("kochać", "milovat", "cswikt-inv")])

    seen = {}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        seen["prompt"] = str(messages[0].parts[0].content)
        return ModelResponse(
            parts=[TextPart('{"words": [{"known": "milovat", "target": "kochać"}]}')]
        )

    translate.translate_pending(conn, s, model=FunctionModel(fn))
    assert "milovat" in seen["prompt"]
    assert "candidates: kochać" in seen["prompt"]


def test_translate_pending_words_cap(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    for w, c in (("málo", 30), ("kámo", 20), ("hospoda", 10)):
        add_personal_word(conn, "cs", w, c)

    def fn(messages, info: AgentInfo) -> ModelResponse:
        sent = str(messages[0].parts[0].content).splitlines()
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {"words": [{"known": line.split()[0], "target": "x"} for line in sent]}
                    )
                )
            ]
        )

    stats = translate.translate_pending(conn, s, model=FunctionModel(fn), words=2)
    assert stats["pending"] == 2
    # the least-used word never reached the model and stays pending
    assert [r["lemma"] for r in store.personal_pending(conn)] == ["hospoda"]


def test_translate_pending_batch_error_tolerated(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    add_personal_word(conn, "cs", "málo", 30)
    add_personal_word(conn, "cs", "kámo", 20)
    add_personal_word(conn, "cs", "hospoda", 10)

    calls = {"n": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated gateway death")
        sent = str(messages[0].parts[0].content).splitlines()
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {"words": [{"known": line.split()[0], "target": "kochać"} for line in sent]}
                    )
                )
            ]
        )

    stats = translate.translate_pending(conn, s, model=FunctionModel(fn), batch=1)
    assert stats["errors"] == 1
    assert stats["mapped"] == 2  # the failed batch's word stays pending
    assert [r["lemma"] for r in store.personal_pending(conn)] == ["málo"]
    detail = json.loads(
        conn.execute("SELECT detail FROM changes WHERE action='personal:prepass'").fetchone()[
            "detail"
        ]
    )
    assert "RuntimeError" in detail["last_error"]


def test_poll_personal_order_and_status_filter(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    store.add_lemmas(
        conn,
        [
            {"lemma": "koleś", "freq_rank": 1, "freq": 0, "forms": ["koleś"]},
            {"lemma": "kochać", "freq_rank": 2, "freq": 0, "forms": ["kocha"]},
        ],
    )
    for known, lemma in (("kámo", "koleś"), ("milovat", "kochać")):
        add_personal_word(conn, "cs", known, 10 if known == "kámo" else 99)
        lid = conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO personal_targets (known_lang, known, lemma_id) VALUES ('cs', ?, ?)",
            (known, lid),
        )
    conn.commit()
    store.set_status(conn, "koleś", "generated")  # already has a card: excluded

    rows = store.poll_personal_untranslated(conn, 10)
    assert [r["lemma"] for r in rows] == ["kochać"]
    assert rows[0]["personal_known"] == "milovat"


VALID_DRAFT = {
    "lemma": "kochać",
    "pos": "sloveso",
    "variants": [{"gloss": "milovat", "style": "neutral"}],
    "preferred": 0,
    "examples": [
        {"target": "Kocham cię, kochanie.", "known": "Miluju tě.", "cloze": "[...] cię, kochanie."}
    ],
    "false_friend_note": "",
}


def scripted_submit(messages, info: AgentInfo) -> ModelResponse:
    names = {
        p.tool_name
        for m in messages
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart)
    }
    if "poll_vocab" not in names:
        return ModelResponse(parts=[ToolCallPart("poll_vocab", {"n": 5})])
    if "submit_translations" not in names:
        return ModelResponse(parts=[ToolCallPart("submit_translations", {"drafts": [VALID_DRAFT]})])
    return ModelResponse(parts=[TextPart("report: 1 card")])


def test_agent_personal_mode_writes_personal_kind(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    store.add_lemmas(
        conn, [{"lemma": "kochać", "freq_rank": 1, "freq": 7, "forms": ["kocha", "kocham"]}]
    )
    add_personal_word(conn, "cs", "milovat", 7)
    lid = conn.execute("SELECT id FROM lemmas WHERE lemma='kochać'").fetchone()["id"]
    conn.execute(
        "INSERT INTO personal_targets (known_lang, known, lemma_id) VALUES ('cs', 'milovat', ?)",
        (lid,),
    )
    conn.commit()

    agent = build_agent(s, s.db_path, model=FunctionModel(scripted_submit))
    result = agent.run_sync("go", deps=Deps(db_path=s.db_path, model_name="test", personal=True))
    assert "1 card" in result.output
    row = store.get_content(conn, "kochać", kind="personal")
    assert row is not None and row["preferred_gloss"] == "milovat"
    assert store.get_content(conn, "kochać", kind="lemma") is None


def test_generate_cli_personal_deck_wiring(tmp_path, monkeypatch):
    s, _ = seeded_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)

    calls = {}

    def fake_prepass(conn, s, words=None):
        calls["prepass"] = words
        return {"pending": 0}

    monkeypatch.setattr(translate, "translate_pending", fake_prepass)

    class FakeAgent:
        def run_sync(self, prompt, deps, usage_limits=None):
            calls["personal"] = deps.personal
            conn2 = store.connect(s.db_path)
            store.add_lemmas(conn2, [{"lemma": "x", "freq_rank": 1, "freq": 1, "forms": []}])
            lid = conn2.execute("SELECT id FROM lemmas WHERE lemma='x'").fetchone()["id"]
            store.upsert_content(conn2, lid, "personal", "g", "{}", "fake")
            conn2.close()
            from types import SimpleNamespace

            return SimpleNamespace(output="done", usage=None)

    monkeypatch.setattr(cli, "build_agent", lambda s, db_path, **kw: FakeAgent())
    result = CliRunner().invoke(
        cli.app, ["generate", "--deck", "personal", "--count", "1", "--words", "100"]
    )
    assert result.exit_code == 0
    assert calls == {"prepass": 100, "personal": True}
    assert "pre-pass" in result.stdout

    bad = CliRunner().invoke(cli.app, ["generate", "--deck", "bogus"])
    assert bad.exit_code != 0
    wrong_deck = CliRunner().invoke(cli.app, ["generate", "--words", "5"])
    assert wrong_deck.exit_code != 0


def _personal_deck_db(tmp_path):
    s, _ = seeded_settings(tmp_path)
    conn = store.connect(s.db_path)
    store.add_lemmas(
        conn,
        [
            {"lemma": "kochać", "freq_rank": 1, "freq": 150, "forms": ["kochać", "kocha"]},
            {"lemma": "koleś", "freq_rank": 50_001, "freq": 7, "forms": ["koleś"]},
        ],
    )
    for lemma, kind, gloss in (("kochać", "lemma", "milovat"), ("koleś", "personal", "kámo")):
        lid = conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"]
        store.upsert_content(
            conn,
            lid,
            kind,
            gloss,
            json.dumps(
                {
                    "lemma": lemma,
                    "pos": "sloveso",
                    "gender": "",
                    "variants": [{"gloss": gloss, "style": "neutral"}],
                    "preferred": 0,
                    "examples": [
                        {"target": "Kocham cię.", "known": "Miluju tě.", "cloze": "[...] cię."}
                    ],
                    "false_friend_note": "",
                }
            ),
            "testmodel",
        )
        store.set_status(conn, lemma, "generated")
    return s


async def test_build_emits_personal_subdeck(tmp_path, monkeypatch):
    from parlance.deck import build as deckbuild
    from parlance.tts import audio_name

    async def fake_render(texts, voice, out_dir, rate):
        out_dir.mkdir(parents=True, exist_ok=True)
        for t in texts:
            (out_dir / audio_name(t, voice, rate)).write_bytes(b"FAKE_MP3")
        return {t: audio_name(t, voice, rate) for t in texts}

    monkeypatch.setattr(deckbuild, "render_lines", fake_render)
    s = _personal_deck_db(tmp_path)
    out_path, counts = await deckbuild.build(s)

    assert counts["personal"] == 1 and counts["notes"] == 2
    z = zipfile.ZipFile(out_path)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        z.extract("collection.anki2", td)
        con = sqlite3.connect(f"{td}/collection.anki2")
        guids = {r[0] for r in con.execute("SELECT guid FROM notes")}
        assert "lemma:kochać" in guids and "personal:koleś" in guids
        col_decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        deck_names = {d["name"] for d in col_decks.values()}
        assert "PL::PERSONAL" in deck_names


def test_chats_import_multi_lang(tmp_path, monkeypatch):
    """One export, two gate languages: cs keeps Czech words, en keeps English ones."""
    import json as _json

    s, _ = seeded_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    root = tmp_path / "ig"
    root.mkdir()
    thread = {
        "participants": [{"name": "me"}, {"name": "friend"}],
        "thread_path": "inbox/t1",
        "messages": [
            {"sender_name": "me", "timestamp_ms": 1, "content": "you call me tonight"},
            {"sender_name": "me", "timestamp_ms": 2, "content": "mas tam"},
        ],
    }
    (root / "message_t1.json").write_text(_json.dumps(thread), encoding="utf-8")
    # gate wordlists on disk for both languages: the CLI must never download
    (tmp_path / "wordlist" / "cs_50k.txt").write_text(
        "máš 100\nmám 90\ntam 50\njá 40\n", encoding="utf-8"
    )
    en = tmp_path / "wordlist" / "en_50k.txt"
    en.write_text("you 100\ncall 50\ntonight 30\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli.app, ["chats", "import", str(root), "--lang", "cs", "--lang", "en"]
    )
    assert result.exit_code == 0
    assert "[cs]" in result.stdout and "[en]" in result.stdout
    conn = store.connect(s.db_path)
    cs_words = {r["lemma"] for r in store.list_personal_words(conn, "cs", limit=-1)}
    en_words = {r["lemma"] for r in store.list_personal_words(conn, "en", limit=-1)}
    assert "mít" in cs_words and "you" not in cs_words
    assert {"you", "call"} <= en_words and "mít" not in en_words
