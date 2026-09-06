"""typer app: worklist, import-chats, profile, generate, curate, deck, practice.

Only the verbs whose phases have landed exist as commands; the rest arrive
with their phase (see TODO.md).
"""

import json

import typer

from . import store, worklist
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
