#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autonomous-but-bounded crawl planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

from agent_permissions import PermissionAudit, build_permission_audit
from db import get_connection, list_site_profiles
from platforms import ADAPTERS, get_adapter_class

EXECUTABLE_PLATFORMS = ("xiaohongshu", "bilibili")
DEFAULT_CURRENT_PLATFORM_RATIOS = {"xiaohongshu": 0.6, "bilibili": 0.4}
DEFAULT_FUTURE_PLATFORM_RATIOS = {"xiaohongshu": 0.4, "bilibili": 0.25, "zhihu": 0.2, "weibo": 0.15}
DEFAULT_KEYWORD_RATIOS = {"core_seed": 0.6, "discovered": 0.25, "emerging": 0.15}
STOP_STATUSES = {"blocked", "login_required", "captcha_required", "rate_limited", "skipped_robots"}


@dataclass
class PlatformPlan:
    platform: str
    ratio: float
    quota: int
    executable: bool
    health_score: float = 1.0
    status: str = "active"
    supported_channels: List[str] = field(default_factory=list)
    channel_order: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    safety_action: str = "continue"

    def asdict(self) -> Dict[str, object]:
        return {
            "platform": self.platform,
            "ratio": self.ratio,
            "quota": self.quota,
            "executable": self.executable,
            "health_score": self.health_score,
            "status": self.status,
            "supported_channels": self.supported_channels,
            "channel_order": self.channel_order,
            "keywords": self.keywords,
            "safety_action": self.safety_action,
        }


@dataclass
class CrawlPlan:
    mode: str
    total_limit: int
    permission_audit: PermissionAudit
    keyword_sources: Dict[str, List[str]]
    platform_allocations: List[PlatformPlan]
    execution_groups: List[Dict[str, object]]
    notes: List[str] = field(default_factory=list)

    def asdict(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "total_limit": self.total_limit,
            "permission_audit": self.permission_audit.asdict(),
            "keyword_sources": self.keyword_sources,
            "platform_allocations": [allocation.asdict() for allocation in self.platform_allocations],
            "execution_groups": self.execution_groups,
            "notes": self.notes,
        }


def build_crawl_plan(
    config: Dict[str, object] | None,
    *,
    db_path: str | None = None,
    mode: str = "test",
    total_limit: int = 10,
    platform_filter: str = "all",
    seed: str = "",
) -> CrawlPlan:
    config = config or {}
    output_config = config.get("output") or {}
    permission_audit = build_permission_audit(config, output_config=output_config)
    selected_platforms = _select_platforms(config, platform_filter)
    platform_ratios = _resolve_platform_ratios(config, selected_platforms)
    keyword_sources = _resolve_keyword_sources(config, db_path=db_path, seed=seed)
    quotas = allocate_quota(platform_ratios, max(0, int(total_limit or 0)))
    profiles = {profile["platform"]: profile for profile in list_site_profiles(db_path=db_path)}
    keyword_pool = _weighted_keywords(config, keyword_sources, max(1, int(total_limit or 1)))

    allocations: List[PlatformPlan] = []
    notes: List[str] = []
    for platform in selected_platforms:
        profile = profiles.get(platform) or {}
        health_score = float(profile.get("health_score") or 1.0)
        status = str(profile.get("status") or "active")
        quota = quotas.get(platform, 0)
        if health_score <= 0 or status in STOP_STATUSES:
            quota = 0
        channels = _adapter_supported_channels(platform)
        channel_order = _channel_order(channels)
        executable = platform in EXECUTABLE_PLATFORMS
        if not executable:
            notes.append(f"{platform}: adapter interface exists, real collection is not enabled yet")
        if status in STOP_STATUSES:
            notes.append(f"{platform}: skipped because site profile status is {status}")
        allocations.append(
            PlatformPlan(
                platform=platform,
                ratio=float(platform_ratios.get(platform, 0)),
                quota=quota,
                executable=executable,
                health_score=health_score,
                status=status,
                supported_channels=channels,
                channel_order=channel_order,
                keywords=keyword_pool[: max(0, quota)] or keyword_pool[:1],
                safety_action="stop_or_slow_down" if status in STOP_STATUSES else "continue",
            )
        )

    if not keyword_pool:
        notes.append("no seed or discovered keywords are available; autonomous crawl will not start")
    if not permission_audit.autonomy_enabled:
        notes.append("agent.autonomy_enabled is false; plan is dry-run only until explicitly enabled")
    if not permission_audit.ok:
        notes.append("permission audit has violations; autonomous execution should stop")

    return CrawlPlan(
        mode=mode,
        total_limit=max(0, int(total_limit or 0)),
        permission_audit=permission_audit,
        keyword_sources=keyword_sources,
        platform_allocations=allocations,
        execution_groups=_build_execution_groups(allocations),
        notes=notes,
    )


def allocate_quota(ratios: Dict[str, float], total: int) -> Dict[str, int]:
    if total <= 0 or not ratios:
        return {platform: 0 for platform in ratios}
    positive = {platform: max(0.0, float(ratio)) for platform, ratio in ratios.items()}
    ratio_sum = sum(positive.values())
    if ratio_sum <= 0:
        return {platform: 0 for platform in positive}
    raw = {platform: (ratio / ratio_sum) * total for platform, ratio in positive.items()}
    quotas = {platform: int(value) for platform, value in raw.items()}
    remainder = total - sum(quotas.values())
    ranked = sorted(raw, key=lambda platform: (raw[platform] - quotas[platform], raw[platform]), reverse=True)
    for platform in ranked[:remainder]:
        quotas[platform] += 1
    return quotas


def _select_platforms(config: Dict[str, object], platform_filter: str) -> List[str]:
    if platform_filter and platform_filter != "all":
        return [platform_filter] if platform_filter in ADAPTERS else []
    agent_config = config.get("agent") or {}
    discovery_config = config.get("discovery") or {}
    allowed = _string_list(agent_config.get("allowed_platforms"))
    configured = allowed or _string_list(discovery_config.get("platforms")) or list(DEFAULT_CURRENT_PLATFORM_RATIOS)
    return [platform for platform in configured if platform in ADAPTERS]


def _resolve_platform_ratios(config: Dict[str, object], selected_platforms: Sequence[str]) -> Dict[str, float]:
    planner_config = config.get("planner") or {}
    configured = planner_config.get("platform_ratios") or {}
    default_ratios = DEFAULT_CURRENT_PLATFORM_RATIOS
    if any(platform in {"zhihu", "weibo"} for platform in selected_platforms):
        default_ratios = DEFAULT_FUTURE_PLATFORM_RATIOS
    ratios = {
        platform: float(configured.get(platform, default_ratios.get(platform, 0.0)))
        for platform in selected_platforms
    }
    if not any(ratios.values()):
        ratios = {platform: 1.0 for platform in selected_platforms}
    return ratios


def _resolve_keyword_sources(
    config: Dict[str, object],
    *,
    db_path: str | None = None,
    seed: str = "",
) -> Dict[str, List[str]]:
    discovery_config = config.get("discovery") or {}
    core = []
    if seed.strip():
        core.append(seed.strip())
    core.extend(_keywords_from_config(config.get("keywords")))
    core.extend(_string_list(discovery_config.get("seed_keywords")))
    discovered = _load_discovered_keywords(db_path=db_path, limit=50)
    emerging = discovered[:10]
    return {
        "core_seed": _dedupe_ordered(core),
        "discovered": _dedupe_ordered([term for term in discovered if term not in core]),
        "emerging": _dedupe_ordered([term for term in emerging if term not in core]),
    }


def _weighted_keywords(config: Dict[str, object], keyword_sources: Dict[str, List[str]], total_limit: int) -> List[str]:
    ratios = _resolve_keyword_ratios(config)
    quotas = allocate_quota(ratios, max(1, total_limit))
    ordered: List[str] = []
    for source_name in ("core_seed", "discovered", "emerging"):
        source_terms = keyword_sources.get(source_name) or []
        take = max(1, quotas.get(source_name, 0)) if source_terms else 0
        ordered.extend(source_terms[:take])
    for source_name in ("core_seed", "discovered", "emerging"):
        ordered.extend(keyword_sources.get(source_name) or [])
    return _dedupe_ordered(ordered)


def _resolve_keyword_ratios(config: Dict[str, object]) -> Dict[str, float]:
    planner_config = config.get("planner") or {}
    configured = planner_config.get("keyword_ratios") or {}
    return {
        source_name: float(configured.get(source_name, default_ratio))
        for source_name, default_ratio in DEFAULT_KEYWORD_RATIOS.items()
    }


def _adapter_supported_channels(platform: str) -> List[str]:
    try:
        adapter_class = get_adapter_class(platform)
    except Exception:
        return []
    return list(getattr(adapter_class, "supported_channels", ["browser"]))


def _channel_order(channels: Iterable[str]) -> List[str]:
    preferred = ["api", "http", "browser", "ocr", "import"]
    channel_set = set(channels)
    ordered = [channel for channel in preferred if channel in channel_set]
    ordered.extend(sorted(channel_set - set(ordered)))
    return ordered


def _build_execution_groups(allocations: Sequence[PlatformPlan]) -> List[Dict[str, object]]:
    browser_allocations = [
        allocation.platform
        for allocation in allocations
        if allocation.quota > 0 and allocation.executable and "browser" in allocation.channel_order
    ]
    non_browser_allocations = [
        allocation.platform
        for allocation in allocations
        if allocation.quota > 0 and allocation.executable and "browser" not in allocation.channel_order
    ]
    groups = []
    if non_browser_allocations:
        groups.append({"name": "parallel_non_browser", "parallel": True, "platforms": non_browser_allocations})
    if browser_allocations:
        groups.append(
            {
                "name": "browser_safe_serial",
                "parallel": False,
                "platforms": browser_allocations,
                "reason": "shared browser profile should not be driven concurrently",
            }
        )
    return groups


def _load_discovered_keywords(*, db_path: str | None = None, limit: int = 50) -> List[str]:
    try:
        with get_connection(db_path) as connection:
            rows = connection.execute(
                """
                SELECT term
                FROM discovered_keywords
                ORDER BY score DESC, frequency DESC, updated_at DESC
                LIMIT ?
                """,
                (int(limit or 50),),
            ).fetchall()
    except Exception:
        return []
    return [str(row["term"]).strip() for row in rows if str(row["term"]).strip()]


def _keywords_from_config(value: object) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Iterable):
        return []
    terms = []
    for item in value:
        if isinstance(item, dict):
            term = str(item.get("keyword") or item.get("name") or "").strip()
        else:
            term = str(item or "").strip()
        if term:
            terms.append(term)
    return terms


def _string_list(value: object) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dedupe_ordered(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result
