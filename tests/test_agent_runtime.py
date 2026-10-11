#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime, integration, and approval tests for the agent execution loop (Tier 1/2).

These tests use *self-contained* fake tools (no crawler DB, no network) so the loop
mechanics — normal closure, step budget, stuck detection, per-tool budget, approval
gating, permission denial, and resume — are exercised deterministically. The OpenAI
provider is exercised against an in-process ``http.server`` mock (Tier 2): real HTTP,
real JSON serialisation, real tool-call parsing, no external service.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import pytest

from agent.permissions import PermissionGate
from agent.providers import OpenAICompatibleProvider, ScriptedProvider
from agent.runtime import AgentRuntime, RuntimeConfig
from agent.state import AgentStore
from agent.tools import ToolRegistry, spec_from_function
from agent.types import RunStatus, ToolCall


def _echo(msg: str) -> str:
    """Read-only tool: returns its argument prefixed."""
    return f"echo:{msg}"


def _ping(x: int) -> int:
    """Read-only tool: echoes an integer (used for stuck-loop detection)."""
    return x


#: Shared sink so tests can observe side-effecting tool invocations.
_CALL_SINK: list[str] = []


def _mutate(x: str) -> str:
    """Side-effecting tool: records its argument and returns it."""
    _CALL_SINK.append(x)
    return f"mutated:{x}"


def _boom(x: str) -> str:
    """Read-only tool capped at two invocations via per-run budget."""
    return f"boom:{x}"


def _build_registry(echo=True, ping=True, boom_budget=None) -> ToolRegistry:
    registry = ToolRegistry()
    if echo:
        registry.register(spec_from_function(_echo, name="echo", description="read-only echo", tags=("read",)))
    if ping:
        registry.register(spec_from_function(_ping, name="ping", description="read-only ping", tags=("read",)))
    registry.register(
        spec_from_function(
            _mutate,
            name="mutate",
            description="side-effecting",
            mutates=True,
            tags=("db", "write"),
        )
    )
    if boom_budget is not None:
        registry.register(
            spec_from_function(_boom, name="boom", description="capped", tags=("read",), max_invocations=boom_budget)
        )
    return registry


def _runtime(store, registry, *, policy="deny_mutations", max_steps=10, sink=None, approver=None, gate=None):
    gate = gate or PermissionGate(allow_all=(policy == "allow_all"))
    config = RuntimeConfig(max_steps=max_steps, approval_policy=policy)
    return AgentRuntime(
        provider=None,  # replaced by caller via attribute, or we build with provider
        registry=registry,
        store=store,
        gate=gate,
        config=config,
        sink=sink,
        approver=approver,
    )


def test_normal_closure_finishes(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()
    provider = ScriptedProvider([{"tool": "echo", "args": {"msg": "hi"}}, {"final": "all done"}])
    rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    result = rt.run("do the thing")

    assert result.status == "finished"
    assert result.final_text == "all done"
    assert result.tool_calls == 1
    assert result.steps_used == 2
    kinds = [e.kind for e in result.events]
    assert "task" in kinds and "decision" in kinds and "tool_result" in kinds and "final" in kinds


def test_approval_blocked_under_deny_mutations(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    _CALL_SINK.clear()
    registry = _build_registry()
    provider = ScriptedProvider([{"tool": "mutate", "args": {"x": "a"}}, {"final": "ok"}])
    rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=False))
    result = rt.run("mutate something")

    assert result.status == "blocked"
    assert "approval" in (result.error or "").lower()
    assert _CALL_SINK == []  # the side-effecting tool must NOT have executed
    assert result.tool_calls == 1  # the attempt itself is counted


def test_allow_all_executes_mutating_tool(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    _CALL_SINK.clear()
    registry = _build_registry()
    provider = ScriptedProvider([{"tool": "mutate", "args": {"x": "a"}}, {"final": "ok"}])
    rt = AgentRuntime(
        provider=provider,
        registry=registry,
        store=store,
        gate=PermissionGate(allow_all=True),
        config=RuntimeConfig(approval_policy="allow_all"),
    )
    result = rt.run("mutate something")

    assert result.status == "finished"
    assert _CALL_SINK == ["a"]  # the side-effecting tool DID execute
    assert result.final_text == "ok"


def test_ask_policy_consults_approver(tmp_path):
    _CALL_SINK.clear()
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()
    provider = ScriptedProvider([{"tool": "mutate", "args": {"x": "a"}}, {"final": "ok"}])

    # approver denies -> blocked
    rt_no = AgentRuntime(
        provider=provider,
        registry=registry,
        store=store,
        gate=PermissionGate(allow_all=False),
        config=RuntimeConfig(approval_policy="ask"),
        approver=lambda call: False,
    )
    res_no = rt_no.run("x")
    assert res_no.status == "blocked"

    # fresh run, approver approves -> executes
    _CALL_SINK.clear()
    store2 = AgentStore(tmp_path / "agent2.db")
    registry2 = _build_registry()
    provider2 = ScriptedProvider([{"tool": "mutate", "args": {"x": "a"}}, {"final": "ok"}])
    rt_yes = AgentRuntime(
        provider=provider2,
        registry=registry2,
        store=store2,
        gate=PermissionGate(allow_all=False),
        config=RuntimeConfig(approval_policy="ask"),
        approver=lambda call: True,
    )
    res_yes = rt_yes.run("x")
    assert res_yes.status == "finished"
    assert _CALL_SINK == ["a"]


def test_stuck_detection(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry(echo=False, ping=True)
    # Repeat the identical (tool, args) call three times; threshold is 3 within window 6.
    provider = ScriptedProvider(
        [
            {"tool": "ping", "args": {"x": 1}},
            {"tool": "ping", "args": {"x": 1}},
            {"tool": "ping", "args": {"x": 1}},
            {"final": "never reached"},
        ]
    )
    rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    result = rt.run("loop")

    assert result.status == "stuck"
    assert result.steps_used == 3


def test_max_steps_exceeded(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry(ping=False, echo=True)
    provider = ScriptedProvider([{"tool": "echo", "args": {"msg": str(i)}} for i in range(8)])
    rt = AgentRuntime(
        provider=provider,
        registry=registry,
        store=store,
        gate=PermissionGate(allow_all=True),
        config=RuntimeConfig(max_steps=5),
    )
    result = rt.run("spin")

    assert result.status == "exceeded"
    assert result.steps_used == 5
    assert result.tool_calls == 5


def test_per_tool_budget_exceeded(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry(boom_budget=2)
    provider = ScriptedProvider(
        [
            {"tool": "boom", "args": {"x": "1"}},
            {"tool": "boom", "args": {"x": "2"}},
            {"tool": "boom", "args": {"x": "3"}},
            {"final": "unreached"},
        ]
    )
    rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    result = rt.run("overspend")

    assert result.status == "exceeded"  # budget exhaustion terminates the run
    assert result.tool_calls == 3  # the over-budget attempt counts as attempted


def test_permission_denied_blocks_tool(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()  # echo is read-tagged
    provider = ScriptedProvider([{"tool": "echo", "args": {"msg": "hi"}}, {"final": "ok"}])
    gate = PermissionGate(allowed_capabilities=[])  # no capabilities granted at all
    rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=gate)
    result = rt.run("x")

    assert result.status == "blocked"
    assert "permission" in (result.error or "").lower()


def test_resume_continues_blocked_run(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    _CALL_SINK.clear()
    registry = _build_registry()
    # One provider instance is shared so its script index survives the first (blocked) run.
    provider = ScriptedProvider([{"tool": "mutate", "args": {"x": "a"}}, {"final": "approved-final"}])

    rt1 = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=False))
    res1 = rt1.run("mutate then finish")
    assert res1.status == "blocked"

    # Resume with an approving policy; the loop continues from the stored step counter
    # and the provider emits its next (final) turn.
    rt2 = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    res2 = rt2.resume(res1.run_id)

    assert res2.status == "finished"
    assert res2.final_text == "approved-final"


def test_resume_finished_run_returns_as_is(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()
    provider = ScriptedProvider([{"tool": "echo", "args": {"msg": "hi"}}, {"final": "done"}])
    rt1 = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    res1 = rt1.run("x")
    assert res1.status == "finished"

    rt2 = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
    res2 = rt2.resume(res1.run_id)
    assert res2.status == "finished"
    assert res2.run_id == res1.run_id


def test_tool_result_is_model_visible_and_truncated(tmp_path):
    store = AgentStore(tmp_path / "agent.db")

    def _big() -> dict:
        return {"data": "x" * 5000}

    registry = ToolRegistry()
    registry.register(spec_from_function(_big, name="big", description="big", tags=("read",)))
    provider = ScriptedProvider([{"tool": "big", "args": {}}, {"final": "ok"}])
    rt = AgentRuntime(
        provider=provider,
        registry=registry,
        store=store,
        gate=PermissionGate(allow_all=True),
        config=RuntimeConfig(max_message_chars=200),
    )
    result = rt.run("x")
    tool_events = [e for e in result.events if e.kind == "tool_result"]
    assert tool_events
    content = tool_events[0].payload["content"]
    assert len(content) <= 200 + len("...[truncated]")
    # the full value is still preserved in the event log
    assert tool_events[0].payload["value"]["data"].startswith("xxxx")


# --------------------------------------------------------------------------- #
# Tier 2: OpenAICompatibleProvider against an in-process mock endpoint
# --------------------------------------------------------------------------- #
class _MockHandler(BaseHTTPRequestHandler):
    responses: list[dict] = []
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _MockHandler.calls.append(body)
        if _MockHandler.responses:
            payload = _MockHandler.responses.pop(0)
        else:
            payload = {"choices": [{"message": {"role": "assistant", "content": "done", "tool_calls": []}}]}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence test server logs
        pass


def _start_mock_server(responses: list[dict]) -> tuple[HTTPServer, int]:
    _MockHandler.responses = list(responses)
    _MockHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_openai_provider_local_integration(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": json.dumps({"msg": "hello"})},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
        {
            "choices": [{"message": {"role": "assistant", "content": "finished", "tool_calls": []}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        },
    ]
    server, port = _start_mock_server(responses)
    try:
        from agent.tracing import SqliteTraceSink

        sink = SqliteTraceSink(store, run_id="mock-run")
        provider = OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{port}/v1", api_key="test", model="mock", timeout=5.0, max_retries=1, sink=sink
        )
        rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True), sink=sink)
        result = rt.run("use the echo tool then finish", run_id="mock-run")
    finally:
        server.shutdown()

    assert result.status == "finished"
    assert result.final_text == "finished"
    assert result.tool_calls == 1
    assert result.usage["total"] == 17  # 8 + 9 aggregated from per-turn usage
    # the provider emitted provider_call trace spans into the store
    assert any(e.kind == "provider_call" for e in result.events)

    # The mock server must have received the tool result in the second request.
    assert len(_MockHandler.calls) == 2
    second = _MockHandler.calls[1]
    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert tool_messages, "model should have been shown the tool result"


def test_openai_provider_4xx_is_not_retried(tmp_path):
    class _ErrHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.dumps({"error": "nope"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), _ErrHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = AgentStore(tmp_path / "agent.db")
    registry = _build_registry()
    try:
        provider = OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{port}/v1", api_key="t", model="m", timeout=5.0, max_retries=3
        )
        rt = AgentRuntime(provider=provider, registry=registry, store=store, gate=PermissionGate(allow_all=True))
        result = rt.run("x")
    finally:
        server.shutdown()
    assert result.status == "failed"
    assert "400" in (result.error or "")
