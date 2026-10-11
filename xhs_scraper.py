#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XHS 网页端采集工具
说明：
1. 使用公开网页中浏览器可见的内容采集评论
2. 需要用户手动完成登录、短信验证码、安全验证
3. 只做公开可见页面解析，不绕过验证码，不调用私有接口
"""

from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from typing import Dict, Iterable, List

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from browser_utils import (
    canonicalize_explore_url,
    create_chrome_driver,
    has_comment_area,
    is_unavailable_note_page,
    page_contains_block_keywords,
)

COMMENT_SELECTORS = [
    '[class*="parent-comment"]',
    '[class*="comment-inner"]',
    '[class*="comment-content"]',
    '[class*="comment-item"]',
    '[class*="CommentItem"]',
    '[class*="note-comment"]',
    '[class*="comments-el"]',
    'div[class*="comment"]',
    'li[class*="comment"]',
]
COMMENT_CONTAINER_SELECTORS = [
    ".comments-container",
    ".comments-el",
    '[class*="comments-container"]',
    '[class*="comment-container"]',
    '[class*="comment-list"]',
    '[class*="comments-list"]',
    '[class*="note-comments"]',
    '[class*="interaction-container"]',
]
MORE_REPLY_TEXTS = ["展开回复", "更多回复", "查看回复", "全部回复"]
UI_ONLY_PHRASES = {
    "举报评论",
    "回复",
    "赞",
    "更多",
    "展开",
    "查看",
    "全部回复",
    "展开回复",
    "更多回复",
    "查看回复",
}
NOTE_TITLE_SELECTORS = [
    "#detail-title",
    '[id="detail-title"]',
    ".note-scroller #detail-title",
    "h1",
    '[class*="title"]',
    '[class*="note-title"]',
]
NOTE_CONTENT_SELECTORS = [
    "#detail-desc .note-text",
    "#detail-desc",
    ".note-scroller #detail-desc .note-text",
    ".note-scroller #detail-desc",
    '[class*="note-content"]',
    '[class*="note-text"]',
    '[class*="desc"]',
]
NOTE_CONTAINER_SELECTORS = [
    ".note-scroller",
    ".note-content",
    ".interaction-container",
    "article",
    "main",
    '[class*="note"]',
]
AUTHOR_SELECTORS = [
    ".author-wrapper .name .username",
    ".author-wrapper .name",
    ".author-container .name .username",
    ".author-container .name",
    'a[class*="name"]',
]
TIME_SELECTORS = [
    ".bottom-container .date",
    ".note-scroller .bottom-container .date",
    '[class*="date"]',
    '[class*="time"]',
    '[class*="publish"]',
]
MEDIA_CONTAINER_SELECTORS = [
    '[class*="media-container"]',
    '[class*="note-slider"]',
    '[class*="swiper"]',
    '[class*="carousel"]',
    '[class*="video-player"]',
    '[class*="browser-player"]',
    '[class*="note-image"]',
]
COMMENT_TEXT_SELECTORS = [
    ".content .note-text",
    ".content span.note-text",
    ".content",
    '[class*="content"] .note-text',
    '[class*="reply-content"] .note-text',
]
NOTE_SKIP_PHRASES = {
    "登录",
    "验证码",
    "首页",
    "购物",
    "消息",
    "搜索",
    "相关搜索",
    "大家都在搜",
    "推荐搜索",
}
COMMENT_NOISE_PHRASES = {
    "这是一片荒地点击评论",
    "点击评论",
    "发送",
    "取消",
}
NOISE_CONTAINER_KEYWORDS = ("avatar", "author", "user", "profile", "comment")


class XHSCommentScraper:
    def __init__(self, headless: bool = False):
        self.driver = None
        self.headless = headless
        self.comments: List[Dict[str, str]] = []
        self.last_run_info: Dict[str, object] = {}
        self.last_status = ""
        self.last_fail_reason = ""

    def setup_driver(self):
        """配置浏览器驱动。"""
        self.driver = create_chrome_driver(headless=self.headless)
        return self.driver

    def wait_user_login(self):
        """等待用户手动完成登录。"""
        print("\n请在弹出的 Chrome 浏览器中手动完成登录。")
        print("如果出现短信验证码、安全验证、滑块验证，请手动处理。")
        print("确认浏览器中已经看到笔记正文和评论区后，再回到终端继续。")
        input("确认页面已准备好后，按 Enter 继续：")
        time.sleep(2)

    def _save_debug_page(self, filename: str):
        with open(filename, "w", encoding="utf-8") as file:
            file.write(self.driver.page_source)

    def _has_comment_area(self) -> bool:
        return has_comment_area(self.driver)

    def _extract_visible_texts(self, selectors: Iterable[str], scope=None) -> List[str]:
        target = scope or self.driver
        results = []
        seen = set()

        for selector in selectors:
            try:
                elements = target.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    text = self._normalize_text(element.text)
                except Exception:
                    continue
                if not text or text in seen:
                    continue
                seen.add(text)
                results.append(text)
        return results

    def _first_visible_text(self, selectors: Iterable[str], scope=None) -> str:
        texts = self._extract_visible_texts(selectors, scope=scope)
        return texts[0] if texts else ""

    def _first_visible_element(self, selectors: Iterable[str], scope=None):
        target = scope or self.driver
        for selector in selectors:
            try:
                elements = target.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    def _filter_note_lines(self, text: str, excluded: Iterable[str] | None = None) -> List[str]:
        excluded_values = {self._normalize_text(value) for value in (excluded or []) if value}
        filtered_lines = []

        for raw_line in (text or "").splitlines():
            line = self._normalize_text(raw_line)
            if not line:
                continue
            if line in excluded_values:
                continue
            if any(skip_word in line for skip_word in NOTE_SKIP_PHRASES):
                continue
            if re.fullmatch(r"[0-9\s\.万千wW]+", line):
                continue
            filtered_lines.append(line)

        deduped = []
        seen = set()
        for line in filtered_lines:
            if line not in seen:
                seen.add(line)
                deduped.append(line)
        return deduped

    def _extract_note_container_text(self) -> str:
        def collect_candidates(selectors: Iterable[str]) -> List[str]:
            collected = []
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue

                for element in elements:
                    try:
                        if not element.is_displayed():
                            continue
                        class_name = (element.get_attribute("class") or "").lower()
                        if "comment" in class_name:
                            continue
                        text = self._normalize_text(element.text)
                    except Exception:
                        continue
                    if text and len(text) > 10:
                        collected.append(text)
            return collected

        candidates = collect_candidates(NOTE_CONTENT_SELECTORS)
        if not candidates:
            candidates = collect_candidates(NOTE_CONTAINER_SELECTORS)

        if not candidates:
            try:
                body_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                body_text = ""
            candidates.append(self._normalize_text(body_text))

        candidates = [candidate for candidate in candidates if candidate]
        candidates.sort(key=len, reverse=True)
        return candidates[0] if candidates else ""

    def _extract_meta_content(self, meta_name: str) -> str:
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, f'meta[name="{meta_name}"]')
        except Exception:
            elements = []

        for element in elements:
            try:
                content = self._normalize_text(element.get_attribute("content") or "")
            except Exception:
                content = ""
            if content:
                return content
        return ""

    def _extract_metric_from_selectors(self, selectors: Iterable[str]) -> str:
        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    text = self._normalize_text(element.text)
                except Exception:
                    continue
                if not text or text in UI_ONLY_PHRASES:
                    continue
                return text
        return ""

    def _extract_engagement_metric(
        self,
        *,
        selectors: Iterable[str],
        label: str,
        meta_name: str = "",
    ) -> str:
        value = self._extract_metric_from_selectors(selectors)
        if value:
            return value

        value = self._extract_metric_by_label(label)
        if value and value not in UI_ONLY_PHRASES and value != label:
            return value

        if meta_name:
            return self._extract_meta_content(meta_name)
        return ""

    def _extract_note_media_scope(self):
        return self._first_visible_element(MEDIA_CONTAINER_SELECTORS)

    def _element_inside_noise_container(self, element) -> bool:
        try:
            current = element
            for _ in range(6):
                current = current.find_element(By.XPATH, "./..")
                class_name = (current.get_attribute("class") or "").lower()
                if any(keyword in class_name for keyword in NOISE_CONTAINER_KEYWORDS):
                    return True
        except Exception:
            return False
        return False

    def _extract_metric_by_label(self, label: str) -> str:
        xpath = (
            "//*[self::button or self::a or self::span or self::div]"
            f"[contains(normalize-space(.), '{label}')]"
        )
        try:
            elements = self.driver.find_elements(By.XPATH, xpath)
        except Exception:
            elements = []

        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                text = self._normalize_text(element.text)
            except Exception:
                continue
            if not text or label not in text:
                continue

            match = re.search(rf"{label}\s*([0-9\.万千wW]+)", text)
            if match:
                return match.group(1)
            match = re.search(rf"([0-9\.万千wW]+)\s*{label}", text)
            if match:
                return match.group(1)
            if len(text) <= 20:
                return text.replace(label, "").strip() or text

        return ""

    def _count_note_images(self) -> int:
        media_scope = self._extract_note_media_scope()
        target = media_scope or self.driver

        try:
            images = target.find_elements(By.CSS_SELECTOR, "img[src]")
        except Exception:
            return 0

        urls = set()
        for image in images:
            try:
                if not image.is_displayed():
                    continue
                if self._element_inside_noise_container(image):
                    continue
                rect = image.rect or {}
                if rect.get("width", 0) < 120 or rect.get("height", 0) < 120:
                    continue
                src = (image.get_attribute("src") or "").strip()
            except Exception:
                continue
            src_lower = src.lower()
            if any(keyword in src_lower for keyword in ("avatar", "emoji", "icon")):
                continue
            if src:
                urls.add(src)
        return len(urls)

    def _is_video_note(self) -> bool:
        media_scope = self._extract_note_media_scope()
        target = media_scope or self.driver
        selectors = [
            "video",
            '[class*="video-player"] video',
            '[class*="browser-player"] video',
            '[class*="note-video"] video',
            '[class*="player"] video',
        ]
        for selector in selectors:
            try:
                elements = target.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if element.is_displayed():
                        return True
                except Exception:
                    continue
        return False

    def _initialize_run_state(self, note_url: str = ""):
        self.comments = []
        self.last_run_info = {"note_url": note_url, "status": "started"}
        self.last_status = "started"
        self.last_fail_reason = ""

    def _scrape_loaded_page(
        self,
        *,
        note_url: str = "",
        max_comments: int = 100,
        scroll_pause: int = 3,
        wait_for_login: int = 0,
        manual_confirm: bool | None = None,
    ):
        if manual_confirm is None:
            manual_confirm = wait_for_login != 0

        is_unavailable, unavailable_reason = is_unavailable_note_page(self.driver)
        if is_unavailable:
            self.last_status = "unavailable"
            self.last_fail_reason = unavailable_reason or "网页端不可访问或 App-only"
            self.last_run_info.update(
                {
                    "status": "unavailable",
                    "fail_reason": self.last_fail_reason,
                }
            )
            print(f"当前笔记网页端不可访问，已跳过：{self.last_fail_reason}")
            return []

        if manual_confirm:
            self.wait_user_login()
        else:
            time.sleep(2)

        if not self.check_page_state(manual_confirm=manual_confirm):
            self.last_run_info["status"] = "blocked"
            self.last_status = self.last_status or "failed"
            self.last_fail_reason = self.last_fail_reason or "页面仍处于登录/验证状态"
            raise RuntimeError("页面仍处于登录/验证状态，已取消当前笔记采集。")

        self.scroll_comments(max_comments=max_comments, scroll_pause=scroll_pause)
        self._parse_comments(note_url=note_url, max_comments=max_comments)
        self.last_status = "crawled" if self.comments else "no_comments"
        self.last_fail_reason = "" if self.comments else "no_comments_or_parse_failed"
        self.last_run_info["status"] = "success" if self.comments else "empty"
        self.last_run_info["comment_count"] = len(self.comments)
        return self.comments

    def scrape_current_page_note_content(
        self,
        note_url: str = "",
        keyword: str = "",
        search_title: str = "",
        raw_url: str = "",
    ) -> Dict[str, object]:
        """
        在当前已经打开的笔记详情页中直接提取笔记正文和基础元数据，不重新打开 URL。
        """
        if not self.driver:
            raise RuntimeError("当前没有可用浏览器页面，无法解析笔记正文。")

        current_url = (self.driver.current_url or "").strip()
        clean_url = canonicalize_explore_url(current_url or note_url) or note_url or current_url
        raw_access_url = raw_url or current_url or note_url or clean_url

        note_title = self._first_visible_text(NOTE_TITLE_SELECTORS)
        if not note_title:
            note_title = self._extract_meta_content("og:title").replace(" - 小红书", "")

        author_name = self._first_visible_text(AUTHOR_SELECTORS)
        publish_time = self._first_visible_text(TIME_SELECTORS)
        desc_element = self._first_visible_element(NOTE_CONTENT_SELECTORS)
        raw_note_text = self._normalize_text(desc_element.text if desc_element is not None else "")
        if not raw_note_text:
            raw_note_text = self._extract_note_container_text()
        filtered_lines = self._filter_note_lines(
            raw_note_text,
            excluded=[note_title, author_name, publish_time, search_title],
        )
        note_content = " ".join(filtered_lines[:12]).strip()
        if not note_content and raw_note_text:
            note_content = " ".join(filtered_lines[:8]).strip() or self._normalize_text(raw_note_text)

        return {
            "keyword": keyword,
            "search_title": search_title,
            "clean_url": clean_url,
            "raw_url": raw_access_url,
            "note_title": note_title or search_title,
            "note_content": note_content,
            "author_name": author_name,
            "publish_time": publish_time,
            "like_count": self._extract_engagement_metric(
                selectors=[".engage-bar .like-wrapper .count", ".interactions .like-wrapper .count"],
                label="赞",
                meta_name="og:xhs:note_like",
            ),
            "collect_count": self._extract_engagement_metric(
                selectors=[".engage-bar .collect-wrapper .count", ".interactions .collect-wrapper .count"],
                label="收藏",
                meta_name="og:xhs:note_collect",
            ),
            "comment_count_display": self._extract_engagement_metric(
                selectors=[".engage-bar .chat-wrapper .count", ".interactions .chat-wrapper .count"],
                label="评论",
                meta_name="og:xhs:note_comment",
            ),
            "share_count": self._extract_engagement_metric(
                selectors=[".engage-bar .share-wrapper .count", ".interactions .share-wrapper .count"],
                label="分享",
            ),
            "image_count": self._count_note_images(),
            "is_video": self._is_video_note(),
            "raw_note_text": raw_note_text,
            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def check_page_state(
        self,
        manual_confirm: bool = False,
        debug_filename: str = "debug_page_before_scroll.html",
    ) -> bool:
        """检查当前页面是否可能仍停留在登录/验证页。"""
        self._save_debug_page(debug_filename)
        page = self.driver.page_source
        has_comment_area = self._has_comment_area()
        has_block_keywords = page_contains_block_keywords(page)

        self.last_run_info.update(
            {
                "blocked_page": has_block_keywords,
                "has_comment_area": has_comment_area,
                "debug_file": debug_filename,
            }
        )

        if has_block_keywords and has_comment_area:
            print("页面源码中含有登录/验证码相关词，但已检测到评论区域，继续尝试解析。")
            return True

        if has_block_keywords and not has_comment_area:
            print("\n警告：页面源码里检测到登录/验证码相关内容，且当前未发现评论区。")
            print(f"已保存 {debug_filename}，请先在浏览器中手动处理登录或验证。")

            if not manual_confirm:
                self.wait_user_login()
                self._save_debug_page(debug_filename)
                page = self.driver.page_source
                has_comment_area = self._has_comment_area()
                has_block_keywords = page_contains_block_keywords(page)
                self.last_run_info.update(
                    {
                        "blocked_page": has_block_keywords,
                        "has_comment_area": has_comment_area,
                    }
                )

                if not has_block_keywords or has_comment_area:
                    return True

            choice = input("输入 y 继续尝试采集，其他键退出当前页面：").strip().lower()
            if choice != "y":
                self.last_fail_reason = "页面仍处于登录/验证状态"
                self.last_status = "failed"
            return choice == "y"

        return True

    def find_comment_container(self):
        """尝试寻找评论容器。"""
        for selector in COMMENT_CONTAINER_SELECTORS:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                visible = [element for element in elements if element.is_displayed()]
                if visible:
                    print(f"找到可能的评论容器：{selector}")
                    return visible[0]
            except Exception:
                continue

        print("未找到明确评论容器，将滚动整个页面。")
        return None

    def get_comment_selector(self) -> str:
        """统一评论选择器。"""
        return ", ".join(COMMENT_SELECTORS)

    def _count_comment_elements(self) -> int:
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, self.get_comment_selector())
        except Exception:
            return 0

        count = 0
        for element in elements:
            try:
                text = self._normalize_text(element.text)
                if text:
                    count += 1
            except Exception:
                continue
        return count

    def click_more_buttons(self, max_clicks: int = 8):
        """谨慎展开回复，不点击页面无关的“更多”控件。"""
        clicked = 0
        seen = set()

        candidates = []
        xpath = (
            "//*[self::button or self::a or self::div or self::span]"
            "[contains(normalize-space(.), '展开回复')"
            " or contains(normalize-space(.), '更多回复')"
            " or contains(normalize-space(.), '查看回复')"
            " or contains(normalize-space(.), '全部回复')]"
        )

        try:
            candidates.extend(self.driver.find_elements(By.XPATH, xpath))
        except Exception:
            pass

        for selector in ['.show-more', '[class*="show-more"]', '[class*="reply-container"] .show-more']:
            try:
                candidates.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue

        for element in candidates:
            if clicked >= max_clicks:
                break

            try:
                text = self._normalize_text(element.text)
                if not text or not any(keyword in text for keyword in MORE_REPLY_TEXTS):
                    continue
                if not element.is_displayed() or not element.is_enabled():
                    continue

                key = (element.id, text)
                if key in seen:
                    continue
                seen.add(key)

                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                time.sleep(0.4)
                self.driver.execute_script("arguments[0].click();", element)
                clicked += 1
                print(f"展开回复：{text}")
                time.sleep(1)
            except Exception:
                continue

        if clicked:
            print(f"本轮展开回复按钮 {clicked} 个。")

    def scroll_comments(self, max_comments: int = 100, scroll_pause: int = 3, max_scroll_attempts: int = 30):
        """滚动加载评论。"""
        print("\n开始滚动加载评论...")

        comment_container = self.find_comment_container()
        no_change_count = 0
        last_count = 0
        attempts_used = 0

        for attempt in range(1, max_scroll_attempts + 1):
            current_count = self._count_comment_elements()
            print(f"第 {attempt} 次滚动，当前疑似评论元素数：{current_count}")

            if current_count >= max_comments:
                print(f"已达到目标评论数：{current_count}")
                attempts_used = attempt
                break

            self.click_more_buttons(max_clicks=4)

            try:
                if comment_container:
                    self.driver.execute_script(
                        "arguments[0].scrollTop = arguments[0].scrollTop + Math.max(arguments[0].clientHeight, 600);",
                        comment_container,
                    )
                else:
                    self.driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 700));")
            except Exception:
                self.driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 700));")

            time.sleep(scroll_pause)

            new_count = self._count_comment_elements()
            if new_count <= last_count:
                no_change_count += 1
            else:
                no_change_count = 0
            last_count = max(last_count, new_count)
            attempts_used = attempt

            if new_count >= max_comments:
                print(f"滚动后已达到目标评论数：{new_count}")
                break

            if no_change_count >= 4:
                print("评论数量连续多次未增加，停止滚动。")
                break

        print(f"滚动完成，共滚动 {attempts_used} 次。")

    def scrape_comments(
        self,
        note_url,
        max_comments=100,
        scroll_pause=3,
        wait_for_login=30,
        manual_confirm=None,
    ):
        """
        爬取评论。
        :param note_url: 小红书笔记 URL
        :param max_comments: 最大爬取评论数
        :param scroll_pause: 滚动间隔
        :param wait_for_login: 为 0 时跳过固定人工确认，仅在检测到登录/验证时暂停
        :param manual_confirm: True 时主动等待用户确认，False 时仅在检测到登录/验证码且无评论区时暂停
        """
        if not self.driver:
            self.setup_driver()

        self._initialize_run_state(note_url)

        print(f"\n正在访问：{note_url}")
        self.driver.get(note_url)
        time.sleep(5)

        return self._scrape_loaded_page(
            note_url=note_url,
            max_comments=max_comments,
            scroll_pause=scroll_pause,
            wait_for_login=wait_for_login,
            manual_confirm=manual_confirm,
        )

    def scrape_current_page_comments(
        self,
        note_url: str = "",
        max_comments: int = 30,
        scroll_pause: int = 2,
        wait_for_login: int = 0,
        manual_confirm: bool | None = False,
    ):
        """
        在当前已经打开的笔记详情页上直接采集评论，不重新打开 URL。
        """
        if not self.driver:
            raise RuntimeError("当前没有可用浏览器页面，无法直接解析当前笔记。")

        self._initialize_run_state(note_url)
        return self._scrape_loaded_page(
            note_url=note_url,
            max_comments=max_comments,
            scroll_pause=scroll_pause,
            wait_for_login=wait_for_login,
            manual_confirm=manual_confirm,
        )

    def _parse_comments(self, note_url: str = "", max_comments: int = 100):
        """解析评论数据。"""
        self.comments = []

        try:
            self._save_debug_page("debug_page.html")
            print("页面源码已保存到 debug_page.html")

            unique_elements = {}
            for selector in COMMENT_SELECTORS:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    elements = []
                print(f"选择器 {selector} 找到 {len(elements)} 个元素")

                for element in elements:
                    unique_elements.setdefault(element.id, element)

            comment_elements = list(unique_elements.values())
            print(f"合并去重后，共有 {len(comment_elements)} 个疑似评论元素。")

            if not comment_elements:
                print("\n警告：未找到评论元素。")
                print("请检查 debug_page.html 是否仍是登录页、验证码页或页面结构已变化。")
                return

            seen_keys = set()

            for index, element in enumerate(comment_elements, 1):
                if len(self.comments) >= max_comments:
                    break

                try:
                    raw_display_text = (element.text or "").strip()
                    raw_text = self._normalize_text(raw_display_text)
                    if not raw_text:
                        continue

                    username = self._extract_first_text(
                        element,
                        [
                            'a[class*="name"]',
                            '[class*="author"] a',
                            '[class*="name"]',
                            '[class*="nickname"]',
                            '[class*="user"]',
                        ],
                    )
                    publish_time = self._extract_first_text(
                        element,
                        [
                            '[class*="time"]',
                            '[class*="date"] span',
                            '[class*="date"]',
                            '[class*="Time"]',
                        ],
                    )
                    likes = self._extract_first_text(
                        element,
                        [
                            '[class*="like"] [class*="count"]',
                            '[class*="like-wrapper"] [class*="count"]',
                            '[class*="Like"] [class*="count"]',
                            '[class*="interactions"] [class*="like"] [class*="count"]',
                        ],
                    )
                    content = self._extract_comment_content(
                        element,
                        raw_display_text,
                        raw_text,
                        username,
                        publish_time,
                        likes,
                    )

                    if not content:
                        continue
                    if self._is_ui_only_comment(content, raw_display_text, raw_text):
                        continue

                    if username and publish_time:
                        dedupe_key = (content, publish_time, username)
                    else:
                        dedupe_key = ("content-only", content)

                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)

                    comment_data = {
                        "序号": len(self.comments) + 1,
                        "笔记链接": note_url,
                        "用户名": username,
                        "评论内容": content,
                        "原始文本": raw_display_text or raw_text,
                        "发布时间": publish_time,
                        "点赞数": likes,
                        "爬取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    self.comments.append(comment_data)
                except Exception as error:
                    print(f"解析第 {index} 个元素时出错：{error}")
                    continue

            print(f"成功解析 {len(self.comments)} 条评论。")
        except Exception as error:
            print(f"解析评论失败：{error}")
            import traceback

            traceback.print_exc()

    def _extract_comment_content(
        self,
        element,
        raw_display_text: str,
        raw_text: str,
        username: str,
        publish_time: str,
        likes: str,
    ) -> str:
        candidates = self._extract_text_candidates(
            element,
            COMMENT_TEXT_SELECTORS,
        )

        filtered_candidates = []
        for candidate in candidates:
            cleaned_candidate = self._clean_comment_text(candidate, username, publish_time, likes)
            if not cleaned_candidate:
                continue
            filtered_candidates.append(cleaned_candidate)

        if filtered_candidates:
            filtered_candidates.sort(key=len, reverse=True)
            return filtered_candidates[0]

        raw_line_count = len([line for line in (raw_display_text or raw_text).splitlines() if line.strip()])
        if raw_line_count <= 6:
            return self._clean_comment_text(raw_display_text or raw_text, username, publish_time, likes)
        return ""

    def _clean_comment_text(
        self,
        text: str,
        username: str,
        publish_time: str,
        likes: str,
    ) -> str:
        lines = [self._normalize_text(line) for line in (text or "").splitlines() if self._normalize_text(line)]
        filtered_lines = []

        for line in lines:
            if line in {username, publish_time, likes}:
                continue
            if line in UI_ONLY_PHRASES:
                continue
            if line in COMMENT_NOISE_PHRASES:
                continue
            if line.startswith("回复") and len(line) <= 8:
                continue
            if line.startswith("展开") and "回复" in line:
                continue
            if re.fullmatch(r"\d+", line):
                continue
            if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}.*", line):
                continue
            if re.fullmatch(r"\d{1,2}[-/]\d{1,2}.*", line):
                continue
            if re.fullmatch(r"(赞|回复|收藏|分享)", line):
                continue
            filtered_lines.append(line)

        if not filtered_lines:
            return ""

        merged = self._normalize_text(" ".join(filtered_lines[:3]).strip())
        if merged in COMMENT_NOISE_PHRASES:
            return ""
        if "这是一片荒地" in merged or "点击评论" in merged:
            return ""
        return merged

    def _extract_text_candidates(self, element, selectors: Iterable[str]) -> List[str]:
        results = []
        seen = set()

        for selector in selectors:
            try:
                found = element.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue

            for sub_element in found:
                try:
                    text = self._normalize_text(sub_element.text)
                except Exception:
                    continue
                if not text or text in seen:
                    continue
                seen.add(text)
                results.append(text)

        return results

    def _extract_first_text(self, element, selectors: Iterable[str]) -> str:
        candidates = self._extract_text_candidates(element, selectors)
        return candidates[0] if candidates else ""

    def _is_ui_only_comment(self, content: str, raw_display_text: str, raw_text: str) -> bool:
        if content in UI_ONLY_PHRASES:
            return True
        if content in COMMENT_NOISE_PHRASES:
            return True
        if "这是一片荒地" in content or "点击评论" in content:
            return True

        if page_contains_block_keywords(raw_text) and len(content) <= 8:
            return True

        stripped_lines = [line.strip() for line in raw_display_text.splitlines() if line.strip()]
        if stripped_lines and all(line in UI_ONLY_PHRASES or re.fullmatch(r"\d+", line) for line in stripped_lines):
            return True

        return False

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\u200b", " ")
        normalized = normalized.replace("\xa0", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def save_to_csv(self, filename="xhs_comments.csv"):
        """保存为 CSV 文件。"""
        if not self.comments:
            print("没有数据可保存。")
            return

        fieldnames = [
            "序号",
            "笔记链接",
            "用户名",
            "评论内容",
            "原始文本",
            "发布时间",
            "点赞数",
            "爬取时间",
        ]

        with open(filename, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.comments)

        print(f"数据已保存到：{filename}")

    def close(self):
        """关闭浏览器。"""
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None


def main():
    print("=" * 50)
    print("小红书评论爬虫工具")
    print("=" * 50)

    note_url = input("\n请输入小红书笔记链接：").strip()
    max_comments = input("最大爬取评论数，默认50：").strip()
    max_comments = int(max_comments) if max_comments.isdigit() else 50

    scraper = XHSCommentScraper(headless=False)

    try:
        comments = scraper.scrape_comments(
            note_url,
            max_comments=max_comments,
            scroll_pause=3,
        )

        if comments:
            filename = f"xhs_comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            scraper.save_to_csv(filename)
            print(f"\n爬取完成！共获取 {len(comments)} 条评论")
        else:
            print("\n未获取到评论数据。")

    except Exception as error:
        print(f"\n爬取过程中出错：{error}")
        import traceback

        traceback.print_exc()
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
