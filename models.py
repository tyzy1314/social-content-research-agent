#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
平台无关的数据模型。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List


def utc_now_text() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class EngagementMetrics:
    like_count: str = ""
    comment_count: str = ""
    share_count: str = ""
    favorite_count: str = ""
    view_count: str = ""


@dataclass
class ContentItem:
    platform: str
    source_url: str
    content_id: str = ""
    author_name: str = ""
    title: str = ""
    body: str = ""
    publish_time: str = ""
    collected_at: str = field(default_factory=utc_now_text)
    engagement: EngagementMetrics = field(default_factory=EngagementMetrics)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    last_error: str = ""
    blocked_reason: str = ""

    def to_db_kwargs(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "content_id": self.content_id,
            "clean_url": self.source_url,
            "raw_url": self.source_url,
            "source_url": self.source_url,
            "title": self.title,
            "note_title": self.title,
            "note_content": self.body,
            "body": self.body,
            "author_name": self.author_name,
            "publish_time": self.publish_time,
            "like_count": self.engagement.like_count,
            "comment_count_display": self.engagement.comment_count,
            "share_count": self.engagement.share_count,
            "collect_count": self.engagement.favorite_count,
            "favorite_count": self.engagement.favorite_count,
            "view_count": self.engagement.view_count,
            "status": self.status,
            "fail_reason": self.last_error or self.blocked_reason,
            "raw_metadata": self.raw_metadata,
        }

    def asdict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContentComment:
    platform: str
    content_id: str = ""
    source_url: str = ""
    comment_id: str = ""
    username: str = ""
    content: str = ""
    raw_text: str = ""
    publish_time: str = ""
    likes: str = ""
    crawled_at: str = field(default_factory=utc_now_text)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    platform: str
    source_url: str
    title: str = ""
    content_id: str = ""
    author_name: str = ""
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitPolicy:
    min_delay_seconds: float = 1.5
    max_delay_seconds: float = 5.0
    concurrency: int = 1
    max_retries: int = 3


@dataclass
class CrawlRunStats:
    scanned_count: int = 0
    new_items_count: int = 0
    duplicate_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0
    login_required_count: int = 0
    captcha_required_count: int = 0
    rate_limited_count: int = 0
    skipped_robots_count: int = 0
    circuit_breaker_triggered: bool = False
    cooldown_platforms: List[str] = field(default_factory=list)

    def asdict(self) -> Dict[str, Any]:
        return asdict(self)
