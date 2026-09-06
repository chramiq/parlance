import pytest

from parlance import worklist
from parlance.store import connect, stats

FORMS = """jestem 100
jesteś 90
są 80
być 70
kocham 60
kocha 50
kochasz 40
"""


def test_build_queue_collapses_and_ranks():
    forms = [tuple(line.split()) for line in FORMS.strip().splitlines()]
    queue = worklist.build_queue([(w, int(c)) for w, c in forms])
    by_lemma = {it.lemma: it for it in queue}
    assert set(by_lemma) == {"być", "kochać"}
    assert by_lemma["być"].freq == 340
    assert by_lemma["kochać"].freq == 150
    assert "jestem" in by_lemma["być"].forms
    assert queue[0].lemma == "być"


def test_run_seeds_store_idempotently(tmp_path):
    src = tmp_path / "pl_50k.txt"
    src.write_text(FORMS, encoding="utf-8")
    db = tmp_path / "p.db"
    total, inserted, skipped = worklist.run(db_path=db, source_path=src, scan_limit=100)
    assert (total, inserted, skipped) == (2, 2, 0)
    assert worklist.run(db_path=db, source_path=src, scan_limit=100)[1:] == (0, 2)
    assert stats(connect(db)) == {"worklist": 2}


def test_empty_source_fails_loudly(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="0 forms"):
        worklist.run(db_path=tmp_path / "p.db", source_path=src, scan_limit=100)
