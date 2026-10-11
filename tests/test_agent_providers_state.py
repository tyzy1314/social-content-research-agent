"""Tests for agent.state.AgentStore, agent.tracing sinks, and the three model providers.

Covers the state store (seq assignment, event round-trip, message rebuilding, budgets),
the tracing sinks (JSONL / SQLite / fan-out), ScriptedProvider (mock replay), and
OpenAICompatibleProvider / OpencodeCliProvider against an in-test HTTP mock and a fake
CLI runner respectively. No test ever calls a real model or the real opencode binary.
"""
# allow: SIZE — the task mandates one test module covering the frozen contract's five required areas; HTTP mock and fake runner replace live I/O.

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.errors import ModelError, ModelProtocolError, ProviderNotAvailable
from agent.providers import OpencodeCliProvider, OpenAICompatibleProvider, ScriptedProvider
from agent.state import AgentStore
from agent.tracing import JsonlTraceSink, MultiTraceSink, SqliteTraceSink
from agent.types import Event, ModelTurn, ToolCall


class RecordingSink:
    """TraceSink double that records every emitted event in memory."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    """Serve scripted responses from ``server.state`` (responses list + request log)."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        state = self.server.state  # type: ignore[attr-defined]
        state["requests"].append({"headers": dict(self.headers), "body": json.loads(body)})
        status, text = state["responses"].pop(0)
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Silence per-request stderr logging."""


@pytest.fixture
def mock_openai():
    """In-test OpenAI-compatible endpoint: pop (status, body) pairs per request."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    server.state = {"responses": [], "requests": []}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def store(tmp_path) -> AgentStore:
    """AgentStore backed by a throwaway SQLite file."""
    return AgentStore(tmp_path / "agent_runs.db")


# ---------------------------------------------------------------------------
# AgentStore
# ---------------------------------------------------------------------------


def test_create_and_get_run(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="分析小红书数据", provider="scripted", model=None, max_steps=5)
    run = store.get_run("r1")
    assert run is not None
    assert run["run_id"] == "r1"
    assert run["task"] == "分析小红书数据"
    assert run["status"] == "running"
    assert run["provider"] == "scripted"
    assert run["model"] is None
    assert run["max_steps"] == 5
    assert run["steps_used"] == 0
    assert store.get_run("missing") is None


def test_append_event_seq_monotonic_per_run(store: AgentStore) -> None:
    for run_id in ("a", "b"):
        store.create_run(run_id=run_id, task="t", provider="p", model="m", max_steps=1)
    seqs_a = [
        store.append_event("a", step=0, role="user", kind="task", payload={"task": "x"}).seq,
        store.append_event("b", step=0, role="user", kind="task", payload={"task": "y"}).seq,
        store.append_event("a", step=1, role="assistant", kind="final", payload={"final_text": "d"}).seq,
        store.append_event("b", step=1, role="assistant", kind="final", payload={"final_text": "e"}).seq,
    ]
    assert seqs_a == [1, 1, 2, 2]


def test_events_round_trip_and_payload_encoding(store: AgentStore, tmp_path) -> None:
    store.create_run(run_id="r1", task="t", provider="p", model="m", max_steps=1)
    payload = {"b": 1, "a": "分析"}
    stored = store.append_event("r1", step=2, role="assistant", kind="decision", payload=payload)
    events = store.events("r1")
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].step == 2
    assert events[0].role == "assistant"
    assert events[0].kind == "decision"
    assert dict(events[0].payload) == payload
    assert stored.ts.endswith("+00:00") or "+00:00" in stored.ts
    conn = sqlite3.connect(str(tmp_path / "agent_runs.db"))
    raw = conn.execute("SELECT payload FROM agent_events WHERE run_id='r1'").fetchone()[0]
    conn.close()
    assert raw == '{"a": "分析", "b": 1}'  # ensure_ascii=False + sort_keys=True


def test_messages_rebuild_openai_style(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="分析小红书数据", provider="scripted", model=None, max_steps=3)
    store.append_event("r1", step=0, role="user", kind="task", payload={"task": "分析小红书数据"})
    store.append_event(
        "r1",
        step=1,
        role="assistant",
        kind="decision",
        payload={"tool_call_id": "call_1", "tool": "db_stats", "args": {}},
    )
    store.append_event(
        "r1",
        step=1,
        role="tool",
        kind="tool_result",
        payload={"tool_call_id": "call_1", "content": "rows=3"},
    )
    store.append_event("r1", step=2, role="assistant", kind="final", payload={"final_text": "共 3 条"})
    assert store.messages("r1") == [
        {"role": "user", "content": "分析小红书数据"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "db_stats", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "rows=3"},
        {"role": "assistant", "content": "共 3 条"},
    ]


def test_messages_group_consecutive_decisions_and_skip_trace(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="t", provider="p", model="m", max_steps=3)
    store.append_event("r1", step=0, role="user", kind="task", payload={"task": "t"})
    store.append_event(
        "r1", step=1, role="assistant", kind="decision",
        payload={"tool_call_id": "c1", "tool": "db_stats", "args": {}},
    )
    store.append_event(
        "r1", step=1, role="assistant", kind="decision",
        payload={"tool_call_id": "c2", "tool": "db_query", "args": {"sql": "SELECT 1"}},
    )
    store.append_event("r1", step=1, role="trace", kind="provider_call", payload={"provider": "openai"})
    store.append_event("r1", step=2, role="assistant", kind="final", payload={"final_text": "done"})
    messages = store.messages("r1")
    assert len(messages) == 3
    assert messages[1]["role"] == "assistant"
    assert [tc["id"] for tc in messages[1]["tool_calls"]] == ["c1", "c2"]
    assert messages[1]["tool_calls"][1]["function"]["name"] == "db_query"
    assert messages[2] == {"role": "assistant", "content": "done"}


def test_update_run_and_validation(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="t", provider="p", model="m", max_steps=2)
    store.update_run("r1", status="finished", steps_used=2, final_text="ok")
    run = store.get_run("r1")
    assert run["status"] == "finished"
    assert run["steps_used"] == 2
    assert run["final_text"] == "ok"
    with pytest.raises(ValueError):
        store.update_run("r1", bogus=1)
    with pytest.raises(KeyError):
        store.update_run("missing", status="failed")


def test_budget_bump_sequential_atomicity(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="t", provider="p", model="m", max_steps=5)
    assert store.budget_get("r1", "db_query") == 0
    assert store.budget_bump("r1", "db_query") == 1
    assert store.budget_bump("r1", "db_query") == 2
    assert store.budget_get("r1", "db_query") == 2
    assert store.budget_get("r1", "export_data") == 0


def test_list_runs_ordering(store: AgentStore) -> None:
    for run_id in ("r1", "r2", "r3"):
        store.create_run(run_id=run_id, task="t", provider="p", model="m", max_steps=1)
    runs = store.list_runs()
    assert [r["run_id"] for r in runs] == ["r3", "r2", "r1"]
    assert [r["run_id"] for r in store.list_runs(limit=2)] == ["r3", "r2"]


# ---------------------------------------------------------------------------
# tracing
# ---------------------------------------------------------------------------


def test_jsonl_trace_sink(tmp_path) -> None:
    sink = JsonlTraceSink("run-42", base_dir=tmp_path / "traces")
    sink.emit(Event(seq=0, ts="t0", step=0, role="trace", kind="provider_call", payload={"provider": "scripted"}))
    sink.emit(Event(seq=0, ts="t1", step=1, role="trace", kind="checkpoint", payload={"n": 2}))
    sink.close()
    sink.close()  # idempotent
    lines = (tmp_path / "traces" / "run-42.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["provider_call", "checkpoint"]
    assert json.loads(lines[0])["payload"] == {"provider": "scripted"}


def test_sqlite_trace_sink_excluded_from_messages(store: AgentStore) -> None:
    store.create_run(run_id="r1", task="t", provider="p", model="m", max_steps=1)
    sink = SqliteTraceSink(store, run_id="r1")
    sink.emit(Event(seq=0, ts="t0", step=0, role="trace", kind="provider_call", payload={"provider": "openai"}))
    sink.close()
    events = store.events("r1")
    assert len(events) == 1
    assert events[0].role == "trace"
    assert store.messages("r1") == []


def test_multi_trace_sink_fan_out(store: AgentStore, tmp_path) -> None:
    first, second = RecordingSink(), RecordingSink()
    multi = MultiTraceSink([first, second])
    event = Event(seq=0, ts="t0", step=0, role="trace", kind="provider_call", payload={})
    multi.emit(event)
    multi.close()
    assert first.events == [event]
    assert second.events == [event]
    assert first.closed and second.closed


# ---------------------------------------------------------------------------
# ScriptedProvider (MOCK — not evidence of real model behaviour)
# ---------------------------------------------------------------------------


def test_scripted_provider_dict_script_and_trace() -> None:
    sink = RecordingSink()
    provider = ScriptedProvider(
        [{"tool": "db_stats", "args": {"deep": True}}, {"final": "done"}],
        sink=sink,
    )
    turn1 = provider.complete(system="s", messages=[], tools=[], step=0)
    assert turn1.tool_calls == (ToolCall(tool="db_stats", args={"deep": True}, call_id="call_0"),)
    turn2 = provider.complete(system="s", messages=[], tools=[], step=1)
    assert turn2.final_text == "done"
    assert turn2.tool_calls == ()
    assert [e.kind for e in sink.events] == ["provider_call", "provider_call"]
    assert all(e.payload["provider"] == "scripted" for e in sink.events)


def test_scripted_provider_accepts_model_turns() -> None:
    preset = ModelTurn(tool_calls=(ToolCall(tool="t", args={}, call_id="native"),), finish_reason="tool_calls")
    provider = ScriptedProvider([preset])
    assert provider.complete(system="s", messages=[], tools=[], step=0) is preset


def test_scripted_provider_exhaustion_raises() -> None:
    sink = RecordingSink()
    provider = ScriptedProvider([{"final": "only"}], sink=sink)
    provider.complete(system="s", messages=[], tools=[], step=0)
    with pytest.raises(ModelProtocolError):
        provider.complete(system="s", messages=[], tools=[], step=1)
    assert sink.events[-1].payload["error"] == "ModelProtocolError"


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider (in-test http.server mock)
# ---------------------------------------------------------------------------


def _tool_calls_body() -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "db_stats", "arguments": '{"deep": true}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def test_openai_translates_tool_calls(mock_openai) -> None:
    mock_openai.state["responses"].append((200, _tool_calls_body()))  # type: ignore[attr-defined]
    sink = RecordingSink()
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="test-key",
        model="test-model",
        sink=sink,
    )
    turn = provider.complete(
        system="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "db_stats", "description": "stats", "parameters": {}}}],
        step=0,
    )
    assert turn.tool_calls == (ToolCall(tool="db_stats", args={"deep": True}, call_id="call_abc"),)
    assert turn.final_text is None
    assert turn.finish_reason == "tool_calls"
    assert turn.usage == {"prompt": 10, "completion": 5, "total": 15}
    request = mock_openai.state["requests"][0]  # type: ignore[attr-defined]
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["body"]["model"] == "test-model"
    assert request["body"]["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert request["body"]["tool_choice"] == "auto"
    assert len(sink.events) == 1
    assert sink.events[0].kind == "provider_call"


def test_openai_plain_text_response(mock_openai) -> None:
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "你好"}}]})
    mock_openai.state["responses"].append((200, body))  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="k",
        model="m",
    )
    turn = provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert turn.final_text == "你好"
    assert turn.tool_calls == ()
    assert turn.finish_reason == "stop"


def test_openai_retries_429_then_succeeds(mock_openai, monkeypatch) -> None:
    monkeypatch.setattr("agent.providers._backoff_delay", lambda attempt: 0)
    state = mock_openai.state  # type: ignore[attr-defined]
    state["responses"].append((429, '{"error": "rate limited"}'))
    state["responses"].append((200, json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})))
    sink = RecordingSink()
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="k",
        model="m",
        max_retries=3,
        sink=sink,
    )
    turn = provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert turn.final_text == "ok"
    assert len(state["requests"]) == 2
    assert sink.events[0].payload["attempts"] == 2


def test_openai_400_is_fatal_single_request(mock_openai) -> None:
    mock_openai.state["responses"].append((400, '{"error": "bad request"}'))  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="k",
        model="m",
        max_retries=3,
    )
    with pytest.raises(ModelError):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert len(mock_openai.state["requests"]) == 1  # type: ignore[attr-defined]


def test_openai_malformed_json_is_protocol_error(mock_openai) -> None:
    mock_openai.state["responses"].append((200, "not-json{{"))  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="k",
        model="m",
    )
    with pytest.raises(ModelProtocolError):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert len(mock_openai.state["requests"]) == 1  # type: ignore[attr-defined]


def test_openai_invalid_tool_arguments_is_protocol_error(mock_openai) -> None:
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "not-json"}}
                        ]
                    }
                }
            ]
        }
    )
    mock_openai.state["responses"].append((200, body))  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        base_url=f"http://127.0.0.1:{mock_openai.server_address[1]}",
        api_key="k",
        model="m",
    )
    with pytest.raises(ModelProtocolError):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)


# ---------------------------------------------------------------------------
# OpencodeCliProvider (fake runner — the real binary is never invoked)
# ---------------------------------------------------------------------------


def _install_fake_runner(monkeypatch, outputs: list[str]) -> list[str]:
    """Replace _run_cli with a queue of canned stdout strings; returns the prompt log."""
    prompts: list[str] = []

    def fake_run_cli(self: OpencodeCliProvider, prompt: str) -> str:
        prompts.append(prompt)
        return outputs[min(len(prompts) - 1, len(outputs) - 1)]

    monkeypatch.setattr(OpencodeCliProvider, "_run_cli", fake_run_cli)
    return prompts


def test_opencode_missing_binary_raises_provider_not_available(tmp_path, monkeypatch) -> None:
    sink = RecordingSink()
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"), sink=sink)

    def missing(self: OpencodeCliProvider, prompt: str) -> str:
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(OpencodeCliProvider, "_run_cli", missing)
    with pytest.raises(ProviderNotAvailable):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert sink.events[-1].payload["error"] == "ProviderNotAvailable"
    provider.close()


def test_opencode_contamination_rejected_after_retries(tmp_path, monkeypatch) -> None:
    prompts = _install_fake_runner(
        monkeypatch,
        [json.dumps({"type": "tool_use", "tool": "bash", "args": {"cmd": "ls"}}) + "\n"],
    )
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"), max_retries=2)
    tools = [{"type": "function", "function": {"name": "db_stats", "description": "stats", "parameters": {}}}]
    with pytest.raises(ModelProtocolError, match="tool_use"):
        provider.complete(system="sys", messages=[{"role": "user", "content": "查一下数据"}], tools=tools, step=0)
    assert len(prompts) == 3  # initial + 2 stricter re-prompts
    assert "Respond with ONLY a JSON object. Do not use any tools." in prompts[0]
    assert "attempted to use tools" in prompts[1]
    provider.close()


def test_opencode_text_decision_returns_tool_call(tmp_path, monkeypatch) -> None:
    decision = json.dumps({"action": "db_stats", "args": {"table": "content"}})
    stdout = json.dumps({"type": "text", "part": {"text": f"```json\n{decision}\n```"}}) + "\n"
    prompts = _install_fake_runner(monkeypatch, [stdout])
    sink = RecordingSink()
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"), sink=sink)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "db_stats",
                "description": "数据库统计",
                "parameters": {"type": "object", "properties": {"table": {"type": "string"}}},
            },
        }
    ]
    turn = provider.complete(
        system="你是数据助手",
        messages=[{"role": "user", "content": "统计一下"}],
        tools=tools,
        step=3,
    )
    assert turn.tool_calls == (ToolCall(tool="db_stats", args={"table": "content"}, call_id="call_3"),)
    assert turn.final_text is None
    prompt = prompts[0]
    assert "统计一下" in prompt
    assert "db_stats" in prompt and "数据库统计" in prompt
    assert "Respond with ONLY a JSON object. Do not use any tools." in prompt
    assert sink.events[-1].kind == "provider_call"
    provider.close()


def test_opencode_finish_decision(tmp_path, monkeypatch) -> None:
    stdout = json.dumps({"type": "text", "part": {"text": '{"action": "FINISH", "args": {"final": "共 3 条"}}'}})
    _install_fake_runner(monkeypatch, [stdout])
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"))
    turn = provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    assert turn.final_text == "共 3 条"
    assert turn.tool_calls == ()
    provider.close()


def test_opencode_unknown_action_is_protocol_error(tmp_path, monkeypatch) -> None:
    stdout = json.dumps({"type": "text", "part": {"text": '{"action": "no_such_tool", "args": {}}'}})
    _install_fake_runner(monkeypatch, [stdout])
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"))
    tools = [{"type": "function", "function": {"name": "db_stats", "description": "d", "parameters": {}}}]
    with pytest.raises(ModelProtocolError):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=tools, step=0)
    provider.close()


def test_opencode_isolated_workdir(tmp_path) -> None:
    provider = OpencodeCliProvider(workdir=str(tmp_path / "iso"))
    config = json.loads((tmp_path / "iso" / "opencode.json").read_text(encoding="utf-8"))
    assert config == {"$schema": "https://opencode.ai/config.json"}
    provider.close()
    assert (tmp_path / "iso" / "opencode.json").exists()  # caller-provided dir is not removed

    default_provider = OpencodeCliProvider()
    workdir = default_provider._workdir
    assert workdir.exists()
    default_provider.close()
    assert not workdir.exists()  # throwaway temp dir is cleaned up


def test_opencode_non_json_line_is_protocol_error(tmp_path, monkeypatch) -> None:
    _install_fake_runner(monkeypatch, ["this is not json\n"])
    provider = OpencodeCliProvider(workdir=str(tmp_path / "oc"))
    with pytest.raises(ModelProtocolError):
        provider.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[], step=0)
    provider.close()
