"""Practice subsystem: Gemini Live drills over the store.

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
from .agent import LANG_NAMES

PROMPT_PATH = Path(__file__).parent / "prompts" / "practice.md"


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
            "description": "Recent vocabulary cards (lemma, gloss, example sentence) to drill.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"n": {"type": "INTEGER", "description": "how many, ~10"}},
            },
        },
        {
            "name": "log_error",
            "description": "Record a repeated learner mistake: error pattern, wrong form, correct form.",
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
            "description": "End of session: short summary (what was trained, mistakes, recommendations).",
            "parameters": {
                "type": "OBJECT",
                "properties": {"summary": {"type": "STRING"}},
                "required": ["summary"],
            },
        },
    ]


def transcript_turns(path: Path) -> list[tuple[str, str]]:
    """Aggregate consecutive transcription chunks from a session transcript into (who, text) turns."""
    turns: list[tuple[str, str]] = []
    cur_who: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal cur_who
        if buf and cur_who:
            turns.append((cur_who, "".join(buf).strip()))
        buf.clear()

    if not path.exists():
        return turns
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            sc = json.loads(line).get("server_content") or {}
        except json.JSONDecodeError:
            continue
        it = (sc.get("input_transcription") or {}).get("text")
        ot = (sc.get("output_transcription") or {}).get("text")
        who = "you" if it else ("coach" if ot else None)
        if who is None:
            continue
        if who != cur_who:
            flush()
            cur_who = who
        buf.append(str(it or ot or ""))
    flush()
    return turns


def dispatch(tools: PracticeTools, name: str, args: dict):
    return getattr(tools, name)(**args)


def _connect_kwargs(s, instructions: str) -> dict:
    from google.genai import types

    return {
        "model": s.gemini_live_model,
        "config": types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=instructions,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY"
            ),
            tools=[{"function_declarations": function_declarations()}],
        ),
    }


def _handle_tool_call(tools: PracticeTools, tool_call, stats: dict) -> list:
    from google.genai import types

    calls = tool_call.function_calls or []
    responses = []
    for fc in calls:
        result = dispatch(tools, fc.name or "", dict(fc.args or {}))
        stats["tool_calls"] += 1
        responses.append(
            types.FunctionResponse(name=fc.name, id=fc.id, response={"result": result})
        )
        if fc.name == "save_session_summary":
            stats["summary"] = True
    print(f"[tool: {', '.join(fc.name or '?' for fc in calls)}]")
    return responses


def _open(settings_or_none, transcript_dir: Path | None):
    from .agent import LANG_NAMES
    from .config import get_settings

    s = settings_or_none or get_settings()
    assert s.gemini_api_key, "GEMINI_API_KEY missing"
    with closing(store.connect(s.db_path)) as conn:
        session_id = store.open_practice_session(conn, s.gemini_live_model)
        digest = store.practice_digest(conn)
    instructions = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{KNOWN}", LANG_NAMES.get(s.known_lang, s.known_lang))
        .replace("{TARGET}", LANG_NAMES.get(s.target_lang, s.target_lang))
        .replace("{DIGEST}", digest)
    )
    tools = PracticeTools(conn_path=s.db_path, session_id=session_id)
    transcript = (transcript_dir or s.db_path.parent / "practice") / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    return s, session_id, instructions, tools, transcript


async def run_practice_voice(settings, transcript_dir: Path | None = None) -> dict:
    """Hands-free voice drill (P3): continuous mic, auto-VAD, barge-in, Ctrl+C to end."""
    import sounddevice as sd
    from google import genai

    s, session_id, instructions, tools, transcript = _open(settings, transcript_dir)
    stats: dict = {"session_id": session_id, "turns": 0, "tool_calls": 0, "summary": False}
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
    out_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

    def on_mic(indata, frames, time_info, status):
        if not mic_q.full():
            loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

    async def pump_mic(session):
        while True:
            chunk = await mic_q.get()
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )

    def player():
        with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", blocksize=2048) as st:
            while True:
                chunk = asyncio.run_coroutine_threadsafe(out_q.get(), loop).result()
                st.write(chunk)

    # startup mic meter: 3 s during which the user is asked to speak, so a dead input shows immediately
    def mic_peak() -> int:
        import time

        chunks: list[bytes] = []
        with sd.RawInputStream(
            samplerate=16000,
            channels=1,
            dtype="int16",
            blocksize=1600,
            callback=lambda d, f, t, s_: chunks.append(bytes(d)),
        ):
            time.sleep(3.0)
        peak = 0
        for c in chunks:
            for i in range(0, len(c) - 1, 2):
                peak = max(peak, abs(int.from_bytes(c[i : i + 2], "little", signed=True)))
        return peak

    print("[mic check: say something for 3 seconds...]", flush=True)
    peak = await asyncio.to_thread(mic_peak)
    status = "ok" if peak > 500 else "SUSPICIOUSLY QUIET — check your input device"
    print(f"[mic level {peak} — {status}]")

    client = genai.Client(api_key=s.gemini_api_key)
    async with client.aio.live.connect(**_connect_kwargs(s, instructions)) as session:
        print(f"[practice #{session_id}] listening. Speak; Ctrl+C to finish.")

        async def recv():
            # session.receive() is a PER-TURN stream: it ends at turn_complete
            # and must be called again for the next turn (official example pattern).
            you: list[str] = []
            coach: list[str] = []
            while True:
                async for msg in session.receive():
                    with transcript.open("a", encoding="utf-8") as f:
                        f.write(msg.model_dump_json() + "\n")
                    if msg.tool_call:
                        responses = _handle_tool_call(tools, msg.tool_call, stats)
                        await session.send_tool_response(function_responses=responses)
                        continue
                    sc = msg.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        while not out_q.empty():
                            out_q.get_nowait()
                        print("[interrupted]")
                    if sc.input_transcription and sc.input_transcription.text:
                        you.append(sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        coach.append(sc.output_transcription.text)
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                out_q.put_nowait(part.inline_data.data)
                    if sc.turn_complete:
                        stats["turns"] += 1
                        u, c = "".join(you).strip(), "".join(coach).strip()
                        if u:
                            print(f"you> {u}")
                        if c:
                            print(f"coach> {c}")
                        you.clear()
                        coach.clear()

        with sd.RawInputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=2048, callback=on_mic
        ):
            pump = asyncio.create_task(pump_mic(session))
            play = asyncio.create_task(asyncio.to_thread(player))
            listen = asyncio.create_task(recv())
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                for t in (pump, listen):
                    t.cancel()
                play.cancel()  # may leave the audio thread mid-write; harmless at teardown
        if not stats["summary"]:
            try:
                async with asyncio.timeout(60):
                    await session.send_realtime_input(
                        text="The session is ending — call save_session_summary now with a short summary."
                    )
                    async for msg in session.receive():
                        if msg.tool_call:
                            responses = _handle_tool_call(tools, msg.tool_call, stats)
                            await session.send_tool_response(function_responses=responses)
                        if msg.server_content and msg.server_content.turn_complete:
                            break
            except TimeoutError:
                pass
    if not stats["summary"]:
        with closing(store.connect(s.db_path)) as conn:
            store.close_practice_session(conn, session_id, "")
        print("[practice] no summary saved, transcript only:", transcript)
    return stats


async def run_practice(
    settings, transcript_dir: Path | None = None, lines: list[str] | None = None
) -> dict:
    """Drill session against Gemini Live. lines lets scripts drive it instead of stdin."""
    s, session_id, instructions, tools, transcript = _open(settings, transcript_dir)
    stats: dict = {"session_id": session_id, "turns": 0, "tool_calls": 0, "summary": False}
    client = genai.Client(api_key=s.gemini_api_key)
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
                responses = _handle_tool_call(tools, msg.tool_call, stats)
                await session.send_tool_response(function_responses=responses)
                continue  # the tool response triggers more model output; keep consuming
            sc = msg.server_content
            if sc and sc.output_transcription and sc.output_transcription.text:
                pieces.append(sc.output_transcription.text)
            if sc and sc.turn_complete:
                return "".join(pieces).strip()
        return "".join(pieces).strip()

    async with client.aio.live.connect(**_connect_kwargs(s, instructions)) as session:
        known = LANG_NAMES.get(s.known_lang, s.known_lang)
        target = LANG_NAMES.get(s.target_lang, s.target_lang)
        print(
            f"[practice #{session_id}] connected. Type {known} for help or {target} to drill; /end to quit."
        )
        try:
            while True:
                line = await next_line()
                if line.strip() == "/end":
                    break
                await session.send_realtime_input(text=line)
                reply = await asyncio.wait_for(consume_turn(session), timeout=120)
                stats["turns"] += 1
                if reply:
                    print(f"coach> {reply}")
        except KeyboardInterrupt:
            print("\n[ended by user]")
        if not stats["summary"]:
            try:
                async with asyncio.timeout(60):
                    await session.send_realtime_input(
                        text="The session is ending — call save_session_summary now with a short summary."
                    )
                    await consume_turn(session)
            except TimeoutError:
                pass
    if not stats["summary"]:
        with closing(store.connect(s.db_path)) as conn:
            store.close_practice_session(conn, session_id, "")
        print("[practice] no summary saved, transcript only:", transcript)
    return stats
