#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The agent execution loop (frozen contract: ``docs/agent/architecture.md``).

``AgentRuntime`` drives one model-driven run. Each step it rebuilds the model
message list from the append-only event log, asks the provider for the next turn,
executes any tool calls through ``ToolRegistry.invoke`` (the *only* path to a
handler), records every decision and outcome as an event, detects stuck loops,
enforces the step budget, and resolves approval-gated side-effecting tools
according to the configured ``approval_policy``.

Every exit path updates ``agent_runs`` before returning, so the run row is always
a faithful mirror of the last executed step (OpenHands pattern: the event log is
the source of truth; the run row is derived state).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, Callable, Literal, Mapping, Sequence

from agent.errors import (
    AgentError,
    ApprovalRequired,
    BudgetExceeded,
    ModelError,
    ModelProtocolError,
    PermissionDenied,
    ToolNotFound,
)
from agent.state import AgentStore
from agent.tools import InvocationBudget, ToolRegistry, approval_hash, model_visible
from agent.tracing import TraceSink
from agent.types import Event, RunResult, ToolCall

if TYPE_CHECKING:  # keep the runtime import-light; only annotations need these
    from agent.permissions import PermissionGate
    from agent.providers import ModelProvider

_ApprovalPolicy = Literal["deny_mutations", "allow_all", "ask"]

_DEFAULT_SYSTEM_PROMPT = (
    "你是一个受控的自动化智能体，围绕一套小红书内容采集与分析知识库工具工作。"
    "每一步你只能发出一个工具调用，或输出最终结论（FINISH）。"
    "只读工具可直接调用；会修改数据或写入文件的工具需要人工批准，若未被预先批准，运行会暂停并说明原因。"
    "优先用只读工具调查现状；只有在被明确授权时才请求副作用工具。给出最终结论时，用中文简洁总结已完成的操作与结果。"
)


def new_run_id() -> str:
    """Return a collision-resistant run identifier (timestamp + random suffix)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run_{stamp}_{secrets.token_hex(4)}"


@dataclass
class RuntimeConfig:
    """Tunables for one :class:`AgentRuntime` (frozen contract)."""

    max_steps: int = 10
    stuck_threshold: int = 3
    stuck_window: int = 6
    approval_policy: _ApprovalPolicy = "deny_mutations"
    system_prompt: str | None = None
    max_message_chars: int = 4000
    #: Hard wall-clock budget for the whole run; ``None`` disables it.
    max_wall_clock_seconds: float | None = None
    #: Total-token budget (prompt+completion summed over turns); ``None`` disables it.
    max_total_tokens: int | None = None


class AgentRuntime:
    """The model-driven execution loop.

    Args:
        provider: Model provider producing :class:`~agent.types.ModelTurn` per step.
        registry: Tool registry; ``invoke`` is the only route to handlers.
        store: Append-only event store owning run/event/budget state.
        gate: Permission gate consulted on every invocation.
        config: Runtime tunables; sensible defaults when omitted.
        sink: Optional trace sink for ``role="trace"`` spans (provider_call,
            gate_check, approval, checkpoint). The runtime never closes the sink;
            the caller owns its lifecycle.
        approver: Optional callback ``(ToolCall) -> bool`` consulted under the
            ``ask`` policy to decide whether a side-effecting call is allowed.
    """

    def __init__(
        self,
        *,
        provider: "ModelProvider",
        registry: ToolRegistry,
        store: AgentStore,
        gate: "PermissionGate",
        config: RuntimeConfig | None = None,
        sink: TraceSink | None = None,
        approver: Callable[[ToolCall], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._store = store
        self._gate = gate
        self._config = config or RuntimeConfig()
        self._sink = sink
        self._approver = approver
        self._system_prompt = self._config.system_prompt or _DEFAULT_SYSTEM_PROMPT

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #
    def run(self, task: str, *, run_id: str | None = None) -> RunResult:
        """Start a fresh run for ``task`` and drive it to a terminal state."""
        run_id = run_id or new_run_id()
        self._usage = {"prompt": 0, "completion": 0, "total": 0}
        provider_name = self._provider.name
        model = getattr(self._provider, "_model", None)
        self._store.create_run(
            run_id=run_id,
            task=task,
            provider=provider_name,
            model=model,
            max_steps=self._config.max_steps,
        )
        self._store.append_event(run_id, step=0, role="user", kind="task", payload={"task": task})
        self._trace(0, "checkpoint", {"event": "run_start", "provider": provider_name, "policy": self._config.approval_policy})
        try:
            return self._run_loop(run_id, task)
        finally:
            self._provider.close()

    def resume(self, run_id: str) -> RunResult:
        """Continue an existing run (``blocked`` / ``running``) from stored state.

        Terminal runs (``finished``/``failed``/``exceeded``/``stuck``/``cancelled``)
        are returned as-is. Otherwise the loop is re-driven from the stored step
        counter and event log; the caller's ``approval_policy`` applies (e.g. build
        with ``allow_all`` to execute tools a prior ``deny_mutations`` run blocked).
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"no run with id {run_id}")
        status = run["status"]
        if status in ("finished", "failed", "exceeded", "stuck", "cancelled"):
            return self._build_result(run_id, status, final_text=run.get("final_text"), error=run.get("error"))
        self._usage = {"prompt": 0, "completion": 0, "total": 0}
        task = run["task"]
        try:
            return self._run_loop(run_id, task)
        finally:
            self._provider.close()

    def cancel(self, run_id: str) -> RunResult:
        """Cancel a run that is ``running`` or ``blocked``.

        A live loop notices the cancellation at its next step boundary (the loop
        re-reads the run row each iteration), so cancellation is cooperative
        rather than a hard kill — matching how ``resume`` derives state from the
        event log instead of client-held state.
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"no run with id {run_id}")
        if run["status"] in ("finished", "failed", "exceeded", "stuck", "cancelled"):
            return self._build_result(run_id, run["status"], final_text=run.get("final_text"), error=run.get("error"))
        self._trace(0, "checkpoint", {"event": "cancel", "run_id": run_id})
        return self._finish(run_id, "cancelled", error="cancelled by caller")

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def _run_loop(self, run_id: str, task: str) -> RunResult:
        started = time.monotonic()
        while True:
            run = self._store.get_run(run_id)
            if run is None:
                raise RuntimeError(f"run {run_id} disappeared mid-loop")

            # External cancellation (:meth:`cancel`) is observed at the step boundary.
            if run["status"] == "cancelled":
                return self._finish(run_id, "cancelled", error="run was cancelled")

            wall_limit = self._config.max_wall_clock_seconds
            if wall_limit is not None and (time.monotonic() - started) >= wall_limit:
                return self._finish(
                    run_id, "exceeded", error=f"exceeded wall-clock budget of {wall_limit:g}s"
                )

            steps_used = int(run["steps_used"])
            if steps_used >= self._config.max_steps:
                return self._finish(
                    run_id, "exceeded", error=f"exceeded max_steps={self._config.max_steps}"
                )

            step = steps_used + 1
            self._store.update_run(run_id, steps_used=step)
            self._trace(step, "checkpoint", {"event": "step_start", "steps_used": step})

            messages = self._store.messages(run_id)
            tools = self._registry.openai_tools()
            try:
                turn = self._provider.complete(
                    system=self._system_prompt,
                    messages=messages,
                    tools=tools,
                    step=step,
                )
            except (ModelError, ModelProtocolError) as exc:
                return self._finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}")

            self._accumulate_usage(turn.usage)

            token_budget = self._config.max_total_tokens
            if token_budget is not None and self._usage.get("total", 0) > token_budget:
                return self._finish(
                    run_id, "exceeded", error=f"exceeded token budget of {token_budget} tokens"
                )

            if turn.tool_calls:
                try:
                    self._execute_turn(run_id, step, turn)
                except _StopRun as stop:
                    # A tool call terminated the run (approval/permission/budget).
                    return stop.result
                if self._is_stuck(run_id):
                    return self._finish(
                        run_id, "stuck", error="detected repeated identical (tool, args) calls"
                    )
                continue

            if turn.final_text is not None:
                self._store.append_event(
                    run_id, step=step, role="assistant", kind="final", payload={"final_text": turn.final_text}
                )
                return self._finish(run_id, "finished", final_text=turn.final_text)

            # Neither tool calls nor a final answer: protocol error -> failure.
            return self._finish(
                run_id, "failed", error="model returned neither tool calls nor a final answer"
            )

    def _execute_turn(self, run_id: str, step: int, turn: Any) -> None:
        """Record each decision, invoke its tool, and persist the result.

        Every attempted call (success, failure, or terminal exception) is counted
        in the run row's ``tool_calls`` counter immediately, so the count is
        faithful even when a call terminates the run. Terminal exceptions
        (approval / permission / budget) raise ``_StopRun`` which the loop converts
        into the terminal :class:`RunResult`.
        """
        for call in turn.tool_calls:
            self._store.append_event(
                run_id,
                step=step,
                role="assistant",
                kind="decision",
                payload={"tool": call.tool, "tool_call_id": call.call_id, "args": dict(call.args)},
            )
            self._bump_tool_calls(run_id)
            approved_for = self._approval_hash_for(call)
            try:
                result = self._registry.invoke(
                    call,
                    gate=self._gate,
                    budget=InvocationBudget(self._store, run_id),
                    run_id=run_id,
                    step=step,
                    approved_for=approved_for,
                )
            except ApprovalRequired as exc:
                self._record_tool_result(
                    run_id, step, call, ok=False,
                    content=f"需要人工批准才能执行副作用工具 '{call.tool}'", error=str(exc),
                )
                self._trace(step, "approval", {"tool": call.tool, "call_id": call.call_id, "granted": False})
                raise _StopRun(self._finish(run_id, "blocked", error=f"approval required for tool '{call.tool}'"))
            except PermissionDenied as exc:
                self._record_tool_result(
                    run_id, step, call, ok=False,
                    content=f"权限被拒绝: '{call.tool}'", error=str(exc),
                )
                raise _StopRun(self._finish(run_id, "blocked", error=f"permission denied for tool '{call.tool}'"))
            except BudgetExceeded as exc:
                self._record_tool_result(
                    run_id, step, call, ok=False,
                    content=f"工具调用预算耗尽: '{call.tool}'", error=str(exc),
                )
                raise _StopRun(self._finish(run_id, "exceeded", error=f"budget exceeded for tool '{call.tool}'"))
            except ToolNotFound as exc:
                # A hallucinated/unknown tool name must NOT crash the run: surface it
                # to the model as a recoverable tool error so it can retry with a real
                # tool from the registry (round-2 reliability fix).
                known = ", ".join(self._registry.names())
                self._record_tool_result(
                    run_id, step, call, ok=False,
                    content=f"工具 '{call.tool}' 不存在。可用工具: {known}",
                    error=str(exc), error_kind="invalid_input",
                )
                continue

            self._record_tool_result(
                run_id, step, call, ok=result.ok,
                content=model_visible(result, self._config.max_message_chars),
                value=result.value, error=result.error,
                error_kind=result.error_kind, duration_ms=result.duration_ms,
                attempts=result.attempts,
            )

    def _bump_tool_calls(self, run_id: str) -> None:
        """Increment the run row's attempted-tool-call counter by one."""
        run = self._store.get_run(run_id)
        if run is not None:
            self._store.update_run(run_id, tool_calls=int(run["tool_calls"]) + 1)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _approval_hash_for(self, call: ToolCall) -> str | None:
        """Return the arg-hash to pre-approve ``call``, or ``None`` to require it.

        ``allow_all`` (headless ``--yes``) and an approving ``ask`` callback both
        bind approval to the exact argument hash; everything else leaves the call
        to be gated by ``ToolRegistry.invoke`` (which raises ``ApprovalRequired``
        for any side-effecting tool).
        """
        if self._config.approval_policy == "allow_all":
            self._trace(0, "approval", {"tool": call.tool, "granted": True, "policy": "allow_all"})
            return approval_hash(call.tool, call.args)
        if self._config.approval_policy == "ask" and self._approver is not None and self._approver(call):
            self._trace(0, "approval", {"tool": call.tool, "granted": True, "policy": "ask"})
            return approval_hash(call.tool, call.args)
        return None

    def _record_tool_result(
        self,
        run_id: str,
        step: int,
        call: ToolCall,
        *,
        ok: bool,
        content: str,
        value: Any = None,
        error: str | None = None,
        error_kind: str | None = None,
        duration_ms: int = 0,
        attempts: int = 1,
    ) -> None:
        """Append a ``tool_result`` event (``role="tool"``) for message rebuild."""
        payload: dict[str, Any] = {
            "tool_call_id": call.call_id,
            "tool": call.tool,
            "ok": ok,
            "content": content,
            "duration_ms": duration_ms,
            "attempts": attempts,
        }
        if value is not None:
            payload["value"] = value
        if error is not None:
            payload["error"] = error
        if error_kind is not None:
            payload["error_kind"] = error_kind
        self._store.append_event(run_id, step=step, role="tool", kind="tool_result", payload=payload)

    def _is_stuck(self, run_id: str) -> bool:
        """True when any (tool, args) hash repeats >= ``stuck_threshold`` times.

        The window is the last ``stuck_window`` ``decision`` events (OpenHands
        iteration/budget guard). Counting repeats rather than strict consecutiveness
        catches loops that interleave with other calls.
        """
        events = self._store.events(run_id)
        decisions = [e for e in events if e.kind == "decision" and "tool" in e.payload]
        window = decisions[-self._config.stuck_window:]
        if len(window) < self._config.stuck_threshold:
            return False
        counts: dict[str, int] = {}
        for event in window:
            key = approval_hash(event.payload["tool"], event.payload.get("args", {}))
            counts[key] = counts.get(key, 0) + 1
        return any(c >= self._config.stuck_threshold for c in counts.values())

    def _finish(self, run_id: str, status: str, *, final_text: str | None = None, error: str | None = None) -> RunResult:
        """Persist the terminal run state and build the :class:`RunResult`."""
        self._trace(0, "checkpoint", {"event": "run_end", "status": status})
        self._store.update_run(run_id, status=status, final_text=final_text, error=error)
        return self._build_result(run_id, status, final_text=final_text, error=error)

    def _build_result(
        self, run_id: str, status: str, *, final_text: str | None = None, error: str | None = None
    ) -> RunResult:
        run = self._store.get_run(run_id) or {}
        events = tuple(self._store.events(run_id))
        # Prefer the per-turn usage accumulated by the loop; fall back to summing any
        # provider_call trace events (useful when no sink was attached to the provider).
        usage = dict(getattr(self, "_usage", None) or {"prompt": 0, "completion": 0, "total": 0})
        if not any(usage.values()):
            for event in events:
                if event.kind == "provider_call":
                    raw = event.payload.get("usage") or {}
                    for key in ("prompt", "completion", "total"):
                        value = raw.get(key)
                        if isinstance(value, int):
                            usage[key] = usage.get(key, 0) + value
        return RunResult(
            run_id=run_id,
            status=status,
            final_text=final_text if final_text is not None else run.get("final_text"),
            steps_used=int(run.get("steps_used", 0)),
            tool_calls=int(run.get("tool_calls", 0)),
            usage=usage,
            error=error,
            events=events,
        )

    def _accumulate_usage(self, usage: Mapping[str, int]) -> None:
        """Fold one turn's token usage into the run's running totals."""
        bucket = getattr(self, "_usage", None)
        if bucket is None:
            bucket = self._usage = {"prompt": 0, "completion": 0, "total": 0}
        for key in ("prompt", "completion", "total"):
            value = usage.get(key)
            if isinstance(value, int):
                bucket[key] = bucket.get(key, 0) + value

    def _trace(self, step: int, kind: str, payload: Mapping[str, Any]) -> None:
        if self._sink is None:
            return
        self._sink.emit(
            Event(seq=0, ts=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                  step=step, role="trace", kind=kind, payload=dict(payload))
        )


class _StopRun(Exception):
    """Internal control-flow signal carrying a terminal :class:`RunResult`."""

    def __init__(self, result: RunResult) -> None:
        super().__init__(result.status)
        self.result = result
