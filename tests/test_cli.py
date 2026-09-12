from types import SimpleNamespace

from pydantic_ai import UsageLimitExceeded
from typer.testing import CliRunner

from parlance import cli, store
from parlance.config import Settings


def test_generate_driver_credits_then_stops(tmp_path, monkeypatch):
    db = tmp_path / "parlance.db"
    conn = store.connect(db)
    store.add_lemmas(conn, [{"lemma": "x", "freq_rank": 1, "freq": 1, "forms": []}])
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path))

    class FakeAgent:
        calls = 0

        def run_sync(self, prompt, deps=None, usage_limits=None):
            # run 1: one card lands, then the context budget dies mid-run,
            # run 2: nothing new gets written -> driver must stop there.
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                conn2 = store.connect(db)
                lemma_id = conn2.execute("SELECT id FROM lemmas WHERE lemma='x'").fetchone()["id"]
                store.upsert_content(conn2, lemma_id, "lemma", "g", "{}", "fake")
                conn2.close()
                raise UsageLimitExceeded("simulated context exhaustion")
            return SimpleNamespace(output="", usage=None)

    monkeypatch.setattr(cli, "build_agent", lambda s, db_path: FakeAgent())
    result = CliRunner().invoke(cli.app, ["generate", "--count", "5"])
    assert result.exit_code == 0
    assert FakeAgent.calls == 2
    assert "total=1/5" in result.stdout
    assert "submitted nothing" in result.stdout
