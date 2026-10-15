"""typer app: single entry point; every command delegates to a subsystem module."""

import json
from contextlib import closing
from pathlib import Path
from typing import Annotated

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
chats_app = typer.Typer(no_args_is_help=True, help="Import personal chats into a private wordlist.")
app.add_typer(chats_app, name="chats")
profile_app = typer.Typer(no_args_is_help=True, help="Show and edit the language-pair profile.")
app.add_typer(profile_app, name="profile")


@worklist_app.command("build")
def worklist_build(scan_forms: int | None = None):
    """Fetch the frequency list if needed and seed the lemma worklist."""
    s = get_settings()
    total, inserted, skipped = worklist.run(
        db_path=s.db_path,
        source_path=s.wordlist_path,
        scan_limit=scan_forms or s.worklist_scan_forms,
        lang=s.target_lang,
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


def _content_count(conn, kind: str = "lemma") -> int:
    return conn.execute("SELECT COUNT(*) n FROM content WHERE kind=?", (kind,)).fetchone()["n"]


def _run_card_changes(conn, kind: str, since_id: int) -> tuple[int, int]:
    """Cards created vs edited after a journal row id (content:upsert entries).

    Returns:
        (new_cards, edited_cards) for the given deck kind.
    """
    new = edited = 0
    for r in conn.execute(
        "SELECT detail FROM changes WHERE action='content:upsert' AND id > ?", (since_id,)
    ):
        detail = json.loads(r["detail"])
        if detail.get("kind") != kind:
            continue
        if detail.get("created"):
            new += 1
        else:
            edited += 1
    return new, edited


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
        result = agent.run_sync(prompt, deps=Deps(db_path=s.db_path, model_name=s.gemini_model))
        typer.echo(result.output)
        typer.echo(f"usage: {result.usage}")


@app.command("generate")
def generate(
    count: int = 20,
    batch: int = 15,
    max_requests: int = 60,
    random_sample: bool = False,
    prompt: str = "",
    deck: str = typer.Option("frequency", "--deck", help="frequency | personal."),
    words: int = typer.Option(
        0, "--words", help="Personal deck only: pre-pass just the N most-used chat words (0 = all)."
    ),
):
    """Translate new vocabulary cards via the agent restart-loop.

    random_sample polls a random corpus slice instead of the frequency head.
    prompt appends a user persona/task note to the agent's instructions for
    this run.
    deck personal: pending chat words are pre-passed to target lemmas first,
    then cards are written into the personal deck. words caps the pre-pass
    to the most-used chat words.
    """
    if deck not in ("frequency", "personal"):
        typer.echo(f"unknown deck {deck!r}: expected frequency or personal")
        raise typer.Abort()
    if words and deck != "personal":
        typer.echo("--words applies to the personal deck only")
        raise typer.Abort()
    s = get_settings()
    personal = deck == "personal"
    kind = "personal" if personal else "lemma"
    submitted = 0
    skip_clause = (
        " Cognate skipping is ENABLED for this run: for lemmas whose similar_known word "
        "means the SAME thing in your judgment, set_statuses them skipped with a reason "
        "instead of writing cards. Different meaning = false friend = full card with a note."
        if s.skip_free_cognates
        else ""
    )
    with closing(store.connect(s.db_path)) as conn:
        if personal:
            from .chats.translate import translate_pending

            typer.echo(f"pre-pass: {translate_pending(conn, s, words=words or None)}")
        agent = build_agent(s, s.db_path, extra_prompt=prompt)
        while submitted < count:
            # journal watermark: monotonic, immune to same-second timestamp ties
            run_start_id = conn.execute("SELECT COALESCE(MAX(id), 0) i FROM changes").fetchone()[
                "i"
            ]
            remaining = min(count - submitted, batch)
            try:
                result = agent.run_sync(
                    f"Submit {remaining} new vocabulary cards following your "
                    f"instructions, then report." + skip_clause,
                    deps=Deps(
                        db_path=s.db_path,
                        model_name=s.gemini_model,
                        randomize=random_sample,
                        personal=personal,
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
            # new cards drive the loop; edits are reported for visibility only
            new, edited = _run_card_changes(conn, kind, run_start_id)
            submitted += new
            typer.echo(f"run: new={new} edited={edited} total={submitted}/{count} usage={usage}")
            if report:
                typer.echo(f"  agent: {report[:200]}")
            if new == 0:
                typer.echo("run submitted nothing new; stopping instead of burning budget.")
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
    """Build hint pairs from kaikki dumps and the known-language validation wordlist (downloads if missing)."""
    from pathlib import Path

    from .lexicon import build_pairs, build_words

    s = get_settings()
    src = Path(extract_dir) if extract_dir else None
    with closing(store.connect(s.db_path)) as conn:
        stats = build_pairs(conn, s.known_lang, s.target_lang, s.db_path.parent / "lexicon", src)
        stats["words"] = build_words(conn, s.known_lang, s.db_path.parent / "lexicon")
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


@app.command("practice-log")
def practice_log(
    session_id: int | None = typer.Argument(None, help="Session id; latest when omitted."),
):
    """Replay a practice session: turns (you/coach), logged errors, summary."""
    from .practice import transcript_turns

    s = get_settings()
    with closing(store.connect(s.db_path)) as conn:
        sess = store.get_practice_session(conn, session_id)
        if not sess:
            typer.echo("no practice sessions yet")
            return
        errors = store.session_errors(conn, sess["id"])
    sid = sess["id"]
    typer.echo(f"=== practice #{sid} | {sess['started_at']} | model {sess['model']} ===")
    transcript = s.db_path.parent / "practice" / f"{sid}.jsonl"
    for who, text in transcript_turns(transcript):
        typer.echo(f"{who:>5}> {text}")
    if errors:
        typer.echo("--- logged errors ---")
        for e in errors:
            lemma = f" [{e['lemma']}]" if e["lemma"] else ""
            typer.echo(f"  {e['pattern']}: {e['wrong']} -> {e['right']}{lemma}")
    if sess["summary"]:
        typer.echo(f"--- summary ---\n  {sess['summary']}")


@app.command("anki-sync")
def anki_sync():
    """Pull today's Anki reviews into the store (via AnkiConnect on desktop Anki)."""
    from datetime import date

    from . import anki

    s = get_settings()
    try:
        lemmas = anki.fetch_reviewed_today(s.anki_connect_url)
    except anki.AnkiConnectError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1)
    with closing(store.connect(s.db_path)) as conn:
        # Anki "today" is the local calendar day
        stored, unknown = store.record_anki_reviews(
            conn,
            lemmas,
            date.today().isoformat(),  # noqa: DTZ011
        )
    typer.echo(
        f"reviewed today: {len(lemmas)} notes, {stored} stored, {len(unknown)} unknown to the store"
    )
    if unknown:
        typer.echo("not in store: " + ", ".join(unknown))


@chats_app.command("import")
def chats_import(
    paths: Annotated[list[Path], typer.Argument(help="Extracted Meta export directories.")],
    lang: Annotated[
        list[str] | None,
        typer.Option("--lang", help="Gate languages, repeatable (default: KNOWN_LANG from .env)."),
    ] = None,
):
    """Accumulate the owner's own words from chat exports (idempotent, multi-export).

    Each --lang runs the same export through that language's public-word
    gate, so one set of chats yields several source-language wordlists; the
    pre-pass maps all of them to the target language later.
    """
    from .chats import meta

    s = get_settings()
    langs = lang or [s.known_lang]
    typer.echo(
        "WARNING: chat exports contain your full personal history. Do not read\n"
        "through these files -- let parlance do it: it keeps only single common\n"
        "public words and never stores sentences."
    )
    with closing(store.connect(s.db_path)) as conn:
        for lg in langs:
            stats = meta.import_chats(
                conn, [Path(p) for p in paths], lg, s.data_dir / "wordlist" / f"{lg}_50k.txt"
            )
            typer.echo(f"[{lg}] " + " ".join(f"{k}={v}" for k, v in stats.items()))
            typer.echo(f"[{lg}] {store.personal_words_stats(conn, lg)}")


@chats_app.command("templates")
def chats_templates(
    paths: Annotated[list[Path], typer.Argument(help="Extracted Meta export directories.")],
    lang: Annotated[
        str, typer.Option("--lang", help="Gate language (default: KNOWN_LANG from .env).")
    ] = "",
    floor: int = typer.Option(20, help="Minimum repeats for a content to be proposed."),
):
    """Propose repeated message contents for the stop list: robot UI templates.

    Meta writes localized template strings as message content; they repeat
    identically, so exact-content repetition is the signal. Picks are stored
    and every following chats import skips them. Run this on each new export
    before importing it.
    """
    from .chats import meta

    s = get_settings()
    lg = lang or s.known_lang
    with closing(store.connect(s.db_path)) as conn:
        candidates = meta.template_candidates(
            conn,
            [Path(p) for p in paths],
            lg,
            s.data_dir / "wordlist" / f"{lg}_50k.txt",
            floor,
        )
        existing = store.chat_stops(conn)
        if candidates:
            typer.echo("repeated contents (count | text):")
            for i, (c, n) in enumerate(candidates, 1):
                typer.echo(f"{i:>3}. {n:>6}  {c}")
            pick = typer.prompt("Exclude which numbers? comma-separated, empty = none", default="")
            chosen: list[str] = []
            for tok in pick.replace(",", " ").split():
                idx = int(tok)
                if not 1 <= idx <= len(candidates):
                    raise typer.BadParameter(f"{idx} is not a listed number")
                chosen.append(candidates[idx - 1][0])
            stops = list(dict.fromkeys(existing + chosen))
            if stops != existing:
                store.save_chat_stops(conn, stops)
            typer.echo(f"stop list: {len(stops)} contents")
        else:
            typer.echo(
                f"no candidates (floor={floor}); stop list unchanged: {len(existing)} contents"
            )
        for c in store.chat_stops(conn):
            typer.echo(f"  stopped: {c}")


@chats_app.command("list")
def chats_list(
    limit: int = typer.Option(50, help="Rows to show (0 = all)."),
    status: str = "pending",
    lang: str = "",
):
    """Show personal words in chat-frequency order."""
    s = get_settings()
    lang = lang or s.known_lang
    with closing(store.connect(s.db_path)) as conn:
        rows = store.list_personal_words(conn, lang, status=status, limit=limit or -1)
        reasons = store.personal_skip_reasons(conn, lang) if status == "skipped" else {}
    typer.echo(f"{'count':>6}  lemma")
    for r in rows:
        typer.echo(
            f"{r['count']:>6}  {r['lemma']}"
            + (f"  [{reasons.get(r['lemma'], '')}]" if reasons else "")
        )
    if not rows:
        typer.echo(f"(no personal words with status={status})")


@profile_app.command("show")
def profile_show():
    """Print the stored pair profile (what seeds sentence and gloss style)."""
    from . import profile as profile_mod

    s = get_settings()
    with closing(store.connect(s.db_path)) as conn:
        typer.echo(profile_mod.show(conn))


@profile_app.command("edit")
def profile_edit():
    """Edit the profile in $EDITOR; JSON and fields are validated before saving."""
    from . import profile as profile_mod

    s = get_settings()
    with closing(store.connect(s.db_path)) as conn:
        try:
            typer.echo(profile_mod.edit(conn))
        except ValueError as e:
            typer.echo(f"profile rejected: {e}")
            raise typer.Abort()


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
            deps=Deps(db_path=s.db_path, model_name=s.gemini_model, audit=True),
            usage_limits=UsageLimits(request_limit=60),
        )
        typer.echo(result.output)
        typer.echo(f"usage: {result.usage}")
        _report_tool_rejects(result)
