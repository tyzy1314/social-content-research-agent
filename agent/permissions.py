#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime permission gate wrapping the existing ``agent_permissions.py`` allowlist.

``agent_permissions.py`` already defined ``build_permission_audit``,
``capability_allowed`` and ``output_path_allowed``, but nothing in the runtime ever
called them. :class:`PermissionGate` is that missing call site: every
``ToolRegistry.invoke`` goes through ``allow_tool``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from agent.errors import PermissionDenied
from agent_permissions import (
    DEFAULT_ALLOWED_CAPABILITIES,
    DEFAULT_FORBIDDEN_CAPABILITIES,
    build_permission_audit,
    capability_allowed,
    output_path_allowed,
)

_NETWORK_TAGS = frozenset({"network", "http", "api", "egress", "fetch", "download"})

_DEFAULT_TAG_CAPABILITIES = ("read", "write", "network")


def _capability_for_tag(tag: str) -> str | None:
    """Map a tool tag to the capability it requires (``None`` = no requirement).

    Frozen contract mapping: ``read`` -> capability ``read``, ``write`` -> capability
    ``write``, network-egress tags -> capability ``network``; any other tag imposes
    no capability of its own (e.g. ``("db", "write")`` requires only ``write``).
    """
    if tag == "read":
        return "read"
    if tag == "write":
        return "write"
    if tag in _NETWORK_TAGS:
        return "network"
    return None


def _is_path_arg(key: str) -> bool:
    """Return whether an arg key looks path-ish (``*_path`` / ``path`` / ``output`` / ``file``)."""
    return key == "path" or key.endswith("_path") or key in ("output", "file")


class PermissionGate:
    """Runtime enforcement of the allowlist for every tool invocation.

    Wraps (does not rewrite) ``agent_permissions.py``: the audit is built with
    ``build_permission_audit``, capability checks go through ``capability_allowed``,
    and path checks go through ``output_path_allowed`` — giving the previously dead
    allowlist its first runtime call site.

    Args:
        allowed_capabilities: Capabilities accepted for tool tags. ``None`` grants
            the repo default capability set plus the tag-mapped ``read`` / ``write`` /
            ``network`` capabilities. Forbidden capabilities are always stripped.
        write_roots: Directories path-ish args may resolve into; ``None`` keeps the
            repository default output dirs.
        allow_all: Bypass every check (headless ``--yes`` mode).
    """

    def __init__(
        self,
        *,
        allowed_capabilities: Iterable[str] | None = None,
        write_roots: Iterable[str] | None = None,
        allow_all: bool = False,
    ) -> None:
        self._allow_all = allow_all
        if allowed_capabilities is None:
            effective = set(DEFAULT_ALLOWED_CAPABILITIES) | set(_DEFAULT_TAG_CAPABILITIES)
        else:
            effective = {str(cap).strip() for cap in allowed_capabilities if str(cap).strip()}
        effective -= set(DEFAULT_FORBIDDEN_CAPABILITIES)
        config: dict[str, Any] = {"agent": {"autonomy_enabled": True}}
        if write_roots:
            config["agent"]["allowed_output_dirs"] = [str(root) for root in write_roots]
        self._audit = build_permission_audit(config)
        # The constructor's capability set is authoritative for tag checks; the rest
        # of the audit (forbidden list, output dirs, violations) stays as built.
        self._audit.allowed_capabilities = sorted(effective)

    def allow_tool(self, tool: str, args: Mapping[str, Any], *, tags: Sequence[str]) -> None:
        """Check a tool call against capabilities and path args.

        Raises ``PermissionDenied`` when the tool's tags/capabilities are outside the
        allowlist, or when a path-ish arg (``*_path`` / ``path`` / ``output`` /
        ``file``) resolves outside ``write_roots``.

        Args:
            tool: Tool name (used in error messages).
            args: Call arguments; path-ish string values are resolved and checked.
            tags: The spec's capability tags.

        Raises:
            PermissionDenied: A required capability is not allowed, or a path arg
                falls outside the allowed write roots.
        """
        if self._allow_all:
            return
        for tag in tags:
            capability = _capability_for_tag(str(tag))
            if capability is None:
                continue
            if not capability_allowed(self._audit, capability):
                raise PermissionDenied(
                    f"tool '{tool}' requires capability '{capability}' which is not allowed"
                )
        for key, value in args.items():
            if _is_path_arg(key) and isinstance(value, str) and not output_path_allowed(self._audit, value):
                raise PermissionDenied(f"tool '{tool}' argument '{key}' is outside the allowed write roots: {value}")

    def allow_path(self, path: str) -> None:
        """Check a single filesystem path against the allowed write roots.

        Args:
            path: File or directory path (resolved before checking).

        Raises:
            PermissionDenied: The path resolves outside every allowed write root.
        """
        if self._allow_all:
            return
        if not output_path_allowed(self._audit, path):
            raise PermissionDenied(f"path is outside the allowed write roots: {path}")

    def audit(self) -> dict[str, Any]:
        """Return the serialisable permission audit for the trace."""
        return self._audit.asdict()
