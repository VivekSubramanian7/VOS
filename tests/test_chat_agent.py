"""Chat agent tests — scripted model, fake graph, injected clock. No provider.

The properties that matter: the tool loop terminates no matter what the model does
(rounds cap), tools only ever read, sessions are isolated and expire, the budget
refusal costs zero model calls, and every model call lands in the cassette so
BudgetGuard sees chat spend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_shell import FakeGraph

from vos.cassette import Cassette
from vos.contracts import CaptureRecord

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from vos.web.chat_agent import (  # noqa: E402
    _BUDGET_REFUSAL,
    _FALLBACK,
    KitchenChat,
    SessionStore,
    chat_session_key,
)


class ScriptedModel:
    """Plays back a fixed sequence of AIMessages; repeats the last one forever.

    `bind_tools` returns self — the agent only ever calls `ainvoke` on the result,
    and recording the bound tools lets tests assert what the model was offered.
    """

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list] = []
        self.bound_tools: list | None = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        template = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        # A real model returns a *new* message each call. Reusing one object would
        # let add_messages dedupe by id and silently replace instead of append.
        message = template.model_copy(deep=True)
        message.id = f"scripted-{len(self.calls)}"
        return message


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _graph_with_thought(text: str = "buy oat milk") -> FakeGraph:
    graph = FakeGraph()
    record = CaptureRecord.create(
        chat_id=42, message_id=1, text=text, captured_at=datetime.now(UTC)
    )
    graph.thoughts[record.id] = {"record": record, "status": "captured"}
    return graph


# --- happy paths ----------------------------------------------------------- #


async def test_plain_chat_needs_no_tools():
    model = ScriptedModel([AIMessage(content="Hi! What can I help with?")])
    chat = KitchenChat(model, FakeGraph())

    reply = await chat.reply("s1", "hello")

    assert reply == "Hi! What can I help with?"
    assert len(model.calls) == 1


async def test_tool_call_reaches_the_graph_and_informs_the_reply():
    model = ScriptedModel(
        [
            _tool_call("search_thoughts", {"term": "milk"}),
            AIMessage(content="You noted: buy oat milk."),
        ]
    )
    chat = KitchenChat(model, _graph_with_thought("buy oat milk"))

    reply = await chat.reply("s1", "did anyone mention milk?")

    assert reply == "You noted: buy oat milk."
    assert len(model.calls) == 2
    # The tool's output must be visible to the second model call.
    second_call = model.calls[1]
    assert any("oat milk" in str(getattr(m, "content", "")) for m in second_call)


async def test_history_carries_across_turns():
    model = ScriptedModel([AIMessage(content="noted")])
    chat = KitchenChat(model, FakeGraph())

    await chat.reply("s1", "my name is Ada")
    await chat.reply("s1", "what is my name?")

    # The second turn's prompt must contain the first turn's exchange.
    latest_prompt = model.calls[-1]
    texts = [str(getattr(m, "content", "")) for m in latest_prompt]
    assert any("my name is Ada" in t for t in texts)


async def test_sessions_are_isolated():
    model = ScriptedModel([AIMessage(content="ok")])
    chat = KitchenChat(model, FakeGraph())

    await chat.reply("alice", "alice's secret")
    await chat.reply("bob", "hi")

    bob_prompt = model.calls[-1]
    assert not any(
        "alice's secret" in str(getattr(m, "content", "")) for m in bob_prompt
    )


# --- termination ------------------------------------------------------------ #


async def test_rounds_cap_bounds_a_tool_hungry_model():
    """A model that always wants another tool call must terminate at the cap and
    the user must get a graceful sentence, not an empty message."""
    model = ScriptedModel([_tool_call("recent_thoughts", {})])
    chat = KitchenChat(model, FakeGraph(), max_tool_rounds=2)

    reply = await chat.reply("s1", "loop forever")

    assert reply == _FALLBACK
    assert len(model.calls) == 3  # cap + the final refused round


async def test_model_failure_degrades_to_an_apology():
    class ExplodingModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("provider down")

    chat = KitchenChat(ExplodingModel(), FakeGraph())
    reply = await chat.reply("s1", "hello")
    assert "can't reach the model" in reply


# --- budget ------------------------------------------------------------------ #


async def test_budget_refusal_costs_no_model_calls():
    class ExceededBudget:
        def exceeded(self) -> bool:
            return True

    model = ScriptedModel([AIMessage(content="never sent")])
    chat = KitchenChat(model, FakeGraph(), budget=ExceededBudget())

    reply = await chat.reply("s1", "hello")

    assert reply == _BUDGET_REFUSAL
    assert model.calls == []


# --- cassette ----------------------------------------------------------------- #


async def test_every_model_call_lands_in_the_cassette(tmp_path: Path):
    model = ScriptedModel(
        [
            _tool_call("recent_thoughts", {}),
            AIMessage(content="here you go"),
        ]
    )
    cassette = Cassette(tmp_path / "cassettes")
    chat = KitchenChat(model, FakeGraph(), model_name="anthropic:claude-opus-5", cassette=cassette)

    await chat.reply("s1", "what's new?")

    entries = cassette.entries(chat_session_key("s1"))
    assert len(entries) == 2  # one per model call, spend visible to BudgetGuard
    assert all(e.model == "anthropic:claude-opus-5" for e in entries)


# --- SessionStore -------------------------------------------------------------- #


def test_session_store_expires_after_ttl():
    now = {"t": 0.0}
    store = SessionStore(ttl_s=10.0, clock=lambda: now["t"])
    store.put("s1", [HumanMessage(content="hi")])

    now["t"] = 5.0
    assert store.get("s1"), "session evicted before its TTL"

    now["t"] = 20.1
    assert store.get("s1") == [], "session survived past its TTL"


def test_session_store_trims_to_max_messages():
    store = SessionStore(max_messages=4)
    store.put("s1", [HumanMessage(content=str(i)) for i in range(10)])
    kept = store.get("s1")
    assert len(kept) == 4
    assert [m.content for m in kept] == ["6", "7", "8", "9"]  # oldest dropped


def test_session_store_returns_copies():
    store = SessionStore()
    store.put("s1", [HumanMessage(content="hi")])
    store.get("s1").append(HumanMessage(content="mutation"))
    assert len(store.get("s1")) == 1
