"""TTS rendering with a content-hash cache.

One variant per utterance: a learner-slow rate (default -25%, user-overridable
via TTS_RATE) -- the user is learning the language, so the slow version IS
the audio. Natural variants are one render away if ever wanted.
"""

import asyncio
import hashlib
import random
from pathlib import Path

import edge_tts


def audio_name(text: str, voice: str, rate: str) -> str:
    """Deterministic media filename for one text+voice+rate triple."""
    return hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:16] + ".mp3"


async def _save_with_retry(text: str, voice: str, rate: str, path: Path) -> None:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
            return
        except Exception as e:  # noqa: BLE001 -- edge-tts fails in many shapes; any failure is transient until attempt 3
            last_err = e
            await asyncio.sleep(min(2**attempt, 4) + random.random())
    raise RuntimeError(f"TTS failed after 3 attempts for {text[:40]!r}: {last_err}")


async def render_lines(texts: list[str], voice: str, out_dir: Path, rate: str) -> dict[str, str]:
    """Render every text once at the given rate; return {text: basename}. Cached files are skipped."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [t for t in texts if not (out_dir / audio_name(t, voice, rate)).exists()]
    for i, text in enumerate(todo, 1):
        print(f"  tts [{i}/{len(todo)}] {text[:50]}")
        await _save_with_retry(text, voice, rate, out_dir / audio_name(text, voice, rate))
    return {t: audio_name(t, voice, rate) for t in texts}
