import gzip
import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from parlance import store
from parlance.agent import Deps, build_agent
from parlance.config import Settings
from parlance.lexicon import build_pairs, iter_translations, parse_hint_json


def _write_extract(dir_, lang, entries):
    path = dir_ / f"{lang}-extract.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


PL_ENTRIES = [
    {
        "word": "móc",
        "translations": [{"lang_code": "cs", "word": "moci"}, {"lang_code": "en", "word": "can"}],
    },
    {"word": "szukać", "translations": [{"lang_code": "cs", "word": "hledat"}]},
    {"word": "cholera", "translations": [{"lang_code": "cs", "word": "sakra"}]},
]
CS_ENTRIES = [
    {"word": "šukat", "translations": [{"lang_code": "pl", "word": "szukać"}]},
    {"word": "moct", "translations": [{"lang_code": "pl", "word": "móc"}]},
]


def test_parse_hint_json():
    good = '[["hledat", "vyhledávat"], ["pot"], "jediný"]'
    assert parse_hint_json(good) == [["hledat", "vyhledávat"], ["pot"], ["jediný"]]
    narrated = 'Tak pozor:\n```json\n[["a"], ["b", "c"]]\n```'
    assert parse_hint_json(narrated) == [["a"], ["b", "c"]]
    assert parse_hint_json("no array here at all") is None
    assert parse_hint_json('["broken", ') is None


def test_merge_pairs_unions_sources(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.merge_pairs(conn, "pl", "cs", [("móc", "moci", "gemini-live")])
    store.merge_pairs(
        conn, "pl", "cs", [("móc", "moci", "cswikt-inv"), ("móc", "moct", "gemini-live")]
    )
    rows = conn.execute(
        "SELECT known, sources FROM lexicon_pairs WHERE target='móc' ORDER BY known"
    ).fetchall()
    assert [(r["known"], sorted(__import__("json").loads(r["sources"]))) for r in rows] == [
        ("moci", ["cswikt-inv", "gemini-live"]),
        ("moct", ["gemini-live"]),
    ]


def test_iter_translations_filters_lang(tmp_path):
    p = _write_extract(tmp_path, "pl", PL_ENTRIES)
    pairs = dict(iter_translations(p, "cs"))
    assert "en" not in json.dumps(pairs)
    assert pairs == {"móc": "moci", "szukać": "hledat", "cholera": "sakra"}


def test_build_pairs_merges_both_directions(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_extract(src, "pl", PL_ENTRIES)
    _write_extract(src, "cs", CS_ENTRIES)
    conn = store.connect(tmp_path / "t.db")
    stats = build_pairs(conn, "cs", "pl", tmp_path / "cache", src)
    assert stats["lemmas_with_hints"] == 3
    got = store.pair_suggestions(conn, "szukać", "cs", "pl")
    assert "hledat" in got  # direct from pl edition
    assert "šukat" in got  # inverted from cs edition
    assert sorted(store.pair_suggestions(conn, "móc", "cs", "pl")) == ["moci", "moct"]
    assert store.pair_suggestions(conn, "nieistniejące", "cs", "pl") == []


def fake_model_capture_poll(captured):
    def fn(messages, info: AgentInfo) -> ModelResponse:
        names = [
            p.tool_name
            for m in messages
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolCallPart)
        ]
        for m in messages:
            for p in getattr(m, "parts", []):
                if isinstance(p, ToolReturnPart) and p.tool_name == "poll_vocab":
                    captured.extend(p.content)
        if "poll_vocab" not in names:
            return ModelResponse(parts=[ToolCallPart("poll_vocab", {"n": 5})])
        return ModelResponse(parts=[TextPart("done")])

    return fn


def test_poll_attaches_suggestions(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    store.add_lemmas(conn, [{"lemma": "szukać", "freq_rank": 1, "freq": 1, "forms": []}])
    store.replace_pairs(conn, "pl", "cs", [("pl", "szukać", "cs", "hledat", '["plwikt"]')])
    captured = []
    settings = Settings(known_lang="cs", target_lang="pl")
    agent = build_agent(settings, db, model=FunctionModel(fake_model_capture_poll(captured)))
    agent.run_sync("go", deps=Deps(db_path=db, model_name="testmodel"))
    assert captured and captured[0].get("suggested_known") == ["hledat"]
