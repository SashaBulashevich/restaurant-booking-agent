"""Telegram-транспорт агента (userbot на Telethon).

Чому userbot, а не Bot API: бот не може написати людині першим, поки вона
сама не натисне /start, а за умовою менеджер першим не пише.

Запуск:
  python run_telegram.py --login    одноразовий вхід (телефон + код вводите ви)
  python run_telegram.py --start    почати НОВУ розмову: агент пише першим і слухає
  python run_telegram.py            продовжити збережену розмову (після перезапуску)

Оператор (людина) спілкується з агентом через Saved Messages («Збережене»):
агент ставить там питання з позначкою «🤖 [agent]», ви відповідаєте звичайним
повідомленням. Команди: /retry (повторити після збою LLM), /stop (зупинити).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient, events

from agent.config import ROOT, Config
from agent.core import Agent, Out
from agent.voice import synthesize, transcribe

DEBOUNCE_SECONDS = 3.0   # кілька повідомлень підряд від менеджера обробляємо разом
log = logging.getLogger("telegram")


class TelegramAgent:
    def __init__(self, cfg: Config, client: TelegramClient):
        self.cfg = cfg
        self.client = client
        self.agent = Agent.resume_or_new(cfg)
        self.lock = asyncio.Lock()
        self.buffer: list[tuple[str, bool]] = []
        self.flush_task: asyncio.Task | None = None
        self.manager = None
        self.last_manager_voice = False
        self.tmp = cfg.log_dir / "tmp"

    async def deliver(self, outs: list[Out]) -> None:
        for o in outs:
            if o.to == "operator":
                await self.client.send_message("me", o.text)
                continue
            use_voice = self.cfg.voice_mode == "always" or (self.cfg.voice_mode == "mirror" and self.last_manager_voice)
            async with self.client.action(self.manager, "record-audio" if use_voice else "typing"):
                await asyncio.sleep(min(1.0 + len(o.text) / 60, 4.0))   # людський темп
                if use_voice:
                    try:
                        ogg = await synthesize(self.cfg, o.text, self.tmp)
                        sent = await self.client.send_file(self.manager, ogg, voice_note=True, caption=o.text)
                        self._seen(sent.id)
                        continue
                    except Exception as exc:  # TTS впав: надсилаємо текстом
                        log.warning("TTS failed, sending text: %s", exc)
                        self.agent.log.write("SYSTEM", "TTS failed", decision=f"надіслано текстом: {exc}"[:200])
                sent = await self.client.send_message(self.manager, o.text)
                self._seen(sent.id)

    def _seen(self, msg_id: int) -> None:
        """Запам'ятовує останнє оброблене повідомлення, щоб після перезапуску дочитати пропущені."""
        conv = self.agent.conv
        if conv and isinstance(msg_id, int) and msg_id > conv.last_seen_id:
            conv.last_seen_id = msg_id
            conv.save(self.agent.state_path)

    async def catch_up(self, manager_id: int) -> int:
        """Після перезапуску: обробити повідомлення менеджера, що прийшли, поки агент був вимкнений."""
        conv = self.agent.conv
        if not conv or not conv.last_seen_id:
            return 0
        missed = [m async for m in self.client.iter_messages(manager_id, min_id=conv.last_seen_id, reverse=True)
                  if not m.out]
        for m in missed:
            await self.on_manager_message(SimpleNamespace(message=m))
        return len(missed)

    async def on_manager_message(self, event) -> None:
        msg = event.message
        self._seen(msg.id)
        if msg.voice or msg.audio:
            try:
                self.tmp.mkdir(parents=True, exist_ok=True)
                path = await msg.download_media(file=str(self.tmp / f"in_{msg.id}.ogg"))
                text = await asyncio.to_thread(transcribe, self.cfg, Path(path))
            except Exception as exc:
                log.warning("STT failed: %s", exc)
                text = "(voice message that could not be transcribed - politely ask the manager to repeat it in text)"
            self.buffer.append((text, True))
        elif msg.text:
            self.buffer.append((msg.text, False))
        else:
            self.buffer.append(("(the manager sent a non-text message, e.g. a sticker or photo)", False))
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
        self.flush_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        async with self.lock:
            items, self.buffer = self.buffer, []
            if not items or self.agent.conv is None:
                return
            text = "\n".join(t for t, _ in items)
            voice = any(v for _, v in items)
            self.last_manager_voice = voice
            outs = await asyncio.to_thread(self.agent.on_manager, text, voice)
            await self.deliver(outs)

    async def on_operator_message(self, event) -> None:
        text = (event.message.text or "").strip()
        if not text or text.startswith("🤖 [agent]") or self.agent.conv is None:
            return
        async with self.lock:
            outs = await asyncio.to_thread(self.agent.on_operator, text)
            await self.deliver(outs)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="лише увійти в акаунт і вийти")
    parser.add_argument("--start", action="store_true", help="почати нову розмову з менеджером")
    parser.add_argument("--selftest", action="store_true", help="перевірити Telegram без повідомлень менеджеру")
    args = parser.parse_args()

    cfg = Config()
    if not (cfg.tg_api_id and cfg.tg_api_hash):
        sys.exit("Заповніть TG_API_ID і TG_API_HASH у .env (див. README).")
    if not cfg.manager_username and not args.login:
        sys.exit("Заповніть MANAGER_USERNAME у .env: кому писатиме агент.")
    client = TelegramClient(str(ROOT / cfg.tg_session), cfg.tg_api_id, cfg.tg_api_hash)
    await client.start()   # перший раз запитає телефон і код у терміналі
    me = await client.get_me()
    print(f"Увійшли як @{me.username or me.id}")
    if args.login:
        await client.disconnect()
        return

    if args.selftest:
        # Перевірка без повідомлень менеджеру: лише «Збережене» і пошук менеджера.
        manager = await client.get_entity(cfg.manager_username)
        print(f"Менеджера знайдено: @{manager.username} (id {manager.id})")
        bot = TelegramAgent(cfg, client)
        bot.manager = "me"
        bot.last_manager_voice = True
        await bot.deliver([Out("manager", "Self-test: this is how the agent's voice reply will sound."),
                           Out("operator", "Self-test: оператор отримує повідомлення сюди.")])
        print("Готово: перевірте «Збережене» (голосове з підписом і текст).")
        await client.disconnect()
        return

    if args.start:
        state = cfg.log_dir / "conversation_state.json"
        if state.exists():   # старі спроби не видаляємо, а архівуємо
            archive = cfg.log_dir / "archive" / datetime.now().strftime("%Y%m%d_%H%M%S")
            archive.mkdir(parents=True, exist_ok=True)
            for name in ("conversation_state.json", "decision_log.md", "decision_log.jsonl"):
                if (cfg.log_dir / name).exists():
                    shutil.move(str(cfg.log_dir / name), str(archive / name))

    bot = TelegramAgent(cfg, client)
    bot.manager = await client.get_input_entity(cfg.manager_username)
    manager_id = (await client.get_entity(cfg.manager_username)).id

    client.add_event_handler(bot.on_manager_message, events.NewMessage(chats=manager_id, incoming=True))
    client.add_event_handler(bot.on_operator_message, events.NewMessage(chats=me.id, outgoing=True))

    if args.start:
        outs = bot.agent.start()
        await bot.deliver(outs)
        if bot.agent.conv is None:
            await client.disconnect()
            return
    elif bot.agent.conv is None:
        sys.exit("Збереженої розмови на сьогодні немає. Запустіть з --start.")
    else:
        n = await bot.catch_up(manager_id)
        print(f"Продовжую збережену розмову. Пропущених повідомлень менеджера: {n}")

    print(f"Агент слухає чат з {cfg.manager_username}. Зупинити: Ctrl+C")
    await client.run_until_disconnected()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
