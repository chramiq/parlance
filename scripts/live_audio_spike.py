"""P1 audio plumbing spike: mic -> Gemini Live -> speakers, with barge-in.

Validates the full duplex audio path before any parlance logic exists:
16 kHz PCM capture up, 24 kHz playback down, both-way transcriptions printed,
interruption flushes the playout buffer. Quit with Ctrl+C.

Run: uv run python scripts/live_audio_spike.py
"""

import asyncio

import sounddevice as sd
from google import genai
from google.genai import types

from parlance.config import get_settings

RATE_IN, RATE_OUT = 16000, 24000


async def main() -> None:
    key = get_settings().gemini_api_key
    client = genai.Client(api_key=key)
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction="Jsi český kamarád. Odpovídej česky, krátce, na cokoli uslyšíš.",
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    loop = asyncio.get_running_loop()
    mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
    out_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

    def on_mic(indata, frames, time_info, status):
        loop.call_soon_threadsafe(
            lambda: mic_q.put_nowait(bytes(indata)) if not mic_q.full() else None
        )

    def player():
        with sd.RawOutputStream(
            samplerate=RATE_OUT, channels=1, dtype="int16", blocksize=2048
        ) as st:
            while True:
                chunk = asyncio.run_coroutine_threadsafe(out_q.get(), loop).result()
                st.write(chunk)

    async with client.aio.live.connect(model="gemini-3.8-live", config=cfg) as session:
        print("connected. speak; Ctrl+C to quit")

        async def pump_mic():
            while True:
                chunk = await mic_q.get()
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )

        async def recv():
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    while not out_q.empty():
                        out_q.get_nowait()  # barge-in: drop queued playback
                    print("\n[interrupted]")
                if sc.input_transcription and sc.input_transcription.text:
                    print(f"\nYOU:    {sc.input_transcription.text}")
                if sc.output_transcription and sc.output_transcription.text:
                    print(f"GEMINI: {sc.output_transcription.text}")
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            out_q.put_nowait(part.inline_data.data)

        with sd.RawInputStream(
            samplerate=RATE_IN, channels=1, dtype="int16", blocksize=1600, callback=on_mic
        ):
            play_task = asyncio.to_thread(player)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(pump_mic())
                tg.create_task(recv())
                tg.create_task(play_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
