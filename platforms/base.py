#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台适配器抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Optional

from crawl_guard import CrawlGuard, RiskDetectionResult
from models import ContentComment, ContentItem, RateLimitPolicy, SearchResult

from platforms.capabilities import (
    CAP_COMMENTS,
    CAP_UNKNOWN,
    MODE_UNAVAILABLE,
    AccessResult,
    CapabilityStatus,
    PlatformInfo,
    UnifiedComment,
    UnifiedContent,
    capability_status,
    not_supported,
)


class BaseAdapter(ABC):
    platform_name: str = ""
    supported_channels: list[str] = ["http"]

    def __init__(self, *, crawler_config: dict | None = None):
        self.crawler_config = crawler_config or {}
        self.guard = CrawlGuard.from_config(self.platform_name, self.crawler_config)

    @abstractmethod
    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        """搜索入口，返回平台无关搜索结果。"""

    @abstractmethod
    def fetch_item(self, url_or_id: str) -> ContentItem:
        """抓取单条内容。"""

    @abstractmethod
    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        """抓取评论。"""

    @abstractmethod
    def normalize(self, raw: dict) -> ContentItem:
        """将平台原始字段映射为通用内容模型。"""

    def detect_login_block(self, response_or_driver) -> RiskDetectionResult:
        if hasattr(response_or_driver, "current_url"):
            return self.guard.detect_driver(response_or_driver)
        status_code = int(getattr(response_or_driver, "status_code", 0) or 0)
        text = getattr(response_or_driver, "text", "") or ""
        url = getattr(response_or_driver, "url", "") or ""
        headers = dict(getattr(response_or_driver, "headers", {}) or {})
        return self.guard.detect_http(status_code=status_code, url=url, text=text, headers=headers)

    def get_rate_limit_policy(self) -> RateLimitPolicy:
        return RateLimitPolicy(
            min_delay_seconds=float(self.crawler_config.get("min_delay_seconds", 1.5)),
            max_delay_seconds=float(self.crawler_config.get("max_delay_seconds", 5)),
            concurrency=int(self.crawler_config.get("concurrency", 1)),
            max_retries=int(self.crawler_config.get("max_retries", 3)),
        )

    # ------------------------------------------------------------------ #
    # 统一能力层（第四轮）
    #
    # 默认一律 NOT_SUPPORTED：适配器必须显式覆写才能"声明"能力，
    # 因此不存在"看起来支持、实际返回空数据"的假适配器。
    # ------------------------------------------------------------------ #
    def capabilities(self) -> tuple[str, ...]:
        """本适配器**实际实现**的能力集合。"""
        return ()

    def platform_info(self) -> PlatformInfo:
        """平台声明（能力、接入方式、真实验收等级）。必须覆写。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 platform_info()")

    def check_access(self, capability: str = "") -> AccessResult:
        """检查某项能力当前是否可用（不做静默降级）。"""
        target = capability or CAP_COMMENTS
        return not_supported(self.platform_name, target, f"{self.platform_name} 未实现能力层")

    def search_content(self, keyword: str, limit: int = 10) -> AccessResult:
        return not_supported(self.platform_name, "search_content")

    def get_content_detail(self, content_id: str) -> AccessResult:
        return not_supported(self.platform_name, "content_detail")

    def list_comments(self, content_id: str, limit: int = 30) -> AccessResult:
        return not_supported(self.platform_name, "comments")

    def list_replies(self, content_id: str, parent_comment_id: str, limit: int = 20) -> AccessResult:
        return not_supported(self.platform_name, "replies")

    def list_creator_content(self, creator_id: str, limit: int = 20) -> AccessResult:
        return not_supported(self.platform_name, "creator_content")

    def collect_barrage(self, content_id: str, limit: int = 200) -> AccessResult:
        return not_supported(self.platform_name, "barrage")

    def normalize_content(self, raw: dict) -> UnifiedContent:
        raise NotImplementedError(f"{self.platform_name} 未实现 normalize_content()")

    def normalize_comment(self, raw: dict, *, platform_content_id: str = "") -> UnifiedComment:
        raise NotImplementedError(f"{self.platform_name} 未实现 normalize_comment()")

    # ------------------------------------------------------------------ #
    # 第五轮：接入模式 + 能力级运行时状态
    #
    # 契约：
    #   1. access_modes()       —— 本平台**支持的**接入模式（六选子集）；
    #   2. probe_capability()   —— 真实探测一项能力当前是否可用，返回 CapabilityStatus；
    #   3. capability_matrix()  —— 全能力矩阵，未实现的能力一律 UNKNOWN + 未探测原因。
    #
    # 硬约束：探测遇到 401/403/412/429 等限制信号**立即停止**，记录原因，
    # 绝不重试、绝不换 UA/IP/账号、绝不伪造请求或破解签名。
    # ------------------------------------------------------------------ #
    def access_modes(self) -> tuple[str, ...]:
        """本平台支持的接入模式集合。"""
        try:
            return self.platform_info().access_modes
        except NotImplementedError:
            return (MODE_UNAVAILABLE,)

    def default_access_mode(self) -> str:
        try:
            return self.platform_info().default_access_mode
        except NotImplementedError:
            return MODE_UNAVAILABLE

    def login_free_capabilities(self) -> tuple[str, ...]:
        """明确可免登录使用的能力（其余一律不得声称免登录）。"""
        try:
            return self.platform_info().login_free_capabilities
        except NotImplementedError:
            return ()

    def probe_capability(self, capability: str, *, live: bool = True) -> CapabilityStatus:
        """探测一项能力的**运行时**真实状态。

        Args:
            capability: 能力名。
            live: 是否发起真实网络请求。False 时用于离线场景，必须标记
                  ``probed=False`` 并给出未探测原因，绝不虚构 AVAILABLE。
        """
        if capability not in self.capabilities():
            return capability_status(
                self.platform_name, capability,
                status="NOT_SUPPORTED",
                reason=f"{self.platform_name} 未实现 {capability} 能力（适配器未声明）",
                probed=False,
                evidence={"not_probed_reason": "适配器未声明该能力，无需发起请求"},
            )
        return capability_status(
            self.platform_name, capability,
            status=CAP_UNKNOWN,
            reason=f"{type(self).__name__} 未实现 probe_capability()，状态未知",
            probed=False,
            evidence={"not_probed_reason": "适配器未实现运行时探测"},
        )

    def capability_matrix(self, *, live: bool = True) -> dict[str, dict[str, Any]]:
        """全能力运行时矩阵（供能力矩阵文档与智能体调度消费）。"""
        from platforms.capabilities import ALL_CAPABILITIES

        return {cap: self.probe_capability(cap, live=live).as_dict() for cap in ALL_CAPABILITIES}

    def try_http_only(self, url: str) -> Optional[ContentItem]:
        import requests
        from bs4 import BeautifulSoup

        if self.crawler_config.get("respect_robots_txt", True):
            robots_result = self.guard.check_robots_allowed(url)
            if robots_result.blocked:
                return None

        response = requests.get(
            url,
            timeout=int(self.crawler_config.get("request_timeout_seconds", 20)),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        risk = self.detect_login_block(response)
        if risk.blocked:
            return None

        soup = BeautifulSoup(response.text, "lxml")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
        body = soup.get_text(" ", strip=True)
        return ContentItem(
            platform=self.platform_name,
            source_url=response.url or url,
            title=title,
            body=body[:5000],
            raw_metadata={"http_status": response.status_code},
        )

    def close(self) -> None:
        return None
