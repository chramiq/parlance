"""typer app: worklist, init, generate. More verbs land with their phase (see TODO.md)."""

import json
from contextlib import closing

import typer
from pydantic_ai import UsageLimitExceeded, UsageLimits

from . import store, worklist
from .agent import LANG_NAMES, Deps, build_agent
from .config import get_settings

app = typer.Typer(no_args_is_help=True)
worklist_app = typer.Typer(no_args_is_help=True, help="Build and inspect the lemma worklist.")
app.add_typer(worklist_app, name="worklist")
deck_app = typer.Typer(no_args_is_help=True, help="Assemble the Anki deck from stored cards.")
app.add_typer(deck_app, name="deck")


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


def _report_tool_rejects(result) -> None:
    """Echo any per-item rejections reported by the write tools this run."""
    from pydantic_ai.messages import ToolReturnPart

    for m in result.all_messages():
        for p in getattr(m, "parts", []):
            if isinstance(p, ToolReturnPart) and p.tool_name in (
                "submit_translations",
                "set_statuses",
            ):
                txt = str(p.content)
                if '"rejected": [{' in txt:
                    typer.echo(f"  tool reject: {txt[:400]}")


@app.command("init")
def init_profile(
    note: str = typer.Option("", "--note", help="Persona facts the profile should bake in."),
):
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
def generate(
    count: int = 20,
    batch: int = 15,
    max_requests: int = 60,
    random_sample: bool = False,
    prompt: str = "",
):
    """Translate new vocabulary cards via the agent restart-loop.

    random_sample polls a random corpus slice instead of the frequency head.
    prompt appends a user persona/task note to the agent's instructions for
    this run.
    """
    s = get_settings()
    submitted = 0
    skip_clause = (
        " Cognate skipping is ENABLED for this run: for lemmas whose similar_known word "
        "means the SAME thing in your judgment, set_statuses them skipped with a reason "
        "instead of writing cards. Different meaning = false friend = full card with a note."
        if s.skip_free_cognates
        else ""
    )
    with closing(store.connect(s.db_path)) as conn:
        agent = build_agent(s, s.db_path, extra_prompt=prompt)
        while submitted < count:
            before = _content_count(conn)
            remaining = min(count - submitted, batch)
            if not random_sample and s.gemini_api_key:
                import asyncio as _aio

                from .lexicon import hint_lemmas, lemmas_missing_hints

                upcoming = [r["lemma"] for r in store.poll_untranslated(conn, remaining)]
                missing = lemmas_missing_hints(conn, upcoming)
                if missing:
                    rows = _aio.run(hint_lemmas(conn, s, missing))
                    typer.echo(f"prefill: hinted {len(missing)} lemmas ({rows} rows)")
            try:
                result = agent.run_sync(
                    f"Submit {remaining} new vocabulary cards following your "
                    f"instructions, then report." + skip_clause,
                    deps=Deps(
                        db_path=s.db_path,
                        model_name=s.litellm_model or "unknown",
                        randomize=random_sample,
                    ),
                    usage_limits=UsageLimits(request_limit=max_requests),
                )
                report = result.output.strip().splitlines()[0] if result.output else ""
                usage = result.usage
                _report_tool_rejects(result)
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


@deck_app.command("build")
def deck_build(limit: int | None = None):
    """Render audio and write the vocab .apkg into var/out/."""
    import asyncio

    from .deck.build import build

    path, counts = asyncio.run(build(get_settings(), limit=limit))
    typer.echo(counts)
    typer.echo(f"deck written: {path}")


@app.command("lexicon")
def lexicon_build(
    extract_dir: str = typer.Option(
        "", help="Folder with pre-downloaded {lang}-extract.jsonl.gz files."
    ),
):
    """Build the translation-hint table from kaikki edition dumps (downloads if missing)."""
    from pathlib import Path

    from .lexicon import build_pairs

    s = get_settings()
    src = Path(extract_dir) if extract_dir else None
    with closing(store.connect(s.db_path)) as conn:
        stats = build_pairs(conn, s.known_lang, s.target_lang, s.db_path.parent / "lexicon", src)
    typer.echo(stats)


@app.command("llm-hints")
def lexicon_llm_hints(
    batch: int = 20,
    limit: int | None = typer.Option(None, help="Process at most this many lemmas (testing)."),
    workers: int = 4,
):
    """Backfill gemini-live translation hints for every lemma lacking one (parallel, resumable)."""
    import asyncio

    from .lexicon import build_llm_hints as _llm

    s = get_settings()
    if not s.gemini_api_key:
        typer.echo("GEMINI_API_KEY missing in .env")
        raise typer.Abort()
    with closing(store.connect(s.db_path)) as conn:
        stats = asyncio.run(_llm(conn, s, batch=batch, limit=limit, workers=workers))
    typer.echo(stats)


@app.command("practice")
def practice(
    text: bool = typer.Option(False, "--text", help="Typed I/O instead of voice (scripted tests)."),
):
    """Voice drill session with the Gemini Live coach (space-free, auto-VAD)."""
    import asyncio as _asyncio

    from .practice import run_practice, run_practice_voice

    s = get_settings()
    runner = run_practice if text else run_practice_voice
    try:
        stats = _asyncio.run(runner(s))
    except KeyboardInterrupt:
        typer.echo("ended")
        return
    typer.echo(stats)


@app.command("audit")
def audit(sample: int = 15, prompt: str = ""):
    """Self-check pass: the agent reviews a random sample of stored cards and fixes defects."""
    s = get_settings()
    with closing(store.connect(s.db_path)) as conn:
        if not _content_count(conn):
            typer.echo("no cards to audit; run generate first")
            raise typer.Abort()
        agent = build_agent(s, s.db_path, extra_prompt=prompt, audit=True)
        result = agent.run_sync(
            f"Audit {sample} cards using review_sample, fix clear defects, then report.",
            deps=Deps(db_path=s.db_path, model_name=s.litellm_model or "unknown"),
            usage_limits=UsageLimits(request_limit=60),
        )
        typer.echo(result.output)
        typer.echo(f"usage: {result.usage}")
        _report_tool_rejects(result)
