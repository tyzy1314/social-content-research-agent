#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frozen data contracts for the agent layer (see ``docs/agent/architecture.md``).

Every type here is a frozen dataclass: the runtime only ever appends events, never
mutates a value it already handed out. Providers normalise into these shapes so the
loop never touches a raw SDK object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

DecisionKind = Literal["call_tool", "finish"]
ErrorKind = Literal["retryable", "invalid_input", "failed"]
RunStatus = Literal["running", "finished", "failed", "blocked", "exceeded", "stuck", "cancelled"]


@dataclass(frozen=True)
class ToolCall:
    """A single request to execute a registered tool.

    Attributes:
        tool: Registered tool name.
        args: Keyword arguments for the handler (JSON-serialisable values).
        call_id: Stable id — provider-native when available, else ``f"call_{seq}"``.
    """

    tool: str
    args: Mapping[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one ``ToolRegistry.invoke`` call, safe to show the model.

    Attributes:
        call_id: The originating :class:`ToolCall` id.
        tool: Tool name.
        ok: Whether the handler completed successfully.
        value: Full handler return value on success (JSON-serialisable).
        error: Model-visible error text; never a traceback or local absolute path.
        error_kind: ``retryable`` / ``invalid_input`` / ``failed`` when ``ok`` is false.
        duration_ms: Wall-clock duration of the invocation.
        attempts: Number of handler attempts (the foundations layer always uses 1).
    """

    call_id: str
    tool: str
    ok: bool
    value: Any = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    duration_ms: int = 0
    attempts: int = 1


@dataclass(frozen=True)
class ModelTurn:
    """Normalised provider output. Providers never return raw SDK objects.

    Attributes:
        tool_calls: Tool calls requested by the model this turn.
        final_text: Final answer text when the model is done.
        finish_reason: Provider-reported finish reason, if any.
        usage: Token accounting with prompt/completion/total keys.
        raw: Provider-specific payload kept only for tracing.
    """

    tool_calls: tuple[ToolCall, ...] = ()
    final_text: str | None = None
    finish_reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    raw: Any = None


@dataclass(frozen=True)
class Decision:
    """The next-step decision ADT consumed by the runtime loop.

    Attributes:
        kind: ``call_tool`` to run a tool, ``finish`` to end the run.
        tool: Target tool name when ``kind`` is ``call_tool``.
        args: Tool arguments when ``kind`` is ``call_tool``.
        final: Final answer text when ``kind`` is ``finish``.
        source: Where the decision came from — model output, script, or forced.
    """

    kind: DecisionKind
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    final: str | None = None
    source: Literal["model", "scripted", "forced"] = "model"


@dataclass(frozen=True)
class Event:
    """One append-only row in ``agent_events``.

    Attributes:
        seq: Monotonic per-run sequence number.
        ts: UTC ISO-8601 timestamp.
        step: Loop step that produced the event.
        role: Message role the runtime will rebuild from this event.
        kind: ``decision``|``tool_call``|``tool_result``|``error``|``checkpoint``|``final``.
        payload: Kind-specific JSON-serialisable body.
    """

    seq: int
    ts: str
    step: int
    role: Literal["system", "user", "assistant", "tool", "trace"]
    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class RunResult:
    """Final summary of one agent run.

    Attributes:
        run_id: Run identifier.
        status: Terminal (or current) run status.
        final_text: Final answer text when the run finished.
        steps_used: Loop steps consumed.
        tool_calls: Total tool invocations attempted.
        usage: Aggregated token accounting.
        error: Error text when the run failed/blocked/exceeded/stuck.
        events: Full append-only event log of the run.
    """

    run_id: str
    status: RunStatus
    final_text: str | None
    steps_used: int
    tool_calls: int
    usage: Mapping[str, int]
    error: str | None = None
    events: tuple[Event, ...] = ()


