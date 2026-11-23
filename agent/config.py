"""Налаштування агента з файлу .env.

Усі факти, які агент може повідомити менеджеру, задаються тут, а не моделлю.
Чого немає в конфігурації, того агент не знає, і він запитує оператора.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    return value or default


@dataclass
class Config:
    groq_api_key: str | None = field(default_factory=lambda: _env("GROQ_API_KEY"))
    # Основна модель і резервні (fallback), якщо основна недоступна.
    models: list[str] = field(
        default_factory=lambda: (_env("LLM_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b") or "").split(",")
    )
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "whisper-large-v3-turbo"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "en-GB-RyanNeural"))
    # mirror: голосом відповідаємо на голосове; always: завжди голосом; off: лише текст.
    voice_mode: str = field(default_factory=lambda: _env("VOICE_MODE", "mirror"))

    # Telegram (userbot): звичайний бот не може написати людині першим.
    tg_api_id: int | None = field(default_factory=lambda: int(_env("TG_API_ID", "0") or 0) or None)
    tg_api_hash: str | None = field(default_factory=lambda: _env("TG_API_HASH"))
    tg_session: str = field(default_factory=lambda: _env("TG_SESSION", "agent"))
    manager_username: str | None = field(default_factory=lambda: _env("MANAGER_USERNAME"))

    # Bot API (звичайний бот від @BotFather)
    bot_token: str | None = field(default_factory=lambda: _env("BOT_TOKEN"))
    # Хто є оператором (людиною, яку агент питає про невідоме): його username у Telegram.
    operator_username: str | None = field(default_factory=lambda: _env("OPERATOR_USERNAME"))
    # Числовий ID менеджера, якщо відомий (бот може спробувати написати першим).
    manager_id: int | None = field(default_factory=lambda: int(_env("MANAGER_ID", "0") or 0) or None)

    # Факти про бронювання (задає людина, не модель).
    guest_name: str | None = field(default_factory=lambda: _env("GUEST_NAME"))
    guest_phone: str | None = field(default_factory=lambda: _env("GUEST_PHONE"))
    principal: str = field(default_factory=lambda: _env("PRINCIPAL", "my client"))
    party_size: int = field(default_factory=lambda: int(_env("PARTY_SIZE", "2") or 2))
    booking_time: str = field(default_factory=lambda: _env("BOOKING_TIME", "17:00"))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Europe/London"))
    # За скільки хвилин до броні вже пізно починати розмову.
    min_lead_minutes: int = field(default_factory=lambda: int(_env("MIN_LEAD_MINUTES", "30") or 30))

    log_dir: Path = field(default_factory=lambda: ROOT / (_env("LOG_DIR", "logs") or "logs"))
