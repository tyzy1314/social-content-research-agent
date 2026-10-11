#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exception hierarchy for the agent layer (see ``docs/agent/architecture.md``).

Rule (pydantic-ai pattern): a tool handler may raise :class:`ModelRetry` to feed a
retryable hint back to the model, :class:`ToolValidationError` for bad input, and
anything else becomes a :class:`ToolFailed` outcome with an **opaque** message
(``"tool X failed"``) — never a traceback, never a local absolute path.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.types import ErrorKind


class AgentError(Exception):
    """Base class for every error raised by the ``agent`` package."""


class ModelError(AgentError):
    """Provider transport or protocol failure (HTTP error, timeout, bad status)."""


class ModelProtocolError(ModelError):
    """Provider output that cannot be trusted (non-JSON, unknown action, contamination)."""


class ToolError(AgentError):
    """Base class for tool execution errors; ``kind`` drives the model-visible mapping.

    Args:
        message: Human-readable message (may be model-visible depending on ``kind``).
        kind: Error kind recorded on the resulting :class:`~agent.types.ToolResult`.
    """

    def __init__(self, message: str, *, kind: ErrorKind = "failed") -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind


class ToolNotFound(ToolError):
    """A tool name is not registered (kind defaults to ``invalid_input``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="invalid_input")


class ToolValidationError(ToolError):
    """Schema mismatch between the call's args and the tool's parameter schema."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="invalid_input")


class ToolFailed(ToolError):
    """The handler raised; the caller must render an opaque ``tool X failed`` message."""


class ModelRetry(ToolError):
    """The handler asks the model to retry with a hint (kind ``retryable``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="retryable")


class PermissionDenied(AgentError):
    """The permission gate refused the tool call (capability or path outside the allowlist)."""


class ApprovalRequired(AgentError):
    """A gated tool call needs explicit approval bound to its exact argument hash.

    Attributes:
        tool: Tool name that requires approval.
        args: Exact args of the pending call (approval is bound to this payload).
        call_id: Id of the call waiting for approval.
    """

    def __init__(self, tool: str, args: Mapping[str, Any], call_id: str) -> None:
        super().__init__(f"approval required for tool '{tool}' (call_id={call_id})")
        self.tool = tool
        self.args = args
        self.call_id = call_id


class MaxStepsExceeded(AgentError):
    """The run consumed its ``max_steps`` budget."""


class StuckDetected(AgentError):
    """The loop repeated identical (tool, args) hashes past the stuck threshold."""


class BudgetExceeded(AgentError):
    """A tool's per-run invocation budget is exhausted."""


class ProviderNotAvailable(AgentError):
    """The requested model provider is not configured or not installed."""
