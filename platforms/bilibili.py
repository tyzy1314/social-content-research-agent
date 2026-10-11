#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bilibili public-page Selenium adapter."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin, urlparse

from selenium.webdriver.common.by import By

from browser_utils import create_chrome_driver
from crawl_guard import RiskDetectionResult, contains_http_status_code
from models import ContentComment, ContentItem, EngagementMetrics, SearchResult
from platforms.base import BaseAdapter

BILIBILI_BASE_URL = "https://www.bilibili.com"
BILIBILI_SEARCH_URL = "https://search.bilibili.com/all?keyword={keyword}"
DEBUG_PAGE_PATH = Path("debug_bilibili_page.html")

VIDEO_LINK_SELECTORS = [
    'a[href*="/video/"]',
    'a[href*="BV"]',
    '[class*="bili-video-card"] a[href]',
    '[class*="video-card"] a[href]',
    '[class*="search-result"] a[href]',
]
SEARCH_CARD_SELECTORS = [
    '[class*="bili-video-card"]',
    '[class*="video-card"]',
    '[class*="search-result"]',
    '[class*="video"]',
]
TITLE_SELECTORS = [
    "h1",
    '[class*="video-title"]',
    '[class*="VideoTitle"]',
    '[class*="title"]',
]
DESC_SELECTORS = [
    '[class*="desc-info-text"]',
    '[class*="video-desc"]',
    '[class*="desc"]',
    '[class*="description"]',
]
AUTHOR_SELECTORS = [
    '[class*="up-name"]',
    '[class*="up-info"] [class*="name"]',
    '[class*="author"] a',
    'a[href*="space.bilibili.com"]',
]
COMMENT_CONTAINER_SELECTORS = [
    '[class*="reply-item"]',
    '[class*="comment-item"]',
    '[class*="reply-wrap"]',
    '[class*="root-reply"]',
]
COMMENT_CONTENT_SELECTORS = [
    '[class*="reply-content"]',
    '[class*="comment-content"]',
    '[class*="content"]',
]
COMMENT_USER_SELECTORS = [
    '[class*="user-name"]',
    '[class*="nickname"]',
    '[class*="name"]',
    'a[href*="space.bilibili.com"]',
]
COMMENT_LIKE_SELECTORS = [
    '[class*="like"]',
    '[class*="reply-like"]',
]
UNAVAILABLE_MARKERS = [
    "视频不见了",
    "稿件不存在",
    "页面不存在",
    "该视频已失效",
    "视频已删除",
    "当前内容暂不可访问",
    "当前内容不可访问",
]
CAPTCHA_MARKERS = ["验证码", "安全验证", "人机验证", "滑块验证", "captcha"]
RATE_LIMIT_MARKERS = ["访问过于频繁", "请求过于频繁", "请稍后再试", "too many requests"]
BLOCK_MARKERS = ["forbidden", "blocked", "账号异常", "页面访问受限"]
# 纯数字状态码（403/404/429）只在 HTTP 状态码语境下匹配，
# 否则正文里的播放量、弹幕数、评论内容会误判为风控。
UNAVAILABLE_STATUS_CODES = [404]
RATE_LIMIT_STATUS_CODES = [429]
BLOCK_STATUS_CODES = [403]
LOGIN_REQUIRED_MARKERS = ["需要登录后查看", "登录后查看", "请先登录"]
STRONG_LOGIN_REQUIRED_MARKERS = [
    "请先登录",
    "登录后查看",
    "登录后才能",
    "需要登录",
    "当前内容需要登录",
    "请登录后继续",
]
LOGIN_MODAL_MARKERS = ["账号登录", "密码登录", "扫码登录"]
COMMENT_NOISE_PHRASES = {
    "回复",
    "展开回复",
    "查看更多",
    "发送",
    "登录后发表评论",
    "还没有评论",
    "抢沙发",
}


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def extract_bilibili_content_id(url: str) -> str:
    value = url or ""
    match = re.search(r"(BV[0-9A-Za-z]{8,20})", value)
    if match:
        return match.group(1)
    match = re.search(r"/video/(av\d+)", value, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def canonicalize_bilibili_video_url(url: str) -> str:
    if not url:
        return ""
    normalized = urljoin(BILIBILI_BASE_URL, url.strip())
    parsed = urlparse(normalized)
    if "bilibili.com" not in parsed.netloc:
        return ""
    content_id = extract_bilibili_content_id(normalized)
    if not content_id:
        return ""
    return f"https://www.bilibili.com/video/{content_id}"


def has_bilibili_video_content(driver) -> bool:
    """Return True when public video body signals are visible on the current page."""
    try:
        current_url = (driver.current_url or "").strip()
    except Exception:
        current_url = ""
    if not extract_bilibili_content_id(current_url):
        return False

    content_selectors = [
        "h1",
        '[class*="video-title"]',
        '[class*="VideoTitle"]',
        '[class*="bpx-player"]',
        '[class*="player"] video',
        '[class*="up-name"]',
        '[class*="up-info"]',
        '[class*="desc-info-text"]',
        '[class*="video-desc"]',
        '[class*="reply"]',
        '[class*="comment"]',
    ]
    for selector in content_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                rect = element.rect or {}
                if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                    continue
                text = normalize_text(element.text or "")
                if text or element.find_elements(By.CSS_SELECTOR, "video, canvas, img, button, svg"):
                    return True
            except Exception:
                continue

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        body_text = ""
    return any(marker in body_text for marker in ("播放", "弹幕", "点赞", "收藏", "评论", "简介", "UP主"))


class BilibiliAdapter(BaseAdapter):
    platform_name = "bilibili"
    supported_channels = ["http", "browser", "ocr", "import"]

    def __init__(self, *, headless: bool = False, crawler_config: dict | None = None):
        super().__init__(crawler_config=crawler_config)
        self.headless = headless
        self.driver = None

    def _ensure_driver(self):
        if self.driver is None:
            self.driver = create_chrome_driver(headless=self.headless)
        return self.driver

    def open_search_page(self, keyword: str) -> str:
        driver = self._ensure_driver()
        search_url = BILIBILI_SEARCH_URL.format(keyword=quote(keyword or ""))
        print(f"\n正在打开 Bilibili 搜索页：{search_url}")
        driver.get(search_url)
        time.sleep(4)
        self._save_debug_page()
        return search_url

    def open_url(self, url: str) -> Dict[str, object]:
        driver = self._ensure_driver()
        target_url = canonicalize_bilibili_video_url(url) or url
        driver.get(target_url)
        time.sleep(4)
        self._save_debug_page()
        return {"ok": True, "current_url": self.current_url(), "target_url": target_url}

    def iter_search_candidates(self, limit: int = 10) -> List[Dict[str, object]]:
        driver = self._ensure_driver()
        candidates: List[Dict[str, object]] = []
        seen = set()

        def add_candidate(element, href: str = ""):
            raw_href = href or self._safe_attr(element, "href")
            clean_url = canonicalize_bilibili_video_url(raw_href)
            title = self._candidate_title(self._candidate_root(element)) or self._candidate_title(element)
            raw_text = self._element_text(element)
            signature = clean_url or raw_href or title or raw_text
            if not signature or signature in seen:
                return
            seen.add(signature)
            candidates.append(
                {
                    "title": title,
                    "href": clean_url or raw_href,
                    "raw_text": raw_text,
                    "candidate_type": "video" if clean_url else "skipped_non_item",
                    "signature": signature,
                }
            )

        for selector in VIDEO_LINK_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                if len(candidates) >= limit:
                    break
                if not self._is_displayed(element):
                    continue
                add_candidate(element)
            if len(candidates) >= limit:
                break

        if len(candidates) < limit:
            for selector in SEARCH_CARD_SELECTORS:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for element in elements:
                    if len(candidates) >= limit:
                        break
                    if not self._is_displayed(element):
                        continue
                    href = ""
                    try:
                        link = element.find_element(By.CSS_SELECTOR, 'a[href*="/video/"], a[href*="BV"]')
                        href = self._safe_attr(link, "href")
                    except Exception:
                        pass
                    add_candidate(element, href=href)
                if len(candidates) >= limit:
                    break

        return candidates[: max(0, int(limit or 10))]

    def open_candidate(self, candidate: Dict[str, object]) -> Dict[str, object]:
        href = str(candidate.get("href") or "")
        clean_url = canonicalize_bilibili_video_url(href)
        if not clean_url:
            return {"ok": False, "reason": "skipped_non_item", "current_url": href}
        return self.open_url(clean_url)

    def classify_current_page(self) -> Dict[str, object]:
        driver = self._ensure_driver()
        self._save_debug_page()
        current_url = self.current_url()
        body_text = self._body_text()
        body_lower = body_text.lower()

        guard_result = self.guard.detect_driver(driver)
        video_signals = has_bilibili_video_content(driver) or self._has_video_signals(body_text)
        strong_login_required = _contains_any(body_text, STRONG_LOGIN_REQUIRED_MARKERS)
        explicit_block_marker = _contains_any(body_lower, BLOCK_MARKERS) or _contains_any_status_code(
            body_lower, BLOCK_STATUS_CODES
        )
        login_modal_blocks_content = self._has_visible_login_modal() and not video_signals

        if guard_result.blocked:
            if guard_result.status == "login_required" and video_signals and not strong_login_required:
                pass
            elif guard_result.status == "blocked" and video_signals and not explicit_block_marker:
                pass
            else:
                if guard_result.status == "login_required":
                    self.write_login_required_debug(
                        status=guard_result.status,
                        reason=guard_result.reason or guard_result.status,
                        has_video_content=video_signals,
                    )
                return {
                    "status": guard_result.status,
                    "reason": guard_result.reason or guard_result.status,
                    "risk_result": guard_result,
                    "has_bilibili_video_content": video_signals,
                }

        if len(body_text.strip()) < 50 and not video_signals:
            classification = {
                "status": "blocked",
                "reason": "blank_page",
                "risk_result": RiskDetectionResult(
                    blocked=True,
                    reason="blank_page",
                    severity="medium",
                    action="stop_platform",
                    status="blocked",
                    raw_signal={"url": current_url, "body_length": len(body_text)},
                ),
                "has_bilibili_video_content": video_signals,
            }
            self.write_status_debug(status="blocked", reason="blank_page", has_video_content=video_signals)
            return classification
        if _contains_any(body_lower, CAPTCHA_MARKERS):
            return self._classification_risk("captcha_required", "captcha_required", action="require_user_action", has_video_content=video_signals)
        if _contains_any(body_lower, RATE_LIMIT_MARKERS) or _contains_any_status_code(body_lower, RATE_LIMIT_STATUS_CODES):
            return self._classification_risk("rate_limited", "rate_limited", action="pause", severity="medium", has_video_content=video_signals)
        if explicit_block_marker:
            return self._classification_risk("blocked", "blocked", has_video_content=video_signals)
        if strong_login_required or login_modal_blocks_content:
            classification = self._classification_risk("login_required", "login_required", action="require_user_action", has_video_content=video_signals)
            classification["has_bilibili_video_content"] = video_signals
            self.write_login_required_debug(
                status="login_required",
                reason="login_required",
                has_video_content=video_signals,
            )
            return classification
        if _contains_any(body_text, UNAVAILABLE_MARKERS) or _contains_any_status_code(body_lower, UNAVAILABLE_STATUS_CODES):
            self.write_status_debug(status="unavailable", reason="video_unavailable", has_video_content=video_signals)
            return {"status": "unavailable", "reason": "video_unavailable", "has_bilibili_video_content": video_signals}
        if not extract_bilibili_content_id(current_url):
            self.write_status_debug(status="skipped_non_item", reason="not_video_page", has_video_content=video_signals)
            return {"status": "skipped_non_item", "reason": "not_video_page", "has_bilibili_video_content": video_signals}
        if video_signals:
            return {"status": "available", "reason": "", "has_bilibili_video_content": video_signals}
        self.write_status_debug(status="unknown", reason="video_structure_unknown", has_video_content=video_signals)
        return {"status": "unknown", "reason": "video_structure_unknown", "has_bilibili_video_content": video_signals}

    def scrape_current_page_item(self) -> Dict[str, object]:
        driver = self._ensure_driver()
        self._save_debug_page()
        current_url = self.current_url()
        content_id = extract_bilibili_content_id(current_url)
        body_text = self._body_text()

        title = self._first_visible_text(TITLE_SELECTORS)
        if not title:
            title = normalize_text((driver.title or "").replace("_哔哩哔哩_bilibili", ""))

        description = self._best_description()
        author_name = self._first_visible_text(AUTHOR_SELECTORS)
        publish_time = self._extract_publish_time(body_text)
        metrics = self._extract_metrics(body_text)
        crawled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        source_url = canonicalize_bilibili_video_url(current_url) or current_url

        return {
            "platform": self.platform_name,
            "keyword": "",
            "content_id": content_id,
            "source_url": source_url,
            "clean_url": source_url,
            "raw_url": current_url,
            "title": title,
            "note_title": title,
            "body": description,
            "note_content": description,
            "raw_note_text": description,
            "author_name": author_name,
            "publish_time": publish_time,
            "view_count": metrics.get("view_count", ""),
            "like_count": metrics.get("like_count", ""),
            "coin_count": metrics.get("coin_count", ""),
            "collect_count": metrics.get("favorite_count", ""),
            "favorite_count": metrics.get("favorite_count", ""),
            "comment_count_display": metrics.get("comment_count", ""),
            "share_count": metrics.get("share_count", ""),
            "danmaku_count": metrics.get("danmaku_count", ""),
            "duration": metrics.get("duration", ""),
            "tags": self._extract_tags(),
            "is_video": True,
            "crawled_at": crawled_at,
            "raw_metadata": {"metrics": metrics, "debug_page": str(DEBUG_PAGE_PATH)},
        }

    def scrape_current_page_comments(self, limit: int = 10) -> List[Dict[str, object]]:
        driver = self._ensure_driver()
        self._scroll_to_comments()
        self._save_debug_page()
        content_id = extract_bilibili_content_id(self.current_url())
        source_url = canonicalize_bilibili_video_url(self.current_url()) or self.current_url()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        comments: List[Dict[str, object]] = []
        seen = set()
        containers = []
        for selector in COMMENT_CONTAINER_SELECTORS:
            try:
                containers.extend(driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue

        for element in containers:
            if len(comments) >= limit:
                break
            if not self._is_displayed(element):
                continue
            raw_text = self._element_text(element)
            if not raw_text or len(raw_text) > 1000:
                continue
            username = self._first_visible_text(COMMENT_USER_SELECTORS, scope=element)
            content = self._comment_content(element, raw_text, username=username)
            if not self._is_valid_comment_content(content):
                continue
            publish_time = self._extract_publish_time(raw_text)
            likes = self._first_visible_text(COMMENT_LIKE_SELECTORS, scope=element)
            likes = self._metric_number(likes)
            key = (username, content, publish_time)
            if key in seen:
                continue
            seen.add(key)
            comment_id = self._safe_attr(element, "data-id") or self._safe_attr(element, "id") or f"{content_id}-{len(comments) + 1}"
            comments.append(
                {
                    "platform": self.platform_name,
                    "content_id": content_id,
                    "source_url": source_url,
                    "comment_id": comment_id,
                    "username": username,
                    "content": content,
                    "raw_text": raw_text,
                    "publish_time": publish_time,
                    "likes": likes,
                    "crawled_at": timestamp,
                    "raw_metadata": {"raw_text": raw_text},
                }
            )
        return comments

    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        self.open_search_page(seed)
        results = []
        for item in self.iter_search_candidates(limit=limit):
            if item.get("candidate_type") != "video":
                continue
            href = str(item.get("href") or "")
            results.append(
                SearchResult(
                    platform=self.platform_name,
                    source_url=href,
                    content_id=extract_bilibili_content_id(href),
                    title=str(item.get("title") or ""),
                    raw_metadata=item,
                )
            )
        return results

    def fetch_item(self, url_or_id: str) -> ContentItem:
        self.open_url(url_or_id)
        classification = self.classify_current_page()
        if classification.get("status") != "available":
            source_url = canonicalize_bilibili_video_url(url_or_id) or self.current_url() or url_or_id
            reason = str(classification.get("reason") or classification.get("status") or "")
            return ContentItem(
                platform=self.platform_name,
                source_url=source_url,
                content_id=extract_bilibili_content_id(source_url),
                status=str(classification.get("status") or "failed"),
                last_error=reason,
                blocked_reason=reason if classification.get("status") in {"blocked", "login_required", "captcha_required", "rate_limited"} else "",
                raw_metadata={"classification": classification},
            )
        return self.normalize(self.scrape_current_page_item())

    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        raw_comments = self.scrape_current_page_comments(limit=limit)
        return [
            ContentComment(
                platform=self.platform_name,
                content_id=str(raw.get("content_id") or item_id),
                source_url=str(raw.get("source_url") or self.current_url()),
                comment_id=str(raw.get("comment_id") or ""),
                username=str(raw.get("username") or ""),
                content=str(raw.get("content") or ""),
                raw_text=str(raw.get("raw_text") or ""),
                publish_time=str(raw.get("publish_time") or ""),
                likes=str(raw.get("likes") or ""),
                crawled_at=str(raw.get("crawled_at") or ""),
                raw_metadata=dict(raw.get("raw_metadata") or {}),
            )
            for raw in raw_comments
        ]

    def normalize(self, raw: dict) -> ContentItem:
        source_url = str(raw.get("source_url") or raw.get("clean_url") or raw.get("raw_url") or "").strip()
        return ContentItem(
            platform=self.platform_name,
            source_url=source_url,
            content_id=str(raw.get("content_id") or extract_bilibili_content_id(source_url)),
            author_name=str(raw.get("author_name") or ""),
            title=str(raw.get("title") or raw.get("note_title") or ""),
            body=str(raw.get("body") or raw.get("note_content") or ""),
            publish_time=str(raw.get("publish_time") or ""),
            engagement=EngagementMetrics(
                like_count=str(raw.get("like_count") or ""),
                comment_count=str(raw.get("comment_count_display") or ""),
                share_count=str(raw.get("share_count") or ""),
                favorite_count=str(raw.get("favorite_count") or raw.get("collect_count") or ""),
                view_count=str(raw.get("view_count") or ""),
            ),
            raw_metadata=dict(raw.get("raw_metadata") or raw),
            status=str(raw.get("status") or "pending"),
        )

    def current_url(self) -> str:
        if self.driver is None:
            return ""
        try:
            return (self.driver.current_url or "").strip()
        except Exception:
            return ""

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _classification_risk(
        self,
        status: str,
        reason: str,
        *,
        action: str = "stop_platform",
        severity: str = "high",
        has_video_content: bool = False,
    ) -> Dict[str, object]:
        result = RiskDetectionResult(
            blocked=True,
            reason=reason,
            severity=severity,
            action=action,
            status=status,
            raw_signal={"url": self.current_url()},
        )
        self.write_status_debug(status=status, reason=reason, has_video_content=has_video_content)
        return {"status": status, "reason": reason, "risk_result": result, "has_bilibili_video_content": has_video_content}

    def _save_debug_page(self) -> None:
        try:
            DEBUG_PAGE_PATH.write_text(self.driver.page_source or "", encoding="utf-8")
        except Exception:
            pass

    def write_login_required_debug(self, *, status: str, reason: str, has_video_content: bool) -> Path:
        return self.write_status_debug(status=status, reason=reason, has_video_content=has_video_content)

    def write_status_debug(self, *, status: str, reason: str, has_video_content: bool) -> Path:
        from notifier import ensure_directory

        log_dir = ensure_directory("outputs/logs")
        output_path = log_dir / f"debug_bilibili_status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            title = normalize_text(self.driver.title or "")
        except Exception:
            title = ""
        body_text = self._body_text()
        lines = [
            f"current_url: {self.current_url()}",
            f"title: {title}",
            f"page_status: {status}",
            f"reason: {reason}",
            f"has_bilibili_video_content: {has_video_content}",
            "",
            "body_first_1000:",
            body_text[:1000],
        ]
        try:
            output_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        return output_path

    def _body_text(self) -> str:
        try:
            return self.driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""

    def _has_video_signals(self, body_text: str) -> bool:
        if extract_bilibili_content_id(self.current_url()) and self._first_visible_text(TITLE_SELECTORS):
            return True
        return bool(extract_bilibili_content_id(self.current_url()) and any(marker in body_text for marker in ("播放", "弹幕", "点赞", "收藏", "评论")))

    def _has_visible_login_modal(self) -> bool:
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, '[class*="login"], [class*="Login"], [id*="login"], [id*="Login"]')
        except Exception:
            return False
        for element in elements:
            if not self._is_displayed(element):
                continue
            text = self._element_text(element)
            if any(marker in text for marker in LOGIN_MODAL_MARKERS):
                return True
        return False

    def _candidate_title(self, element) -> str:
        candidates = [
            self._safe_attr(element, "title"),
            self._safe_attr(element, "data-title"),
            self._safe_attr(element, "aria-label"),
        ]
        try:
            title_elements = element.find_elements(By.CSS_SELECTOR, 'h3, h4, [class*="title"], [class*="name"]')
        except Exception:
            title_elements = []
        for title_element in title_elements:
            candidates.append(self._element_text(title_element))
            candidates.append(self._safe_attr(title_element, "title"))
        candidates.append(self._element_text(element))

        for value in candidates:
            title = self._pick_title_line(value)
            if title:
                return title
        return ""

    def _pick_title_line(self, text: str) -> str:
        lines = [normalize_text(line) for line in str(text or "").splitlines()]
        for line in lines:
            if self._looks_like_candidate_title(line):
                return line[:160]
        return ""

    def _looks_like_candidate_title(self, line: str) -> bool:
        value = normalize_text(line)
        if len(value) < 4:
            return False
        if any(noise in value for noise in ("播放", "弹幕", "UP主", "广告", "观看", "粉丝")):
            return False
        if re.fullmatch(r"[0-9\s:.,万亿+-]+", value):
            return False
        if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}.*", value):
            return False
        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value):
            return False
        return True

    def _candidate_root(self, element):
        for class_name in ("bili-video-card", "video-card", "search-result", "video"):
            try:
                return element.find_element(By.XPATH, f"./ancestor-or-self::*[contains(@class, '{class_name}')][1]")
            except Exception:
                continue
        return element

    def _best_description(self) -> str:
        texts = self._visible_texts(DESC_SELECTORS)
        for text in texts:
            if self._is_good_description(text):
                return text[:2000]
        return ""

    def _is_good_description(self, text: str) -> bool:
        value = normalize_text(text)
        if len(value) < 8:
            return False
        if len(value) > 2500:
            return False
        if any(noise in value for noise in ("首页", "动态", "热门", "排行榜", "登录", "注册")):
            return False
        return True

    def _extract_metrics(self, body_text: str) -> Dict[str, str]:
        return {
            "view_count": self._metric_after_keywords(body_text, ("播放", "观看")),
            "danmaku_count": self._metric_after_keywords(body_text, ("弹幕",)),
            "like_count": self._metric_after_keywords(body_text, ("点赞", "赞")),
            "coin_count": self._metric_after_keywords(body_text, ("投币",)),
            "favorite_count": self._metric_after_keywords(body_text, ("收藏",)),
            "share_count": self._metric_after_keywords(body_text, ("分享",)),
            "comment_count": self._metric_after_keywords(body_text, ("评论",)),
            "duration": self._extract_duration(body_text),
        }

    def _metric_after_keywords(self, text: str, keywords: Iterable[str]) -> str:
        value = normalize_text(text)
        for keyword in keywords:
            patterns = [
                rf"{re.escape(keyword)}[：:\s]*([0-9][0-9.,]*\s*[万亿]?)",
                rf"([0-9][0-9.,]*\s*[万亿]?)\s*{re.escape(keyword)}",
            ]
            for pattern in patterns:
                match = re.search(pattern, value)
                if match:
                    return normalize_text(match.group(1))
        return ""

    def _metric_number(self, text: str) -> str:
        match = re.search(r"([0-9][0-9.,]*\s*[万亿]?)", normalize_text(text))
        return normalize_text(match.group(1)) if match else ""

    def _extract_publish_time(self, text: str) -> str:
        value = normalize_text(text)
        patterns = [
            r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2})?)",
            r"(20\d{2}年\d{1,2}月\d{1,2}日(?:\s+\d{1,2}:\d{2})?)",
            r"(\d{1,2}分钟前|\d{1,2}小时前|\d{1,2}天前)",
        ]
        for pattern in patterns:
            match = re.search(pattern, value)
            if match:
                return match.group(1)
        return ""

    def _extract_duration(self, text: str) -> str:
        match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text or "")
        return match.group(1) if match else ""

    def _extract_tags(self) -> List[str]:
        tags = []
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="tag"], [class*="tag"]')
        except Exception:
            return tags
        seen = set()
        for element in elements:
            text = self._element_text(element).lstrip("#")
            if text and len(text) <= 30 and text not in seen:
                seen.add(text)
                tags.append(text)
        return tags[:12]

    def _scroll_to_comments(self) -> None:
        try:
            self.driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 1.2, 900));")
            time.sleep(1.5)
            self.driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 1.2, 900));")
            time.sleep(1.5)
        except Exception:
            time.sleep(1)

    def _comment_content(self, element, raw_text: str, *, username: str) -> str:
        texts = self._visible_texts(COMMENT_CONTENT_SELECTORS, scope=element)
        for text in texts:
            if self._is_valid_comment_content(text):
                return text[:500]

        lines = [normalize_text(line) for line in raw_text.splitlines()]
        filtered = []
        for line in lines:
            if not line or line == username:
                continue
            if self._extract_publish_time(line) == line:
                continue
            if line in COMMENT_NOISE_PHRASES:
                continue
            if re.fullmatch(r"[0-9.,万亿赞\s]+", line):
                continue
            filtered.append(line)
        return normalize_text(" ".join(filtered[:3]))[:500]

    def _is_valid_comment_content(self, text: str) -> bool:
        value = normalize_text(text)
        if len(value) < 2 or len(value) > 500:
            return False
        if value in COMMENT_NOISE_PHRASES:
            return False
        if any(noise in value for noise in ("相关推荐", "热门视频", "播放全部", "登录后发表评论")):
            return False
        return True

    def _first_visible_text(self, selectors: Iterable[str], scope=None) -> str:
        texts = self._visible_texts(selectors, scope=scope)
        return texts[0] if texts else ""

    def _visible_texts(self, selectors: Iterable[str], scope=None) -> List[str]:
        target = scope or self.driver
        results = []
        seen = set()
        for selector in selectors:
            try:
                elements = target.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                if not self._is_displayed(element):
                    continue
                text = self._element_text(element)
                if text and text not in seen:
                    seen.add(text)
                    results.append(text)
        return results

    def _element_text(self, element) -> str:
        try:
            return normalize_text(element.text or "")
        except Exception:
            return ""

    def _safe_attr(self, element, name: str) -> str:
        try:
            return (element.get_attribute(name) or "").strip()
        except Exception:
            return ""

    def _is_displayed(self, element) -> bool:
        try:
            return bool(element.is_displayed())
        except Exception:
            return False


def _contains_any(value: str, markers: Iterable[str]) -> bool:
    lowered = (value or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _contains_any_status_code(value: str, status_codes: Iterable[int]) -> bool:
    return any(contains_http_status_code(value, status_code) for status_code in status_codes)
