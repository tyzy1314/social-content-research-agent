#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the agent foundations layer: types, tools, permissions, errors."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from agent.errors import (
    ApprovalRequired,
    BudgetExceeded,
    ModelRetry,
    PermissionDenied,
    ToolError,
    ToolNotFound,
)
from agent.permissions import PermissionGate
from agent.tools import InvocationBudget, ToolRegistry, ToolSpec, approval_hash, model_visible, spec_from_function
from agent.types import Decision, Event, ModelTurn, RunResult, ToolCall, ToolResult

TEST_FILE_PATH = str(Path(__file__).resolve())


class MemoryBudgetStore:
    """Tiny in-memory stand-in for the agent.state.AgentStore budget surface."""

    def __init__(self) -> None:
        self._used: dict[tuple[str, str], int] = {}

    def budget_get(self, run_id: str, tool: str) -> int:
        return self._used.get((run_id, tool), 0)

    def budget_bump(self, run_id: str, tool: str) -> int:
        key = (run_id, tool)
        self._used[key] = self._used.get(key, 0) + 1
        return self._used[key]


def _budget(run_id: str = "run-1") -> InvocationBudget:
    """Build a budget over a fresh in-memory store."""
    return InvocationBudget(MemoryBudgetStore(), run_id)


def _gate(**kwargs: Any) -> PermissionGate:
    """Build a gate allowing the read/write/network tag capabilities."""
    return PermissionGate(allowed_capabilities={"read", "write", "network"}, **kwargs)


def _register(*specs: ToolSpec) -> ToolRegistry:
    """Register the given specs on a fresh registry."""
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def test_spec_from_function_builds_json_schema_for_supported_types() -> None:
    """Given a handler using every supported type, the spec maps each annotation."""

    def sample(
        name: str,
        limit: int,
        verbose: bool,
        scale: float,
        tags: list[str],
        meta: dict[str, str],
        raw: list,
        blob: dict,
        note: Optional[str] = None,
        count: int = 0,
    ) -> str:
        """Sample tool."""
        return name

    spec = spec_from_function(sample, tags=("read",))

    props = spec.parameters["properties"]
    assert spec.name == "sample"
    assert spec.description == "Sample tool."
    assert spec.tags == ("read",)
    assert props["name"] == {"type": "string"}
    assert props["limit"] == {"type": "integer"}
    assert props["verbose"] == {"type": "boolean"}
    assert props["scale"] == {"type": "number"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["meta"] == {"type": "object", "additionalProperties": {"type": "string"}}
    assert props["raw"] == {"type": "array"}
    assert props["blob"] == {"type": "object"}
    assert props["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert spec.parameters["required"] == ["name", "limit", "verbose", "scale", "tags", "meta", "raw", "blob"]
    assert spec.mutates is False
    assert spec.requires_approval is False


def test_spec_from_function_supports_pep604_union_optional() -> None:
    """Given an ``int | None`` parameter, the schema becomes anyOf integer/null."""

    def unioned(value: int | None = None) -> int | None:
        return value

    spec = spec_from_function(unioned)

    assert spec.parameters["properties"]["value"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert "required" not in spec.parameters


def test_spec_from_function_rejects_unsupported_types_at_registration_time() -> None:
    """Given unsupported/missing annotations, TypeError is raised at registration."""

    def bad_set(items: set) -> None:
        """Unsupported container."""

    def bad_list(items: list[int]) -> None:
        """Unsupported item type."""

    def unannotated(value) -> None:
        """No annotation at all."""

    with pytest.raises(TypeError):
        spec_from_function(bad_set)
    with pytest.raises(TypeError):
        spec_from_function(bad_list)
    with pytest.raises(TypeError):
        spec_from_function(unannotated)


def test_registry_registers_and_exports_openai_tools() -> None:
    """Given a registered spec, lookup/names/openai export all reflect it."""

    def echo(text: str) -> str:
        """Echo text back."""
        return text

    spec = spec_from_function(echo, tags=("read",))
    registry = _register(spec)

    assert registry.names() == ["echo"]
    assert registry.get("echo") is spec
    with pytest.raises(ToolNotFound):
        registry.get("missing")

    exported = registry.openai_tools()
    assert exported[0]["type"] == "function"
    assert exported[0]["function"]["name"] == "echo"
    assert exported[0]["function"]["parameters"]["type"] == "object"

    with pytest.raises(ToolError):
        registry.register(spec)


def test_invoke_raises_budget_exhaustion_and_never_runs_handler() -> None:
    """Given a spent budget, invoke raises BudgetExceeded without executing the handler."""
    executed: list[str] = []

    def counted(query: str) -> str:
        executed.append(query)
        return f"result:{query}"

    registry = _register(spec_from_function(counted, max_invocations=1, tags=("read",)))
    gate = _gate()
    budget = _budget()

    first = registry.invoke(
        ToolCall(tool="counted", args={"query": "a"}, call_id="c1"),
        gate=gate,
        budget=budget,
        run_id="run-1",
        step=1,
    )
    assert first.ok is True
    assert first.value == "result:a"
    assert first.error_kind is None

    with pytest.raises(BudgetExceeded):
        registry.invoke(
            ToolCall(tool="counted", args={"query": "b"}, call_id="c2"),
            gate=gate,
            budget=budget,
            run_id="run-1",
            step=2,
        )

    assert executed == ["a"]


def test_invoke_denies_capability_not_in_gate_allowlist() -> None:
    """Given a tool whose capability is not allowed, PermissionDenied stops the handler."""
    executed: list[str] = []

    def writer(data: str) -> str:
        executed.append(data)
        return "written"

    registry = _register(spec_from_function(writer, tags=("write",)))
    gate = PermissionGate(allowed_capabilities={"read"})
    budget = _budget()

    with pytest.raises(PermissionDenied):
        registry.invoke(
            ToolCall(tool="writer", args={"data": "x"}, call_id="c1"),
            gate=gate,
            budget=budget,
            run_id="run-1",
            step=1,
        )

    assert executed == []


def test_invoke_requires_arg_hash_bound_approval_for_mutating_tool() -> None:
    """Given a mutates=True tool, approval is required and bound to exact args."""
    executed: list[str] = []

    def wipe(table: str) -> str:
        executed.append(table)
        return "wiped"

    registry = _register(spec_from_function(wipe, mutates=True))
    gate = _gate()
    budget = _budget()

    with pytest.raises(ApprovalRequired) as excinfo:
        registry.invoke(
            ToolCall(tool="wipe", args={"table": "notes"}, call_id="c9"),
            gate=gate,
            budget=budget,
            run_id="run-1",
            step=1,
        )
    assert excinfo.value.tool == "wipe"
    assert excinfo.value.call_id == "c9"
    assert executed == []

    approved = approval_hash("wipe", {"table": "notes"})
    result = registry.invoke(
        ToolCall(tool="wipe", args={"table": "notes"}, call_id="c9"),
        gate=gate,
        budget=budget,
        run_id="run-1",
        step=1,
        approved_for=approved,
    )
    assert result.ok is True
    assert executed == ["notes"]

    # Approval is bound to the exact args: a different payload must not reuse it.
    with pytest.raises(ApprovalRequired):
        registry.invoke(
            ToolCall(tool="wipe", args={"table": "other"}, call_id="c10"),
            gate=gate,
            budget=budget,
            run_id="run-1",
            step=2,
            approved_for=approved,
        )
    assert executed == ["notes"]


def test_approval_hash_matches_documented_formula() -> None:
    """The hash helper equals sha256 over the canonical JSON payload, first 16 chars."""
    payload = json.dumps({"tool": "wipe", "args": {"table": "notes"}}, sort_keys=True)
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    assert approval_hash("wipe", {"table": "notes"}) == expected


def test_invoke_returns_invalid_input_for_bad_args() -> None:
    """Given type-mismatched or missing args, the result is invalid_input with the message."""
    executed: list[str] = []

    def add(a: int, b: int) -> int:
        executed.append("called")
        return a + b

    registry = _register(spec_from_function(add))
    gate = _gate()
    budget = _budget()

    wrong_type = registry.invoke(
        ToolCall(tool="add", args={"a": "x", "b": 2}, call_id="c1"),
        gate=gate,
        budget=budget,
        run_id="run-1",
        step=1,
    )
    assert wrong_type.ok is False
    assert wrong_type.error_kind == "invalid_input"
    assert wrong_type.error is not None and "'a'" in wrong_type.error

    missing = registry.invoke(
        ToolCall(tool="add", args={"a": 1}, call_id="c2"),
        gate=gate,
        budget=budget,
        run_id="run-1",
        step=2,
    )
    assert missing.ok is False
    assert missing.error_kind == "invalid_input"
    assert missing.error is not None and "missing required argument 'b'" in missing.error

    assert executed == []


def test_invoke_maps_model_retry_to_retryable_error() -> None:
    """A handler raising ModelRetry yields kind=retryable and the hint as error."""

    def flaky(query: str) -> str:
        raise ModelRetry("try a narrower query")

    registry = _register(spec_from_function(flaky))
    result = registry.invoke(
        ToolCall(tool="flaky", args={"query": "x"}, call_id="c1"),
        gate=_gate(),
        budget=_budget(),
        run_id="run-1",
        step=1,
    )

    assert result.ok is False
    assert result.error_kind == "retryable"
    assert result.error == "try a narrower query"


def test_invoke_opaque_failure_for_raising_handler() -> None:
    """An unexpected handler exception becomes an opaque failed result, no leaks."""

    def boom(query: str) -> str:
        raise RuntimeError(f"internal detail at {TEST_FILE_PATH}")

    registry = _register(spec_from_function(boom))
    result = registry.invoke(
        ToolCall(tool="boom", args={"query": "x"}, call_id="c1"),
        gate=_gate(),
        budget=_budget(),
        run_id="run-1",
        step=1,
    )

    assert result.ok is False
    assert result.error_kind == "failed"
    assert result.error == "tool boom failed"
    assert "Traceback" not in result.error
    assert TEST_FILE_PATH not in result.error
    assert str(Path(__file__)) not in result.error
    assert "internal detail" not in result.error


def test_invoke_checks_budget_before_permission_gate() -> None:
    """Given both a spent budget and a deny-all gate, BudgetExceeded wins (order)."""

    def never(data: str) -> str:
        return "ran"

    registry = _register(spec_from_function(never, tags=("write",), max_invocations=1))
    gate = PermissionGate(allowed_capabilities={"read"})  # would deny the write tag
    budget = _budget()
    budget.bump("never")  # exhaust the single allowed slot

    with pytest.raises(BudgetExceeded):
        registry.invoke(
            ToolCall(tool="never", args={"data": "x"}, call_id="c1"),
            gate=gate,
            budget=budget,
            run_id="run-1",
            step=1,
        )


def test_invoke_checks_permission_gate_before_approval() -> None:
    """Given a mutating tool with a denied capability, PermissionDenied wins over approval."""

    def wipe(table: str) -> str:
        return "wiped"

    registry = _register(spec_from_function(wipe, mutates=True, tags=("write",)))
    gate = PermissionGate(allowed_capabilities={"read"})

    with pytest.raises(PermissionDenied):
        registry.invoke(
            ToolCall(tool="wipe", args={"table": "notes"}, call_id="c1"),
            gate=gate,
            budget=_budget(),
            run_id="run-1",
            step=1,
        )


def test_invoke_checks_approval_before_argument_validation() -> None:
    """Given a mutating tool with bad args and no approval, ApprovalRequired wins (order)."""

    def wipe(table: str) -> str:
        return "wiped"

    registry = _register(spec_from_function(wipe, mutates=True))

    with pytest.raises(ApprovalRequired):
        registry.invoke(
            ToolCall(tool="wipe", args={"table": 123}, call_id="c1"),
            gate=_gate(),
            budget=_budget(),
            run_id="run-1",
            step=1,
        )


def test_gate_denies_paths_outside_write_roots(tmp_path: Path) -> None:
    """Given write_roots, path args outside them are denied through the gate."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside" / "notes.txt"
    gate = PermissionGate(write_roots=[str(allowed)])

    gate.allow_path(str(allowed / "notes.txt"))
    with pytest.raises(PermissionDenied):
        gate.allow_path(str(outside))

    def export_data(output_path: str) -> str:
        return output_path

    registry = _register(spec_from_function(export_data))
    with pytest.raises(PermissionDenied):
        registry.invoke(
            ToolCall(tool="export_data", args={"output_path": str(outside)}, call_id="c1"),
            gate=gate,
            budget=_budget(),
            run_id="run-1",
            step=1,
        )

    audit = gate.audit()
    assert isinstance(audit, dict)
    assert str(allowed.resolve()) in audit["allowed_output_dirs"]


def test_invocation_budget_tracks_remaining_and_bumps() -> None:
    """Given a duck-typed store, remaining/bump track per-tool usage."""
    budget = _budget("run-9")

    assert budget.remaining("db_stats", 3) == 3
    assert budget.bump("db_stats") == 1
    assert budget.remaining("db_stats", 3) == 2
    assert budget.remaining("other", 1) == 1
    assert budget.remaining("db_stats", None) == sys.maxsize


def test_model_visible_truncates_long_values_and_keeps_value_full() -> None:
    """Given a huge success value, the rendered text is truncated but value stays full."""
    long_value = "x" * 10_000
    result = ToolResult(call_id="c1", tool="big", ok=True, value=long_value)

    rendered = model_visible(result, max_chars=100)

    assert len(rendered) == 100
    assert rendered.endswith("...[truncated]")
    assert result.value == long_value

    short = ToolResult(call_id="c2", tool="small", ok=True, value={"a": 1})
    assert model_visible(short) == '{"a": 1}'

    failed = ToolResult(call_id="c3", tool="small", ok=False, error="tool small failed", error_kind="failed")
    assert model_visible(failed) == "tool small failed"


def test_package_root_reexports_foundation_surface() -> None:
    """The agent package root exposes the foundation names without later modules."""
    import agent

    for name in (
        "ToolCall",
        "ToolResult",
        "ModelTurn",
        "Decision",
        "Event",
        "RunResult",
        "ToolSpec",
        "ToolRegistry",
        "spec_from_function",
        "PermissionGate",
        "InvocationBudget",
        "model_visible",
        "approval_hash",
        "AgentError",
        "ToolError",
        "PermissionDenied",
        "ApprovalRequired",
        "BudgetExceeded",
    ):
        assert hasattr(agent, name), name

    turn = agent.ModelTurn(final_text="done")
    decision = agent.Decision(kind="finish", final="done")
    event = agent.Event(seq=1, ts="2026-01-01T00:00:00Z", step=1, role="user", kind="checkpoint", payload={})
    run = agent.RunResult(run_id="r", status="finished", final_text="ok", steps_used=1, tool_calls=0, usage={})

    assert turn.final_text == "done"
    assert decision.kind == "finish"
    assert event.seq == 1
    assert run.status == "finished"

    # Hermetic check: a fresh interpreter importing only `agent` must not pull in
    # the later modules (their test files may already be imported in this process).
    probe = "import agent, sys; print([m for m in ('agent.state','agent.providers','agent.tracing','agent.runtime') if m in sys.modules])"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.stdout.strip() == "[]"
