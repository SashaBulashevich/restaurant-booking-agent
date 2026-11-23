"""Логіка агента, незалежна від транспорту (Telegram або консоль).

Транспорт передає агенту події: «почати», «менеджер написав», «оператор
відповів». Агент повертає список вихідних повідомлень: менеджеру або оператору.

Принципи:
- Fail-safe: якщо модель недоступна або відповідь не пройшла перевірку,
  менеджеру нічого не надсилається, а оператор отримує сповіщення.
- Факти беруться тільки з конфігу, зі слів менеджера або від оператора.
- Підсумок броні збирає код зі структурованих полів, а не модель.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .brain import Brain, BrainError, Decision, human_date, human_time
from .config import Config
from .logbook import DecisionLog
from .state import CLOSED, CONFIRMED, REQUESTING, WAITING_OPERATOR, Conversation

NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
HOLDING = "Let me just check that and I'll come back to you in a moment."
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@dataclass
class Out:
    to: str      # "manager" | "operator"
    text: str


TIME_RE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", re.I)


def extract_time(text: str, exclude: str | None = None) -> str | None:
    """Знаходить час у тексті менеджера: «18:30», «6.30pm», «7 pm». Повертає HH:MM.

    exclude: пропустити цей час (наприклад, той, що ми просили).
    """
    for h, m, ap in TIME_RE.findall(text or ""):
        if not m and not ap:
            continue  # просто число, наприклад «for 2»
        hour, minute = int(h), int(m or 0)
        if ap.lower() == "pm" and hour < 12:
            hour += 12
        if ap.lower() == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60 and f"{hour:02d}:{minute:02d}" != exclude:
            return f"{hour:02d}:{minute:02d}"
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


class Agent:
    def __init__(self, cfg: Config, brain: Brain | None = None, conv: Conversation | None = None):
        self.cfg = cfg
        self.brain = brain or Brain(cfg)
        self.log = DecisionLog(cfg.log_dir, cfg.timezone)
        self.conv = conv
        self.state_path = cfg.log_dir / "conversation_state.json"

    # ---------- допоміжне ----------
    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg.timezone))

    def _save(self) -> None:
        if self.conv:
            self.conv.save(self.state_path)

    def recap(self) -> str:
        c = self.conv
        n = c.party_size
        name = self.cfg.guest_name or c.guest_name
        who = f" under the name {name}" if name else ""
        return (
            "Wonderful, thank you ever so much. Just to confirm the booking: "
            f"a table for {NUMBER_WORDS.get(n, n)} ({n} people) at {human_time(c.target_time)} "
            f"today, {human_date(c.booking_date)}, at {c.restaurant_name}{who}. "
            "We look forward to it. Many thanks again!"
        )

    def _mentions_booking(self, text: str) -> bool:
        """Перевірка, що в першому повідомленні є кількість гостей і час."""
        low = (text or "").lower()
        n = self.conv.party_size
        has_party = str(n) in low or NUMBER_WORDS.get(n, "#") in low
        h, m = (int(x) for x in self.conv.target_time.split(":"))
        h12 = h % 12 or 12
        variants = [self.conv.target_time, f"{h12} pm", f"{h12}pm", f"{h12} o'clock", f"{h12}:{m:02d}"]
        return has_party and any(v in low for v in variants)

    def _manager(self, text: str, understood: str, decision: str, model: str = "") -> Out:
        self.conv.add("agent", text, self.cfg.timezone)
        self.log.write("OUT", text, understood, decision, model=model)
        return Out("manager", text)

    def _operator(self, text: str) -> Out:
        self.log.write("OPERATOR", text, decision="повідомлення оператору (людині)")
        return Out("operator", "🤖 [agent] " + text)

    # ---------- події ----------
    def start(self) -> list[Out]:
        """Агент першим починає розмову."""
        now = self.now()
        h, m = (int(x) for x in self.cfg.booking_time.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now > slot - timedelta(minutes=self.cfg.min_lead_minutes):
            return [self._operator(
                f"Зараз {now:%H:%M} в {self.cfg.timezone}. Бронювати на {self.cfg.booking_time} сьогодні вже пізно, "
                "розмову не починаю.")]
        self.conv = Conversation(booking_date=now.date().isoformat(), booking_time=self.cfg.booking_time,
                                 party_size=self.cfg.party_size)
        try:
            d = self.brain.decide(self.conv, opening=True, now_local=f"{now:%H:%M}")
            text, model = d.reply, d.model
            if not self._mentions_booking(text):
                raise BrainError(f"opening does not mention party/time: {text!r}")
            why = "Почати розмову: попросити столик (текст від LLM, перевірено, що є кількість гостей і час)."
        except BrainError as exc:
            greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
            text = (f"{greeting}! I'm an assistant booking on behalf of {self.cfg.principal}. "
                    f"Would it be possible to reserve a table for {NUMBER_WORDS.get(self.conv.party_size)} "
                    f"at {human_time(self.conv.target_time)} today, please?")
            model = "template"
            why = f"LLM недоступна або дала неповний текст ({str(exc)[:120]}), тож використано перевірений шаблон."
        out = [self._manager(text, "Розмову ще не почато, менеджер першим не пише.", why, model)]
        self._save()
        return out

    def on_manager(self, text: str, voice: bool = False) -> list[Out]:
        self.conv.add("manager", text, self.cfg.timezone, voice=voice)
        if self.conv.status == WAITING_OPERATOR:
            self.log.write("IN", text, "Менеджер написав, поки чекаємо відповіді оператора.",
                           "Не відповідаю, щоб не вигадувати; переслав оператору.", voice=voice)
            self._save()
            return [self._operator(f"Поки чекаю вашої відповіді, менеджер написав: «{text}»")]
        return self._process(trigger=text, voice=voice)

    def on_operator(self, text: str) -> list[Out]:
        cmd = text.strip().lower()
        if cmd == "/stop":
            self.conv.status = CLOSED
            self._save()
            return [self._operator("Зупинено оператором. Менеджеру більше нічого не надсилаю.")]
        if cmd == "/retry":
            return self._process(trigger=None)
        if self.conv.status != WAITING_OPERATOR:
            return []
        self.conv.add("operator", text, self.cfg.timezone)
        self.log.write("FROM_OPERATOR", text, "Відповідь оператора (людини) на питання агента.", "Продовжити розмову з менеджером.",
                       source="operator")
        # Зміна часу дозволена тільки явно оператором
        if self.conv.pending_time and re.match(r"^\s*(так|yes|y|ok|ок|да|згоден|погоджуємось)\b", cmd):
            self.conv.agreed_time = self.conv.pending_time
            self.conv.add("system", f"Operator approved the new time {self.conv.agreed_time}.", self.cfg.timezone)
        self.conv.pending_time = None
        self.conv.pending_operator_question = None
        self.conv.status = CONFIRMED if self.conv.restaurant_name and self._was_confirmed() else REQUESTING
        return self._process(trigger=None)

    def _was_confirmed(self) -> bool:
        return any(t.role == "agent" and t.text.startswith("Wonderful, thank you ever so much. Just to confirm")
                   for t in self.conv.turns)

    # ---------- головний крок ----------
    def _process(self, trigger: str | None, voice: bool = False) -> list[Out]:
        if self.conv.status == CLOSED:
            self._save()
            return []
        try:
            d = self.brain.decide(self.conv)
        except BrainError as exc:
            # FAIL-SAFE: менеджеру нічого, людині сповіщення.
            if trigger is not None:
                self.log.write("IN", trigger, "Не вдалося отримати рішення від LLM.",
                               "Fail-safe: менеджеру не відповідаю, кличу оператора.", error=str(exc)[:300])
            self._save()
            return [self._operator(
                "LLM недоступна або відповідає невалідно, тому менеджеру нічого не надіслано. "
                f"Останнє повідомлення менеджера: «{trigger or '-'}». Напишіть /retry, щоб спробувати ще раз.")]

        d, notes = self.apply_guards(d)

        # Модель-фактчекер: чи немає у відповіді вигаданих фактів.
        # (Тексти, складені кодом із перевірених даних, не перевіряються.)
        if d.action in {"reply", "wait", "end_conversation"} and d.reply and not d.from_code:
            try:
                claims = self.brain.verify(self.conv, d.reply)
            except BrainError as exc:
                claims = None
                notes.append(f"фактчекер недоступний ({str(exc)[:80]}); не надсилаю без перевірки")
            if claims is None or claims:
                if claims:
                    notes.append("фактчекер: " + "; ".join(claims))
                draft = d.reply
                d.action, d.reply = "ask_operator", HOLDING
                d.operator_question = (
                    f"Менеджер: «{trigger or '-'}». Моя чернетка відповіді: «{draft}». "
                    "Вона містить дані, яких я не отримував (або перевірка не вдалась). Що відповісти по суті?")
        decision =d.decision + (" | Guard: " + "; ".join(notes) if notes else "")
        if trigger is not None:
            self.log.write("IN", trigger, d.understood, decision, action=d.action, model=d.model, voice=voice)

        outs: list[Out] = []
        if d.action == "ask_operator":
            self.conv.status = WAITING_OPERATOR
            last_manager = next((t.text for t in reversed(self.conv.turns) if t.role == "manager"), "")
            offered = d.manager_time or extract_time(last_manager, exclude=self.conv.target_time)
            if offered and offered != self.conv.target_time:
                self.conv.pending_time = offered
            self.conv.pending_operator_question = d.operator_question
            outs.append(self._manager(d.reply or HOLDING, d.understood,
                                      "Утримую розмову, поки оператор відповідає.", d.model))
            outs.append(self._operator(
                f"Потрібна ваша відповідь: {d.operator_question}\n(Напишіть відповідь сюди одним повідомленням.)"))
        elif d.action == "confirm_booking":
            self.conv.status = CONFIRMED
            outs.append(self._manager(self.recap(), d.understood,
                                      "Менеджер погодив бронь: повторюю підтверджені деталі (текст зібрано кодом).",
                                      d.model))
            outs.append(self._operator("✅ Бронь підтверджено: " + self.recap()))
        elif d.action == "end_conversation":
            self.conv.status = CLOSED
            outs.append(self._manager(d.reply, d.understood, decision, d.model))
            if not self._was_confirmed():
                outs.append(self._operator(f"Розмову завершено без броні. Причина: {d.understood}"))
        elif d.action == "reply" or (d.action == "wait" and d.reply):
            outs.append(self._manager(d.reply, d.understood, decision, d.model))
        self._save()
        return outs

    # ---------- захисні перевірки ----------
    def apply_guards(self, d: Decision) -> tuple[Decision, list[str]]:
        c, notes = self.conv, []

        # 1. Назва ресторану: лише якщо її справді написав менеджер.
        if d.restaurant_name:
            if _norm(d.restaurant_name) and _norm(d.restaurant_name) in _norm(c.manager_text()):
                c.restaurant_name = c.restaurant_name or d.restaurant_name
            else:
                notes.append(f"назву «{d.restaurant_name}» менеджер не писав, тому відкинуто")

        # 1b. Ім'я для броні: лише з конфігу або від оператора.
        if d.guest_name and not self.cfg.guest_name and not c.guest_name:
            operator_text = " ".join(t.text for t in c.turns if t.role == "operator")
            if _norm(d.guest_name) and _norm(d.guest_name) in _norm(operator_text):
                c.guest_name = d.guest_name
            else:
                notes.append(f"ім'я «{d.guest_name}» не надав оператор, тому відкинуто")

        # 2. Відповідь не має містити телефонів/email, яких немає у фактах.
        known_phone = _digits(self.cfg.guest_phone or "") or "∅"
        for target in (d.reply,):
            bad_phone = [p for p in PHONE_RE.findall(target) if _digits(p) != known_phone]
            if bad_phone or EMAIL_RE.search(target):
                notes.append("у відповіді контактні дані, яких немає у фактах; не надсилаю")
                d.action, d.reply = "ask_operator", HOLDING
                d.operator_question = (d.operator_question or
                                       "Менеджер, схоже, просить контактні дані. Які дані можна передати?")

        # 3. Підтвердження броні: лише за явної згоди, з відомою назвою і тим самим часом.
        if d.action == "confirm_booking":
            if c.status == CONFIRMED:
                notes.append("бронь уже підтверджена, повторно підсумок не шлю")
                d.action = "reply"
                if not d.reply:
                    d.reply, d.from_code = "Thank you so much again. Have a lovely day, goodbye!", True
            elif d.manager_time and d.manager_time != c.target_time:
                notes.append(f"менеджер пропонує {d.manager_time} замість {c.target_time}; вирішує оператор")
                d.action, d.reply = "ask_operator", HOLDING
                d.operator_question = (f"Менеджер пропонує {d.manager_time} замість {c.target_time}. "
                                       "Погоджуємось? (так/ні або інший варіант)")
            elif not d.manager_agreed:
                notes.append("модель хоче підтвердити, але явної згоди менеджера немає")
                d.action = "reply" if d.reply else "wait"
            elif not c.restaurant_name:
                notes.append("назва ресторану невідома, тому спершу питаю її")
                d.action, d.from_code = "reply", True
                d.reply = ("Wonderful, thank you! Might I just ask the name of the restaurant, "
                           "so that I have the booking noted correctly?")
        return d, notes

    @classmethod
    def resume_or_new(cls, cfg: Config, brain: Brain | None = None) -> "Agent":
        """Продовжує збережену розмову, якщо вона на сьогодні і не закрита."""
        agent = cls(cfg, brain)
        if agent.state_path.exists():
            conv = Conversation.load(agent.state_path)
            if conv.booking_date == agent.now().date().isoformat() and conv.status != CLOSED:
                agent.conv = conv
        return agent
