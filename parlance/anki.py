"""AnkiConnect client: today's reviewed words from desktop Anki.

Reviews happen on the phone; AnkiWeb relays them; desktop Anki (running the
AnkiConnect add-on, listening on localhost) is the only readable end of that
chain -- AnkiDroid and AnkiWeb expose no API of their own. This module only
reads; it never writes to Anki.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.client import HTTPException

# genanki model names (deck/model.py). Scoping the search to them keeps
# non-parlance decks out of practice targets in any language pair.
NOTE_MODELS = ("parlance-lemma", "parlance-lemma-cognate")

# AnkiWeb sync: near-instant when up to date, but the first sync after
# login can take minutes. The query itself is always local and fast.
SYNC_TIMEOUT = 300.0
QUERY_TIMEOUT = 15.0


class AnkiConnectError(RuntimeError):
    """Operator-readable failure; the message says what to do, not what broke."""


def _request(url: str, action: str, params: dict, timeout: float):
    payload = json.dumps({"action": action, "params": params, "version": 6}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    # urllib.error.URLError covers the refused/unreachable open; OSError and
    # HTTPException cover failures mid-response (resets, remote hangups,
    # truncated bodies)
    except (urllib.error.URLError, OSError, HTTPException):
        raise AnkiConnectError(
            f"AnkiConnect unreachable at {url} -- is Anki desktop running "
            "with the AnkiConnect add-on (code 2055492159)?"
        ) from None
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise AnkiConnectError(f"AnkiConnect at {url} returned invalid JSON: {e}") from None
    if body.get("error") is not None:
        msg = str(body["error"])
        if "auth not configured" in msg:
            raise AnkiConnectError(
                "desktop Anki is not logged into AnkiWeb: open Anki, press the "
                "Sync button and sign in with the account the phone uses"
            )
        raise AnkiConnectError(f"AnkiConnect {action} failed: {msg}")
    return body.get("result")


def reviewed_today_query() -> str:
    """Anki search for parlance notes answered today (rated:1 = Anki day)."""
    models = " or ".join(f'note:"{m}"' for m in NOTE_MODELS)
    return f"({models}) rated:1"


def fetch_reviewed_today(url: str, sync_first: bool = True) -> list[str]:
    """Lemmas of parlance cards answered today.

    Args:
        url: AnkiConnect endpoint, e.g. http://127.0.0.1:8765.
        sync_first: sync AnkiWeb into desktop Anki first, so phone reviews
            made since the last desktop sync are included.

    Returns:
        Lemma strings in Anki's return order.

    Raises:
        AnkiConnectError: Anki closed, the add-on missing or refusing
            requests, or the desktop not signed into AnkiWeb.
    """
    if sync_first:
        _request(url, "sync", {}, SYNC_TIMEOUT)
    notes = _request(url, "notesInfo", {"query": reviewed_today_query()}, QUERY_TIMEOUT)
    lemmas: list[str] = []
    for note in notes or []:
        # fields are keyed by name with {value, order}; the first field is
        # never assumed -- Anki does not guarantee key order
        field = (note.get("fields") or {}).get("Lemma") or {}
        value = str(field.get("value") or "").strip()
        if value:
            lemmas.append(value)
    return lemmas
