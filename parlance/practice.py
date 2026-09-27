"""Practice subsystem: Gemini Live drills over the store via pydantic-ai realtime.

The coach is a pydantic-ai agent over the store; the realtime session runs the
tool loop (declared blocking on the 3.8 models), input/output transcriptions,
context compression, and reconnects. Memory persistence is sqlite; the model
never owns state: each session opens with a digest (recent summaries,
recurring error patterns, targets) and closes by journaling a summary via
save_session_summary.

Two transports share one session: run_practice (typed in, no playback) and
run_practice_voice (mic/speaker, auto-VAD, barge-in). Gemini Live answers
with AUDIO only, so in typed mode the output transcription is the text
channel.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    SpeechPartDelta,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.realtime import (
    RealtimeResponseInterruptedEvent,
    RealtimeSessionErrorEvent,
    RealtimeSessionReconnectEvent,
    RealtimeTurnCompleteEvent,
    ReconnectPolicy,
)
from pydantic_ai.realtime.google import (
    ContextCompression,
    GoogleRealtimeModel,
    GoogleRealtimeModelSettings,
)

from . import anki, store
from .config import Settings, get_settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "practice.md"


@dataclass
class PracticeTools:
    conn_path: Path
    session_id: int

    def get_practice_targets(self, n: int = 10) -> list[dict]:
        """Words reviewed in Anki today, topped up with recent cards."""
        today = date.today().isoformat()  # noqa: DTZ011 -- Anki "today" is the local calendar day
        with closing(store.connect(self.conn_path)) as conn:
            # one lemma can carry both a 'lemma' and a 'personal' content row;
            # dedupe across both queries so drill slots are never wasted
            rows: list = []
            seen: set[str] = set()
            for r in conn.execute(
                """SELECT l.lemma, c.preferred_gloss, c.payload FROM anki_reviews r
                   JOIN lemmas l ON l.lemma = r.lemma
                   JOIN content c ON c.lemma_id = l.id AND c.kind IN ('lemma', 'personal')
                   WHERE r.reviewed_on = ? AND l.status = 'generated'
                   ORDER BY l.freq_rank LIMIT ?""",
                (today, n),
            ):
                if r["lemma"] not in seen:
                    seen.add(r["lemma"])
                    rows.append(r)
            for r in conn.execute(
                """SELECT l.lemma, c.preferred_gloss, c.payload FROM content c
                   JOIN lemmas l ON l.id = c.lemma_id
                   WHERE c.kind IN ('lemma', 'personal') AND l.status = 'generated'
                   ORDER BY c.id DESC LIMIT ?""",
                (n,),
            ):
                if len(rows) >= n:
                    break
                if r["lemma"] not in seen:
                    rows.append(r)
                    seen.add(r["lemma"])
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


@dataclass
class PracticeDeps:
    db_path: Path
    session_id: int


def build_practice_agent(instructions: str) -> Agent[PracticeDeps, str]:
    """The drill coach: instructions from practice.md, tools bridged to the store."""
    agent: Agent[PracticeDeps, str] = Agent(
        deps_type=PracticeDeps,
        instructions=instructions,
        output_type=str,
        retries=3,
    )

    @agent.tool
    def get_practice_targets(ctx: RunContext[PracticeDeps], n: int = 10) -> list[dict]:
        """Vocabulary to drill: words reviewed in Anki today first, then recent cards."""
        tools = PracticeTools(conn_path=ctx.deps.db_path, session_id=ctx.deps.session_id)
        return tools.get_practice_targets(n)

    @agent.tool
    def log_error(
        ctx: RunContext[PracticeDeps],
        pattern: str,
        wrong: str,
        right: str,
        lemma: str | None = None,
    ) -> str:
        """Record a repeated learner mistake: error pattern, wrong form, correct form."""
        tools = PracticeTools(conn_path=ctx.deps.db_path, session_id=ctx.deps.session_id)
        return tools.log_error(pattern, wrong, right, lemma)

    @agent.tool
    def save_session_summary(ctx: RunContext[PracticeDeps], summary: str) -> str:
        """End of session: short summary (what was trained, mistakes, recommendations)."""
        tools = PracticeTools(conn_path=ctx.deps.db_path, session_id=ctx.deps.session_id)
        return tools.save_session_summary(summary)

    return agent


def build_realtime_model(s: Settings) -> GoogleRealtimeModel:
    """The Gemini Live model: API key, compression for long drills, auto-reconnect."""
    if not s.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required (see .env.example)")
    settings = GoogleRealtimeModelSettings(
        google_context_compression=ContextCompression(),
        reconnect=ReconnectPolicy(),
    )
    if s.practice_voice:
        settings["google_voice"] = s.practice_voice
    return GoogleRealtimeModel(
        s.gemini_live_model,
        provider=GoogleProvider(api_key=s.gemini_api_key),
        settings=settings,
    )


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


def _log_speech(transcript: Path, who: str, text: str) -> None:
    """Append one transcript line in the raw Live JSONL shape transcript_turns reads."""
    key = "input_transcription" if who == "you" else "output_transcription"
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"server_content": {key: {"text": text}}}) + "\n")


def _open(settings_or_none, transcript_dir: Path | None):
    """Open the store session, render instructions, and build agent + deps."""
    from .agent import LANG_NAMES

    s = settings_or_none or get_settings()
    with closing(store.connect(s.db_path)) as conn:
        session_id = store.open_practice_session(conn, s.gemini_live_model)
        digest = store.practice_digest(conn)
        # pull today's Anki reviews before the session so the coach drills
        # what the learner actually reviewed; failure degrades to a warning.
        # The AnkiWeb sync can take minutes after a fresh login, so say so
        # before going quiet.
        print("[anki] pulling today's reviews...", flush=True)
        try:
            lemmas = anki.fetch_reviewed_today(s.anki_connect_url)
            stored, unknown = store.record_anki_reviews(
                # Anki "today" is the local calendar day
                conn,
                lemmas,
                date.today().isoformat(),  # noqa: DTZ011
            )
            extra = f", {len(unknown)} unknown to the store" if unknown else ""
            print(f"[anki] {stored} words reviewed today pulled{extra}", flush=True)
        except anki.AnkiConnectError as e:
            print(f"[anki] pull skipped: {e}", flush=True)
    instructions = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{KNOWN}", LANG_NAMES.get(s.known_lang, s.known_lang))
        .replace("{TARGET}", LANG_NAMES.get(s.target_lang, s.target_lang))
        .replace("{DIGEST}", digest)
    )
    agent = build_practice_agent(instructions)
    deps = PracticeDeps(db_path=s.db_path, session_id=session_id)
    transcript = (transcript_dir or s.db_path.parent / "practice") / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    return s, session_id, agent, deps, transcript


def _note_tool(ev, stats: dict) -> None:
    if isinstance(ev, FunctionToolCallEvent):
        stats["tool_calls"] += 1
        print(f"[tool: {ev.part.tool_name}]", flush=True)
    elif isinstance(ev, FunctionToolResultEvent) and ev.part.tool_name == "save_session_summary":
        stats["summary"] = True


async def _pump_replies(session, transcript: Path, stats: dict, turn_q: asyncio.Queue[str]) -> None:
    """The session's only event consumer; each completed coach turn goes on turn_q."""
    pieces: list[str] = []
    async for ev in session:
        _note_tool(ev, stats)
        if (
            isinstance(ev, PartDeltaEvent)
            and isinstance(ev.delta, SpeechPartDelta)
            and ev.delta.transcript_delta
        ):
            _log_speech(transcript, "coach", ev.delta.transcript_delta)
            pieces.append(ev.delta.transcript_delta)
        elif isinstance(ev, RealtimeSessionReconnectEvent):
            print("[session reconnected]")
        elif isinstance(ev, RealtimeSessionErrorEvent) and ev.recoverable:
            print(f"[session error: {ev.message}]")
        elif isinstance(ev, RealtimeTurnCompleteEvent):
            turn_q.put_nowait("".join(pieces).strip())
            pieces.clear()


async def _request_summary(session, stats: dict, turn_q: asyncio.Queue[str]) -> None:
    """Ask the coach to journal the session; one closing turn, 60 s of patience."""
    try:
        async with asyncio.timeout(60):
            await session.send(
                "The session is ending — call save_session_summary now with a short summary."
            )
            await turn_q.get()
    except TimeoutError:
        pass


async def _stubborn(coro):
    """Re-enter an awaitable across shutdown cancels: Ctrl+C under asyncio.run
    cancels this task once and the delivery lands on the next await."""
    while True:
        try:
            return await coro
        except asyncio.CancelledError:
            continue


def _close_unjournaled(db_path: Path, session_id: int, stats: dict, transcript: Path) -> None:
    if not stats["summary"]:
        with closing(store.connect(db_path)) as conn:
            store.close_practice_session(conn, session_id, "")
        print("[practice] no summary saved, transcript only:", transcript)


async def run_practice(
    settings, transcript_dir: Path | None = None, lines: list[str] | None = None
) -> dict:
    """Typed drill session against Gemini Live. lines lets scripts drive it instead of stdin."""
    s, session_id, agent, deps, transcript = _open(settings, transcript_dir)
    model = build_realtime_model(s)
    stats: dict = {"session_id": session_id, "turns": 0, "tool_calls": 0, "summary": False}
    scripted = iter(lines) if lines is not None else None

    async def next_line() -> str:
        if scripted is not None:
            return next(scripted, "/end")
        return await asyncio.to_thread(input, "ty> ")

    from .agent import LANG_NAMES

    turn_q: asyncio.Queue[str] = asyncio.Queue()
    async with agent.realtime(model, deps=deps).session() as session:
        known = LANG_NAMES.get(s.known_lang, s.known_lang)
        target = LANG_NAMES.get(s.target_lang, s.target_lang)
        pump = asyncio.create_task(_pump_replies(session, transcript, stats, turn_q))
        print(
            f"[practice #{session_id}] connected. Type {known} for help or {target} to drill; /end to quit."
        )
        try:
            while True:
                line = await next_line()
                if line.strip() == "/end":
                    break
                _log_speech(transcript, "you", line)
                await session.send(line)
                reply = await asyncio.wait_for(turn_q.get(), 120)
                stats["turns"] += 1
                if reply:
                    print(f"coach> {reply}")
        except KeyboardInterrupt:
            print("\n[ended by user]")
        if not stats["summary"]:
            await _request_summary(session, stats, turn_q)
        pump.cancel()
    _close_unjournaled(s.db_path, session_id, stats, transcript)
    return stats


async def run_practice_voice(settings, transcript_dir: Path | None = None) -> dict:
    """Hands-free voice drill: continuous mic, auto-VAD, barge-in, Ctrl+C to end."""
    import sounddevice as sd

    s, session_id, agent, deps, transcript = _open(settings, transcript_dir)
    model = build_realtime_model(s)
    stats: dict = {"session_id": session_id, "turns": 0, "tool_calls": 0, "summary": False}
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
    out_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

    def on_mic(indata, frames, time_info, status):
        if not mic_q.full():
            loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

    async def mic_chunks():
        while True:
            yield await mic_q.get()

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

    async with agent.realtime(model, deps=deps).session() as session:
        print(f"[practice #{session_id}] listening. Speak; Ctrl+C to finish.")

        async def run_events():
            you: list[str] = []
            coach: list[str] = []

            def flush_turn() -> None:
                stats["turns"] += 1
                u, c = "".join(you).strip(), "".join(coach).strip()
                if u:
                    print(f"you> {u}")
                if c:
                    print(f"coach> {c}")
                you.clear()
                coach.clear()

            async for ev in session:
                _note_tool(ev, stats)
                if isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, SpeechPartDelta):
                    delta = ev.delta
                    if delta.audio_chunk and not out_q.full():
                        try:
                            out_q.put_nowait(delta.audio_chunk)
                        except asyncio.QueueFull:
                            pass
                    if delta.transcript_delta:
                        who = "you" if delta.speaker == "user" else "coach"
                        _log_speech(transcript, who, delta.transcript_delta)
                        (you if who == "you" else coach).append(delta.transcript_delta)
                elif isinstance(ev, RealtimeResponseInterruptedEvent):
                    while not out_q.empty():
                        out_q.get_nowait()
                    print("[interrupted]")
                elif isinstance(ev, RealtimeSessionReconnectEvent):
                    print("[session reconnected]")
                elif isinstance(ev, RealtimeSessionErrorEvent) and ev.recoverable:
                    print(f"[session error: {ev.message}]")
                elif isinstance(ev, RealtimeTurnCompleteEvent):
                    flush_turn()
                if stats["summary"] and isinstance(ev, RealtimeTurnCompleteEvent):
                    break

        mic_rate = session.audio_input_sample_rate
        with sd.RawInputStream(
            samplerate=mic_rate, channels=1, dtype="int16", blocksize=2048, callback=on_mic
        ):
            pump = asyncio.create_task(session.send_audio(mic_chunks()))
            play = asyncio.create_task(asyncio.to_thread(player))
            listen = asyncio.create_task(run_events())
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                pump.cancel()
                play.cancel()  # may leave the audio thread mid-write; harmless at teardown
                if not stats["summary"]:
                    # Ctrl+C cancellation lands on the first await here; re-enter
                    # each step and bound the dance by wall clock instead
                    deadline = loop.time() + 60
                    try:
                        await _stubborn(
                            session.send(
                                "The session is ending — call save_session_summary now "
                                "with a short summary."
                            )
                        )
                        while loop.time() < deadline and not stats["summary"]:
                            await _stubborn(asyncio.sleep(0.1))
                    except Exception as e:  # noqa: BLE001 -- best-effort close; transcript-only is the floor
                        print(f"[closing summary skipped: {type(e).__name__}]")
                listen.cancel()
    _close_unjournaled(s.db_path, session_id, stats, transcript)
    return stats
