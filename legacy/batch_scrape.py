#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEGACY：历史 urls.txt 批量评论采集方案。
当前项目主流程已切换为 `python search_stream_scrape.py`。

批量读取 urls.txt 并逐条采集评论。
"""

from __future__ import annotations

import csv
import glob
import os
import time
from datetime import datetime

from browser_utils import canonicalize_explore_url
from xhs_scraper import XHSCommentScraper

DEFAULT_MAX_COMMENTS = 50
DEFAULT_INTERVAL_SECONDS = 60


def prompt_int(prompt: str, default: int) -> int:
    value = input(prompt).strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def load_urls(filename: str = "urls.txt"):
    if not os.path.exists(filename):
        print(f"错误：未找到 {filename}。请先运行 python collect_urls.py")
        return []

    seen = set()
    urls = []
    with open(filename, "r", encoding="utf-8") as file:
        for line in file:
            raw = line.strip()
            dedupe_key = canonicalize_explore_url(raw) or raw
            if not raw or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            urls.append(raw)

    if not urls:
        print(f"错误：{filename} 为空或没有可用链接。")
    return urls


def load_keyword_map():
    files = glob.glob("xhs_urls_*.csv")
    if not files:
        return {}

    latest_file = max(files, key=os.path.getmtime)
    mapping = {}

    try:
        with open(latest_file, "r", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            for row in reader:
                url = canonicalize_explore_url(
                    row.get("clean_url", "") or row.get("笔记链接", "")
                )
                if url and url not in mapping:
                    mapping[url] = row.get("关键词", "")
    except Exception as error:
        print(f"读取关键词映射失败，将继续但关键词列可能为空：{error}")
        return {}

    print(f"已加载关键词映射：{latest_file}")
    return mapping


def should_recreate_driver(error: Exception) -> bool:
    message = str(error).lower()
    fatal_signals = [
        "invalid session id",
        "chrome not reachable",
        "disconnected",
        "no such window",
        "session deleted",
    ]
    return any(signal in message for signal in fatal_signals)


def save_comments(rows, timestamp: str):
    filename = f"xhs_batch_comments_{timestamp}.csv"
    fieldnames = ["序号", "关键词", "笔记链接", "规范链接", "用户名", "评论内容", "原始文本", "发布时间", "点赞数", "爬取时间"]

    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"已保存批量评论 CSV：{filename}")


def save_failures(failures, timestamp: str):
    filename = f"failed_urls_{timestamp}.txt"
    with open(filename, "w", encoding="utf-8") as file:
        for url, reason in failures:
            file.write(f"{url}\t{reason}\n")
    print(f"已保存失败链接：{filename}")


def main():
    print("=" * 60)
    print("小红书批量评论采集")
    print("=" * 60)

    urls = load_urls()
    if not urls:
        return

    max_comments = prompt_int(f"每篇最多采集多少条评论（默认 {DEFAULT_MAX_COMMENTS}）：", DEFAULT_MAX_COMMENTS)
    interval_seconds = prompt_int(
        f"每篇之间间隔多少秒（默认 {DEFAULT_INTERVAL_SECONDS}）：",
        DEFAULT_INTERVAL_SECONDS,
    )

    keyword_map = load_keyword_map()
    scraper = XHSCommentScraper(headless=False)
    all_comments = []
    failures = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    total_comments = 0

    try:
        for index, url in enumerate(urls, 1):
            print("\n" + "=" * 60)
            print(f"正在处理第 {index}/{len(urls)} 篇：{url}")
            print("=" * 60)
            clean_url = canonicalize_explore_url(url) or ""

            try:
                comments = scraper.scrape_comments(
                    url,
                    max_comments=max_comments,
                    scroll_pause=3,
                    wait_for_login=30 if index == 1 else 0,
                    manual_confirm=(index == 1),
                )
                status = scraper.last_status or ("crawled" if comments else "no_comments")
                reason = scraper.last_fail_reason or ""

                if status == "unavailable":
                    failures.append((url, reason or "网页端不可访问或 App-only"))
                    print(f"URL：{url}")
                    print(f"当前状态：unavailable | 当前评论数：0 | 累计评论数：{total_comments}")
                elif comments:
                    keyword = keyword_map.get(clean_url or url, "")
                    for comment in comments:
                        merged = {
                            "关键词": keyword,
                            "笔记链接": comment.get("笔记链接", url),
                            "规范链接": clean_url,
                            "用户名": comment.get("用户名", ""),
                            "评论内容": comment.get("评论内容", ""),
                            "原始文本": comment.get("原始文本", ""),
                            "发布时间": comment.get("发布时间", ""),
                            "点赞数": comment.get("点赞数", ""),
                            "爬取时间": comment.get("爬取时间", ""),
                        }
                        all_comments.append(merged)
                    total_comments += len(comments)
                    print(f"URL：{url}")
                    print(f"当前状态：crawled | 当前评论数：{len(comments)} | 累计评论数：{total_comments}")
                else:
                    failures.append((url, reason or "no_comments_or_parse_failed"))
                    print(f"URL：{url}")
                    print(f"当前状态：no_comments | 当前评论数：0 | 累计评论数：{total_comments}")
            except KeyboardInterrupt:
                print("\n用户中断批量采集，正在保存已获取数据。")
                break
            except Exception as error:
                reason = str(error).strip() or error.__class__.__name__
                failures.append((url, reason))
                print(f"URL：{url}")
                print(f"当前状态：failed | 当前评论数：0 | 累计评论数：{total_comments}")
                print(f"本篇采集失败：{reason}")
                if should_recreate_driver(error):
                    print("浏览器会话异常，准备重建浏览器实例。")
                    scraper.close()
                    scraper = XHSCommentScraper(headless=False)

            if index < len(urls):
                print(f"等待 {interval_seconds} 秒后继续下一篇...")
                time.sleep(interval_seconds)
    finally:
        scraper.close()

    if all_comments:
        rows = []
        for index, comment in enumerate(all_comments, 1):
            row = {"序号": index}
            row.update(comment)
            rows.append(row)
        save_comments(rows, timestamp)
    else:
        print("本次批量任务未获取到任何评论数据。")

    if failures:
        save_failures(failures, timestamp)
    else:
        print("本次批量任务没有失败链接。")

    print(
        f"\n批量采集结束：成功累计 {len(all_comments)} 条评论，失败 {len(failures)} 篇。"
    )


if __name__ == "__main__":
    main()
