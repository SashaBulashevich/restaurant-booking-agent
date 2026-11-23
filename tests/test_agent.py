"""Автотести без мережі: фальшивий LLM-клієнт віддає заздалегідь задані відповіді.

Головне, що перевіряємо: за будь-якого збою або сумнівної відповіді менеджер
не отримує нічого вигаданого, а оператор отримує сповіщення (fail-safe).
"""
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent import brain as brain_mod
from agent.brain import Brain
from agent.config import Config
from agent.core import HOLDING, Agent, extract_time
from agent.state import CONFIRMED, WAITING_OPERATOR, Conversation


class FakeClient:
    """Імітує groq.Groq: кожен виклик бере наступну відповідь із черги."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        item = self.replies.pop(0) if self.replies else RuntimeError("no more replies")
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def decision(**kw):
    base = {"understood": "u", "decision": "d", "action": "reply", "reply": "Thank you.",
            "operator_question": "", "restaurant_name": None, "guest_name": None,
            "manager_agreed": False, "manager_time": None}
    base.update(kw)
    return base


OK = {"ok": True, "unsupported": []}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(brain_mod.time, "sleep", lambda s: None)


def make_agent(tmp_path, replies, **cfg_kw):
    cfg = Config()
    cfg.log_dir = tmp_path
    cfg.guest_name = None
    cfg.guest_phone = None
    cfg.models = ["model-a", "model-b"]
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    agent = Agent(cfg, Brain(cfg, client=FakeClient(replies)))
    today = datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
    agent.conv = Conversation(booking_date=today, booking_time="17:00", party_size=2)
    agent.conv.add("agent", "Good afternoon! Table for two at 17:00 today?", cfg.timezone)
    return agent


def to_manager(outs):
    return [o.text for o in outs if o.to == "manager"]


def to_operator(outs):
    return [o.text for o in outs if o.to == "operator"]


def test_all_models_down_nothing_sent_to_manager(tmp_path):
    agent = make_agent(tmp_path, [TimeoutError()] * 4)
    outs = agent.on_manager("Yes, we have a table.")
    assert to_manager(outs) == []
    assert "LLM недоступна" in to_operator(outs)[0]


def test_invalid_json_is_fail_safe(tmp_path):
    agent = make_agent(tmp_path, ["not json", "{}", '{"action": "dance"}', decision(action="reply", reply="")])
    outs = agent.on_manager("Hello?")
    assert to_manager(outs) == [] and to_operator(outs)


def test_fallback_model_is_used(tmp_path):
    agent = make_agent(tmp_path, [TimeoutError(), TimeoutError(), decision(reply="Lovely, thank you."), OK])
    outs = agent.on_manager("Hi there")
    assert to_manager(outs) == ["Lovely, thank you."]


def test_invented_restaurant_name_rejected(tmp_path):
    # Модель «знає» назву, якої менеджер не писав, тому агент має спитати назву.
    agent = make_agent(tmp_path, [decision(action="confirm_booking", reply="", manager_agreed=True,
                                           restaurant_name="The Ritz")])
    outs = agent.on_manager("Yes, booked for two at 17:00.")
    assert agent.conv.status != CONFIRMED
    assert "name of the restaurant" in to_manager(outs)[0]


def test_confirmation_recap_contains_all_details(tmp_path):
    agent = make_agent(tmp_path, [decision(action="confirm_booking", reply="", manager_agreed=True,
                                           restaurant_name="Dishoom")])
    outs = agent.on_manager("Booked: Dishoom, 17:00, two people.")
    recap = to_manager(outs)[0]
    assert agent.conv.status == CONFIRMED
    for part in ("two", "17:00", "today", "Dishoom", agent.conv.booking_date[:4]):
        assert part in recap


def test_different_time_needs_operator(tmp_path):
    agent = make_agent(tmp_path, [decision(action="confirm_booking", reply="", manager_agreed=True,
                                           restaurant_name="Dishoom", manager_time="18:30")])
    outs = agent.on_manager("Dishoom here, we can do 18:30 for two, booked.")
    assert agent.conv.status == WAITING_OPERATOR
    assert agent.conv.pending_time == "18:30"
    assert "18:30" in to_operator(outs)[0]


def test_operator_approves_new_time(tmp_path):
    agent = make_agent(tmp_path, [
        decision(action="ask_operator", reply="One moment please.", operator_question="Accept 18:30?"),
        decision(reply="18:30 would be lovely, thank you."), OK,
    ])
    agent.on_manager("17:00 is full, 18:30?")
    agent.on_operator("так")
    assert agent.conv.target_time == "18:30"


def test_fact_checker_blocks_invented_facts(tmp_path):
    agent = make_agent(tmp_path, [decision(reply="No allergies at all, thank you."),
                                  {"ok": False, "unsupported": ["claims no allergies"]}])
    outs = agent.on_manager("Any allergies?")
    assert to_manager(outs) == [HOLDING]
    assert agent.conv.status == WAITING_OPERATOR


def test_fact_checker_down_blocks_reply(tmp_path):
    agent = make_agent(tmp_path, [decision(reply="Lovely.")] + [TimeoutError()] * 4)
    outs = agent.on_manager("Hello")
    assert "Lovely." not in to_manager(outs)
    assert agent.conv.status == WAITING_OPERATOR


def test_unknown_phone_not_sent(tmp_path):
    agent = make_agent(tmp_path, [decision(reply="Sure, call us on +44 20 7946 0000.")])
    outs = agent.on_manager("Contact number?")
    assert "7946" not in " ".join(to_manager(outs))


def test_messages_while_waiting_operator_are_forwarded(tmp_path):
    agent = make_agent(tmp_path, [])
    agent.conv.status = WAITING_OPERATOR
    outs = agent.on_manager("Hello? Still there?")
    assert to_manager(outs) == [] and "Still there" in to_operator(outs)[0]


def test_start_refuses_when_too_late(tmp_path):
    cfg_time = (datetime.now(ZoneInfo("Europe/London"))).strftime("%H:%M")
    agent = make_agent(tmp_path, [], booking_time=cfg_time)
    agent.conv = None
    outs = agent.start()
    assert agent.conv is None and to_manager(outs) == []


def test_opening_falls_back_to_template(tmp_path):
    agent = make_agent(tmp_path, [TimeoutError()] * 4, booking_time="23:59")
    agent.conv = None
    outs = agent.start()
    text = to_manager(outs)[0]
    assert "two" in text and "11:59 pm" in text


@pytest.mark.parametrize("text,expected", [
    ("we can do 18:30 for two", "18:30"), ("how about 6.30pm?", "18:30"),
    ("7 pm works", "19:00"), ("table for 2", None), ("12am", "00:00"),
])
def test_extract_time(text, expected):
    assert extract_time(text) == expected
