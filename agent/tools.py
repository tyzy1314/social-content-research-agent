#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tool specs, JSON Schema generation, invocation budgets, and the pipeline.

``ToolRegistry.invoke`` is the only path from the agent loop to a tool handler.
Its stage order is load-bearing: budget check -> permission gate -> approval check
-> args validation -> handler call (with timeout) -> outcome mapping.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time
import types
from collections.abc import Mapping as MappingABC
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from agent.errors import ApprovalRequired, BudgetExceeded, ModelRetry, ToolError, ToolNotFound, ToolValidationError
from agent.types import ToolCall, ToolResult

if TYPE_CHECKING:
    from agent.permissions import PermissionGate


class _BudgetStore(Protocol):
    """Duck-typed budget surface of ``agent.state.AgentStore`` (not imported here)."""

    def budget_get(self, run_id: str, tool: str) -> int:
        """Return how many times ``tool`` was invoked in ``run_id``."""
        ...

    def budget_bump(self, run_id: str, tool: str) -> int:
        """Increment the counter and return the new used count."""
        ...


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: metadata, JSON Schema, and the handler to execute.

    Attributes:
        name: Tool name exposed to the model.
        description: Model-visible description.
        parameters: JSON Schema (``{"type": "object", "properties": ...}``).
        handler: Callable invoked with the validated args as keyword arguments.
        mutates: Side-effecting? drives approval policy.
        requires_approval: Explicit approval switch; defaults to ``mutates`` at build
            time (``spec_from_function``) and is OR-ed with ``mutates`` at invoke time.
        max_invocations: Per-run budget (agent-framework pattern); ``None`` = unlimited.
        timeout_seconds: Wall-clock handler timeout; ``None`` = no timeout.
        tags: e.g. ``("read",)`` / ``("db", "write")`` — mapped to capabilities by the gate.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[..., Any]
    mutates: bool = False
    requires_approval: bool = False
    max_invocations: int | None = None
    timeout_seconds: float | None = None
    tags: tuple[str, ...] = ()


class ToolRegistry:
    """Registry of :class:`ToolSpec` entries and the only execution path to handlers."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a spec; duplicate names fail fast.

        Args:
            spec: The tool specification to register.

        Raises:
            ToolError: A tool with the same name is already registered.
        """
        if spec.name in self._specs:
            raise ToolError(f"tool '{spec.name}' is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        """Look up a spec by name.

        Raises:
            ToolNotFound: No tool is registered under ``name``.
        """
        spec = self._specs.get(name)
        if spec is None:
            raise ToolNotFound(f"tool '{name}' is not registered")
        return spec

    def names(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._specs)

    def openai_tools(self) -> list[dict[str, Any]]:
        """Return the OpenAI ``tools`` array for every registered spec."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": dict(spec.parameters),
                },
            }
            for spec in self._specs.values()
        ]

    def invoke(
        self,
        call: ToolCall,
        *,
        gate: "PermissionGate",
        budget: "InvocationBudget",
        run_id: str,
        step: int,
        approved_for: str | None = None,
    ) -> ToolResult:
        """Execute ``call`` through the fixed six-stage pipeline.

        Stages, in this exact order (load-bearing):

        1. budget check -> :class:`BudgetExceeded` when ``max_invocations`` is spent;
        2. ``gate.allow_tool(tool, args, tags=tags)`` -> :class:`PermissionDenied`;
        3. approval check -> :class:`ApprovalRequired` when approval is required and
           ``approved_for`` does not match this call's hash (raised *without*
           executing the handler);
        4. args validation against ``spec.parameters`` -> ``ok=False`` result with
           ``error_kind="invalid_input"``;
        5. handler call, with ``timeout_seconds`` enforced on a worker thread;
        6. outcome mapping -> ``ToolResult`` (success / ``ModelRetry`` ->
           ``retryable`` / ``ToolValidationError`` -> ``invalid_input`` / anything
           else -> ``failed`` with the opaque message ``f"tool {name} failed"``).

        Approval semantics (microsoft/agent-framework pattern): approval is bound to
        the exact arguments. ``approved_for`` must be ``approval_hash(tool, args)``
        computed *after* this specific call was approved, so a one-shot "yes" can
        never approve a later call whose args changed. Budget is consumed only when
        the handler is about to run, so denied or invalid calls do not burn slots.

        Args:
            call: The tool call to execute.
            gate: Permission gate consulted for capabilities/tags/path args.
            budget: Per-run invocation budget wrapper.
            run_id: Run id used for budget accounting.
            step: Loop step, reserved for trace/event correlation.
            approved_for: ``approval_hash(tool, args)`` when this exact call was
                pre-approved; ``None`` otherwise.

        Returns:
            ToolResult with ``ok=True`` and the handler value, or ``ok=False`` plus
            a model-visible ``error`` and ``error_kind``.

        Raises:
            ToolNotFound: The tool name is not registered.
            BudgetExceeded: Stage 1 — the per-run budget is exhausted.
            PermissionDenied: Stage 2 — the gate refused tags/capabilities/paths.
            ApprovalRequired: Stage 3 — approval required but not bound to these args.
        """
        spec = self.get(call.tool)
        started = time.perf_counter()

        # Stage 1: budget check.
        if spec.max_invocations is not None and budget.remaining(spec.name, spec.max_invocations) <= 0:
            raise BudgetExceeded(f"tool '{spec.name}' exhausted its budget of {spec.max_invocations} invocation(s)")

        # Stage 2: permission gate (capabilities, tags, path args).
        gate.allow_tool(call.tool, call.args, tags=spec.tags)

        # Stage 3: approval bound to the exact argument hash.
        if (spec.requires_approval or spec.mutates) and approved_for != approval_hash(call.tool, call.args):
            raise ApprovalRequired(call.tool, call.args, call.call_id)

        # Stage 4: argument validation against the spec's JSON Schema.
        try:
            kwargs = _validate_args(spec.parameters, call.args)
        except ToolValidationError as exc:
            return ToolResult(
                call_id=call.call_id,
                tool=spec.name,
                ok=False,
                error=str(exc),
                error_kind="invalid_input",
                duration_ms=_elapsed_ms(started),
            )

        # Stage 5: handler call (consume one budget slot only now).
        if spec.max_invocations is not None:
            budget.bump(spec.name)
        try:
            value = _call_handler(spec, kwargs)
        except ModelRetry as exc:
            return ToolResult(
                call_id=call.call_id,
                tool=spec.name,
                ok=False,
                error=str(exc),
                error_kind="retryable",
                duration_ms=_elapsed_ms(started),
            )
        except ToolValidationError as exc:
            return ToolResult(
                call_id=call.call_id,
                tool=spec.name,
                ok=False,
                error=str(exc),
                error_kind="invalid_input",
                duration_ms=_elapsed_ms(started),
            )
        except Exception:  # Stage 6: every other handler failure is failed + opaque.
            return ToolResult(
                call_id=call.call_id,
                tool=spec.name,
                ok=False,
                error=f"tool {spec.name} failed",
                error_kind="failed",
                duration_ms=_elapsed_ms(started),
            )

        # Stage 6: success mapping.
        return ToolResult(
            call_id=call.call_id,
            tool=spec.name,
            ok=True,
            value=value,
            duration_ms=_elapsed_ms(started),
        )


class InvocationBudget:
    """Per-run, per-tool invocation budget backed by a duck-typed store.

    The store only needs ``budget_get(run_id, tool) -> int`` and
    ``budget_bump(run_id, tool) -> int`` (the ``agent.state.AgentStore`` surface).
    ``agent.state`` is deliberately not imported so this layer stays dependency-free.

    Args:
        store: Object exposing ``budget_get`` / ``budget_bump``.
        run_id: Run whose counters this wrapper reads and increments.
    """

    def __init__(self, store: _BudgetStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def remaining(self, tool: str, max_invocations: int | None) -> int:
        """Return how many invocations of ``tool`` are left.

        Args:
            tool: Tool name.
            max_invocations: Per-run cap; ``None`` means unlimited (returns ``sys.maxsize``).

        Returns:
            Slots left, never negative.
        """
        if max_invocations is None:
            return sys.maxsize
        return max(0, max_invocations - self._store.budget_get(self._run_id, tool))

    def bump(self, tool: str) -> int:
        """Consume one invocation slot for ``tool``.

        Returns:
            The new used count after the increment.
        """
        return self._store.budget_bump(self._run_id, tool)


def spec_from_function(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    mutates: bool = False,
    requires_approval: bool | None = None,
    max_invocations: int | None = None,
    timeout_seconds: float | None = None,
    tags: Sequence[str] = (),
) -> ToolSpec:
    """Build a :class:`ToolSpec` from a Python function, deriving its JSON Schema.

    The schema comes from ``inspect.signature`` + ``typing.get_type_hints`` (stdlib
    only). Supported parameter types: ``str``, ``int``, ``float``, ``bool``, ``list``,
    ``dict``, ``list[str]``, ``dict[str, str]``, and ``Optional[...]`` of those.
    Any other parameter type raises :class:`TypeError` here — at registration time —
    never at call time. Parameters named ``self``/``cls`` are skipped so plain
    methods can be registered unbound; variadic ``*args``/``**kwargs`` are rejected.

    Args:
        fn: The handler function.
        name: Tool name; defaults to ``fn.__name__``.
        description: Model-visible description; defaults to the function docstring.
        mutates: Side-effecting tool? drives approval policy.
        requires_approval: Explicit approval switch; ``None`` defaults to ``mutates``.
        max_invocations: Per-run invocation budget; ``None`` = unlimited.
        timeout_seconds: Handler wall-clock timeout; ``None`` = no timeout.
        tags: Capability tags read by :class:`~agent.permissions.PermissionGate`.

    Returns:
        The frozen tool specification ready for ``ToolRegistry.register``.

    Raises:
        TypeError: An annotation is missing or uses an unsupported parameter type.
    """
    tool_name = name if name is not None else fn.__name__
    signature = inspect.signature(fn)
    hints = get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in signature.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"tool '{tool_name}' parameter '{pname}': variadic arguments are not supported")
        annotation = hints.get(pname, param.annotation)
        properties[pname] = _parameter_schema(annotation, tool=tool_name, param=pname)
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    if description is None:
        description = inspect.getdoc(fn) or ""
    return ToolSpec(
        name=tool_name,
        description=description,
        parameters=parameters,
        handler=fn,
        mutates=mutates,
        requires_approval=mutates if requires_approval is None else requires_approval,
        max_invocations=max_invocations,
        timeout_seconds=timeout_seconds,
        tags=tuple(tags),
    )


def approval_hash(tool: str, args: Mapping[str, Any]) -> str:
    """Return the 16-char sha256 prefix that binds an approval to exact arguments.

    This is the exact value ``invoke(approved_for=...)`` compares against: the first
    16 hex chars of sha256 over ``json.dumps({"tool": ..., "args": ...}, sort_keys=True)``.

    Args:
        tool: Tool name of the approved call.
        args: Exact args payload of the approved call.

    Returns:
        16-character lowercase hex digest.
    """
    payload = json.dumps({"tool": tool, "args": dict(args)}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def model_visible(result: ToolResult, max_chars: int = 4000) -> str:
    """Render a tool result as the model-visible message body, truncated to ``max_chars``.

    ``ToolResult.value`` keeps the full payload for the event log/trace; this helper
    is what the runtime puts into the model's context window.

    Args:
        result: The tool result to render.
        max_chars: Maximum length of the returned string.

    Returns:
        The success value (stringified/JSON), or the error text for failures,
        truncated with a ``...[truncated]`` marker when it exceeds ``max_chars``.
    """
    if not result.ok:
        text = result.error if result.error is not None else f"tool {result.tool} failed"
    elif isinstance(result.value, str):
        text = result.value
    else:
        text = json.dumps(result.value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    marker = "...[truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker


def _call_handler(spec: ToolSpec, kwargs: dict[str, Any]) -> Any:
    """Run the handler, enforcing ``timeout_seconds`` on a worker thread when set.

    A timeout surfaces as an ``Exception`` to the caller and is mapped by ``invoke``
    to the opaque ``failed`` outcome; the stuck worker thread is abandoned (Python
    threads cannot be killed) via a non-blocking executor shutdown.
    """
    if spec.timeout_seconds is None:
        return spec.handler(**kwargs)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-tool")
    try:
        future = executor.submit(spec.handler, **kwargs)
        return future.result(timeout=spec.timeout_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _validate_args(parameters: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``args`` against a JSON Schema object fragment.

    Args:
        parameters: The spec's ``{"type": "object", "properties": ...}`` schema.
        args: The call's raw arguments.

    Returns:
        Keyword arguments to pass to the handler (unknown keys dropped, following
        JSON Schema's default allowance for additional properties).

    Raises:
        ToolValidationError: A required key is missing or a value's type mismatches.
    """
    properties = parameters.get("properties") or {}
    required = parameters.get("required") or []
    for key in required:
        if key not in args:
            raise ToolValidationError(f"missing required argument '{key}'")
    validated: dict[str, Any] = {}
    for key, value in args.items():
        schema = properties.get(key)
        if schema is None:
            continue
        if not _value_matches(value, schema):
            raise ToolValidationError(
                f"invalid argument '{key}': expected {_describe(schema)}, got {type(value).__name__}"
            )
        validated[key] = value
    return validated


def _parameter_schema(annotation: Any, *, tool: str, param: str) -> dict[str, Any]:
    """Translate one parameter annotation into a JSON Schema fragment.

    Raises:
        TypeError: The annotation is missing or outside the supported type set.
    """
    if annotation is inspect.Parameter.empty:
        raise TypeError(f"tool '{tool}' parameter '{param}' is missing a type annotation")
    origin = get_origin(annotation)
    if origin is None:
        if annotation is str:
            return {"type": "string"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        if annotation is bool:
            return {"type": "boolean"}
        if annotation is list:
            return {"type": "array"}
        if annotation is dict:
            return {"type": "object"}
        raise TypeError(f"unsupported parameter type {annotation!r} for '{param}' of tool '{tool}'")
    args = get_args(annotation)
    if origin is list:
        if not args:
            return {"type": "array"}
        if args == (str,):
            return {"type": "array", "items": {"type": "string"}}
        raise TypeError(f"unsupported parameter type {annotation!r} for '{param}' of tool '{tool}'")
    if origin is dict:
        if not args:
            return {"type": "object"}
        if args == (str, str):
            return {"type": "object", "additionalProperties": {"type": "string"}}
        raise TypeError(f"unsupported parameter type {annotation!r} for '{param}' of tool '{tool}'")
    if origin is Union or origin is types.UnionType:
        non_none = [part for part in args if part is not type(None)]
        if len(args) == 2 and len(non_none) == 1:
            base = _parameter_schema(non_none[0], tool=tool, param=param)
            return {"anyOf": [base, {"type": "null"}]}
        raise TypeError(f"unsupported parameter type {annotation!r} for '{param}' of tool '{tool}'")
    raise TypeError(f"unsupported parameter type {annotation!r} for '{param}' of tool '{tool}'")


def _value_matches(value: Any, schema: Any) -> bool:
    """Return whether ``value`` satisfies a JSON Schema fragment (structural check)."""
    if not isinstance(schema, MappingABC):
        return True
    variants = schema.get("anyOf")
    if isinstance(variants, (list, tuple)):
        return any(_value_matches(value, variant) for variant in variants)
    kind = schema.get("type")
    if not isinstance(kind, str):
        return True
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "array":
        if not isinstance(value, list):
            return False
        items = schema.get("items")
        if isinstance(items, MappingABC):
            return all(_value_matches(item, items) for item in value)
        return True
    if kind == "object":
        if not isinstance(value, dict):
            return False
        item_schema = schema.get("additionalProperties")
        if isinstance(item_schema, MappingABC):
            return all(_value_matches(item, item_schema) for item in value.values())
        return True
    return True


def _describe(schema: Any) -> str:
    """Render a schema fragment as a short Python-ish type name for error messages."""
    if isinstance(schema, MappingABC):
        variants = schema.get("anyOf")
        if isinstance(variants, (list, tuple)):
            return " or ".join(_describe(variant) for variant in variants)
        kind = schema.get("type")
        if kind == "array":
            items = schema.get("items")
            if isinstance(items, MappingABC) and isinstance(items.get("type"), str):
                return f"list[{items['type']}]"
            return "list"
        if kind == "object":
            item_schema = schema.get("additionalProperties")
            if isinstance(item_schema, MappingABC) and isinstance(item_schema.get("type"), str):
                return f"dict[str, {item_schema['type']}]"
            return "dict"
        if isinstance(kind, str):
            return {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}.get(kind, kind)
    return "the expected type"


def _elapsed_ms(started: float) -> int:
    """Return milliseconds elapsed since ``started`` (``time.perf_counter`` base)."""
    return int((time.perf_counter() - started) * 1000)
