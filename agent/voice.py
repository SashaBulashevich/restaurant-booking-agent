"""Голос: розпізнавання голосових менеджера і озвучення відповідей агента.

- STT: Groq Whisper (whisper-large-v3-turbo) приймає .ogg з Telegram напряму.
- TTS: edge-tts, британський голос en-GB-RyanNeural, видає mp3; ffmpeg
  (з пакета imageio-ffmpeg) конвертує його в OGG/Opus, бо Telegram показує
  як «голосове» лише такий формат.
Якщо озвучення не вдалося, агент надсилає звичайний текст (fail-safe).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import edge_tts
import imageio_ffmpeg

from .config import Config


def transcribe(cfg: Config, audio_path: Path, client=None) -> str:
    if client is None:
        from groq import Groq
        client = Groq(api_key=cfg.groq_api_key, timeout=60)
    with audio_path.open("rb") as f:
        result = client.audio.transcriptions.create(
            file=(audio_path.name, f.read()), model=cfg.stt_model, language="en", temperature=0,
        )
    text = (result.text or "").strip()
    if not text:
        raise ValueError("empty transcription")
    return text


async def synthesize(cfg: Config, text: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / "reply.mp3"
    ogg = out_dir / "reply.ogg"
    await edge_tts.Communicate(text, cfg.tts_voice, rate="-5%").save(str(mp3))
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(mp3),
         "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "48000", str(ogg)],
        check=True, timeout=60,
    )
    return ogg
