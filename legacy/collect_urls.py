#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEGACY：历史关键词收集方案。
当前项目主流程已切换为 `python search_stream_scrape.py`。

按关键词搜索小红书笔记，并收集公开可见的笔记链接。
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from browser_utils import (
    BASE_URL,
    canonicalize_explore_url,
    create_chrome_driver,
    is_available_note_page,
    is_unavailable_note_page,
    page_contains_block_keywords,
    sanitize_filename,
)

DEFAULT_MAX_URLS = 30
DEFAULT_SCROLL_TIMES = 10
DEFAULT_SCROLL_PAUSE = 3
DEFAULT_VALIDATE_WAIT = 4
DEFAULT_RESOLVE_WAIT = 3


def prompt_int(prompt: str, default: int) -> int:
    value = input(prompt).strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def prompt_bool(prompt: str, default: bool = False) -> bool:
    value = input(prompt).strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def build_search_url(keyword: str) -> str:
    return f"{BASE_URL}/search_result?keyword={quote(keyword)}"


def normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def extract_title_text(anchor) -> str:
    candidates = []

    try:
        candidates.append(normalize_text(anchor.text))
    except Exception:
        pass

    for attribute in ("title", "aria-label"):
        try:
            candidates.append(normalize_text(anchor.get_attribute(attribute)))
        except Exception:
            continue

    try:
        parent = anchor.find_element(By.XPATH, "./ancestor-or-self::*[self::section or self::div][1]")
        candidates.append(normalize_text(parent.text))
    except Exception:
        pass

    for candidate in candidates:
        if candidate:
            lines = [line.strip() for line in candidate.splitlines() if line.strip()]
            return lines[0] if lines else candidate

    return ""


def raw_url_needs_resolution(raw_url: str) -> bool:
    return not raw_url or "xsec_token=" not in raw_url


def save_debug_page(driver, filename: str):
    try:
        Path(filename).write_text(driver.page_source or "", encoding="utf-8")
    except Exception:
        pass


def pick_candidate_anchor(driver, clean_url: str, title_text: str = ""):
    matches = []
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    for anchor in anchors:
        try:
            href = (anchor.get_attribute("href") or "").strip()
            if canonicalize_explore_url(href) != clean_url:
                continue
            if not anchor.is_displayed():
                continue
            matches.append(anchor)
        except Exception:
            continue

    if matches:
        matches.sort(key=lambda item: len(normalize_text(item.text)), reverse=True)
        return matches[0]

    if title_text:
        try:
            text_node = driver.find_element(
                By.XPATH,
                f"//*[contains(normalize-space(.), {xpath_literal(title_text)})]",
            )
            try:
                return text_node.find_element(By.XPATH, "./ancestor-or-self::a[@href][1]")
            except Exception:
                return text_node
        except Exception:
            return None

    return None


def xpath_literal(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def open_note_from_search_result(
    driver,
    clean_url: str,
    title_text: str,
    search_url: str,
    debug_filename: str,
    wait_seconds: int = DEFAULT_RESOLVE_WAIT,
) -> tuple[str, str, str]:
    """
    返回 (resolved_raw_url, status, fail_reason)。
    status: available / unavailable / failed
    """
    original_window = driver.current_window_handle
    original_handles = list(driver.window_handles)
    clickable = pick_candidate_anchor(driver, clean_url, title_text=title_text)
    if not clickable:
        return "", "failed", "click_target_not_found"

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clickable)
        time.sleep(0.5)
    except Exception:
        pass

    used_new_tab = False
    resolved_url = ""

    try:
        try:
            ActionChains(driver).key_down(Keys.CONTROL).click(clickable).key_up(Keys.CONTROL).perform()
            time.sleep(1)
            new_handles = [handle for handle in driver.window_handles if handle not in original_handles]
            if new_handles:
                used_new_tab = True
                driver.switch_to.window(new_handles[-1])
        except Exception:
            used_new_tab = False

        if not used_new_tab:
            clickable = pick_candidate_anchor(driver, clean_url, title_text=title_text)
            if not clickable:
                return "", "failed", "click_target_not_found_after_retry"
            driver.execute_script("arguments[0].click();", clickable)

        time.sleep(wait_seconds)

        resolved_url = (driver.current_url or "").strip()
        unavailable, reason = is_unavailable_note_page(driver, debug_filename=debug_filename)
        if unavailable:
            return resolved_url, "unavailable", reason

        if is_available_note_page(driver):
            return resolved_url, "available", ""

        save_debug_page(driver, debug_filename)
        return resolved_url, "failed", "page_state_unknown"
    except WebDriverException as error:
        save_debug_page(driver, debug_filename)
        return resolved_url, "failed", f"click_navigation_failed: {error}"
    finally:
        try:
            if used_new_tab:
                current_handle = driver.current_window_handle
                if current_handle != original_window:
                    driver.close()
                driver.switch_to.window(original_window)
                time.sleep(1)
            else:
                driver.back()
                time.sleep(wait_seconds)
        except Exception:
            try:
                driver.get(search_url)
                time.sleep(wait_seconds)
            except Exception:
                pass


def validate_opened_note_page(driver, debug_filename: str) -> tuple[str, str]:
    unavailable, reason = is_unavailable_note_page(driver, debug_filename=debug_filename)
    if unavailable:
        return "unavailable", reason_or_default(reason)

    if is_available_note_page(driver):
        return "available", ""

    save_debug_page(driver, debug_filename)
    return "failed", "page_state_unknown"


def validate_direct_url(
    driver,
    access_url: str,
    search_url: str,
    debug_filename: str,
    wait_seconds: int = DEFAULT_VALIDATE_WAIT,
) -> tuple[str, str, str]:
    resolved_url = access_url
    try:
        driver.get(access_url)
        time.sleep(wait_seconds)
        resolved_url = (driver.current_url or "").strip() or access_url
        status, fail_reason = validate_opened_note_page(driver, debug_filename=debug_filename)
        return resolved_url, status, fail_reason
    except Exception as error:
        save_debug_page(driver, debug_filename)
        return resolved_url, "failed", f"validate_failed: {error}"
    finally:
        try:
            driver.back()
            time.sleep(wait_seconds)
        except Exception:
            try:
                driver.get(search_url)
                time.sleep(wait_seconds)
            except Exception:
                pass


def wait_search_ready(driver, manual_confirm: bool = True, debug_filename: str = "debug_search_page.html"):
    page_source = driver.page_source
    with open(debug_filename, "w", encoding="utf-8") as file:
        file.write(page_source)

    if manual_confirm:
        print("\n请在浏览器中确认以下事项：")
        print("1. 已经登录小红书")
        print("2. 当前没有验证码或安全验证")
        print("3. 搜索结果已经显示")
        input("确认无误后，回到终端按 Enter 开始收集链接：")
        time.sleep(2)
        page_source = driver.page_source
        with open(debug_filename, "w", encoding="utf-8") as file:
            file.write(page_source)

    if page_contains_block_keywords(page_source):
        result_link_count = len(driver.find_elements(By.CSS_SELECTOR, 'a[href*="/explore/"], a[href*="xiaohongshu.com/explore/"]'))
        if result_link_count == 0 or manual_confirm:
            print(f"检测到登录/验证码相关内容，已保存 {debug_filename}。")
            input("请先在浏览器中手动完成登录/验证，确认搜索结果出现后按 Enter 继续：")
            time.sleep(2)
            page_source = driver.page_source
            with open(debug_filename, "w", encoding="utf-8") as file:
                file.write(page_source)

        if page_contains_block_keywords(page_source):
            choice = input(
                "页面源码中仍检测到登录/验证码相关内容。输入 y 继续尝试，其他键退出："
            ).strip().lower()
            if choice != "y":
                raise RuntimeError("搜索页仍处于登录或验证状态，已取消本次关键词采集。")


def collect_note_links(driver, max_urls: int, max_scrolls: int) -> Dict[str, Dict[str, str]]:
    collected: Dict[str, Dict[str, str]] = {}
    stable_rounds = 0
    last_count = 0

    for round_index in range(1, max_scrolls + 1):
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        for anchor in anchors:
            try:
                raw_link = (anchor.get_attribute("href") or "").strip()
            except Exception:
                continue

            clean_url = canonicalize_explore_url(raw_link)
            if not clean_url:
                continue

            if clean_url not in collected:
                collected[clean_url] = {
                    "title_text": extract_title_text(anchor),
                    "clean_url": clean_url,
                    "raw_url": raw_link,
                }

        print(f"第 {round_index} 轮采集后，已收集 {len(collected)} 条唯一笔记链接。")

        if len(collected) >= max_urls:
            print("已达到目标链接数。")
            break

        if len(collected) <= last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_count = len(collected)

        if stable_rounds >= 3:
            print("连续多轮未收集到新链接，提前停止滚动。")
            break

        driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 900));")
        time.sleep(DEFAULT_SCROLL_PAUSE)

    return dict(list(collected.items())[:max_urls])


def enrich_raw_urls_via_click(
    driver,
    keyword: str,
    items: List[Dict[str, str]],
    wait_seconds: int = DEFAULT_RESOLVE_WAIT,
) -> List[Dict[str, str]]:
    search_url = build_search_url(keyword)
    enriched = []

    for index, item in enumerate(items, 1):
        clean_url = item.get("clean_url") or ""
        raw_url = item.get("raw_url") or ""
        title_text = item.get("标题文本") or item.get("title_text") or ""

        if not clean_url or not raw_url_needs_resolution(raw_url):
            item["status"] = item.get("status") or "collected"
            item["fail_reason"] = item.get("fail_reason") or ""
            enriched.append(item)
            continue

        print(f"正在补全第 {index}/{len(items)} 条链接的 raw_url：{clean_url}")
        resolved_raw_url, status, fail_reason = open_note_from_search_result(
            driver,
            clean_url=clean_url,
            title_text=title_text,
            search_url=search_url,
            wait_seconds=wait_seconds,
        )

        if status == "available" and resolved_raw_url:
            item["raw_url"] = resolved_raw_url
            item["status"] = "available"
            item["fail_reason"] = ""
        elif status == "unavailable":
            item["raw_url"] = resolved_raw_url or raw_url or clean_url
            item["status"] = "unavailable"
            item["fail_reason"] = reason_or_default(fail_reason)
        else:
            item["raw_url"] = raw_url or clean_url
            item["status"] = item.get("status") or "collected"
            item["fail_reason"] = item.get("fail_reason") or ""

        enriched.append(item)
        if index < len(items):
            time.sleep(wait_seconds)

    return enriched


def reason_or_default(reason: str) -> str:
    return reason or "网页端不可访问或 App-only"


def validate_collected_links(
    driver,
    keyword: str,
    items: List[Dict[str, str]],
    wait_seconds: int = DEFAULT_VALIDATE_WAIT,
    resolve_raw_urls: bool = True,
) -> List[Dict[str, str]]:
    validated = []
    search_url = build_search_url(keyword)
    for index, item in enumerate(items, 1):
        clean_url = item.get("clean_url") or ""
        raw_url = item.get("raw_url") or clean_url or ""
        title_text = item.get("title_text") or item.get("标题文本") or ""
        debug_filename = f"debug_validate_{index}.html"

        print(f"正在验证第 {index}/{len(items)} 条链接：{raw_url}")

        if resolve_raw_urls and raw_url_needs_resolution(raw_url):
            resolved_raw_url, status, fail_reason = open_note_from_search_result(
                driver,
                clean_url=clean_url,
                title_text=title_text,
                search_url=search_url,
                debug_filename=debug_filename,
                wait_seconds=wait_seconds,
            )
            if status == "failed" and fail_reason.startswith("click_target_not_found"):
                resolved_raw_url, status, fail_reason = validate_direct_url(
                    driver,
                    access_url=raw_url or clean_url,
                    search_url=search_url,
                    debug_filename=debug_filename,
                    wait_seconds=wait_seconds,
                )
        else:
            resolved_raw_url, status, fail_reason = validate_direct_url(
                driver,
                access_url=raw_url,
                search_url=search_url,
                debug_filename=debug_filename,
                wait_seconds=wait_seconds,
            )

        if resolved_raw_url:
            item["raw_url"] = resolved_raw_url

        item["status"] = status
        item["fail_reason"] = fail_reason

        validated.append(item)

        if index < len(items):
            time.sleep(wait_seconds)

    return validated


def collect_urls_for_keyword(
    keyword: str,
    max_notes: int,
    scroll_times: int,
    *,
    driver=None,
    manual_confirm: bool = True,
    validate_urls: bool = False,
    resolve_raw_urls: bool = True,
) -> List[Dict[str, str]]:
    own_driver = driver is None
    active_driver = driver or create_chrome_driver(headless=False)

    try:
        search_url = build_search_url(keyword)
        print(f"\n正在打开搜索页：{search_url}")
        active_driver.get(search_url)
        time.sleep(5)

        wait_search_ready(active_driver, manual_confirm=manual_confirm)
        collected = collect_note_links(active_driver, max_urls=max_notes, max_scrolls=scroll_times)
        collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        results = []
        for index, (clean_url, item) in enumerate(collected.items(), 1):
            results.append(
                {
                    "序号": index,
                    "关键词": keyword,
                    "标题文本": item.get("title_text", ""),
                    "title_text": item.get("title_text", ""),
                    "clean_url": clean_url,
                    "raw_url": item.get("raw_url") or clean_url,
                    "采集时间": collected_at,
                    "status": "collected",
                    "fail_reason": "",
                }
            )

        if validate_urls and results:
            print(f"开始低频验证候选链接，共 {len(results)} 条。")
            results = validate_collected_links(
                active_driver,
                keyword=keyword,
                items=results,
                wait_seconds=DEFAULT_VALIDATE_WAIT,
                resolve_raw_urls=resolve_raw_urls,
            )

        return results
    finally:
        if own_driver:
            active_driver.quit()


def save_urls_txt(urls, filename: str = "urls.txt"):
    with open(filename, "w", encoding="utf-8") as file:
        for url in urls:
            if url:
                file.write(f"{url}\n")
    print(f"已保存链接列表：{filename}")


def save_urls_csv(keyword: str, rows, timestamp: str):
    safe_keyword = sanitize_filename(keyword, default="keyword")
    filename = f"xhs_urls_{safe_keyword}_{timestamp}.csv"
    fieldnames = [
        "序号",
        "关键词",
        "标题文本",
        "笔记链接",
        "原始链接",
        "clean_url",
        "raw_url",
        "status",
        "fail_reason",
        "采集时间",
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"已保存链接 CSV：{filename}")


def main():
    print("=" * 60)
    print("小红书关键词笔记链接采集")
    print("=" * 60)

    keyword = input("请输入关键词：").strip()
    if not keyword:
        print("错误：关键词不能为空。")
        return

    max_urls = prompt_int(f"最多收集多少篇笔记（默认 {DEFAULT_MAX_URLS}）：", DEFAULT_MAX_URLS)
    max_scrolls = prompt_int(f"搜索页滚动次数（默认 {DEFAULT_SCROLL_TIMES}）：", DEFAULT_SCROLL_TIMES)
    validate_urls = prompt_bool("是否验证链接可访问性？默认 y：", default=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        results = collect_urls_for_keyword(
            keyword,
            max_notes=max_urls,
            scroll_times=max_scrolls,
            manual_confirm=True,
            validate_urls=validate_urls,
        )
        if not results:
            print("未收集到笔记链接，请先检查 debug_search_page.html 或页面结构。")
            return

        csv_rows = []
        for item in results:
            csv_rows.append(
                {
                    "序号": item["序号"],
                    "关键词": item["关键词"],
                    "标题文本": item["标题文本"],
                    "笔记链接": item["clean_url"],
                    "原始链接": item["raw_url"] or item["clean_url"],
                    "clean_url": item["clean_url"],
                    "raw_url": item["raw_url"] or item["clean_url"],
                    "status": item.get("status", "collected"),
                    "fail_reason": item.get("fail_reason", ""),
                    "采集时间": item["采集时间"],
                }
            )

        available_urls = [
            (row["raw_url"] or row["clean_url"])
            for row in csv_rows
            if row.get("status") == "available"
        ]
        save_urls_txt(available_urls)
        save_urls_csv(keyword, csv_rows, timestamp)
        status_counts = {
            "collected": len([row for row in csv_rows if row.get("status") == "collected"]),
            "available": len([row for row in csv_rows if row.get("status") == "available"]),
            "unavailable": len([row for row in csv_rows if row.get("status") == "unavailable"]),
            "failed": len([row for row in csv_rows if row.get("status") == "failed"]),
        }
        if not validate_urls:
            print("本次未启用链接可访问性验证，CSV 中链接状态均为 collected，urls.txt 不会写入未验证链接。")
        print(
            f"\n采集完成，共收集 {len(csv_rows)} 条候选链接。"
            f" collected {status_counts['collected']} 条，"
            f" available {status_counts['available']} 条，"
            f" unavailable {status_counts['unavailable']} 条，"
            f" failed {status_counts['failed']} 条。"
        )
    except KeyboardInterrupt:
        print("\n用户中断采集。")
    except Exception as error:
        print(f"\n采集过程中出错：{error}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
