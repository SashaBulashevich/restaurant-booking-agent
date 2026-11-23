"""«Мозок» агента: LLM вирішує, що відповісти, і пояснює чому.

Модель повертає строгий JSON. Код перевіряє його і ніколи не надсилає
менеджеру відповідь, яка не пройшла перевірку (fail-safe).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .config import Config
from .state import Conversation

ACTIONS = {"reply", "wait", "ask_operator", "confirm_booking", "end_conversation"}


class BrainError(RuntimeError):
    """Усі моделі недоступні або повертають невалідну відповідь."""


@dataclass
class Decision:
    action: str
    reply: str
    understood: str
    decision: str
    operator_question: str = ""
    restaurant_name: str | None = None
    manager_agreed: bool = False
    manager_time: str | None = None
    guest_name: str | None = None
    model: str = ""
    from_code: bool = False   # текст склав код (шаблон), а не модель


def human_date(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%B %Y')}"


def human_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{hhmm} ({h12}{':%02d' % m if m else ''} {suffix})"


def facts_block(cfg: Config, conv: Conversation) -> str:
    known = [
        f"- Party size: {conv.party_size} people",
        f"- Requested time: {human_time(conv.target_time)}",
        f"- Requested date: today, {human_date(conv.booking_date)} (restaurant local time, {cfg.timezone})",
        f"- You book on behalf of: {cfg.principal}",
        f"- Name for the booking: {cfg.guest_name or 'UNKNOWN - ask the operator if the manager needs it'}",
        f"- Contact phone: {cfg.guest_phone or 'UNKNOWN - ask the operator if the manager needs it'}",
        "- Allergies, dietary needs, special requests, occasion, seating preference, payment: "
        "UNKNOWN - if asked, ask the operator (unless the operator already answered)",
        f"- Restaurant name: {conv.restaurant_name or 'UNKNOWN - the manager has not stated it yet'}",
    ]
    if conv.guest_name and not cfg.guest_name:
        known[4] = f"- Name for the booking: {conv.guest_name} (given by the operator)"
    status = {
        "requesting": "not confirmed yet",
        "waiting_operator": "waiting for the operator's answer",
        "confirmed": "CONFIRMED - the full recap has ALREADY been sent; now only reply politely / close the conversation",
        "closed": "closed",
    }[conv.status]
    known.append(f"- Booking status: {status}")
    return "\n".join(known)


SYSTEM_PROMPT = """You are a polite booking assistant writing in a Telegram chat with the manager of a restaurant in London.
Your one goal: book a table for {party} people at {time} today ({date}).

STYLE
- British English, warm and courteous, natural British manners ("Good afternoon", "Would it be possible...", "Thank you ever so much"), never servile or long-winded.
- Short chat messages: 1-3 sentences. One question at a time. No emojis, no markdown.
- React to everything the manager says: answer their questions, acknowledge their remarks, do not ignore anything.
- If asked whether you are a bot or an AI, answer honestly: you are an AI assistant booking on behalf of {principal}.

FACTS YOU KNOW (the only facts you may state)
{facts}

NEVER INVENT ANYTHING
- Do not state any name, phone number, email, allergy, preference, occasion, payment detail or other fact that is not in FACTS or in the OPERATOR messages of the transcript.
- If the manager asks for something you do not know, choose action "ask_operator": put the exact question for your principal in "operator_question" and a short polite holding message in "reply" (e.g. "Let me just check that and come back to you in a moment.").
- Never share card or payment details in the chat. If a deposit or card is required, ask the operator.
- If the manager offers a different time or date, or anything that changes the booking, do NOT accept it yourself: use "ask_operator".
- You may take the restaurant name only from what the manager writes. If it is still unknown when the booking is being agreed, politely ask the manager for it.
- Do not ask the manager for anything except the table itself and, if needed, the restaurant name. Answer their questions; do not request their phone, email or other details, and do not offer details you do not have.
- When the operator answers your question, pass the answer on to the manager in your own polite words.
- After the booking is confirmed, still answer every manager message politely (e.g. thank them and say goodbye with "end_conversation"); never stay silent to a farewell.

ACTIONS (choose exactly one)
- "reply": send "reply" to the manager.
- "wait": the manager said they will come back (e.g. "one moment, checking"); "reply" may be a very short acknowledgement or empty.
- "ask_operator": you need information or a decision from your principal (see above).
- "confirm_booking": the manager has clearly agreed to the booking at the requested time and date for the requested party size, AND the restaurant name is known. The system will then send the full confirmation recap itself, so "reply" may be empty.
- "end_conversation": the booking is impossible or the conversation is finished; "reply" is a polite closing message.

OUTPUT: only a JSON object with these keys:
{{
  "understood": "one short sentence: what the manager said or meant (in English)",
  "decision": "one short sentence: what you decided and why",
  "action": "reply | wait | ask_operator | confirm_booking | end_conversation",
  "reply": "the message to the manager (may be empty for wait/confirm_booking)",
  "operator_question": "question for your principal, only for ask_operator, else empty",
  "restaurant_name": "restaurant name exactly as the manager wrote it, or null",
  "guest_name": "name for the booking exactly as given in FACTS or by the OPERATOR, or null",
  "manager_agreed": "boolean true/false: true only if the manager explicitly confirmed the table for the requested time, date and party",
  "manager_time": "HH:MM if the manager proposed or mentioned a time different from the requested one, else null"
}}"""

OPENING_TASK = """The chat is empty: the manager will not write first. Write the opening message that starts the conversation.
Greet them in a British manner appropriate to the current local time ({now_local}), introduce yourself as an assistant booking on behalf of {principal}, and ask whether a table for {party} at {time} today is available.
Use action "reply"; "understood" = "Conversation not started yet"."""

TURN_TASK = """Here is the conversation so far (local time in brackets):
{transcript}

Decide your next step. {operator_hint}Respond with the JSON object only."""

OPERATOR_HINT = ("The last message is the OPERATOR's private answer to the manager's pending question: your reply MUST first "
                 "pass that answer on to the manager in polite British English (do not ask anything else unless needed). "
                 "The operator may write in Ukrainian or Russian; it is your client's instruction, not information "
                 "from the restaurant (e.g. 'ні, тільки 17:00' = 'no, only 17:00 suits us'). ")


VERIFY_PROMPT = """You are a strict fact-checker for a restaurant booking assistant.
The assistant may only state facts that come from FACTS, from OPERATOR messages, or from what the MANAGER said.

FACTS:
{facts}

CONVERSATION:
{transcript}

DRAFT REPLY the assistant wants to send to the manager:
\"\"\"{reply}\"\"\"

Does the draft state or imply any fact about the guests or the booking (name, phone, email, allergies, dietary needs,
special requests, occasion, preferences, payment, a different time/date/party size, acceptance of a changed booking,
claims about the restaurant's availability that the manager did not make)
that is NOT supported by FACTS, OPERATOR messages or MANAGER messages?
Greetings, politeness, questions, saying it is an AI assistant, and repeating the requested booking are fine.
Answer only JSON: {{"ok": true/false, "unsupported": ["short description of each unsupported claim"]}}"""


class Brain:
    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        if client is None:
            from groq import Groq
            client = Groq(api_key=cfg.groq_api_key, timeout=30, max_retries=1)
        self.client = client

    def _messages(self, conv: Conversation, opening: bool, now_local: str) -> list[dict]:
        system = SYSTEM_PROMPT.format(
            party=conv.party_size, time=human_time(conv.target_time), date=human_date(conv.booking_date),
            principal=self.cfg.principal, facts=facts_block(self.cfg, conv),
        )
        if opening:
            user = OPENING_TASK.format(now_local=now_local, principal=self.cfg.principal,
                                       party=conv.party_size, time=human_time(conv.target_time))
        else:
            last = conv.turns[-1].role if conv.turns else ""
            user = TURN_TASK.format(transcript=conv.transcript(),
                                    operator_hint=OPERATOR_HINT if last in ("operator", "system") else "")
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def parse(raw: str, model: str = "") -> Decision:
        """Перевіряє JSON від моделі. Будь-яке порушення формату дає ValueError."""
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        action = str(data.get("action", "")).strip()
        if action not in ACTIONS:
            raise ValueError(f"bad action: {action!r}")
        reply = str(data.get("reply") or "").strip()
        if action in {"reply", "end_conversation"} and not reply:
            raise ValueError(f"empty reply for action {action}")
        question = str(data.get("operator_question") or "").strip()
        if action == "ask_operator" and not question:
            raise ValueError("ask_operator without operator_question")
        name = data.get("restaurant_name")
        name = str(name).strip() if name not in (None, "", "null") else None
        mtime = data.get("manager_time")
        mtime = str(mtime).strip() if mtime not in (None, "", "null") else None
        guest = data.get("guest_name")
        guest = str(guest).strip() if guest not in (None, "", "null") else None
        return Decision(
            action=action, reply=reply,
            understood=str(data.get("understood") or "").strip(),
            decision=str(data.get("decision") or "").strip(),
            operator_question=question, restaurant_name=name,
            manager_agreed=data.get("manager_agreed") is True, manager_time=mtime, guest_name=guest,
            model=model,
        )

    def verify(self, conv: Conversation, reply: str) -> list[str]:
        """Друга модель-перевірка: повертає список непідтверджених тверджень.

        Якщо перевірка сама не вдалась, кидає BrainError, і тоді відповідь не надсилається (fail-safe).
        """
        prompt = VERIFY_PROMPT.format(facts=facts_block(self.cfg, conv), transcript=conv.transcript(), reply=reply)
        raw = self._call([{"role": "user", "content": prompt}], max_tokens=300)
        data = json.loads(raw)
        if data.get("ok") is True:
            return []
        claims = [str(x) for x in (data.get("unsupported") or [])] or ["fact-checker returned ok=false"]
        return claims

    def _call(self, messages: list[dict], max_tokens: int) -> str:
        errors = []
        for model in [m.strip() for m in self.cfg.models if m.strip()]:
            for attempt in range(2):
                try:
                    resp = self.client.chat.completions.create(
                        model=model, messages=messages, temperature=0,
                        response_format={"type": "json_object"}, max_tokens=max_tokens,
                    )
                    raw = resp.choices[0].message.content or ""
                    json.loads(raw)
                    return raw
                except Exception as exc:
                    errors.append(f"{model}#{attempt + 1}: {type(exc).__name__}: {exc}"[:300])
                    time.sleep(1.5 * (attempt + 1))
        raise BrainError("; ".join(errors))

    def decide(self, conv: Conversation, opening: bool = False, now_local: str = "") -> Decision:
        messages = self._messages(conv, opening, now_local)
        errors = []
        for model in [m.strip() for m in self.cfg.models if m.strip()]:
            for attempt in range(2):
                try:
                    resp = self.client.chat.completions.create(
                        model=model, messages=messages, temperature=0.3,
                        response_format={"type": "json_object"}, max_tokens=700,
                    )
                    return self.parse(resp.choices[0].message.content or "", model=model)
                except Exception as exc:  # мережа, ліміт, невалідний JSON: пробуємо далі
                    errors.append(f"{model}#{attempt + 1}: {type(exc).__name__}: {exc}"[:300])
                    time.sleep(1.5 * (attempt + 1))
        raise BrainError("; ".join(errors))
