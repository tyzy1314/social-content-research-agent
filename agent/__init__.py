#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Foundations layer of the model-driven agent: types, errors, tools, permissions.

This root re-exports only the foundation surface. It deliberately does **not**
import ``agent.state``, ``agent.providers``, ``agent.tracing`` or ``agent.runtime`` —
those modules are added separately and must never create a circular or missing
import through this file.
"""

from __future__ import annotations

from agent.errors import (
    AgentError,
    ApprovalRequired,
    BudgetExceeded,
    MaxStepsExceeded,
    ModelError,
    ModelProtocolError,
    ModelRetry,
    PermissionDenied,
    ProviderNotAvailable,
    StuckDetected,
    ToolError,
    ToolFailed,
    ToolNotFound,
    ToolValidationError,
)
from agent.permissions import PermissionGate
from agent.tools import (
    InvocationBudget,
    ToolRegistry,
    ToolSpec,
    approval_hash,
    model_visible,
    spec_from_function,
)
from agent.types import Decision, Event, ModelTurn, RunResult, ToolCall, ToolResult

__all__ = [
    "AgentError",
    "ApprovalRequired",
    "BudgetExceeded",
    "Decision",
    "Event",
    "InvocationBudget",
    "MaxStepsExceeded",
    "ModelError",
    "ModelProtocolError",
    "ModelRetry",
    "ModelTurn",
    "PermissionDenied",
    "PermissionGate",
    "ProviderNotAvailable",
    "RunResult",
    "StuckDetected",
    "ToolCall",
    "ToolError",
    "ToolFailed",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "approval_hash",
    "model_visible",
    "spec_from_function",
]
