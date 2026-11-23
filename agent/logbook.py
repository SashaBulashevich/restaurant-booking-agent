"""Лог рішень, який веде сам агент: один рядок на кожне повідомлення.

Пише два файли:
- decision_log.jsonl для машинної обробки;
- decision_log.md, таблиця для людей (вона йде у здачу).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


class DecisionLog:
    def __init__(self, log_dir: Path, tz: str):
        self.dir = log_dir
        self.tz = tz
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "decision_log.jsonl"
        self.md = self.dir / "decision_log.md"
        if not self.md.exists():
            self.md.write_text(
                "# Лог рішень агента\n\n"
                "| Час (London) | Напрям | Повідомлення | Що агент зрозумів | Що вирішив |\n"
                "|---|---|---|---|---|\n",
                encoding="utf-8",
            )

    def write(self, direction: str, message: str, understood: str = "", decision: str = "", **extra) -> None:
        """direction: OUT (агент → менеджеру), IN (менеджер → агенту), OPERATOR (обмін з людиною)."""
        now = datetime.now(ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M:%S")
        row = {"at": now, "direction": direction, "message": message,
               "understood": understood, "decision": decision, **extra}
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with self.md.open("a", encoding="utf-8") as f:
            f.write(f"| {now} | {direction} | {_cell(message)} | {_cell(understood)} | {_cell(decision)} |\n")
