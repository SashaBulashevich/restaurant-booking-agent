"""Прогін типових сценаріїв розмови з реальною LLM (без Telegram).

Запуск:  python scenarios.py [назва_сценарію]
Результат: logs/scenarios/<сценарій>.md, повна переписка і лог рішень.
"""
from __future__ import annotations

import shutil
import sys

from agent.config import Config
from agent.core import Agent

# Кожен крок: ("m", текст менеджера) або ("op", відповідь оператора)
SCENARIOS = {
    "happy_path": [
        ("m", "Good afternoon! Yes, 5pm for two is fine. What name shall I put it under?"),
        ("op", "Sasha"),
        ("m", "Lovely, that's booked for Sasha at The Golden Lion, 5pm today, table for two."),
        ("m", "See you later!"),
    ],
    "restaurant_name_unknown": [
        ("m", "Hello. Yes, we have a table for two at 17:00, it's booked."),
        ("m", "It's Dishoom Covent Garden."),
        ("m", "Cheers, bye."),
    ],
    "other_time_offered": [
        ("m", "Hi, sorry, 17:00 is fully booked. I could do 18:30 for two?"),
        ("op", "так"),
        ("m", "Great, 18:30 for two at The Ivy is confirmed."),
    ],
    "other_time_rejected": [
        ("m", "Afraid 17:00 is gone, would 19:00 work?"),
        ("op", "ні, тільки 17:00"),
        ("m", "Then I'm sorry, we can't help today."),
    ],
    "deposit_and_bot_question": [
        ("m", "Hi! Are you a bot?"),
        ("m", "OK. We need a £20 card deposit to hold the table. Can you send the card details?"),
        ("op", "Не давай картку. Скажи, що заплатимо на місці або нехай надішлють посилання на оплату."),
        ("m", "No problem, we can waive it. Booked: The Wolseley, 17:00, two people."),
    ],
    "allergy_question": [
        ("m", "Yes we can seat you at 5. Any allergies or special requests?"),
        ("op", "Немає"),
        ("m", "Perfect, it's confirmed at Rules restaurant, 5pm today for 2."),
    ],
}


def run(name: str, steps) -> None:
    cfg = Config()
    cfg.log_dir = cfg.log_dir / "scenarios" / name
    shutil.rmtree(cfg.log_dir, ignore_errors=True)
    agent = Agent(cfg)
    lines = [f"# Сценарій: {name}\n"]

    def show(outs):
        for o in outs:
            lines.append(f"- **AGENT → {o.to}:** {o.text}")

    show(agent.start())
    for kind, text in steps:
        if kind == "m":
            lines.append(f"- **MANAGER:** {text}")
            show(agent.on_manager(text))
        else:
            lines.append(f"- **OPERATOR:** {text}")
            show(agent.on_operator(text))
    lines.append(f"\nФінальний статус: `{agent.conv.status}`")
    out = cfg.log_dir.parent / f"{name}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), "\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    names = sys.argv[1:] or list(SCENARIOS)
    for n in names:
        run(n, SCENARIOS[n])
