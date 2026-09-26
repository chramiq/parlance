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

SCHEMA_VERSION = 2

# Dev build: no cross-version migrations. The schema below is the only truth;
# on version mismatch connect() refuses and tells you to rebuild. Real
# migrations become a feature the day curated cards are irreplaceable.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lemmas (
    id INTEGER PRIMARY KEY,
    lemma TEXT NOT NULL UNIQUE,
    freq_rank INTEGER NOT NULL,
    freq INTEGER NOT NULL DEFAULT 0,
    forms TEXT NOT NULL DEFAULT '[]',
    pos TEXT,
    cognate_class TEXT,
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
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY,
    lemma_id INTEGER NOT NULL REFERENCES lemmas(id),
    kind TEXT NOT NULL DEFAULT 'lemma',
    preferred_gloss TEXT NOT NULL,
    payload TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (lemma_id, kind)
);
CREATE INDEX IF NOT EXISTS content_gloss ON content(preferred_gloss);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Additive-only tables: safe on existing databases, so executed on EVERY
# connect (all statements must stay IF NOT EXISTS idempotent).
_ADDITIVE = """
CREATE TABLE IF NOT EXISTS lexicon_pairs (
    target_lang TEXT NOT NULL,
    target TEXT NOT NULL,
    known_lang TEXT NOT NULL,
    known TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (target_lang, target, known_lang, known)
);
CREATE INDEX IF NOT EXISTS lexicon_pairs_lookup ON lexicon_pairs(target_lang, target, known_lang);
CREATE TABLE IF NOT EXISTS practice_sessions (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    model TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS practice_errors (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES practice_sessions(id),
    ts TEXT NOT NULL,
    lemma TEXT,
    pattern TEXT NOT NULL,
    wrong TEXT NOT NULL,
    right TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS practice_errors_session ON practice_errors(session_id);
"""


def now() -> str:
    """Current UTC timestamp as an ISO string, second precision, for row stamps."""
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
    if version == 0:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    elif version != SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: db={version}, code={SCHEMA_VERSION}. "
            f"Dev build, no migrations: delete {db_path} and rebuild."
        )
    conn.executescript(_ADDITIVE)
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
    ts = now()
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
                ts,
                ts,
            ),
        )
        inserted += cur.rowcount
        skipped += 1 - cur.rowcount
    if inserted:
        journal(conn, "worklist_seed", None, {"inserted": inserted, "skipped": skipped})
    conn.commit()
    return inserted, skipped


VALID_STATUSES = ("worklist", "generated", "retired", "skipped")


def set_status(conn: sqlite3.Connection, lemma: str, status: str, reason: str = "") -> bool:
    """Transition a lemma's status (worklist | generated | retired | skipped).

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
    conn.execute("UPDATE lemmas SET status=?, updated_at=? WHERE id=?", (status, now(), row["id"]))
    journal(conn, f"status:{status}", row["id"], {"from": row["status"], "reason": reason})
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
        (now(), action, lemma_id, json.dumps(detail, ensure_ascii=False)),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Count lemmas grouped by status."""
    return {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) n FROM lemmas GROUP BY status")
    }


def upsert_content(
    conn: sqlite3.Connection,
    lemma_id: int,
    kind: str,
    preferred_gloss: str,
    payload: str,
    model: str,
) -> bool:
    """Insert or replace the card content for one (lemma, kind).

    Returns:
        True when a new row was created, False on update.
    """
    row = conn.execute(
        "SELECT id, payload FROM content WHERE lemma_id=? AND kind=?", (lemma_id, kind)
    ).fetchone()
    ts = now()
    if row is None:
        conn.execute(
            "INSERT INTO content (lemma_id, kind, preferred_gloss, payload, model, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lemma_id, kind, preferred_gloss, payload, model, ts, ts),
        )
        created = True
    else:
        conn.execute(
            "UPDATE content SET preferred_gloss=?, payload=?, model=?, updated_at=? WHERE id=?",
            (preferred_gloss, payload, model, ts, row["id"]),
        )
        created = False
    journal(conn, "content:upsert", lemma_id, {"kind": kind, "created": created})
    conn.commit()
    return created


def get_content(conn: sqlite3.Connection, lemma: str, kind: str = "lemma") -> sqlite3.Row | None:
    """Fetch the content row for one lemma text, or None."""
    return conn.execute(
        """SELECT c.* FROM content c JOIN lemmas l ON l.id = c.lemma_id
           WHERE l.lemma=? AND c.kind=?""",
        (lemma, kind),
    ).fetchone()


def poll_untranslated(
    conn: sqlite3.Connection, n: int, randomize: bool = False
) -> list[sqlite3.Row]:
    """Up to n worklist lemmas, with corpus forms.

    Default order is frequency rank; randomize gives a random sample across
    the corpus (architecture stress-testing vs full spectrum).
    """
    order = "RANDOM()" if randomize else "freq_rank"
    return list(
        conn.execute(
            f"SELECT lemma, freq_rank, freq, forms, cognate_class FROM lemmas"
            f" WHERE status='worklist' ORDER BY {order} LIMIT ?",
            (n,),
        )
    )


def replace_pairs(
    conn: sqlite3.Connection, target_lang: str, known_lang: str, rows: list[tuple]
) -> int:
    """Replace all lexicon hint rows for a language pair. rows: (target_field, target, known_field, known, sources_json)."""
    conn.execute(
        "DELETE FROM lexicon_pairs WHERE target_lang = ? AND known_lang = ?",
        (target_lang, known_lang),
    )
    conn.executemany(
        "INSERT INTO lexicon_pairs (target_lang, target, known_lang, known, sources)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def merge_pairs(
    conn: sqlite3.Connection, target_lang: str, known_lang: str, rows: list[tuple[str, str, str]]
) -> int:
    """Upsert hint rows, unioning the sources tag. rows: (target, known, source_tag). Returns rows touched."""
    touched = 0
    for target, known, tag in rows:
        cur = conn.execute(
            "SELECT sources FROM lexicon_pairs WHERE target_lang=? AND target=? AND known_lang=? AND known=?",
            (target_lang, target, known_lang, known),
        ).fetchone()
        if cur:
            srcs = set(json.loads(cur["sources"])) | {tag}
            conn.execute(
                "UPDATE lexicon_pairs SET sources=? WHERE target_lang=? AND target=? AND known_lang=? AND known=?",
                (
                    json.dumps(sorted(srcs), ensure_ascii=False),
                    target_lang,
                    target,
                    known_lang,
                    known,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO lexicon_pairs (target_lang, target, known_lang, known, sources) VALUES (?,?,?,?,?)",
                (target_lang, target, known_lang, known, json.dumps([tag], ensure_ascii=False)),
            )
        touched += 1
    conn.commit()
    return touched


def pair_suggestions(
    conn: sqlite3.Connection, lemma: str, known_lang: str, target_lang: str, limit: int = 8
) -> list[str]:
    """Grounded KNOWN-word candidates for a TARGET lemma, shortest first."""
    return [
        r["known"]
        for r in conn.execute(
            "SELECT known FROM lexicon_pairs"
            " WHERE target_lang = ? AND known_lang = ? AND target = ?"
            " ORDER BY (sources LIKE '%wikt%') DESC, LENGTH(known) LIMIT ?",
            (target_lang, known_lang, lemma, limit),
        )
    ]


def open_practice_session(conn: sqlite3.Connection, model: str, scenario: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO practice_sessions (started_at, model, scenario) VALUES (?, ?, ?)",
        (now(), model, scenario),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def close_practice_session(conn: sqlite3.Connection, session_id: int, summary: str) -> None:
    conn.execute(
        "UPDATE practice_sessions SET ended_at = ?, summary = ? WHERE id = ?",
        (now(), summary, session_id),
    )
    conn.commit()


def add_practice_error(
    conn: sqlite3.Connection,
    session_id: int,
    pattern: str,
    wrong: str,
    right: str,
    lemma: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO practice_errors (session_id, ts, lemma, pattern, wrong, right) VALUES (?,?,?,?,?,?)",
        (session_id, now(), lemma, pattern, wrong, right),
    )
    conn.commit()


def practice_digest(conn: sqlite3.Connection, sessions: int = 3, errors: int = 10) -> str:
    """Text digest for session-open injection: recent summaries + recurring error patterns."""
    lines = []
    for r in conn.execute(
        "SELECT summary FROM practice_sessions WHERE summary != '' ORDER BY id DESC LIMIT ?",
        (sessions,),
    ):
        lines.append(f"- last session: {r['summary'][:200]}")
    for r in conn.execute(
        "SELECT pattern, COUNT(*) n FROM practice_errors GROUP BY pattern ORDER BY n DESC LIMIT 5"
    ):
        lines.append(f"- recurring error pattern: {r['pattern']} ({r['n']}x)")
    for r in conn.execute(
        "SELECT lemma, wrong, right FROM practice_errors ORDER BY id DESC LIMIT ?", (errors,)
    ):
        lines.append(
            f"- recent error: {r['wrong']} -> {r['right']}"
            + (f" [{r['lemma']}]" if r["lemma"] else "")
        )
    return "\n".join(lines) if lines else "(no practice history yet)"


def sample_generated(conn: sqlite3.Connection, n: int) -> list[dict]:
    """Up to n random generated cards with full payloads, for audit passes."""
    rows = conn.execute(
        """SELECT l.lemma, l.freq_rank, l.cognate_class, c.preferred_gloss, c.payload, c.model
           FROM content c JOIN lemmas l ON l.id = c.lemma_id
           WHERE c.kind = 'lemma' AND l.status = 'generated'
           ORDER BY RANDOM() LIMIT ?""",
        (n,),
    )
    return [
        {
            "lemma": r["lemma"],
            "freq_rank": r["freq_rank"],
            "cognate_class": r["cognate_class"],
            "preferred_gloss": r["preferred_gloss"],
            "model": r["model"],
            "payload": json.loads(r["payload"]),
        }
        for r in rows
    ]


LOOKUP_SCOPES = ("all", "lemmas", "glosses", "forms")


def find_lexicon(
    conn: sqlite3.Connection, text: str, where: str = "all", limit: int = 20
) -> list[dict]:
    """Search the vocabulary dictionary for a substring.

    Scopes: lemmas (lemma text), glosses (submitted preferred glosses),
    forms (corpus surface forms), all (any of the three). An exact lemma hit
    includes the full card payload when a card exists.
    """
    if where not in LOOKUP_SCOPES:
        raise ValueError(f"unknown scope {where!r}; expected one of {LOOKUP_SCOPES}")
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    clauses = []
    if where in ("all", "lemmas"):
        clauses.append("l.lemma LIKE ? ESCAPE '\\'")
    if where in ("all", "forms"):
        clauses.append("l.forms LIKE ? ESCAPE '\\'")
    if where in ("all", "glosses"):
        clauses.append("c.preferred_gloss LIKE ? ESCAPE '\\'")
    rows = conn.execute(
        f"""SELECT l.lemma, l.freq_rank, l.status, c.preferred_gloss, c.payload
            FROM lemmas l LEFT JOIN content c ON c.lemma_id = l.id AND c.kind = 'lemma'
            WHERE {" OR ".join(clauses)} ORDER BY l.freq_rank LIMIT ?""",
        [like] * len(clauses) + [limit],
    )
    out = []
    for r in rows:
        hit = {
            "lemma": r["lemma"],
            "freq_rank": r["freq_rank"],
            "status": r["status"],
            "preferred_gloss": r["preferred_gloss"],
        }
        if r["lemma"] == text and r["payload"]:
            hit["payload"] = json.loads(r["payload"])
        out.append(hit)
    return out


def meta_set(conn: sqlite3.Connection, key: str, value: dict) -> None:
    """Store a JSON meta document under key."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    journal(conn, f"meta:{key}", None, {})
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str) -> dict | None:
    """Fetch a meta document, or None."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None
