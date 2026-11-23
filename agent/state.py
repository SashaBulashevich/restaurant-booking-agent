"""Стан розмови: історія повідомлень і те, що вже відомо про бронь.

Стан зберігається у JSON після кожного кроку, щоб після перезапуску агент
продовжив ту саму розмову, а не почав нову.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Етапи розмови
REQUESTING = "requesting"            # попросили столик, чекаємо відповідь
WAITING_OPERATOR = "waiting_operator"  # чекаємо відповідь людини-оператора
CONFIRMED = "confirmed"              # бронь підтверджена й повторена в чаті
CLOSED = "closed"                    # розмову завершено (відмова або кінець)


@dataclass
class Turn:
    role: str   # manager | agent | operator | system
    text: str
    at: str
    voice: bool = False


@dataclass
class Conversation:
    booking_date: str                 # YYYY-MM-DD за часом ресторану
    booking_time: str                 # HH:MM
    party_size: int
    status: str = REQUESTING
    restaurant_name: str | None = None
    guest_name: str | None = None     # ім'я від оператора, якщо його немає в конфігу
    agreed_time: str | None = None    # час, змінений лише з дозволу оператора
    pending_operator_question: str | None = None
    pending_time: str | None = None   # час, запропонований менеджером, чекає рішення оператора
    last_seen_id: int = 0             # ID останнього обробленого повідомлення в чаті з менеджером
    turns: list[Turn] = field(default_factory=list)

    def add(self, role: str, text: str, tz: str, voice: bool = False) -> None:
        now = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")
        self.turns.append(Turn(role=role, text=text, at=now, voice=voice))

    @property
    def target_time(self) -> str:
        return self.agreed_time or self.booking_time

    def manager_text(self) -> str:
        return "\n".join(t.text for t in self.turns if t.role == "manager")

    def transcript(self) -> str:
        names = {"manager": "MANAGER", "agent": "YOU (agent)", "operator": "OPERATOR (your principal, private)",
                 "system": "SYSTEM NOTE"}
        lines = []
        for t in self.turns:
            tag = " [voice message, transcribed]" if t.voice else ""
            lines.append(f"[{t.at}] {names.get(t.role, t.role)}{tag}: {t.text}")
        return "\n".join(lines)

    # --- збереження ---
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Conversation":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["turns"] = [Turn(**t) for t in data.get("turns", [])]
        return cls(**data)
