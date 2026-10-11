#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控识别与合规降载工具。

只做检测、降速、暂停、停止和记录，不实现绕过登录、验证码或平台限制的能力。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

SEVERITIES = {"low", "medium", "high"}
ACTIONS = {"continue", "slow_down", "pause", "stop_platform", "require_user_action"}

LOGIN_MARKERS = (
    "登录",
    "请登录",
    "登录后查看",
    "login",
    "login-modal",
    "login-container",
)
CAPTCHA_MARKERS = (
    "验证码",
    "短信验证码",
    "安全验证",
    "滑块验证",
    "人机验证",
    "captcha",
    "fe-captcha",
)
RATE_LIMIT_MARKERS = (
    "访问过于频繁",
    "请求过于频繁",
    "请稍后再试",
    "rate limit",
    "too many requests",
)
# 纯数字状态码不能直接对整页文本做子串匹配（正文里的播放量、弹幕数会误伤），
# 只有出现在 HTTP 状态码语境（如 ``HTTP 429`` / ``错误代码：403``）里才算命中。
HTTP_STATUS_PREFIX_PATTERN = r"(?:\b(?:http(?:/\d(?:\.\d)?)?|error|status|code)\b|错误|状态码)"
ACCESS_BLOCK_MARKERS = (
    "账号异常",
    "访问受限",
    "权限不足",
    "forbidden",
    "blocked",
)
EMPTY_OR_UNAVAILABLE_MARKERS = (
    "当前笔记暂时无法浏览",
    "页面不存在",
    "访问的页面不见了",
    "empty feed",
)

LOGIN_SELECTORS = (
    '[class*="login"]',
    '[id*="login"]',
    '[class*="modal"][class*="login"]',
)
CAPTCHA_SELECTORS = (
    '[class*="captcha"]',
    '[id*="captcha"]',
    '[class*="verify"]',
    '[class*="security"]',
)


@dataclass
class RiskDetectionResult:
    blocked: bool
    reason: str = ""
    severity: str = "low"
    action: str = "continue"
    raw_signal: Dict[str, object] = field(default_factory=dict)
    status: str = ""

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            self.severity = "low"
        if self.action not in ACTIONS:
            self.action = "continue"
        if not self.status:
            self.status = "blocked" if self.blocked else "ok"


@dataclass
class RequestBudget:
    max_requests_per_run: int = 100
    max_blocked_per_run: int = 3
    max_failures_per_run: int = 10
    cooldown_seconds_on_block: int = 600


@dataclass
class CircuitBreakerState:
    request_count: int = 0
    blocked_count: int = 0
    failure_count: int = 0
    consecutive_blocked: int = 0
    consecutive_failed: int = 0
    stopped: bool = False
    stop_reason: str = ""
    cooldown_until: float = 0.0
    block_reasons: Counter = field(default_factory=Counter)


class CrawlGuard:
    def __init__(
        self,
        platform: str,
        budget: RequestBudget | None = None,
        *,
        respect_robots_txt: bool = True,
    ):
        self.platform = platform
        self.budget = budget or RequestBudget()
        self.respect_robots_txt = respect_robots_txt
        self.state = CircuitBreakerState()

    @classmethod
    def from_config(cls, platform: str, crawler_config: Dict[str, object] | None):
        config = crawler_config or {}
        budgets = config.get("platform_budgets") or {}
        platform_budget = budgets.get(platform) or {}
        budget = RequestBudget(
            max_requests_per_run=int(platform_budget.get("max_requests_per_run", 100)),
            max_blocked_per_run=int(platform_budget.get("max_blocked_per_run", 3)),
            max_failures_per_run=int(platform_budget.get("max_failures_per_run", 10)),
            cooldown_seconds_on_block=int(platform_budget.get("cooldown_seconds_on_block", 600)),
        )
        return cls(
            platform=platform,
            budget=budget,
            respect_robots_txt=bool(config.get("respect_robots_txt", True)),
        )

    def can_continue(self) -> Tuple[bool, str]:
        now = time.time()
        if self.state.stopped:
            return False, self.state.stop_reason or "circuit_breaker_stopped"
        if self.state.cooldown_until and now < self.state.cooldown_until:
            return False, f"platform_in_cooldown_until_{int(self.state.cooldown_until)}"
        if self.state.request_count >= self.budget.max_requests_per_run:
            self._stop("request_budget_exhausted")
            return False, self.state.stop_reason
        return True, ""

    def record_request(self):
        self.state.request_count += 1

    def record_success(self):
        self.state.consecutive_blocked = 0
        self.state.consecutive_failed = 0

    def record_failure(self, reason: str = "failed"):
        self.state.failure_count += 1
        self.state.consecutive_failed += 1
        if self.state.failure_count >= self.budget.max_failures_per_run:
            self._stop(f"failure_budget_exhausted:{reason}")

    def record_detection(self, result: RiskDetectionResult):
        if not result.blocked:
            return

        self.state.blocked_count += 1
        self.state.consecutive_blocked += 1
        self.state.block_reasons[result.reason or result.status] += 1

        if result.action == "pause":
            self.state.cooldown_until = time.time() + self.budget.cooldown_seconds_on_block
        if result.action in {"stop_platform", "require_user_action"}:
            self._stop(result.reason or result.status)
        if self.state.blocked_count >= self.budget.max_blocked_per_run:
            self._stop("blocked_budget_exhausted")

    def detect_http(
        self,
        *,
        status_code: int = 0,
        url: str = "",
        text: str = "",
        headers: Dict[str, str] | None = None,
    ) -> RiskDetectionResult:
        headers = headers or {}
        location = headers.get("location") or headers.get("Location") or ""

        if status_code in {401, 403}:
            return self._blocked("login_required" if status_code == 401 else "access_forbidden", "high", "require_user_action", {"status_code": status_code, "url": url})
        if status_code == 429:
            return self._blocked("rate_limited", "medium", "pause", {"status_code": status_code, "url": url})
        if status_code in {301, 302, 303, 307, 308} and "login" in location.lower():
            return self._blocked("login_required", "high", "require_user_action", {"status_code": status_code, "url": url, "location": location})

        return self.detect_text(text=text, url=url, extra_signal={"status_code": status_code})

    def detect_driver(self, driver, *, expected_url_contains: str = "") -> RiskDetectionResult:
        try:
            current_url = (driver.current_url or "").strip()
        except Exception:
            current_url = ""
        try:
            title = (driver.title or "").strip()
        except Exception:
            title = ""
        try:
            page_source = driver.page_source or ""
        except Exception:
            page_source = ""
        try:
            body_text = driver.find_element("tag name", "body").text or ""
        except Exception:
            body_text = ""

        if expected_url_contains and expected_url_contains not in current_url:
            return self._blocked(
                "unexpected_page_structure",
                "medium",
                "slow_down",
                {"url": current_url, "expected_url_contains": expected_url_contains},
            )

        selector_result = self._detect_driver_selectors(driver, current_url=current_url)
        if selector_result.blocked:
            return selector_result

        return self.detect_text(
            text=f"{title}\n{body_text}",
            url=current_url,
            extra_signal={"title": title, "body_length": len(body_text), "source_length": len(page_source)},
        )

    def detect_text(
        self,
        *,
        text: str,
        url: str = "",
        extra_signal: Dict[str, object] | None = None,
    ) -> RiskDetectionResult:
        signal = dict(extra_signal or {})
        signal["url"] = url
        value = f"{url}\n{text or ''}".lower()

        if _contains_any(value, LOGIN_MARKERS):
            return self._blocked("login_required", "high", "require_user_action", signal)
        if _contains_any(value, CAPTCHA_MARKERS):
            return self._blocked("captcha_required", "high", "require_user_action", signal)
        if _contains_any(value, RATE_LIMIT_MARKERS) or contains_http_status_code(value, 429):
            return self._blocked("rate_limited", "medium", "pause", signal)
        if _contains_any(value, ACCESS_BLOCK_MARKERS):
            return self._blocked("blocked", "high", "stop_platform", signal)
        if _contains_any(value, EMPTY_OR_UNAVAILABLE_MARKERS):
            return self._blocked("empty_or_unavailable", "low", "continue", signal)
        return RiskDetectionResult(blocked=False, raw_signal=signal, status="ok")

    def check_robots_allowed(self, url: str, user_agent: str = "*", timeout_seconds: int = 10) -> RiskDetectionResult:
        if not self.respect_robots_txt:
            return RiskDetectionResult(blocked=False, status="ok", raw_signal={"respect_robots_txt": False})
        allowed = is_allowed_by_robots(url, user_agent=user_agent, timeout_seconds=timeout_seconds)
        if allowed:
            return RiskDetectionResult(blocked=False, status="ok", raw_signal={"url": url, "robots_allowed": True})
        return self._blocked("skipped_robots", "medium", "continue", {"url": url, "robots_allowed": False})

    def stats(self) -> Dict[str, object]:
        top_block_reasons = [
            {"reason": reason, "count": count}
            for reason, count in self.state.block_reasons.most_common(5)
        ]
        return {
            "blocked_count": self.state.blocked_count,
            "failed_budget_count": self.state.failure_count,
            "login_required_count": self.state.block_reasons.get("login_required", 0),
            "captcha_required_count": self.state.block_reasons.get("captcha_required", 0),
            "rate_limited_count": self.state.block_reasons.get("rate_limited", 0),
            "skipped_robots_count": self.state.block_reasons.get("skipped_robots", 0),
            "circuit_breaker_triggered": self.state.stopped,
            "cooldown_platforms": [self.platform] if self.state.cooldown_until > time.time() else [],
            "top_block_reasons": top_block_reasons,
            "platform_stop_reason": self.state.stop_reason,
        }

    def _detect_driver_selectors(self, driver, *, current_url: str) -> RiskDetectionResult:
        for selector in LOGIN_SELECTORS:
            if _has_visible_selector(driver, selector):
                return self._blocked("login_required", "high", "require_user_action", {"url": current_url, "selector": selector})
        for selector in CAPTCHA_SELECTORS:
            if _has_visible_selector(driver, selector):
                return self._blocked("captcha_required", "high", "require_user_action", {"url": current_url, "selector": selector})
        return RiskDetectionResult(blocked=False, status="ok", raw_signal={"url": current_url})

    def _blocked(
        self,
        reason: str,
        severity: str,
        action: str,
        raw_signal: Dict[str, object],
    ) -> RiskDetectionResult:
        status = reason if reason in {
            "login_required",
            "captcha_required",
            "rate_limited",
            "skipped_robots",
        } else "blocked"
        return RiskDetectionResult(
            blocked=True,
            reason=reason,
            severity=severity,
            action=action,
            raw_signal=raw_signal,
            status=status,
        )

    def _stop(self, reason: str):
        self.state.stopped = True
        self.state.stop_reason = reason or "circuit_breaker_stopped"


def is_allowed_by_robots(url: str, user_agent: str = "*", timeout_seconds: int = 10) -> bool:
    import requests

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False

    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        response = requests.get(robots_url, timeout=timeout_seconds)
    except Exception:
        return True

    if response.status_code in {401, 403}:
        return False
    if response.status_code >= 400:
        return True

    parser.parse(response.text.splitlines())
    return parser.can_fetch(user_agent, url)


def _contains_any(value: str, markers: Iterable[str]) -> bool:
    return any(marker.lower() in value for marker in markers)


def contains_http_status_code(value: str, status_code: int) -> bool:
    """仅在 HTTP 状态码语境下匹配纯数字状态码，避免正文数字造成误判。"""
    pattern = rf"{HTTP_STATUS_PREFIX_PATTERN}[^0-9]{{0,8}}{status_code}(?!\d)"
    return re.search(pattern, (value or "").lower()) is not None


def _has_visible_selector(driver, selector: str) -> bool:
    try:
        elements = driver.find_elements("css selector", selector)
    except Exception:
        return False
    for element in elements:
        try:
            if element.is_displayed():
                return True
        except Exception:
            continue
    return False


def run_smoke_test() -> Dict[str, Dict[str, object]]:
    """无外部依赖的最小验证，供 `python crawl_guard.py` 直接运行。"""
    guard = CrawlGuard.from_config("smoke", {})
    cases = {
        "http_429": guard.detect_http(status_code=429, url="https://example.test"),
        "http_403": guard.detect_http(status_code=403, url="https://example.test"),
        "login_text": guard.detect_text(text="请先登录后查看内容"),
        "captcha_text": guard.detect_text(text="当前页面需要验证码 / 安全验证 / 滑块验证"),
        "rate_limit_text": guard.detect_text(text="访问过于频繁，请稍后再试"),
        "normal_text": guard.detect_text(text="这是一段普通正文，没有风险提示"),
    }
    return {
        name: {
            "blocked": result.blocked,
            "reason": result.reason,
            "severity": result.severity,
            "action": result.action,
            "status": result.status,
            "raw_signal": result.raw_signal,
        }
        for name, result in cases.items()
    }


if __name__ == "__main__":
    print(json.dumps(run_smoke_test(), ensure_ascii=False, indent=2))
