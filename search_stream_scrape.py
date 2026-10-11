#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从搜索页直接点击笔记详情页，并即时采集笔记正文、评论与总结。
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

import yaml
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from browser_utils import (
    BASE_URL,
    canonicalize_explore_url,
    has_comment_area,
    is_available_note_page,
    is_unavailable_note_page,
    page_contains_block_keywords,
)
from crawl_guard import CrawlGuard
from db import (
    get_note_by_clean_url,
    init_db,
    mark_note_crawled,
    mark_note_failed,
    mark_note_blocked,
    mark_note_no_comments,
    mark_note_unavailable,
    save_comments as save_db_comments,
    save_summary,
    sync_keywords,
    upsert_note,
)
from notifier import ensure_directory, write_stream_run_report
from summarizer import build_summarizer
from xhs_scraper import XHSCommentScraper

CONFIG_PATH = Path("config.yaml")
DEFAULT_SUCCESS_TARGET = 10
DEFAULT_MAX_CANDIDATES = 30
DEFAULT_MAX_COMMENTS = 30
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_NOTE_WAIT_SECONDS = 4
DEFAULT_SCROLL_PAUSE = 2
DEFAULT_FAST_SKIP_SECONDS = 2
GUARD_STATUSES = {"blocked", "login_required", "captcha_required", "rate_limited", "skipped_robots"}
SKIP_DB_STATUSES = {
    "crawled",
    "unavailable",
    "failed",
    "no_comments",
    "skipped_non_note",
    "skipped_author_profile",
    *GUARD_STATUSES,
}
CLICK_AVOID_KEYWORDS = {
    "author",
    "avatar",
    "follow",
    "nickname",
    "profile",
    "user-name",
    "关注",
}
PREFERRED_CLICK_SELECTORS = [
    '[class*="cover"]',
    '[class*="image"]',
    "img",
    '[class*="title"]',
    "h1",
    "h2",
    "h3",
    '[class*="desc"]',
    '[class*="content"]',
]
NON_NOTE_TITLE_KEYWORDS = {
    "大家都在搜",
    "相关搜索",
    "推荐搜索",
    "搜索发现",
    "热门搜索",
    "搜索词建议",
}
NOTE_TITLE_HINT_KEYWORDS = {
    "攻略",
    "合集",
    "附地址",
    "好吃",
    "记录",
    "吃喝",
    "美食",
    "探店",
    "推荐",
    "必吃",
    "清单",
    "小时",
    "测评",
    "总结",
    "打卡",
    "收藏",
}
DATE_PATTERN = re.compile(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2})")
TITLE_PUNCTUATION = "！？?!（）()【】[]…&|｜：:"
TEST_MODE_DEFAULTS = {
    "success_target": 3,
    "max_candidates": 15,
    "max_comments_per_note": 10,
    "sleep_between_notes_seconds": 5,
    "fast_skip_sleep_seconds": 1,
    "write_db": False,
    "generate_summary": False,
}
PROD_MODE_DEFAULTS = {
    "success_target": 20,
    "max_candidates": 80,
    "max_comments_per_note": 50,
    "sleep_between_notes_seconds": 60,
    "fast_skip_sleep_seconds": 2,
    "write_db": True,
    "generate_summary": True,
}

COMMENT_CSV_FIELDNAMES = [
    "序号",
    "关键词",
    "搜索页标题",
    "笔记标题",
    "笔记链接",
    "原始访问链接",
    "用户名",
    "评论内容",
    "原始文本",
    "发布时间",
    "点赞数",
    "爬取时间",
]
NOTE_CSV_FIELDNAMES = [
    "序号",
    "关键词",
    "搜索页标题",
    "笔记标题",
    "笔记正文",
    "作者名",
    "发布时间",
    "点赞数",
    "收藏数",
    "评论数展示值",
    "分享数",
    "图片数量",
    "是否视频",
    "笔记链接",
    "原始访问链接",
    "页面状态",
    "失败原因",
    "采集时间",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="多平台内容流式采集（v3 兼容入口，默认走 XHS 适配器）"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--test", action="store_true", help="使用测试模式默认参数")
    mode_group.add_argument("--prod", action="store_true", help="使用正式模式默认参数")
    return parser.parse_args()


def load_config() -> Dict[str, object]:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def prompt_int(prompt: str, default: int) -> int:
    value = input(prompt).strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def prompt_bool(prompt: str, default: bool = True) -> bool:
    value = input(prompt).strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def resolve_stream_defaults(stream_config: Dict[str, object], args: argparse.Namespace) -> Dict[str, object]:
    defaults = {
        "success_target": int(stream_config.get("success_target", DEFAULT_SUCCESS_TARGET)),
        "max_candidates": int(stream_config.get("max_candidates", DEFAULT_MAX_CANDIDATES)),
        "max_comments_per_note": int(stream_config.get("max_comments_per_note", DEFAULT_MAX_COMMENTS)),
        "sleep_between_notes_seconds": int(stream_config.get("sleep_between_notes_seconds", DEFAULT_INTERVAL_SECONDS)),
        "fast_skip_sleep_seconds": int(stream_config.get("fast_skip_sleep_seconds", DEFAULT_FAST_SKIP_SECONDS)),
        "write_db": bool(stream_config.get("write_db", True)),
        "generate_summary": bool(stream_config.get("generate_summary", True)),
    }
    if args.test:
        defaults.update(TEST_MODE_DEFAULTS)
    elif args.prod:
        defaults.update(PROD_MODE_DEFAULTS)
    return defaults


def wait_before_next(interval_seconds: int, *, fast_skip: bool = False, fast_skip_seconds: int = DEFAULT_FAST_SKIP_SECONDS):
    wait_seconds = fast_skip_seconds if fast_skip else interval_seconds
    wait_seconds = max(1, min(3, wait_seconds)) if fast_skip else max(1, interval_seconds)
    print(f"等待 {wait_seconds} 秒后继续下一篇...")
    time.sleep(wait_seconds)


def build_search_url(keyword: str) -> str:
    return f"{BASE_URL}/search_result?keyword={quote(keyword)}"


def save_page_source(driver, filename: str):
    try:
        Path(filename).write_text(driver.page_source or "", encoding="utf-8")
    except Exception:
        pass


def choose_keyword(config: Dict[str, object]) -> str:
    keywords = config.get("keywords") or []
    discovery = config.get("discovery") or {}
    seed_keywords = discovery.get("seed_keywords") or []
    default_keyword = ""
    if keywords:
        first = keywords[0]
        if isinstance(first, str):
            default_keyword = first.strip()
        elif isinstance(first, dict):
            default_keyword = str(first.get("keyword") or first.get("name") or "").strip()
    if not default_keyword and seed_keywords:
        first_seed = seed_keywords[0]
        if isinstance(first_seed, str):
            default_keyword = first_seed.strip()

    prompt = "请输入关键词"
    if default_keyword:
        prompt += f"（默认 {default_keyword}）"
    prompt += "："

    value = input(prompt).strip()
    return value or default_keyword


def wait_search_ready(driver, debug_filename: str = "debug_search_page.html"):
    save_page_source(driver, debug_filename)
    print("\n请在浏览器中确认以下事项：")
    print("1. 已经登录小红书")
    print("2. 当前没有验证码或安全验证")
    print("3. 搜索结果已经显示")
    input("确认无误后，回到终端按 Enter 开始逐条处理搜索结果：")
    time.sleep(2)
    save_page_source(driver, debug_filename)

    page_source = driver.page_source or ""
    if page_contains_block_keywords(page_source):
        print(f"检测到登录/验证码相关内容，已保存 {debug_filename}。")
        input("请先在浏览器中手动完成登录/验证，确认搜索结果出现后按 Enter 继续：")
        time.sleep(2)
        save_page_source(driver, debug_filename)
        if page_contains_block_keywords(driver.page_source or ""):
            choice = input("页面源码中仍检测到登录/验证码相关内容。输入 y 继续尝试，其他键退出：").strip().lower()
            if choice != "y":
                raise RuntimeError("搜索页仍处于登录或验证状态，已取消本次流式采集。")


def extract_card_title(element) -> str:
    candidates = []
    try:
        candidates.append(element.text or "")
    except Exception:
        pass

    for attribute in ("title", "aria-label"):
        try:
            candidates.append(element.get_attribute(attribute) or "")
        except Exception:
            continue

    try:
        parent = element.find_element(By.XPATH, "./ancestor-or-self::*[self::section or self::article or self::div][1]")
        candidates.append(parent.text or "")
    except Exception:
        pass

    lines = []
    seen = set()
    for candidate in candidates:
        for raw_line in str(candidate or "").splitlines():
            line = normalize_text(raw_line)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)

    if not lines:
        return ""

    scored = sorted(lines, key=score_note_title_line, reverse=True)
    best = scored[0]
    if score_note_title_line(best) > 0:
        return best

    for line in lines:
        if not looks_like_author_date_text(line):
            return line
    return lines[0]


def score_note_title_line(line: str) -> int:
    text = normalize_text(line)
    if not text:
        return -10
    if any(keyword in text for keyword in NON_NOTE_TITLE_KEYWORDS):
        return -10

    score = 0
    if any(keyword in text for keyword in NOTE_TITLE_HINT_KEYWORDS):
        score += 4
    if any(mark in text for mark in TITLE_PUNCTUATION):
        score += 3
    if len(text) >= 8:
        score += 2
    if len(text) >= 14:
        score += 1
    if looks_like_author_date_text(text):
        score -= 5
    if re.fullmatch(r"[0-9\.万千wW赞收藏评论分享\s]+", text):
        score -= 4
    return score


def looks_like_author_date_text(text: str) -> bool:
    value = normalize_text(text)
    if not value:
        return True
    if any(keyword in value for keyword in NOTE_TITLE_HINT_KEYWORDS):
        return False
    if len(value) < 8 and DATE_PATTERN.search(value):
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff🥜✨🍜🍲]+(?:\s+\d{1,2}[-/.]\d{1,2})?", value):
        return DATE_PATTERN.search(value) is not None
    if re.fullmatch(r"[0-9\.万千wW赞收藏评论分享\s]+", value):
        return True
    return False


def looks_like_note_card_text(text: str) -> bool:
    value = normalize_text(text)
    if not value:
        return False
    if any(keyword in value for keyword in NON_NOTE_TITLE_KEYWORDS):
        return False
    if looks_like_author_date_text(value):
        return False
    if any(keyword in value for keyword in NOTE_TITLE_HINT_KEYWORDS):
        return True
    if any(mark in value for mark in TITLE_PUNCTUATION):
        return True
    return len(value) >= 8


def extract_href_from_candidate(element) -> str:
    try:
        href = (element.get_attribute("href") or "").strip()
    except Exception:
        href = ""
    if "/explore/" in href:
        return href

    try:
        anchor = element.find_element(By.CSS_SELECTOR, 'a[href*="/explore/"]')
        href = (anchor.get_attribute("href") or "").strip()
    except Exception:
        href = ""
    if "/explore/" in href:
        return href

    try:
        anchors = element.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = (anchor.get_attribute("href") or "").strip()
        except Exception:
            continue
        if "/explore/" in href:
            return href

    return ""


def resolve_card_root(element):
    try:
        return element.find_element(By.XPATH, "./ancestor-or-self::section[contains(@class, 'note-item')][1]")
    except Exception:
        pass

    try:
        return element.find_element(By.XPATH, "./ancestor-or-self::*[contains(@class, 'note-item')][1]")
    except Exception:
        pass

    return element


def get_search_result_cards(driver) -> List[Dict[str, object]]:
    cards: List[Dict[str, object]] = []
    seen = set()
    primary_selectors = [
        'section.note-item a[href*="/explore/"]',
        '.note-item a[href*="/explore/"]',
        'a[href*="/explore/"]',
        '[href*="/explore/"]',
        '[class*="feeds-page"] a',
    ]
    fallback_selectors = [
        "section.note-item",
        '[class*="note-item"]',
        '[class*="cover"]',
        '[class*="search-result"]',
        "section",
    ]

    def add_card(click_target, href: str = "", preferred_type: str = "", candidate_root=None):
        try:
            if not click_target.is_displayed():
                return
        except Exception:
            return

        href_value = (href or "").strip() or extract_href_from_candidate(click_target)
        full_text = ""
        try:
            full_text = normalize_text(click_target.text)
        except Exception:
            full_text = ""
        title_text = extract_card_title(click_target)
        clean_url = canonicalize_explore_url(href_value) or ""
        candidate_type, skip_reason = classify_candidate(
            title_text=title_text,
            full_text=full_text,
            href_value=href_value,
            preferred_type=preferred_type,
        )
        signature = clean_url or href_value or title_text or full_text or f"element-{len(cards)}"
        if signature in seen:
            return
        seen.add(signature)
        cards.append(
            {
                "click_target": click_target,
                "candidate_root": candidate_root or click_target,
                "href": href_value,
                "clean_url_hint": clean_url,
                "title_text": title_text,
                "full_text": full_text,
                "candidate_type": candidate_type,
                "skip_reason": skip_reason,
                "signature": signature,
            }
        )

    for selector in primary_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                href = extract_href_from_candidate(element)
            except Exception:
                href = ""
            add_card(element, href=href, preferred_type="href_card", candidate_root=resolve_card_root(element))

    for selector in fallback_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
            except Exception:
                continue
            try:
                anchor = element.find_element(By.CSS_SELECTOR, 'a[href*="/explore/"], [href*="/explore/"]')
                href = extract_href_from_candidate(anchor)
                click_target = anchor if anchor.is_displayed() else element
            except Exception:
                href = extract_href_from_candidate(element)
                click_target = element
            add_card(
                click_target,
                href=href,
                preferred_type="clickable_card" if not href else "href_card",
                candidate_root=element,
            )

    return cards


def classify_candidate(
    *,
    title_text: str,
    full_text: str,
    href_value: str,
    preferred_type: str = "",
) -> tuple[str, str]:
    title_value = normalize_text(title_text)
    full_value = normalize_text(full_text)
    reference_text = title_value or full_value

    if href_value and "/explore/" in href_value:
        return "href_card", ""

    if not reference_text:
        return "skipped_non_note", "empty_text"

    if any(keyword in reference_text for keyword in NON_NOTE_TITLE_KEYWORDS):
        return "skipped_non_note", "search_module_or_non_note"

    if looks_like_author_date_text(reference_text) and not looks_like_note_card_text(reference_text):
        return "skipped_non_note", "author_date_only"

    if looks_like_note_card_text(reference_text):
        return "clickable_card", "clickable_without_href"

    if preferred_type == "clickable_card" and full_value and looks_like_note_card_text(full_value):
        return "clickable_card", "clickable_without_href"

    return "skipped_non_note", "href_missing_or_invalid"


def get_note_title(driver, fallback: str = "") -> str:
    selectors = [
        "#detail-title",
        '[id="detail-title"]',
        "h1",
        '[class*="title"]',
        '[class*="note-title"]',
        '[class*="desc"]',
    ]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                text = normalize_text(element.text)
                if text:
                    return text
            except Exception:
                continue
    return fallback


def is_unwanted_click_target(element) -> bool:
    try:
        href = (element.get_attribute("href") or "").strip().lower()
    except Exception:
        href = ""
    try:
        class_name = (element.get_attribute("class") or "").lower()
    except Exception:
        class_name = ""
    try:
        aria_label = normalize_text(element.get_attribute("aria-label") or "").lower()
    except Exception:
        aria_label = ""
    try:
        title_value = normalize_text(element.get_attribute("title") or "").lower()
    except Exception:
        title_value = ""
    try:
        text = normalize_text(element.text or "")
    except Exception:
        text = ""

    combined = " ".join(part for part in (href, class_name, aria_label, title_value) if part)
    if "/user/profile/" in href:
        return True
    if any(keyword in combined for keyword in CLICK_AVOID_KEYWORDS):
        return True
    if "关注" in text:
        return True
    return False


def choose_click_target(card: Dict[str, object]):
    base_target = card.get("click_target")
    candidate_root = card.get("candidate_root") or base_target

    for candidate in (base_target, candidate_root):
        if candidate is None:
            continue
        try:
            if candidate.is_displayed() and not is_unwanted_click_target(candidate):
                href = (candidate.get_attribute("href") or "").strip()
                if "/explore/" in href or candidate == base_target:
                    return candidate
        except Exception:
            continue

    search_roots = [candidate_root, base_target]
    seen_ids = set()
    for root in search_roots:
        if root is None:
            continue
        root_id = id(root)
        if root_id in seen_ids:
            continue
        seen_ids.add(root_id)

        try:
            explore_links = root.find_elements(By.CSS_SELECTOR, 'a[href*="/explore/"]')
        except Exception:
            explore_links = []

        for link in explore_links:
            try:
                if link.is_displayed() and not is_unwanted_click_target(link):
                    return link
            except Exception:
                continue

        for selector in PREFERRED_CLICK_SELECTORS:
            try:
                elements = root.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                except Exception:
                    continue
                if is_unwanted_click_target(element):
                    continue
                return element

    return base_target or candidate_root


def open_card_from_search(driver, card: Dict[str, object], wait_seconds: int = DEFAULT_NOTE_WAIT_SECONDS) -> Dict[str, object]:
    click_target = choose_click_target(card)
    original_window = driver.current_window_handle
    original_handles = list(driver.window_handles)
    navigation_mode = "same_tab"
    before_url = (driver.current_url or "").strip()

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", click_target)
        time.sleep(0.8)
    except Exception:
        pass

    try:
        ActionChains(driver).key_down(Keys.CONTROL).click(click_target).key_up(Keys.CONTROL).perform()
        time.sleep(1.5)
        new_handles = [handle for handle in driver.window_handles if handle not in original_handles]
        if new_handles:
            navigation_mode = "new_tab"
            driver.switch_to.window(new_handles[-1])
        else:
            after_ctrl_click_url = (driver.current_url or "").strip()
            if after_ctrl_click_url == before_url:
                try:
                    driver.execute_script("arguments[0].click();", click_target)
                except Exception:
                    click_target.click()
    except Exception as error:
        return {
            "ok": False,
            "navigation_mode": navigation_mode,
            "reason": f"click_failed: {error}",
            "original_window": original_window,
        }

    time.sleep(wait_seconds)
    return {
        "ok": True,
        "navigation_mode": navigation_mode,
        "original_window": original_window,
        "current_url": (driver.current_url or "").strip(),
    }


def return_to_search_page(driver, search_url: str, navigation_mode: str, original_window: str):
    try:
        if navigation_mode == "new_tab":
            current_handle = driver.current_window_handle
            if current_handle != original_window:
                driver.close()
            driver.switch_to.window(original_window)
            time.sleep(2)
        else:
            driver.back()
            time.sleep(3)
            current_url = (driver.current_url or "").strip()
            if "/search_result" not in current_url:
                driver.get(search_url)
                time.sleep(4)
    except Exception:
        driver.get(search_url)
        time.sleep(4)


def should_skip_by_db(clean_url: str, db_path: str) -> bool:
    note = get_note_by_clean_url(clean_url, db_path)
    return bool(note and note.get("status") in SKIP_DB_STATUSES)


def append_rows(csv_path: Path, fieldnames: List[str], rows: List[Dict[str, object]]):
    if not rows:
        return
    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def append_failure_row(log_path: Path, url: str, status: str, reason: str):
    with open(log_path, "a", encoding="utf-8") as file:
        file.write(f"{url}\t{status}\t{reason}\n")


def build_comment_csv_rows(
    *,
    start_index: int,
    keyword: str,
    search_title: str,
    note_title: str,
    clean_url: str,
    raw_url: str,
    comments: List[Dict[str, str]],
) -> List[Dict[str, object]]:
    rows = []
    for index, comment in enumerate(comments, 1):
        rows.append(
            {
                "序号": start_index + index - 1,
                "关键词": keyword,
                "搜索页标题": search_title,
                "笔记标题": note_title,
                "笔记链接": clean_url or raw_url,
                "原始访问链接": raw_url or clean_url,
                "用户名": comment.get("用户名", ""),
                "评论内容": comment.get("评论内容", ""),
                "原始文本": comment.get("原始文本", ""),
                "发布时间": comment.get("发布时间", ""),
                "点赞数": comment.get("点赞数", ""),
                "爬取时间": comment.get("爬取时间", ""),
            }
        )
    return rows


def build_note_csv_row(index: int, note_data: Dict[str, object], status: str, reason: str) -> Dict[str, object]:
    return {
        "序号": index,
        "关键词": note_data.get("keyword") or "",
        "搜索页标题": note_data.get("search_title") or "",
        "笔记标题": note_data.get("note_title") or note_data.get("search_title") or "",
        "笔记正文": note_data.get("note_content") or "",
        "作者名": note_data.get("author_name") or "",
        "发布时间": note_data.get("publish_time") or "",
        "点赞数": note_data.get("like_count") or "",
        "收藏数": note_data.get("collect_count") or "",
        "评论数展示值": note_data.get("comment_count_display") or "",
        "分享数": note_data.get("share_count") or "",
        "图片数量": note_data.get("image_count") or 0,
        "是否视频": "是" if note_data.get("is_video") else "否",
        "笔记链接": note_data.get("clean_url") or note_data.get("raw_url") or "",
        "原始访问链接": note_data.get("raw_url") or note_data.get("clean_url") or "",
        "页面状态": status,
        "失败原因": reason,
        "采集时间": note_data.get("crawled_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def persist_note_result(
    *,
    db_enabled: bool,
    db_path: str,
    note_data: Dict[str, object],
    status: str,
    reason: str,
    comments: List[Dict[str, str]],
    generate_summary: bool,
    summarizer,
):
    note_record = None
    clean_url = str(note_data.get("clean_url") or "").strip()
    raw_url = str(note_data.get("raw_url") or clean_url).strip()
    note_payload = dict(note_data)
    note_payload["status"] = status
    note_payload["fail_reason"] = reason

    if db_enabled and clean_url:
        upsert_note(
            keyword=str(note_payload.get("keyword") or ""),
            clean_url=clean_url,
            raw_url=raw_url,
            search_title=str(note_payload.get("search_title") or ""),
            note_title=str(note_payload.get("note_title") or note_payload.get("title") or ""),
            note_content=str(note_payload.get("note_content") or ""),
            raw_note_text=str(note_payload.get("raw_note_text") or ""),
            author_name=str(note_payload.get("author_name") or ""),
            publish_time=str(note_payload.get("publish_time") or ""),
            like_count=str(note_payload.get("like_count") or ""),
            collect_count=str(note_payload.get("collect_count") or ""),
            comment_count_display=str(note_payload.get("comment_count_display") or ""),
            share_count=str(note_payload.get("share_count") or ""),
            image_count=int(note_payload.get("image_count") or 0),
            is_video=bool(note_payload.get("is_video")),
            status=status,
            fail_reason=reason,
            last_error=reason,
            blocked_reason=reason if status in GUARD_STATUSES else "",
            db_path=db_path,
        )
        note_record = get_note_by_clean_url(clean_url, db_path)

    if db_enabled and note_record:
        note_id = int(note_record["id"])
        if comments:
            save_db_comments(note_id, clean_url or raw_url, comments, db_path)
            mark_note_crawled(note_id, db_path)
        elif status == "unavailable":
            save_db_comments(note_id, clean_url or raw_url, [], db_path)
            mark_note_unavailable(note_id, reason, db_path)
        elif status == "no_comments":
            save_db_comments(note_id, clean_url or raw_url, [], db_path)
            mark_note_no_comments(note_id, reason, db_path)
        elif status == "failed":
            mark_note_failed(note_id, reason, db_path)
        elif status in GUARD_STATUSES:
            mark_note_blocked(note_id, status=status, reason=reason, db_path=db_path)

    summary_note = dict(note_payload)
    if note_record:
        summary_note["id"] = note_record["id"]

    summary = {}
    if generate_summary and status not in {"failed", *GUARD_STATUSES}:
        if status == "unavailable":
            summary = summarizer.summarize_unavailable(summary_note, reason)
        elif status == "no_comments":
            summary = summarizer.summarize_no_comments(summary_note, reason)
        else:
            summary = summarizer.summarize_note_and_comments(summary_note, comments, use_ai=summarizer.ai_enabled)

    if db_enabled and note_record and summary:
        save_summary(summary_note, summary, db_path)

    return {
        "note": summary_note,
        "summary": summary,
        "status": status,
        "reason": reason,
        "comment_count": len(comments),
    }


def main():
    args = parse_args()
    print("兼容入口提示：推荐逐步迁移到 `python main.py crawl --platform xiaohongshu`。")
    config = load_config()
    stream_config = config.get("stream") or {}
    crawler_config = config.get("crawler") or {}
    output_config = config.get("output") or {}
    safety_config = config.get("safety") or {}
    ai_config = config.get("ai") or {"enabled": False}
    stream_defaults = resolve_stream_defaults(stream_config, args)

    keyword = choose_keyword(config)
    if not keyword:
        print("错误：关键词不能为空。")
        return

    success_target = prompt_int(
        f"目标成功采集笔记数（默认 {int(stream_defaults['success_target'])}）：",
        int(stream_defaults["success_target"]),
    )
    max_candidates = prompt_int(
        f"最大扫描候选数（默认 {int(stream_defaults['max_candidates'])}）：",
        int(stream_defaults["max_candidates"]),
    )
    max_comments = prompt_int(
        f"每篇最多评论数（默认 {int(stream_defaults['max_comments_per_note'])}）：",
        int(stream_defaults["max_comments_per_note"]),
    )
    interval_seconds = prompt_int(
        f"每篇之间间隔秒数（默认 {int(stream_defaults['sleep_between_notes_seconds'])}）：",
        int(stream_defaults["sleep_between_notes_seconds"]),
    )
    fast_skip_seconds = int(stream_defaults["fast_skip_sleep_seconds"])
    write_db = prompt_bool(
        f"是否写入数据库？默认 {'y' if bool(stream_defaults['write_db']) else 'n'}：",
        default=bool(stream_defaults["write_db"]),
    )
    generate_summary = prompt_bool(
        f"是否生成 Markdown 总结？默认 {'y' if bool(stream_defaults['generate_summary']) else 'n'}：",
        default=bool(stream_defaults["generate_summary"]),
    )

    db_path = str(output_config.get("db_path") or "data/xhs_crawler.db")
    csv_dir = ensure_directory(output_config.get("csv_dir") or "outputs/csv")
    log_dir = ensure_directory(output_config.get("log_dir") or "outputs/logs")
    summary_dir = ensure_directory(output_config.get("summary_dir") or "outputs/summaries")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    comments_csv_path = csv_dir / f"stream_comments_{timestamp}.csv"
    notes_csv_path = csv_dir / f"stream_notes_{timestamp}.csv"
    failed_log_path = log_dir / f"stream_failed_{timestamp}.txt"
    search_url = build_search_url(keyword)

    if write_db:
        init_db(db_path)
        sync_keywords([keyword], db_path)

    summarizer = build_summarizer(ai_config)
    scraper = XHSCommentScraper(headless=bool(stream_config.get("headless", False)))
    driver = scraper.setup_driver()
    crawl_guard = CrawlGuard.from_config("xiaohongshu", crawler_config)
    stop_on_login_required = bool(safety_config.get("stop_on_login_required", False))
    allow_manual_repair = bool(safety_config.get("allow_manual_repair", True))
    max_retry_per_note = max(1, int(safety_config.get("max_retry_per_note", 1)))

    processed_signatures = set()
    processed_clean_urls = set()
    note_row_index = 0
    total_comments = 0
    success_count = 0
    scanned_candidates = 0
    skipped_non_note = 0
    unavailable_count = 0
    no_comments_count = 0
    failed_count = 0
    skipped_existing = 0
    href_cards = 0
    clickable_cards = 0
    clicked_without_href_success = 0
    clicked_without_href_failed = 0
    clicked_profile = 0
    skipped_author_profile = 0
    fast_skip_count = 0
    idle_scrolls = 0
    max_idle_scrolls = max(8, max_candidates * 2)
    failed_records: List[Dict[str, str]] = []
    unavailable_records: List[Dict[str, str]] = []
    note_reports: List[Dict[str, object]] = []

    try:
        print(f"\n正在打开搜索页：{search_url}")
        driver.get(search_url)
        time.sleep(5)
        wait_search_ready(driver)

        while success_count < success_target and scanned_candidates < max_candidates and idle_scrolls < max_idle_scrolls:
            guard_can_continue, guard_stop_reason = crawl_guard.can_continue()
            if not guard_can_continue:
                print(f"platform_guard: stopped | reason: {guard_stop_reason}")
                break

            cards = get_search_result_cards(driver)
            next_card = None

            for card in cards:
                signature = str(card.get("signature") or "")
                if signature in processed_signatures:
                    continue

                processed_signatures.add(signature)
                scanned_candidates += 1
                candidate_type = str(card.get("candidate_type") or "skipped_non_note")
                search_title = str(card.get("title_text") or "")
                extracted_href = str(card.get("href") or "")
                skip_reason = str(card.get("skip_reason") or "")

                print(
                    f"candidate_type: {candidate_type} | "
                    f"search_title: {search_title or '未提取到标题'} | "
                    f"extracted_href: {extracted_href or '无'}"
                )

                if candidate_type == "href_card":
                    href_cards += 1
                elif candidate_type == "clickable_card":
                    clickable_cards += 1
                else:
                    skipped_non_note += 1
                    fast_skip_count += 1
                    print(f"page_status: skipped_non_note | reason: {skip_reason or 'non_note_candidate'}")
                    if scanned_candidates >= max_candidates:
                        break
                    continue

                clean_hint = str(card.get("clean_url_hint") or "")
                if clean_hint and clean_hint in processed_clean_urls:
                    continue

                if write_db and clean_hint and should_skip_by_db(clean_hint, db_path):
                    processed_clean_urls.add(clean_hint)
                    skipped_existing += 1
                    print(f"数据库中已存在终态笔记，跳过：{clean_hint}")
                    if scanned_candidates >= max_candidates:
                        break
                    continue

                next_card = card
                break

            if success_count >= success_target or scanned_candidates >= max_candidates:
                break

            if not next_card:
                idle_scrolls += 1
                print(f"当前没有新的可处理笔记卡片，滚动搜索页继续加载，第 {idle_scrolls}/{max_idle_scrolls} 次。")
                driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 900));")
                time.sleep(3)
                continue

            idle_scrolls = 0
            title_hint = str(next_card.get("title_text") or "")
            clean_hint = str(next_card.get("clean_url_hint") or "")
            href_hint = str(next_card.get("href") or "")

            print("\n" + "=" * 60)
            print(f"扫描候选 {scanned_candidates}/{max_candidates}")
            print(f"当前成功笔记 {success_count}/{success_target}")
            print(f"搜索页标题：{title_hint or '未提取到标题'}")
            print(f"搜索页 href：{href_hint}")
            print("=" * 60)

            crawl_guard.record_request()
            open_result = open_card_from_search(driver, next_card, wait_seconds=DEFAULT_NOTE_WAIT_SECONDS)
            if not open_result.get("ok"):
                failed_count += 1
                reason = str(open_result.get("reason") or "click_failed")
                crawl_guard.record_failure(reason)
                if str(next_card.get("candidate_type") or "") == "clickable_card":
                    clicked_without_href_failed += 1
                fast_skip_count += 1
                failed_records.append({"url": href_hint or clean_hint, "status": "failed_click", "reason": reason})
                append_failure_row(failed_log_path, href_hint or clean_hint, "failed_click", reason)
                print(f"click_result_url: 无")
                print(f"page_status: failed_click | reason: {reason}")
                if scanned_candidates < max_candidates and success_count < success_target:
                    wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                continue

            navigation_mode = str(open_result.get("navigation_mode") or "same_tab")
            original_window = str(open_result.get("original_window") or driver.current_window_handle)
            raw_url = (driver.current_url or "").strip()
            print(f"click_result_url: {raw_url or '无'}")

            if "/user/profile/" in raw_url:
                clicked_profile += 1
                skipped_author_profile += 1
                fast_skip_count += 1
                print("page_status: skipped_author_profile | reason: clicked_author_profile")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                if scanned_candidates < max_candidates and success_count < success_target:
                    wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                continue

            if "/explore/" not in raw_url:
                candidate_type = str(next_card.get("candidate_type") or "")
                if candidate_type == "clickable_card":
                    clicked_without_href_failed += 1
                    skipped_non_note += 1
                    fast_skip_count += 1
                    print("page_status: skipped_non_note | reason: click_did_not_enter_explore")
                else:
                    failed_count += 1
                    reason = "click_did_not_enter_explore"
                    crawl_guard.record_failure(reason)
                    fast_skip_count += 1
                    failed_records.append({"url": href_hint or clean_hint, "status": "failed_click", "reason": reason})
                    append_failure_row(failed_log_path, href_hint or clean_hint, "failed_click", reason)
                    print(f"page_status: failed_click | reason: {reason}")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                if scanned_candidates < max_candidates and success_count < success_target:
                    wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                continue

            if str(next_card.get("candidate_type") or "") == "clickable_card":
                clicked_without_href_success += 1

            clean_url = canonicalize_explore_url(raw_url) or clean_hint

            if clean_url and clean_url in processed_clean_urls:
                print(f"当前笔记已在本轮处理过，跳过：{clean_url}")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                continue

            if write_db and clean_url and should_skip_by_db(clean_url, db_path):
                skipped_existing += 1
                processed_clean_urls.add(clean_url)
                print(f"数据库中已存在终态笔记，跳过：{clean_url}")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                continue

            risk_result = crawl_guard.detect_driver(driver, expected_url_contains="/explore/")
            if risk_result.blocked and risk_result.status in GUARD_STATUSES:
                crawl_guard.record_detection(risk_result)
                reason = risk_result.reason or risk_result.status
                note_row_index += 1
                note_data = {
                    "keyword": keyword,
                    "search_title": title_hint,
                    "clean_url": clean_url or raw_url,
                    "raw_url": raw_url,
                    "note_title": title_hint,
                    "note_content": "",
                    "author_name": "",
                    "publish_time": "",
                    "like_count": "",
                    "collect_count": "",
                    "comment_count_display": "",
                    "share_count": "",
                    "image_count": 0,
                    "is_video": False,
                    "raw_note_text": "",
                    "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, risk_result.status, reason)],
                )
                append_failure_row(failed_log_path, raw_url or clean_url, risk_result.status, reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status=risk_result.status,
                    reason=reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print(
                    f"page_status: {risk_result.status} | reason: {reason} | "
                    f"action: {risk_result.action}"
                )
                if risk_result.status in {"login_required", "captcha_required"} and stop_on_login_required:
                    raise RuntimeError(f"详情页需要人工处理：{reason}")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                if crawl_guard.state.stopped:
                    print(f"platform_guard: stopped | reason: {crawl_guard.state.stop_reason}")
                    break
                if scanned_candidates < max_candidates and success_count < success_target:
                    wait_before_next(interval_seconds, fast_skip=False, fast_skip_seconds=fast_skip_seconds)
                continue

            note_data = scraper.scrape_current_page_note_content(
                note_url=clean_url or raw_url,
                keyword=keyword,
                search_title=title_hint,
                raw_url=raw_url,
            )
            note_data["clean_url"] = clean_url or note_data.get("clean_url") or raw_url
            note_data["raw_url"] = raw_url or note_data.get("raw_url") or clean_url
            note_data["search_title"] = title_hint or note_data.get("search_title") or ""
            note_data["note_title"] = note_data.get("note_title") or get_note_title(driver, fallback=title_hint)
            note_data["crawled_at"] = note_data.get("crawled_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if note_data.get("clean_url"):
                processed_clean_urls.add(str(note_data.get("clean_url")))

            unavailable, unavailable_reason = is_unavailable_note_page(driver, debug_filename=f"debug_stream_{scanned_candidates}.html")
            if unavailable:
                note_row_index += 1
                unavailable_count += 1
                unavailable_records.append(
                    {
                        "url": note_data.get("raw_url") or note_data.get("clean_url") or "",
                        "status": "unavailable",
                        "reason": unavailable_reason,
                    }
                )
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, "unavailable", unavailable_reason)],
                )
                append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "unavailable", unavailable_reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status="unavailable",
                    reason=unavailable_reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print(f"page_status: unavailable | reason: {unavailable_reason}")
                return_to_search_page(driver, search_url, navigation_mode, original_window)
                if scanned_candidates < max_candidates and success_count < success_target:
                    wait_before_next(interval_seconds, fast_skip_seconds=fast_skip_seconds)
                continue

            comments: List[Dict[str, str]] = []
            current_status = "failed"
            current_reason = "unknown_failure"

            for attempt in range(1, max_retry_per_note + 1):
                page_source = driver.page_source or ""
                available_page = is_available_note_page(driver)
                visible_comments = has_comment_area(driver)
                risk_result = crawl_guard.detect_driver(driver, expected_url_contains="/explore/")
                blocked_page = risk_result.blocked and risk_result.status in GUARD_STATUSES

                if blocked_page:
                    crawl_guard.record_detection(risk_result)
                    comments = []
                    current_status = risk_result.status
                    current_reason = risk_result.reason or risk_result.status
                    if current_status in {"login_required", "captcha_required"} and stop_on_login_required:
                        raise RuntimeError("详情页仍处于登录/验证状态，已按 safety.stop_on_login_required 停止本次运行。")
                    break

                if not available_page and not visible_comments and not blocked_page:
                    comments = []
                    current_status = "failed"
                    current_reason = "page_state_unknown"
                else:
                    try:
                        comments = scraper.scrape_current_page_comments(
                            note_url=note_data.get("clean_url") or note_data.get("raw_url") or "",
                            max_comments=max_comments,
                            scroll_pause=DEFAULT_SCROLL_PAUSE,
                            wait_for_login=0,
                            manual_confirm=False,
                        )
                        current_status = scraper.last_status or ("crawled" if comments else "no_comments")
                        current_reason = scraper.last_fail_reason or ""
                    except KeyboardInterrupt:
                        raise
                    except Exception as error:
                        comments = []
                        current_status = "failed"
                        current_reason = str(error).strip() or error.__class__.__name__

                if current_status != "failed" or attempt >= max_retry_per_note:
                    break

                save_page_source(driver, f"debug_stream_{scanned_candidates}.html")
                print(f"当前笔记第 {attempt} 次尝试失败：{current_reason}")
                if allow_manual_repair:
                    input("请在浏览器中手动修复当前笔记页后按 Enter 重试：")
                    time.sleep(1)
                else:
                    try:
                        driver.get(note_data.get("raw_url") or note_data.get("clean_url") or raw_url)
                        time.sleep(DEFAULT_NOTE_WAIT_SECONDS)
                    except Exception:
                        time.sleep(2)

            if current_status == "crawled" and comments:
                note_row_index += 1
                success_count += 1
                comment_rows = build_comment_csv_rows(
                    start_index=total_comments + 1,
                    keyword=keyword,
                    search_title=note_data.get("search_title") or "",
                    note_title=note_data.get("note_title") or "",
                    clean_url=note_data.get("clean_url") or "",
                    raw_url=note_data.get("raw_url") or "",
                    comments=comments,
                )
                append_rows(comments_csv_path, COMMENT_CSV_FIELDNAMES, comment_rows)
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, "crawled", "")],
                )
                total_comments += len(comment_rows)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status="crawled",
                    reason="",
                    comments=comments,
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                crawl_guard.record_success()
                print(f"page_status: crawled | 当前评论数：{len(comments)} | 累计评论数：{total_comments}")
            elif current_status == "unavailable":
                note_row_index += 1
                unavailable_count += 1
                reason = current_reason or "网页端不可访问或 App-only"
                unavailable_records.append(
                    {
                        "url": note_data.get("raw_url") or note_data.get("clean_url") or "",
                        "status": "unavailable",
                        "reason": reason,
                    }
                )
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, "unavailable", reason)],
                )
                append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "unavailable", reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status="unavailable",
                    reason=reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print("page_status: unavailable")
            elif current_status == "no_comments":
                note_row_index += 1
                no_comments_count += 1
                reason = current_reason or "未找到评论或解析失败"
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, "no_comments", reason)],
                )
                append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "no_comments", reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status="no_comments",
                    reason=reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print("page_status: no_comments")
            elif current_status in GUARD_STATUSES:
                note_row_index += 1
                reason = current_reason or current_status
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, current_status, reason)],
                )
                append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", current_status, reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status=current_status,
                    reason=reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print(f"page_status: {current_status} | reason: {reason}")
            else:
                note_row_index += 1
                failed_count += 1
                reason = current_reason or "未知异常"
                crawl_guard.record_failure(reason)
                failed_records.append(
                    {
                        "url": note_data.get("raw_url") or note_data.get("clean_url") or "",
                        "status": "failed",
                        "reason": reason,
                    }
                )
                append_rows(
                    notes_csv_path,
                    NOTE_CSV_FIELDNAMES,
                    [build_note_csv_row(note_row_index, note_data, "failed", reason)],
                )
                append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "failed", reason)
                note_report = persist_note_result(
                    db_enabled=write_db,
                    db_path=db_path,
                    note_data=note_data,
                    status="failed",
                    reason=reason,
                    comments=[],
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                if note_report:
                    note_reports.append(note_report)
                print(f"page_status: failed | reason: {reason}")

            return_to_search_page(driver, search_url, navigation_mode, original_window)
            if scanned_candidates < max_candidates and success_count < success_target:
                wait_before_next(interval_seconds, fast_skip_seconds=fast_skip_seconds)

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在优雅退出并保留已采集结果。")
    finally:
        scraper.close()

    guard_stats = crawl_guard.stats()
    run_stats = {
        "scanned_candidates": scanned_candidates,
        "crawled": success_count,
        "unavailable": unavailable_count,
        "no_comments": no_comments_count,
        "failed": failed_count,
        "skipped_non_note": skipped_non_note,
        "total_comments": total_comments,
        "href_cards": href_cards,
        "clickable_cards": clickable_cards,
        "clicked_without_href_success": clicked_without_href_success,
        "clicked_without_href_failed": clicked_without_href_failed,
        "clicked_profile": clicked_profile,
        "skipped_author_profile": skipped_author_profile,
        "fast_skip_count": fast_skip_count,
        "unavailable_records": unavailable_records,
        "failed_records": failed_records,
        **guard_stats,
    }

    if generate_summary:
        write_stream_run_report(
            keyword=keyword,
            run_stats=run_stats,
            note_reports=note_reports,
            summary_dir=summary_dir,
        )

    print("\n" + "=" * 60)
    print("流式采集结束")
    print(f"scanned_candidates：{scanned_candidates}")
    print(f"crawled：{success_count}")
    print(f"unavailable：{unavailable_count}")
    print(f"no_comments：{no_comments_count}")
    print(f"failed：{failed_count}")
    print(f"href_cards：{href_cards}")
    print(f"clickable_cards：{clickable_cards}")
    print(f"clicked_without_href_success：{clicked_without_href_success}")
    print(f"clicked_without_href_failed：{clicked_without_href_failed}")
    print(f"clicked_profile：{clicked_profile}")
    print(f"skipped_author_profile：{skipped_author_profile}")
    print(f"fast_skip_count：{fast_skip_count}")
    print(f"blocked_count：{guard_stats.get('blocked_count', 0)}")
    print(f"login_required_count：{guard_stats.get('login_required_count', 0)}")
    print(f"captcha_required_count：{guard_stats.get('captcha_required_count', 0)}")
    print(f"rate_limited_count：{guard_stats.get('rate_limited_count', 0)}")
    print(f"circuit_breaker_triggered：{guard_stats.get('circuit_breaker_triggered', False)}")
    print(f"skipped_non_note：{skipped_non_note}")
    print(f"total_comments：{total_comments}")
    print(f"跳过已存在终态：{skipped_existing}")
    print(f"笔记 CSV：{notes_csv_path}")
    print(f"评论 CSV：{comments_csv_path}")
    print(f"失败日志：{failed_log_path}")
    if generate_summary:
        print(f"总结目录：{summary_dir}")


if __name__ == "__main__":
    main()
