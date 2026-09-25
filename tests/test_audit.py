import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from parlance import store
from parlance.agent import Deps, build_agent
from parlance.config import Settings


def test_sample_generated_returns_random_cards(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.add_lemmas(
        conn,
        [
            {"lemma": "kochać", "freq_rank": 1, "freq": 150, "forms": ["kocha"]},
            {"lemma": "być", "freq_rank": 2, "freq": 340, "forms": ["jest"]},
        ],
    )
    for lemma in ("kochać", "być"):
        store.set_status(conn, lemma, "generated")
        store.upsert_content(
            conn,
            conn.execute("SELECT id FROM lemmas WHERE lemma=?", (lemma,)).fetchone()["id"],
            "lemma",
            "g",
            json.dumps({"lemma": lemma, "examples": []}),
            "m",
        )
    sample = store.sample_generated(conn, 10)
    assert len(sample) == 2
    assert {r["lemma"] for r in sample} == {"kochać", "być"}
    assert sample[0]["payload"]["lemma"] == sample[0]["lemma"]


AUDIT_EDITED_DRAFT = {
    "lemma": "kochać",
    "pos": "sloveso",
    "variants": [{"gloss": "milovat", "style": "neutral"}],
    "preferred": 0,
    "examples": [{"target": "Kocham cię.", "known": "Miluju tě.", "cloze": "[...] cię."}],
    "false_friend_note": "",
}


def scripted_audit(messages, info: AgentInfo) -> ModelResponse:
    names = [
        p.tool_name
        for m in messages
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart)
    ]
    if "review_sample" not in names:
        return ModelResponse(parts=[ToolCallPart("review_sample", {"n": 5})])
    if "submit_translations" not in names:
        return ModelResponse(
            parts=[ToolCallPart("submit_translations", {"drafts": [AUDIT_EDITED_DRAFT]})]
        )
    return ModelResponse(parts=[TextPart("audit: 1 fixed")])


def test_audit_flow_samples_then_fixes(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    store.add_lemmas(conn, [{"lemma": "kochać", "freq_rank": 1, "freq": 150, "forms": ["kocham"]}])
    store.set_status(conn, "kochać", "generated")
    lemma_id = conn.execute("SELECT id FROM lemmas WHERE lemma='kochać'").fetchone()["id"]
    store.upsert_content(conn, lemma_id, "lemma", "starygloss", "{}", "m")
    agent = build_agent(Settings(), db, model=FunctionModel(scripted_audit), audit=True)
    result = agent.run_sync("audit", deps=Deps(db_path=db, model_name="testmodel"))
    assert "1 fixed" in result.output
    row = store.get_content(conn, "kochać")
    assert row is not None and row["preferred_gloss"] == "milovat"
