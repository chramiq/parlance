"""Chat ingestion: parse -> gate -> accumulate, idempotent across exports.

Fixtures mirror the real Instagram DYI structure: participants include the
owner, content strings carry Meta's double encoding, system templates
appear as owner content. Owner detection needs several threads: the owner
is the participant present in every one, everyone else only in their own.
"""

import json
from pathlib import Path

import pytest

from parlance import store
from parlance.chats import meta

OWNER = "me myself"
OTHER = "kamarad"
THIRD = "treti"


def mojibake(s: str) -> str:
    """What json.load gives for Meta's double-encoded strings: 'chuÅ¥' for 'chuť'."""
    return s.encode("utf-8").decode("latin-1")


def write_thread(root: Path, participants: list[str], messages: list[dict], name: str):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"message_{name}.json").write_text(
        json.dumps(
            {
                "participants": [{"name": p} for p in participants],
                "thread_path": f"inbox/{name}",
                "messages": messages,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def msg(sender: str, text: str, ts: int = 1) -> dict:
    return {"sender_name": sender, "timestamp_ms": ts, "content": text}


@pytest.fixture
def wordlist(tmp_path: Path) -> Path:
    """Tiny public frequency list: forms + ranks, most frequent first."""
    forms = ["máš", "mám", "tam", "díky", "chuť", "hospoda", "hospodu", "já", "mas", "call"]
    p = tmp_path / "cs_50k.txt"
    p.write_text("".join(f"{f} {100 - i}\n" for i, f in enumerate(forms)), encoding="utf-8")
    return p


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def test_import_gates_and_counts(db: Path, wordlist: Path, tmp_path: Path):
    root = tmp_path / "ig"
    write_thread(
        root,
        [OWNER, OTHER],
        [
            # diacritic-less texting: mas -> máš -> mít, mam -> mám -> mít
            msg(OWNER, "mas tam mam diky?"),
            # double-encoded content: mojibake('chuť') -> chuť
            msg(OWNER, mojibake("chuť"), ts=2),
            # other party's words never count
            msg(OTHER, "hospoda hospoda hospoda", ts=3),
            # localized system template
            msg(OWNER, "Posíláte přílohu.", ts=4),
            # non-public words drop at the gate
            msg(OWNER, "xblyat qwertzuiop", ts=5),
            # photo share: no content, ignored
            {"sender_name": OWNER, "timestamp_ms": 6, "photos": [{"uri": "x"}]},
        ],
        "t1",
    )
    write_thread(root, [OWNER, THIRD], [msg(OWNER, "diky", ts=7)], "t2")
    conn = store.connect(db)
    stats = meta.import_chats(conn, [root], "cs", wordlist)

    assert stats["new_messages"] == 4  # owner text messages minus the template
    assert stats["system_strings"] == 1
    lemma = {r["lemma"]: r["count"] for r in store.list_personal_words(conn, "cs", limit=-1)}
    # mas->máš->mít, mam->mám->mít, tam, diky->díky (x2), chuť (from mojibake)
    assert lemma["mít"] == 2
    assert lemma["tam"] == 1
    assert lemma["díky"] == 2
    assert lemma["chuť"] == 1
    # other party's word and gated junk never appear
    assert "hospoda" not in lemma
    assert all("x" not in l for l in lemma)
    assert all(r["status"] == "pending" for r in store.list_personal_words(conn, "cs", limit=-1))


def test_reimport_is_idempotent(db: Path, wordlist: Path, tmp_path: Path):
    root = tmp_path / "ig"
    write_thread(root, [OWNER, OTHER], [msg(OWNER, "mas tam")], "t1")
    write_thread(root, [OWNER, THIRD], [msg(OWNER, "diky")], "t2")
    conn = store.connect(db)
    meta.import_chats(conn, [root], "cs", wordlist)

    def all_words():
        return {(r[0], r[1]) for r in conn.execute("SELECT lemma, count FROM personal_words")}

    before = all_words()
    # curation survives re-imports: a flipped status must not reset to pending
    conn.execute("UPDATE personal_words SET status='accepted' WHERE lemma='mít'")
    conn.commit()

    stats = meta.import_chats(conn, [root], "cs", wordlist)
    assert stats["new_messages"] == 0
    assert stats["skipped_seen"] == 2
    assert all_words() == before
    accepted = store.list_personal_words(conn, "cs", status="accepted", limit=-1)
    assert [r["lemma"] for r in accepted] == ["mít"]


def test_second_export_accumulates_with_its_own_owner(db: Path, wordlist: Path, tmp_path: Path):
    ig = tmp_path / "ig"
    fb = tmp_path / "fb"
    write_thread(ig, [OWNER, OTHER], [msg(OWNER, "mas")], "t1")
    write_thread(ig, [OWNER, THIRD], [msg(OWNER, "tam")], "t2")
    # different platform, different display name for the same person
    write_thread(fb, ["fb-me", "fb-friend"], [msg("fb-me", "mas", ts=9)], "u1")
    write_thread(fb, ["fb-me", "fb-friend2"], [msg("fb-me", "tam", ts=10)], "u2")
    conn = store.connect(db)
    meta.import_chats(conn, [ig], "cs", wordlist)
    meta.import_chats(conn, [fb], "cs", wordlist)
    lemma = {r["lemma"]: r["count"] for r in store.list_personal_words(conn, "cs", limit=-1)}
    assert lemma["mít"] == 2
    assert lemma["tam"] == 2


def test_owner_ambiguity_is_a_structured_error(db: Path, wordlist: Path, tmp_path: Path):
    root = tmp_path / "ig"
    # both participants present in every thread and both send: ambiguous
    write_thread(root, [OWNER, OTHER], [msg(OWNER, "mas")], "t1")
    write_thread(root, [OWNER, OTHER], [msg(OTHER, "tam", ts=2)], "t2")
    conn = store.connect(db)
    with pytest.raises(ValueError, match="owner detection failed"):
        meta.import_chats(conn, [root], "cs", wordlist)


def test_missing_export_is_a_structured_error(db: Path, wordlist: Path, tmp_path: Path):
    conn = store.connect(db)
    with pytest.raises(ValueError, match="no message_"):
        meta.import_chats(conn, [tmp_path / "empty"], "cs", wordlist)


def test_mojibake_fix_roundtrip():
    assert meta._fix(mojibake("chuť")) == "chuť"
    assert meta._fix("plain ascii") == "plain ascii"
    assert meta._fix("already ěščř utf8") == "already ěščř utf8"
