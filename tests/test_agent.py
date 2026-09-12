import json

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from parlance import store
from parlance.agent import (
    Deps,
    Example,
    GlossVariant,
    TranslationDraft,
    _cognate_class,
    build_agent,
    validate_cloze,
)
from parlance.config import Settings


def seeded(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    store.add_lemmas(
        conn,
        [
            {
                # lemma is itself a corpus form -- the reality for ~90% of the
                # real worklist; regression for the unsaveable-card bug
                "lemma": "kochać",
                "freq_rank": 1,
                "freq": 150,
                "forms": ["kochać", "kocha", "kocham", "kochasz"],
            },
            {"lemma": "być", "freq_rank": 2, "freq": 340, "forms": ["jest", "jestem", "są", "być"]},
            {"lemma": "szukać", "freq_rank": 3, "freq": 120, "forms": ["szuka", "szukam"]},
        ],
    )
    return db, conn


VALID_DRAFT = {
    "lemma": "kochać",
    "pos": "sloveso",
    "variants": [
        {"gloss": "milovat", "style": "neutral"},
        {"gloss": "mít rád", "style": "neutral"},
    ],
    "preferred": 0,
    "examples": [
        {
            "target": "Kocham cię, kochanie.",
            "known": "Miluju tě, miláčku.",
            "cloze": "[...] cię, kochanie.",
        }
    ],
    "false_friend_note": "",
}


def test_validate_cloze_happy():
    ex = Example(target="Kocham cię.", known="Miluju tě.", cloze="[...] cię.")
    validate_cloze(ex, ["kocha", "kocham", "kochać"])


def test_validate_cloze_wrong_form_and_multiple_forms():
    with pytest.raises(ModelRetry):
        validate_cloze(Example(target="Nie mam go.", known="", cloze="Nie [...] go."), ["kocham"])
    with pytest.raises(ModelRetry):
        validate_cloze(
            Example(target="Kocham, kocham cię.", known="", cloze="[...] kocham cię."),
            ["kocham"],
        )


def test_draft_rejects_bad_preferred_and_unlabeled_senses():
    with pytest.raises(ValueError):
        TranslationDraft(
            lemma="kochać",
            pos="sloveso",
            variants=[GlossVariant(gloss="milovat")],
            preferred=3,
            examples=[Example(target="Kocham cię.", known="a", cloze="[...] cię.")],
        )
    with pytest.raises(ValueError):
        TranslationDraft(
            lemma="kochać",
            pos="sloveso",
            variants=[GlossVariant(gloss="milovat")],
            preferred=0,
            examples=[
                Example(target="Kocham cię.", known="a", cloze="[...] cię."),
                Example(target="Kochasz mnie.", known="b", cloze="[...] mnie."),
            ],
        )


def test_cognate_classification():
    assert _cognate_class("system", "systém", "") == "free"
    assert _cognate_class("szukać", "šukat", "pozor: neznamená hledat") == "false_friend"
    assert _cognate_class("kochać", "milovat", "") == "none"


def scripted_submit(messages, info: AgentInfo) -> ModelResponse:
    seen_poll = any(
        isinstance(p, ToolCallPart) and p.tool_name == "poll_vocab"
        for m in messages
        for p in getattr(m, "parts", [])
    )
    seen_submit = any(
        isinstance(p, ToolCallPart) and p.tool_name == "submit_translation"
        for m in messages
        for p in getattr(m, "parts", [])
    )
    if not seen_poll:
        return ModelResponse(parts=[ToolCallPart("poll_vocab", {"n": 5})])
    if not seen_submit:
        return ModelResponse(parts=[ToolCallPart("submit_translation", {"draft": VALID_DRAFT})])
    return ModelResponse(parts=[TextPart("report: 1 card")])


def test_agent_tool_loop_writes_card(tmp_path):
    db, conn = seeded(tmp_path)
    agent = build_agent(Settings(), db, model=FunctionModel(scripted_submit))
    result = agent.run_sync("go", deps=Deps(db_path=db, model_name="testmodel"))
    assert "1 card" in result.output
    row = store.get_content(conn, "kochać")
    assert row is not None and row["preferred_gloss"] == "milovat"
    payload = json.loads(row["payload"])
    assert payload["variants"][1]["gloss"] == "mít rád"
    conn_rows = conn.execute(
        "SELECT status, cognate_class, pos FROM lemmas WHERE lemma='kochać'"
    ).fetchone()
    assert (conn_rows["status"], conn_rows["cognate_class"], conn_rows["pos"]) == (
        "generated",
        "none",
        "sloveso",
    )
    actions = {r["action"] for r in conn.execute("SELECT action FROM changes")}
    assert "content:upsert" in actions


def test_submit_rejects_unknown_lemma_and_lookup_scopes(tmp_path):
    db, conn = seeded(tmp_path)
    agent = build_agent(Settings(), db, model=FunctionModel(scripted_submit))
    agent.run_sync("go", deps=Deps(db_path=db, model_name="testmodel"))

    assert any("payload" in h for h in store.find_lexicon(conn, "kochać"))
    assert store.find_lexicon(conn, "milovat", "glosses")[0]["lemma"] == "kochać"
    assert store.find_lexicon(conn, "milovat", "lemmas") == []
    assert store.find_lexicon(conn, "kocham", "forms")[0]["lemma"] == "kochać"
    with pytest.raises(ValueError):
        store.find_lexicon(conn, "x", "bogus")


def test_version_mismatch_fails_loudly(tmp_path):
    db = tmp_path / "m.db"
    con = store.sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="delete .* and rebuild"):
        store.connect(db)


def scripted_edit_then_collide(messages, info: AgentInfo) -> ModelResponse:
    submits = [
        p
        for m in messages
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart) and p.tool_name == "submit_translation"
    ]
    if len(submits) == 0:
        return ModelResponse(parts=[ToolCallPart("submit_translation", {"draft": VALID_DRAFT})])
    if len(submits) == 1:
        edited = dict(VALID_DRAFT, examples=VALID_DRAFT["examples"], false_friend_note="pozor")
        return ModelResponse(parts=[ToolCallPart("submit_translation", {"draft": edited})])
    if len(submits) == 2:
        colliding = dict(
            VALID_DRAFT,
            lemma="być",  # reuses preferred 'milovat' -> advisory warning path
            examples=[
                {"target": "Jestem w domu.", "known": "Jsem doma.", "cloze": "[...] w domu."}
            ],
        )
        return ModelResponse(parts=[ToolCallPart("submit_translation", {"draft": colliding})])
    return ModelResponse(parts=[TextPart("done")])


def test_edit_path_and_collision_warning(tmp_path):
    db, conn = seeded(tmp_path)
    agent = build_agent(Settings(), db, model=FunctionModel(scripted_edit_then_collide))
    agent.run_sync("go", deps=Deps(db_path=db, model_name="testmodel"))
    row = store.get_content(conn, "kochać")
    assert row is not None and json.loads(row["payload"])["false_friend_note"] == "pozor"
    coll = store.get_content(conn, "być")
    assert coll is not None and coll["preferred_gloss"] == "milovat"


def test_meta_roundtrip(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.meta_set(conn, "pair_profile", {"pos_tags": ["sloveso"], "register_tags": ["neutral"]})
    got = store.meta_get(conn, "pair_profile")
    assert got is not None and got["pos_tags"] == ["sloveso"]
    store.meta_set(conn, "pair_profile", {"pos_tags": ["zájmeno"]})
    got = store.meta_get(conn, "pair_profile")
    assert got is not None and got["pos_tags"] == ["zájmeno"]
