"""typer app: worklist, init, generate. More verbs land with their phase (see TODO.md)."""

import json
from contextlib import closing

import typer
from pydantic_ai import UsageLimitExceeded, UsageLimits

from . import store, worklist
from .agent import LANG_NAMES, Deps, build_agent
from .cognates import mark_free_cognates
from .config import get_settings

app = typer.Typer(no_args_is_help=True)
worklist_app = typer.Typer(no_args_is_help=True, help="Build and inspect the lemma worklist.")
app.add_typer(worklist_app, name="worklist")


@worklist_app.command("build")
def worklist_build(scan_forms: int | None = None):
    """Fetch the frequency list if needed and seed the lemma worklist."""
    s = get_settings()
    total, inserted, skipped = worklist.run(
        db_path=s.db_path,
        source_path=s.wordlist_path,
        scan_limit=scan_forms or s.worklist_scan_forms,
    )
    typer.echo(f"queue={total} inserted={inserted} skipped_known={skipped}")


@worklist_app.command("list")
def worklist_list(limit: int = 30, status: str = "worklist"):
    """Show worklist lemmas in frequency order."""
    conn = store.connect(get_settings().db_path)
    rows = store.list_lemmas(conn, status=status, limit=limit)
    typer.echo(f"{'rank':>6}  {'lemma':<20}  {'freq':>9}  forms")
    for r in rows:
        forms = ", ".join(json.loads(r["forms"])[:4])
        typer.echo(f"{r['freq_rank']:>6}  {r['lemma']:<20}  {r['freq']:>9}  {forms}")
    if not rows:
        typer.echo(f"(no lemmas with status={status})")


@worklist_app.command("stats")
def worklist_stats():
    """Show lemma counts per status."""
    typer.echo(store.stats(store.connect(get_settings().db_path)))


def _content_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM content WHERE kind='lemma'").fetchone()["n"]


@app.command("init")
def init_profile(note: str = ""):
    """Establish the language-pair profile (pos tags, register tags, style rules)."""
    s = get_settings()
    known = LANG_NAMES.get(s.known_lang, s.known_lang)
    target = LANG_NAMES.get(s.target_lang, s.target_lang)
    with closing(store.connect(s.db_path)) as conn:
        existing = store.meta_get(conn, "pair_profile")
        if existing:
            typer.echo(json.dumps(existing, ensure_ascii=False, indent=1))
            if not typer.confirm("Pair profile exists. Overwrite?"):
                raise typer.Abort()
        agent = build_agent(s, s.db_path)
        prompt = (
            f"Create the language-pair profile for a learner who knows {known} "
            f"and is learning {target}, then save it with save_pair_profile."
        )
        if note:
            prompt += f" User note about themselves: {note}"
        result = agent.run_sync(
            prompt, deps=Deps(db_path=s.db_path, model_name=s.litellm_model or "unknown")
        )
        typer.echo(result.output)
        typer.echo(f"usage: {result.usage}")


@app.command("generate")
def generate(count: int = 20, batch: int = 15, max_requests: int = 60):
    """Translate new vocabulary cards via the agent restart-loop.

    Each run is request-bounded; when it exhausts its context budget the
    driver starts a fresh agent run, and untranslated polling makes the
    continuation idempotent by construction.
    """
    s = get_settings()
    submitted = 0
    with closing(store.connect(s.db_path)) as conn:
        if s.skip_free_cognates:
            from .cognates import ff_guard

            if not ff_guard(s.target_lang, s.known_lang):
                typer.echo(
                    f"warning: no false-friend guard file for {s.target_lang}->{s.known_lang}; "
                    f"auto-skip may wrongly skip trap words "
                    f"(add parlance/data/false_friends_{s.target_lang}_{s.known_lang}.json)"
                )
            skipped = mark_free_cognates(conn, target_lang=s.target_lang, known_lang=s.known_lang)
            typer.echo(f"free cognates skipped ({len(skipped)}): {', '.join(skipped[:50])}")
        agent = build_agent(s, s.db_path)
        while submitted < count:
            before = _content_count(conn)
            remaining = min(count - submitted, batch)
            try:
                result = agent.run_sync(
                    f"Submit {remaining} new vocabulary cards following your "
                    f"instructions, then report.",
                    deps=Deps(db_path=s.db_path, model_name=s.litellm_model or "unknown"),
                    usage_limits=UsageLimits(request_limit=max_requests),
                )
                report = result.output.strip().splitlines()[0] if result.output else ""
                usage = result.usage
            except UsageLimitExceeded as e:
                report = f"run hit usage limit: {e}"
                usage = "n/a"
            except Exception as e:  # noqa: BLE001 -- the driver must keep looping regardless of a single run's failure mode
                report = f"run FAILED: {type(e).__name__}: {str(e)[:300]}"
                usage = "n/a"
            wrote = _content_count(conn) - before
            submitted += wrote
            typer.echo(f"run: wrote={wrote} total={submitted}/{count} usage={usage}")
            if report:
                typer.echo(f"  agent: {report[:200]}")
            if wrote == 0:
                typer.echo("run submitted nothing; stopping instead of burning budget.")
                break
        typer.echo(store.stats(conn))
