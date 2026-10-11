#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Permission allowlist for autonomous collection features."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List


DEFAULT_ALLOWED_CAPABILITIES = {
    "terminal",
    "browser",
    "codex_browser",
    "oauth",
    "api",
    "http",
    "ocr",
    "local_models",
    "embedding_models",
    "file_export",
}
DEFAULT_FORBIDDEN_CAPABILITIES = {
    "captcha_bypass",
    "stealth_browser",
    "proxy_rotation",
    "private_messages",
    "paid_wall_bypass",
    "bulk_accounts",
    "credential_extraction",
}


@dataclass
class PermissionAudit:
    autonomy_enabled: bool = False
    permission_model: str = "allowlist"
    allowed_capabilities: List[str] = field(default_factory=list)
    blocked_capabilities: List[str] = field(default_factory=list)
    forbidden_capabilities: List[str] = field(default_factory=list)
    allowed_output_dirs: List[str] = field(default_factory=list)
    audit_log_enabled: bool = True
    violations: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def asdict(self) -> Dict[str, object]:
        return {
            "autonomy_enabled": self.autonomy_enabled,
            "permission_model": self.permission_model,
            "allowed_capabilities": self.allowed_capabilities,
            "blocked_capabilities": self.blocked_capabilities,
            "forbidden_capabilities": self.forbidden_capabilities,
            "allowed_output_dirs": self.allowed_output_dirs,
            "audit_log_enabled": self.audit_log_enabled,
            "violations": self.violations,
            "ok": self.ok,
        }


def build_permission_audit(
    config: Dict[str, object] | None,
    *,
    output_config: Dict[str, object] | None = None,
) -> PermissionAudit:
    root = config or {}
    agent_config = root.get("agent") or {}
    output_config = output_config or {}
    configured_capabilities = _as_string_list(agent_config.get("capabilities"))
    forbidden_capabilities = set(DEFAULT_FORBIDDEN_CAPABILITIES)
    forbidden_capabilities.update(_as_string_list(agent_config.get("forbidden_capabilities")))

    if configured_capabilities:
        requested = set(configured_capabilities)
    else:
        requested = set(DEFAULT_ALLOWED_CAPABILITIES)

    allowed_capabilities = sorted((requested & DEFAULT_ALLOWED_CAPABILITIES) - forbidden_capabilities)
    blocked_capabilities = sorted((requested - DEFAULT_ALLOWED_CAPABILITIES) | (requested & forbidden_capabilities))
    permission_model = str(agent_config.get("permission_model") or "allowlist").strip().lower() or "allowlist"
    allowed_output_dirs = _resolve_output_dirs(agent_config.get("allowed_output_dirs"), output_config)

    audit = PermissionAudit(
        autonomy_enabled=bool(agent_config.get("autonomy_enabled", False)),
        permission_model=permission_model,
        allowed_capabilities=allowed_capabilities,
        blocked_capabilities=blocked_capabilities,
        forbidden_capabilities=sorted(forbidden_capabilities),
        allowed_output_dirs=allowed_output_dirs,
        audit_log_enabled=bool(agent_config.get("audit_log_enabled", True)),
    )

    if permission_model != "allowlist":
        audit.violations.append("permission_model_must_be_allowlist")
    if blocked_capabilities:
        audit.violations.append("blocked_capabilities_requested")
    if not allowed_output_dirs:
        audit.violations.append("no_allowed_output_dirs")
    return audit


def capability_allowed(audit: PermissionAudit, capability: str) -> bool:
    return capability in set(audit.allowed_capabilities)


def output_path_allowed(audit: PermissionAudit, path: str | Path) -> bool:
    target = Path(path).resolve()
    for allowed_dir in audit.allowed_output_dirs:
        allowed = Path(allowed_dir).resolve()
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def _resolve_output_dirs(value: object, output_config: Dict[str, object]) -> List[str]:
    configured = _as_string_list(value)
    if not configured:
        configured = [
            str(output_config.get("csv_dir") or "outputs/csv"),
            str(output_config.get("log_dir") or "outputs/logs"),
            str(output_config.get("summary_dir") or "outputs/summaries"),
            str(Path(output_config.get("db_path") or "data/xhs_crawler.db").parent),
        ]
    dirs = []
    for item in configured:
        if not item:
            continue
        dirs.append(str(Path(item).resolve()))
    return sorted(set(dirs))


def _as_string_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
