"""generate driver: per-run metrics count new cards vs edits from the journal."""

from types import SimpleNamespace

from typer.testing import CliRunner

from parlance import cli, store
from parlance.config import Settings


def test_driver_reports_new_and_edited(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    conn = store.connect(s.db_path)
    store.add_lemmas(conn, [{"lemma": "old", "freq_rank": 1, "freq": 5, "forms": []}])
    lid = conn.execute("SELECT id FROM lemmas WHERE lemma='old'").fetchone()["id"]
    store.upsert_content(conn, lid, "lemma", "oldgloss", "{}", "seed")
    conn.close()

    class FakeAgent:
        def run_sync(self, prompt, deps, usage_limits=None):
            c = store.connect(s.db_path)
            store.add_lemmas(c, [{"lemma": "new", "freq_rank": 2, "freq": 1, "forms": []}])
            nid = c.execute("SELECT id FROM lemmas WHERE lemma='new'").fetchone()["id"]
            store.upsert_content(c, nid, "lemma", "g", "{}", "fake")
            store.upsert_content(c, lid, "lemma", "newgloss", "{}", "fake")
            c.close()
            return SimpleNamespace(output="done", usage=None)

    monkeypatch.setattr(cli, "build_agent", lambda *a, **kw: FakeAgent())
    result = CliRunner().invoke(cli.app, ["generate", "--count", "1"])
    assert result.exit_code == 0
    assert "new=1 edited=1 total=1/1" in result.stdout
