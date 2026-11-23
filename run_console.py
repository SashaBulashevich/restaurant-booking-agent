"""Консольний симулятор: ви граєте менеджера ресторану в терміналі.

Логіка агента та сама, що й у Telegram; відрізняється лише транспорт.
Команди:
  op: <текст>  відповісти агенту як оператор (замість Saved Messages)
  /retry       повторити крок після збою LLM
  /quit        вийти
"""
from __future__ import annotations

import sys

from agent.config import Config
from agent.core import Agent


def show(outs) -> None:
    for o in outs:
        who = "AGENT → manager" if o.to == "manager" else "AGENT → operator"
        print(f"\n[{who}] {o.text}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = Config()
    cfg.log_dir = cfg.log_dir / "console"
    agent = Agent(cfg)
    show(agent.start())
    if agent.conv is None:
        return
    while True:
        try:
            line = input("\nmanager> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/retry":
            show(agent.on_operator("/retry"))
        elif line.startswith("op:"):
            show(agent.on_operator(line[3:].strip()))
        else:
            show(agent.on_manager(line))
    print(f"\nЛог рішень: {cfg.log_dir / 'decision_log.md'}")


if __name__ == "__main__":
    main()
