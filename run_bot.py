"""Telegram-транспорт через звичайного бота (Bot API, токен від @BotFather).

Обмеження Bot API: бот може написати людині лише після того, як вона хоч раз
натиснула Start. Тому агент:
  1) пробує написати менеджеру першим (якщо менеджер уже запускав бота, це спрацює);
  2) якщо Telegram відмовляє (403), чекає /start від менеджера і одразу пише
     перше повідомлення сам. Менеджер нічого не пише, лише відкриває бота.

Оператор (людина) пише боту зі свого акаунта (OPERATOR_USERNAME): агент ставить
питання з позначкою «🤖 [agent]», оператор відповідає звичайним повідомленням.
Команди оператора: /retry, /stop, /status.

Запуск:
  python run_bot.py --selftest   перевірити токен і надіслати оператору пробне голосове
  python run_bot.py --start      почати нову розмову
  python run_bot.py              продовжити збережену розмову після перезапуску
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from agent.config import Config
from agent.core import Agent, Out
from agent.voice import synthesize, transcribe

DEBOUNCE_SECONDS = 3.0
log = logging.getLogger("bot")


class BotAPI:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.http = httpx.Client(timeout=60)

    def call(self, method: str, files=None, **params):
        r = self.http.post(self.base + method, data=params if files else None,
                           json=None if files else params, files=files)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(data.get("error_code"), data.get("description", ""))
        return data["result"]

    def download(self, file_id: str, dest: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.http.get(self.file_base + info["file_path"]).content)
        return dest


class TelegramError(RuntimeError):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code


def _uname(u: str | None) -> str:
    return (u or "").lstrip("@").lower()


class BotAgent:
    def __init__(self, cfg: Config, api: BotAPI, new: bool):
        self.cfg, self.api = cfg, api
        self.agent = Agent(cfg) if new else Agent.resume_or_new(cfg)
        self.meta_path = cfg.log_dir / "bot_meta.json"
        self.meta = json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}
        self.manager_chat = self.meta.get("manager_chat") or cfg.manager_id
        self.operator_chat = self.meta.get("operator_chat")
        self.offset = self.meta.get("offset", 0)
        self.waiting_for_start = self.meta.get("waiting_for_start", False) and not new
        self.last_manager_voice = False
        self.tmp = cfg.log_dir / "tmp"

    def save_meta(self) -> None:
        self.meta.update(manager_chat=self.manager_chat, operator_chat=self.operator_chat,
                         offset=self.offset, waiting_for_start=self.waiting_for_start)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    # ---------- надсилання ----------
    def to_operator(self, text: str) -> None:
        print(f"\n[→ оператор] {text}")
        if self.operator_chat:
            try:
                self.api.call("sendMessage", chat_id=self.operator_chat, text=text)
            except TelegramError as exc:
                log.warning("operator send failed: %s", exc)
        else:
            print("  (оператор ще не натиснув Start у боті: питання видно лише тут, у терміналі)")

    async def deliver(self, outs: list[Out]) -> None:
        for o in outs:
            if o.to == "operator":
                self.to_operator(o.text)
                continue
            use_voice = self.cfg.voice_mode == "always" or (self.cfg.voice_mode == "mirror" and self.last_manager_voice)
            print(f"\n[→ менеджер] {o.text}")
            self.api.call("sendChatAction", chat_id=self.manager_chat,
                          action="record_voice" if use_voice else "typing")
            await asyncio.sleep(min(1.0 + len(o.text) / 60, 4.0))
            if use_voice:
                try:
                    ogg = await synthesize(self.cfg, o.text, self.tmp)
                    with ogg.open("rb") as f:
                        self.api.call("sendVoice", files={"voice": ("reply.ogg", f, "audio/ogg")},
                                      chat_id=self.manager_chat, caption=o.text[:1024])
                    continue
                except Exception as exc:
                    log.warning("TTS failed, sending text: %s", exc)
                    self.agent.log.write("SYSTEM", "TTS failed", decision=f"надіслано текстом: {exc}"[:200])
            self.api.call("sendMessage", chat_id=self.manager_chat, text=o.text)

    # ---------- старт ----------
    async def try_open(self) -> None:
        """Пробує написати менеджеру першим; якщо бот ще не запускався менеджером — чекає /start."""
        if not self.manager_chat:
            self.waiting_for_start = True
            self.to_operator(f"🤖 [agent] Чекаю, поки {self.cfg.manager_username} відкриє бота (Start).")
            return
        try:
            self.api.call("sendChatAction", chat_id=self.manager_chat, action="typing")
        except TelegramError as exc:
            self.waiting_for_start = True
            self.save_meta()
            self.agent.log.write("SYSTEM", f"Bot API не дозволяє написати першим ({exc})",
                                 decision="Чекаю, поки менеджер відкриє бота (Start), і тоді пишу першим.")
            self.to_operator(f"🤖 [agent] Telegram не дозволяє боту написати першим ({exc}). "
                             f"Щойно {self.cfg.manager_username} натисне Start, я одразу напишу.")
            return
        await self.open_now()

    async def open_now(self) -> None:
        self.waiting_for_start = False
        self.save_meta()
        await self.deliver(self.agent.start())

    # ---------- обробка вхідних ----------
    def classify(self, msg: dict) -> str:
        user = msg.get("from") or {}
        if msg.get("chat", {}).get("type") != "private":
            return "other"
        if _uname(user.get("username")) == _uname(self.cfg.manager_username) or \
                (self.cfg.manager_id and user.get("id") == self.cfg.manager_id):
            return "manager"
        if self.cfg.operator_username and _uname(user.get("username")) == _uname(self.cfg.operator_username):
            return "operator"
        return "other"

    def manager_text(self, msg: dict) -> tuple[str, bool]:
        media = msg.get("voice") or msg.get("audio")
        if media:
            try:
                path = self.api.download(media["file_id"], self.tmp / f"in_{msg['message_id']}.ogg")
                return transcribe(self.cfg, path), True
            except Exception as exc:
                log.warning("STT failed: %s", exc)
                return "(voice message that could not be transcribed - politely ask the manager to repeat it in text)", True
        if msg.get("text"):
            return msg["text"], False
        return "(the manager sent a non-text message, e.g. a sticker or photo)", False

    def poll(self, timeout: int) -> list[dict]:
        try:
            updates = self.api.call("getUpdates", offset=self.offset, timeout=timeout, allowed_updates=["message"])
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("getUpdates failed: %s", exc)
            time.sleep(3)
            return []
        if updates:
            self.offset = updates[-1]["update_id"] + 1
            self.save_meta()
        return [u["message"] for u in updates if "message" in u]

    async def run(self) -> None:
        print(f"Агент слухає. Менеджер: {self.cfg.manager_username}. Зупинити: Ctrl+C")
        while True:
            msgs = await asyncio.to_thread(self.poll, 30)
            if not msgs:
                continue
            # Debounce: дочекатися, чи менеджер не допише ще щось
            if any(self.classify(m) == "manager" for m in msgs):
                await asyncio.sleep(DEBOUNCE_SECONDS)
                msgs += await asyncio.to_thread(self.poll, 0)
            await self.handle(msgs)

    async def handle(self, msgs: list[dict]) -> None:
        manager_parts: list[tuple[str, bool]] = []
        for m in msgs:
            kind = self.classify(m)
            text = (m.get("text") or "").strip()
            if kind == "operator":
                if self.operator_chat != m["chat"]["id"]:
                    self.operator_chat = m["chat"]["id"]
                    self.save_meta()
                    self.to_operator("🤖 [agent] Оператора зареєстровано. Сюди надходитимуть мої питання.")
                if text in ("/start", ""):
                    continue
                if text == "/status":
                    c = self.agent.conv
                    self.to_operator(f"🤖 [agent] Статус: {c.status if c else 'розмову не почато'}"
                                     f"{', чекаю Start від менеджера' if self.waiting_for_start else ''}")
                    continue
                if self.agent.conv:
                    await self.deliver(await asyncio.to_thread(self.agent.on_operator, text))
            elif kind == "manager":
                if self.manager_chat != m["chat"]["id"]:
                    self.manager_chat = m["chat"]["id"]
                    self.save_meta()
                if text.startswith("/start"):
                    if self.waiting_for_start or self.agent.conv is None:
                        self.agent.log.write("IN", "/start", "Менеджер відкрив бота (технічна дія, не повідомлення).",
                                             "Тепер Telegram дозволяє писати: починаю розмову першим.")
                        await self.open_now()
                    continue
                if self.agent.conv is None:   # менеджер написав, не натискаючи /start: усе одно вітаємось
                    await self.open_now()
                manager_parts.append(self.manager_text(m))
            else:
                log.info("ignored message from %s", (m.get("from") or {}).get("username"))
        if manager_parts and self.agent.conv:
            self.last_manager_voice = any(v for _, v in manager_parts)
            text = "\n".join(t for t, _ in manager_parts)
            print(f"\n[менеджер] {text}")
            await self.deliver(await asyncio.to_thread(self.agent.on_manager, text, self.last_manager_voice))


def archive_previous(cfg: Config) -> None:
    names = ("conversation_state.json", "decision_log.md", "decision_log.jsonl")
    if any((cfg.log_dir / n).exists() for n in names):
        dest = cfg.log_dir / "archive" / datetime.now().strftime("%Y%m%d_%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            if (cfg.log_dir / n).exists():
                shutil.move(str(cfg.log_dir / n), str(dest / n))


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", action="store_true", help="почати нову розмову")
    p.add_argument("--selftest", action="store_true", help="перевірити токен, менеджеру нічого не надсилати")
    args = p.parse_args()

    cfg = Config()
    if not cfg.bot_token:
        sys.exit("Впишіть BOT_TOKEN у .env (токен від @BotFather).")
    if not (cfg.manager_username and cfg.operator_username):
        sys.exit("Заповніть MANAGER_USERNAME і OPERATOR_USERNAME у .env.")
    api = BotAPI(cfg.bot_token)
    me = api.call("getMe")
    print(f"Бот: @{me['username']}")

    if args.start:
        archive_previous(cfg)
    bot = BotAgent(cfg, api, new=args.start)

    if args.selftest:
        msgs = bot.poll(0)
        for m in msgs:
            if bot.classify(m) == "operator":
                bot.operator_chat = m["chat"]["id"]
        bot.save_meta()
        if not bot.operator_chat:
            sys.exit(f"Оператор ще не натиснув Start у @{me['username']} (з акаунта {cfg.operator_username}).")
        bot.manager_chat, bot.last_manager_voice = bot.operator_chat, True   # «менеджером» тимчасово є оператор
        await bot.deliver([Out("manager", "Self-test: this is how the agent's voice reply will sound."),
                           Out("operator", "🤖 [agent] Self-test: питання оператору приходитимуть сюди.")])
        print("Готово: перевірте чат з ботом (голосове з підписом і текст).")
        return

    if args.start:
        await bot.try_open()
    elif bot.agent.conv is None and not bot.waiting_for_start:
        sys.exit("Збереженої розмови на сьогодні немає. Запустіть з --start.")
    await bot.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
