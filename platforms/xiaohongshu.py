#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小红书平台适配器。"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from db import extract_xhs_content_id
from models import ContentComment, ContentItem, EngagementMetrics, SearchResult
from platforms.base import BaseAdapter
from platforms.capabilities import (
    CAP_COMMENTS,
    CAP_CONTENT_DETAIL,
    CAP_NOT_SUPPORTED,
    CAP_SEARCH_CONTENT,
    DATA_VERIFIED,
    MODE_AUTHORIZED_SESSION,
    MODE_LOCAL_DATABASE,
    MODE_USER_IMPORT,
    AccessResult,
    CapabilityStatus,
    PlatformInfo,
    UnifiedComment,
    UnifiedContent,
    capability_status,
    not_supported,
)

# 注意：XHSCommentScraper 依赖 selenium，改为**惰性导入**。
# 否则在无浏览器环境下，连"只用本地已有真实数据"的免登录路径都会因导入失败而不可用。

# 第五轮核查结论：小红书 Web 端内容与评论均需登录态（未登录会被登录墙拦截），
# 不存在合法免登录公开只读接口；因此免登录路径只能是"本地已有真实数据"。
XHS_NO_LOGIN_FREE_REASON = (
    "小红书 Web 端内容与评论需登录态（未登录跳转登录墙），无合法免登录公开只读接口；"
    "本系统不绕过登录墙。免登录可用的路径为本地已有真实采集数据（LOCAL_DATABASE）或用户主动导入。"
)


class XhsAdapter(BaseAdapter):
    platform_name = "xiaohongshu"

    def capabilities(self) -> tuple[str, ...]:
        # 浏览器/登录态驱动：搜索、详情、评论可用；弹幕不适用；二级评论不支持。
        return (CAP_SEARCH_CONTENT, CAP_CONTENT_DETAIL, CAP_COMMENTS)

    def platform_info(self) -> PlatformInfo:
        return PlatformInfo(
            name="xiaohongshu",
            display_name="小红书",
            adapter_key="xiaohongshu",
            capabilities=self.capabilities(),
            access_method="浏览器自动化（需登录态）+ 本地已采集数据库",
            verification=DATA_VERIFIED,
            auth_required=True,
            notes=(
                "库内已有真实采集数据（90 条内容 / 419 条评论）可用于分析；"
                "线上实时抓取依赖登录态与浏览器，本轮未做新的真实抓取，故为 DATA_VERIFIED 而非 LIVE_VERIFIED。"
                f"第五轮核查：{XHS_NO_LOGIN_FREE_REASON}"
            ),
            access_modes=(MODE_LOCAL_DATABASE, MODE_AUTHORIZED_SESSION, MODE_USER_IMPORT),
            default_access_mode=MODE_LOCAL_DATABASE,
            login_free_capabilities=(),  # 线上能力均需登录；免登录仅覆盖本地已有数据
        )

    # ------------------------------------------------------------------ #
    # 第五轮：接入模式 + 能力级运行时状态
    # ------------------------------------------------------------------ #
    def probe_capability(self, capability: str, *, live: bool = True) -> CapabilityStatus:
        """小红书能力探测：线上能力一律需登录态，免登录仅 LOCAL_DATABASE 可用。

        浏览器登录态是否可用由 ``has_session`` 运行时判定（检查是否已初始化 driver），
        不硬编码"可用"。
        """
        if capability not in self.capabilities():
            return capability_status(
                self.platform_name, capability, status=CAP_NOT_SUPPORTED,
                reason=f"xiaohongshu 未实现 {capability}（弹幕/二级评论不适用）",
                probed=False, evidence={"not_probed_reason": "适配器未声明该能力"},
            )
        if self.has_session:
            return capability_status(
                self.platform_name, capability, access_mode=MODE_AUTHORIZED_SESSION,
                status="AVAILABLE",
                reason="已存在浏览器登录态会话，可走授权会话模式（真实抓取结果以实际请求为准）",
                probed=False,
                evidence={"has_session": True, "not_probed_reason": "避免为探测触发额外页面加载与风控"},
            )
        return capability_status(
            self.platform_name, capability, access_mode=MODE_AUTHORIZED_SESSION,
            status="AUTH_REQUIRED",
            reason=f"线上 {capability} 需登录态；{XHS_NO_LOGIN_FREE_REASON}",
            probed=False,
            evidence={"has_session": False, "not_probed_reason": XHS_NO_LOGIN_FREE_REASON},
        )

    @property
    def has_session(self) -> bool:
        """运行时判定是否存在可用浏览器登录态（非硬编码）。"""
        return self.scraper is not None and getattr(self.scraper, "driver", None) is not None

    def check_access(self, capability: str = "", *, live: bool = True) -> AccessResult:
        probe = self.probe_capability(capability or CAP_CONTENT_DETAIL, live=live)
        if probe.status == "AVAILABLE":
            return AccessResult(self.platform_name, probe.capability, "OK", probe.reason,
                                access_mode=probe.access_mode, evidence=probe.evidence)
        if probe.status == CAP_NOT_SUPPORTED:
            return not_supported(self.platform_name, probe.capability, probe.reason)
        return AccessResult(self.platform_name, probe.capability, "AUTH_REQUIRED", probe.reason,
                            access_mode=probe.access_mode, evidence=probe.evidence)

    def search_content(self, keyword: str, limit: int = 10) -> AccessResult:
        return self.check_access(CAP_SEARCH_CONTENT)

    def get_content_detail(self, content_id: str) -> AccessResult:
        return self.check_access(CAP_CONTENT_DETAIL)

    def list_comments(self, content_id: str, limit: int = 30) -> AccessResult:
        return self.check_access(CAP_COMMENTS)

    def normalize_content(self, raw: dict) -> UnifiedContent:
        return UnifiedContent(
            platform=self.platform_name,
            platform_content_id=str(raw.get("content_id") or raw.get("note_id") or ""),
            content_type="note",
            title=str(raw.get("title") or raw.get("note_title") or ""),
            text=str(raw.get("body") or raw.get("note_content") or ""),
            source_url=str(raw.get("source_url") or raw.get("clean_url") or ""),
            author_ref=str(raw.get("author_name") or ""),
            published_at=str(raw.get("publish_time") or ""),
            source_method="xhs_browser",
        )

    def normalize_comment(self, raw: dict, *, platform_content_id: str = "") -> UnifiedComment:
        return UnifiedComment(
            platform=self.platform_name,
            platform_comment_id=str(raw.get("comment_id") or ""),
            platform_content_id=platform_content_id,
            content=str(raw.get("content") or ""),
            parent_comment_id=str(raw.get("parent_comment_id") or ""),
            author_ref=str(raw.get("username") or ""),
            published_at=str(raw.get("publish_time") or ""),
            provenance={"source_method": "xhs_browser"},
        )
    supported_channels = ["browser", "ocr", "import"]

    def __init__(self, *, headless: bool = False, crawler_config: dict | None = None):
        super().__init__(crawler_config=crawler_config)
        self.headless = headless
        self.scraper: object | None = None

    def _ensure_scraper(self):
        """惰性初始化浏览器抓取器（selenium 仅在真正需要线上抓取时才导入）。"""
        if self.scraper is None:
            from xhs_scraper import XHSCommentScraper

            self.scraper = XHSCommentScraper(headless=self.headless)
            self.scraper.setup_driver()
        return self.scraper

    def open_search_page(self, seed: str) -> str:
        from search_stream_scrape import build_search_url

        scraper = self._ensure_scraper()
        driver = scraper.driver
        if driver is None:
            raise RuntimeError("XHS driver is not initialized")
        search_url = build_search_url(seed)
        print(f"\n正在打开搜索页：{search_url}")
        driver.get(search_url)
        return search_url

    def wait_search_ready(self) -> None:
        from search_stream_scrape import wait_search_ready

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        wait_search_ready(scraper.driver)

    def detect_current_risk(self, *, expected_url_contains: str = ""):
        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        return self.guard.detect_driver(scraper.driver, expected_url_contains=expected_url_contains)

    def list_search_candidates(self) -> List[Dict[str, object]]:
        from search_stream_scrape import get_search_result_cards

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        return get_search_result_cards(scraper.driver)

    def scroll_search_results(self) -> None:
        import time

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        scraper.driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 900));")
        time.sleep(3)

    def open_candidate(self, candidate: Dict[str, object]) -> Dict[str, object]:
        from search_stream_scrape import DEFAULT_NOTE_WAIT_SECONDS, open_card_from_search

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        return open_card_from_search(scraper.driver, candidate, wait_seconds=DEFAULT_NOTE_WAIT_SECONDS)

    def current_url(self) -> str:
        scraper = self._ensure_scraper()
        if scraper.driver is None:
            return ""
        return (scraper.driver.current_url or "").strip()

    def return_to_search_page(self, search_url: str, navigation_mode: str, original_window: str) -> None:
        from search_stream_scrape import return_to_search_page

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        return_to_search_page(scraper.driver, search_url, navigation_mode, original_window)

    def scrape_current_note_content(self, *, keyword: str, search_title: str, raw_url: str) -> Dict[str, object]:
        from search_stream_scrape import get_note_title

        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        from browser_utils import canonicalize_explore_url

        clean_url = canonicalize_explore_url(raw_url) or raw_url
        note_data = scraper.scrape_current_page_note_content(
            note_url=clean_url,
            keyword=keyword,
            search_title=search_title,
            raw_url=raw_url,
        )
        note_data["clean_url"] = note_data.get("clean_url") or clean_url
        note_data["raw_url"] = note_data.get("raw_url") or raw_url or clean_url
        note_data["search_title"] = note_data.get("search_title") or search_title
        note_data["note_title"] = note_data.get("note_title") or get_note_title(scraper.driver, fallback=search_title)
        return note_data

    def is_current_note_unavailable(self, *, debug_filename: str = "debug_page.html") -> tuple[bool, str]:
        scraper = self._ensure_scraper()
        if scraper.driver is None:
            raise RuntimeError("XHS driver is not initialized")
        from browser_utils import is_unavailable_note_page

        return is_unavailable_note_page(scraper.driver, debug_filename=debug_filename)

    def scrape_current_comments(self, *, note_url: str, max_comments: int) -> List[Dict[str, str]]:
        from search_stream_scrape import DEFAULT_SCROLL_PAUSE

        scraper = self._ensure_scraper()
        return scraper.scrape_current_page_comments(
            note_url=note_url,
            max_comments=max_comments,
            scroll_pause=DEFAULT_SCROLL_PAUSE,
            wait_for_login=0,
            manual_confirm=False,
        )

    def search(self, seed: str, limit: int = 10) -> Iterable[SearchResult]:
        from search_stream_scrape import build_search_url, get_search_result_cards

        scraper = self._ensure_scraper()
        driver = scraper.driver
        if driver is None:
            return []

        driver.get(build_search_url(seed))
        cards = get_search_result_cards(driver)[: max(0, int(limit or 10))]
        results: List[SearchResult] = []
        for card in cards:
            url = str(card.get("href") or card.get("card_href") or "").strip()
            from browser_utils import canonicalize_explore_url

            clean_url = canonicalize_explore_url(url) if url else ""
            if not clean_url:
                continue
            results.append(
                SearchResult(
                    platform=self.platform_name,
                    source_url=clean_url,
                    content_id=extract_xhs_content_id(clean_url),
                    title=str(card.get("title_text") or card.get("search_title") or ""),
                    raw_metadata=dict(card),
                )
            )
        return results

    def fetch_item(self, url_or_id: str) -> ContentItem:
        scraper = self._ensure_scraper()
        driver = scraper.driver
        if driver is None:
            raise RuntimeError("XHS driver is not initialized")

        from browser_utils import canonicalize_explore_url

        url = canonicalize_explore_url(url_or_id) or url_or_id
        driver.get(url)
        risk = self.guard.detect_driver(driver, expected_url_contains="/explore/")
        if risk.blocked:
            return ContentItem(
                platform=self.platform_name,
                source_url=url,
                content_id=extract_xhs_content_id(url),
                status=risk.status,
                last_error=risk.reason,
                blocked_reason=risk.reason,
                raw_metadata={"risk": risk.raw_signal},
            )

        raw = scraper.scrape_current_page_note_content(note_url=url, raw_url=driver.current_url)
        return self.normalize(raw)

    def fetch_comments(self, item_id: str, limit: int = 30) -> List[ContentComment]:
        scraper = self._ensure_scraper()
        raw_comments = scraper.scrape_current_page_comments(note_url=item_id, max_comments=limit)
        comments: List[ContentComment] = []
        for raw in raw_comments:
            comments.append(
                ContentComment(
                    platform=self.platform_name,
                    content_id=extract_xhs_content_id(item_id),
                    source_url=item_id,
                    username=(raw.get("用户名") or "").strip(),
                    content=(raw.get("评论内容") or "").strip(),
                    raw_text=(raw.get("原始文本") or "").strip(),
                    publish_time=(raw.get("发布时间") or "").strip(),
                    likes=(raw.get("点赞数") or "").strip(),
                    crawled_at=(raw.get("爬取时间") or "").strip(),
                    raw_metadata=raw,
                )
            )
        return comments

    def normalize(self, raw: dict) -> ContentItem:
        source_url = (raw.get("clean_url") or raw.get("raw_url") or "").strip()
        return ContentItem(
            platform=self.platform_name,
            source_url=source_url,
            content_id=extract_xhs_content_id(source_url),
            author_name=(raw.get("author_name") or "").strip(),
            title=(raw.get("note_title") or "").strip(),
            body=(raw.get("note_content") or "").strip(),
            publish_time=(raw.get("publish_time") or "").strip(),
            engagement=EngagementMetrics(
                like_count=(raw.get("like_count") or "").strip(),
                comment_count=(raw.get("comment_count_display") or "").strip(),
                share_count=(raw.get("share_count") or "").strip(),
                favorite_count=(raw.get("collect_count") or "").strip(),
            ),
            raw_metadata=raw,
            status=(raw.get("status") or "pending").strip(),
        )

    def try_http_only(self, url: str) -> Optional[ContentItem]:
        return None

    def close(self) -> None:
        if self.scraper:
            self.scraper.close()
            self.scraper = None
