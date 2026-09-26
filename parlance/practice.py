"""Practice subsystem (ADR-0006): Gemini Live drills over the store.

Session = Live API conversation with function calls bridged to the store.
Memory persistence is sqlite; the model never owns state: each session opens
with a digest (recent summaries, recurring error patterns, targets) and
closes by journaling a summary via save_session_summary.

Typed transport now (voice mounts in P3): the user types, the model answers
with AUDIO (Live is audio-only) which we do not play — output transcription
is the text channel. Turns are disciplined: one line in, one full model turn
out, prompt again.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import types

from . import store

LIVE_MODEL = "gemini-3.8-live"

DRILL_PERSONA = """Jsi trenér polštiny pro českého studenta (začátečník+).

Otevři session otázkou, co chce dnes trénovat — nebo navrhni scénář podle
minulých chyb (digest níže). Typická scéna: "jdeš do obchodu, potřebuješ
poprosit prodavačku o…". Nech studenta mluvit polsky; když neví, řekne česky
("jak mám začít?") — odpověz česky, dej mu přesnou polskou frázi, a nech ho
opakovat, dokud to nesedí. Chyby opravuj česky, stručně, a vždy ho nech větu
zopakovat správně. Když udělá stejnou chybu dvakrát, zaznamenej ji přes
log_error. Používej get_practice_targets na začátku, když potřebuješ slovíčka
na procvičení. Odpovídej stručně (1-3 věty). Na konci (uslyšíš "konec" nebo uživatel píše /end) shrň do
save_session_summary: co se trénovalo, hlavní chyby, co příště."""


@dataclass
class PracticeTools:
    conn_path: Path
    session_id: int

    def get_practice_targets(self, n: int = 10) -> list[dict]:
        """Recent cards + words with logged errors, for drilling."""
        with closing(store.connect(self.conn_path)) as conn:
            rows = conn.execute(
                """SELECT l.lemma, c.preferred_gloss, c.payload FROM content c
                   JOIN lemmas l ON l.id = c.lemma_id
                   WHERE c.kind = 'lemma' AND l.status = 'generated'
                   ORDER BY c.id DESC LIMIT ?""",
                (n,),
            ).fetchall()
        return [
            {
                "lemma": r["lemma"],
                "gloss": r["preferred_gloss"],
                "example": json.loads(r["payload"])["examples"][0]["target"],
            }
            for r in rows
        ]

    def log_error(self, pattern: str, wrong: str, right: str, lemma: str | None = None) -> str:
        with closing(store.connect(self.conn_path)) as conn:
            store.add_practice_error(conn, self.session_id, pattern, wrong, right, lemma)
        return "logged"

    def save_session_summary(self, summary: str) -> str:
        with closing(store.connect(self.conn_path)) as conn:
            store.close_practice_session(conn, self.session_id, summary)
        return "saved"


def function_declarations() -> list[dict]:
    return [
        {
            "name": "get_practice_targets",
            "description": "Vrátí nedávná slovíčka (lemma, překlad, vzorová věta) na procvičení.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"n": {"type": "INTEGER", "description": "kolik, ~10"}},
            },
        },
        {
            "name": "log_error",
            "description": "Zaznamenej opakovanou chybu studenta: typ chyby, špatný tvar, správný tvar.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "pattern": {"type": "STRING"},
                    "wrong": {"type": "STRING"},
                    "right": {"type": "STRING"},
                    "lemma": {"type": "STRING"},
                },
                "required": ["pattern", "wrong", "right"],
            },
        },
        {
            "name": "save_session_summary",
            "description": "Na závěr session: stručné shrnutí (co se trénovalo, chyby, doporučení).",
            "parameters": {
                "type": "OBJECT",
                "properties": {"summary": {"type": "STRING"}},
                "required": ["summary"],
            },
        },
    ]


def dispatch(tools: PracticeTools, name: str, args: dict):
    return getattr(tools, name)(**args)


async def run_practice(
    settings, transcript_dir: Path | None = None, lines: list[str] | None = None
) -> dict:
    """Drill session against Gemini Live. `lines` lets scripts drive it instead of stdin."""
    from .config import get_settings  # local import keeps module light for tests

    s = settings or get_settings()
    assert s.gemini_api_key, "GEMINI_API_KEY missing"
    with closing(store.connect(s.db_path)) as conn:
        session_id = store.open_practice_session(conn, LIVE_MODEL)
        digest = store.practice_digest(conn)
    tools = PracticeTools(conn_path=s.db_path, session_id=session_id)
    transcript = (transcript_dir or s.db_path.parent / "practice") / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=s.gemini_api_key)
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=DRILL_PERSONA + f"\n\n## Digest z minulých sessions\n{digest}",
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[{"function_declarations": function_declarations()}],
    )
    stats: dict = {"session_id": session_id, "turns": 0, "tool_calls": 0, "summary": False}
    scripted = iter(lines) if lines is not None else None

    async def next_line() -> str:
        if scripted is not None:
            return next(scripted, "/end")
        return await asyncio.to_thread(input, "ty> ")

    async def consume_turn(session) -> str:
        """Consume one model turn: dispatch tools on the way, return aggregated reply text."""
        pieces: list[str] = []
        async for msg in session.receive():
            with transcript.open("a", encoding="utf-8") as f:
                f.write(msg.model_dump_json() + "\n")
            if msg.tool_call:
                calls = msg.tool_call.function_calls or []
                responses = []
                for fc in calls:
                    result = dispatch(tools, fc.name or "", dict(fc.args or {}))
                    stats["tool_calls"] += 1
                    responses.append(
                        types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result})
                    )
                    if fc.name == "save_session_summary":
                        stats["summary"] = True
                print(f"[nástroj: {', '.join(fc.name or '?' for fc in calls)}]")
                await session.send_tool_response(function_responses=responses)
                continue  # the tool response triggers more model output; keep consuming
            sc = msg.server_content
            if sc and sc.output_transcription and sc.output_transcription.text:
                pieces.append(sc.output_transcription.text)
            if sc and sc.turn_complete:
                return "".join(pieces).strip()
        return "".join(pieces).strip()

    async with client.aio.live.connect(model=LIVE_MODEL, config=cfg) as session:
        print(f"[practice #{session_id}] připojeno. Piš česky nebo polsky; '/end' pro konec.")
        try:
            while True:
                line = await next_line()
                if line.strip() == "/end":
                    break
                await session.send_realtime_input(text=line)
                reply = await asyncio.wait_for(consume_turn(session), timeout=120)
                stats["turns"] += 1
                if reply:
                    print(f"trenér> {reply}")
        except KeyboardInterrupt:
            print("\n[ukončeno uživatelem]")
        if not stats["summary"]:
            try:
                async with asyncio.timeout(60):
                    await session.send_realtime_input(
                        text="Session končí — zavolej teď save_session_summary s krátkým shrnutím."
                    )
                    await consume_turn(session)
            except TimeoutError:
                pass
    if not stats["summary"]:
        with closing(store.connect(s.db_path)) as conn:
            store.close_practice_session(conn, session_id, "")
        print("[practice] bez shrnutí, uložen jen transkript:", transcript)
    return stats
