"""sqlite3 source of truth: lemmas, cards, changes journal.

The store owns identity: a lemma exists at most once (UNIQUE), so overlap is
impossible by schema. Every mutation beyond bulk seeding is journaled into
the changes table so curation is auditable. Deck builds read from here; they
never write. Schema version lives in PRAGMA user_version.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lemmas (
    id INTEGER PRIMARY KEY,
    lemma TEXT NOT NULL UNIQUE,
    freq_rank INTEGER NOT NULL,
    freq INTEGER NOT NULL DEFAULT 0,
    forms TEXT NOT NULL DEFAULT '[]',
    pos TEXT,
    cognate_class TEXT,
    preferred_gloss TEXT,
    variants TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'worklist',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    lemma_id INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and create/migrate) the database at db_path.

    Args:
        db_path: File location; parent directory is created if missing.

    Returns:
        Connection with Row factory; tables guaranteed present.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


def add_lemmas(conn: sqlite3.Connection, rows: Iterable[dict]) -> tuple[int, int]:
    """Seed worklist rows, skipping lemmas that already exist.

    Args:
        conn: Open connection from connect().
        rows: Dicts with keys lemma, freq_rank, freq, forms (list of str).

    Returns:
        (inserted, skipped) counts; skipped means the lemma was already known.
    """
    now = _now()
    inserted = skipped = 0
    for r in rows:
        lemma = r.get("lemma")
        if not isinstance(lemma, str) or not lemma:
            raise ValueError(f"lemma row missing a lemma string: {r!r}")
        cur = conn.execute(
            """INSERT OR IGNORE INTO lemmas
               (lemma, freq_rank, freq, forms, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                lemma,
                r["freq_rank"],
                r["freq"],
                json.dumps(r["forms"], ensure_ascii=False),
                now,
                now,
            ),
        )
        inserted += cur.rowcount
        skipped += 1 - cur.rowcount
    if inserted:
        journal(conn, "worklist_seed", None, {"inserted": inserted, "skipped": skipped})
    conn.commit()
    return inserted, skipped


VALID_STATUSES = ("worklist", "generated", "retired")


def set_status(conn: sqlite3.Connection, lemma: str, status: str) -> bool:
    """Transition a lemma's status (worklist | generated | retired).

    A transition to the current status is a no-op (not journaled).

    Returns:
        True when the lemma exists, regardless of whether it changed.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {VALID_STATUSES}")
    row = conn.execute("SELECT id, status FROM lemmas WHERE lemma=?", (lemma,)).fetchone()
    if row is None:
        return False
    if row["status"] == status:
        return True
    conn.execute("UPDATE lemmas SET status=?, updated_at=? WHERE id=?", (status, _now(), row["id"]))
    journal(conn, f"status:{status}", row["id"], {"from": row["status"]})
    conn.commit()
    return True


def list_lemmas(
    conn: sqlite3.Connection, status: str = "worklist", limit: int = 30
) -> list[sqlite3.Row]:
    """Return lemmas in frequency-rank order filtered by status."""
    return list(
        conn.execute(
            "SELECT freq_rank, lemma, freq, forms, status FROM lemmas WHERE status=? ORDER BY freq_rank LIMIT ?",
            (status, limit),
        )
    )


def journal(conn: sqlite3.Connection, action: str, lemma_id: int | None, detail: dict) -> None:
    """Append one entry to the mutation journal (committed by caller's commit)."""
    conn.execute(
        "INSERT INTO changes (ts, action, lemma_id, detail) VALUES (?, ?, ?, ?)",
        (_now(), action, lemma_id, json.dumps(detail, ensure_ascii=False)),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Count lemmas grouped by status."""
    return {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) n FROM lemmas GROUP BY status")
    }
