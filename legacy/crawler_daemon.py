#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEGACY：历史后台队列采集方案。
当前项目主流程已切换为 `python search_stream_scrape.py`。

小红书公开评论采集后台循环任务。
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from legacy.collect_urls import collect_urls_for_keyword
from db import (
    get_daily_dashboard_stats,
    init_db,
    mark_note_crawled,
    mark_note_failed,
    mark_note_no_comments,
    mark_note_unavailable,
    save_comments,
    save_search_results,
    save_summary,
    sync_keywords,
    touch_keyword_search,
    claim_pending_notes,
)
from notifier import ensure_directory, notify_summary
from summarizer import build_summarizer
from xhs_scraper import XHSCommentScraper

CONFIG_PATH = Path("config.yaml")

DEFAULT_CONFIG = {
    "keywords": [],
    "crawler": {
        "search": {
            "max_notes_per_keyword": 30,
            "scroll_times": 10,
            "validate_urls": False,
            "sleep_between_keywords_seconds": 15,
        },
        "comments": {
            "max_comments_per_note": 50,
            "scroll_pause_seconds": 3,
            "interval_between_notes_seconds": 60,
        },
        "queue": {
            "pending_batch_size": 5,
        },
        "daemon": {
            "loop_sleep_seconds": 300,
            "manual_confirm_first_search": True,
            "manual_confirm_first_note": True,
        },
    },
    "output": {
        "db_path": "data/xhs_crawler.db",
        "summary_dir": "outputs/summaries",
        "log_dir": "outputs/logs",
    },
    "notify": {
        "enabled": False,
        "channels": [],
    },
}


def deep_merge(base: Dict[str, object], override: Dict[str, object]) -> Dict[str, object]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, object]:
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_path}")

    with open(config_path, "r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}

    return deep_merge(DEFAULT_CONFIG, loaded)


def extract_keywords(config: Dict[str, object]) -> List[str]:
    raw_keywords = config.get("keywords") or []
    keywords = []

    for item in raw_keywords:
        if isinstance(item, str):
            keyword = item.strip()
            if keyword:
                keywords.append(keyword)
        elif isinstance(item, dict):
            keyword = str(item.get("keyword") or item.get("name") or "").strip()
            enabled = item.get("enabled", True)
            if keyword and enabled:
                keywords.append(keyword)

    return keywords


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


def setup_logger(log_dir: str | Path) -> logging.Logger:
    directory = ensure_directory(log_dir)
    logger = logging.getLogger("crawler_daemon")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(directory / "crawler_daemon.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def collect_and_enqueue_notes(
    config: Dict[str, object],
    db_path: str,
    search_manual_used: bool,
    logger: logging.Logger,
) -> tuple[bool, Dict[str, int]]:
    keywords = extract_keywords(config)
    sync_keywords(keywords, db_path)
    stats = {"found": 0, "unavailable": 0}

    if not keywords:
        logger.info("配置中没有可用关键词，本轮跳过搜索阶段。")
        return search_manual_used, stats

    search_settings = config["crawler"]["search"]
    daemon_settings = config["crawler"]["daemon"]
    for index, keyword in enumerate(keywords, 1):
        try:
            manual_confirm = bool(daemon_settings.get("manual_confirm_first_search", True)) and not search_manual_used
            if manual_confirm:
                search_manual_used = True

            logger.info("开始搜索关键词：%s", keyword)
            results = collect_urls_for_keyword(
                keyword,
                max_notes=int(search_settings.get("max_notes_per_keyword", 30)),
                scroll_times=int(search_settings.get("scroll_times", 10)),
                manual_confirm=manual_confirm,
                validate_urls=bool(search_settings.get("validate_urls", False)),
            )

            saved_count = save_search_results(keyword, results, db_path)
            touch_keyword_search(keyword, db_path)
            stats["found"] += len(results)
            stats["unavailable"] += len(
                [item for item in results if item.get("status") == "unavailable"]
            )
            logger.info(
                "关键词 %s 本轮发现 %s 条链接，写入/更新 notes %s 条，其中 unavailable %s 条。",
                keyword,
                len(results),
                saved_count,
                len([item for item in results if item.get("status") == "unavailable"]),
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("关键词 %s 搜索阶段发生错误。", keyword)

        if index < len(keywords):
            sleep_seconds = int(search_settings.get("sleep_between_keywords_seconds", 15))
            if sleep_seconds > 0:
                logger.info("搜索阶段等待 %s 秒后继续下一个关键词。", sleep_seconds)
                time.sleep(sleep_seconds)

    return search_manual_used, stats


def process_pending_notes(
    config: Dict[str, object],
    db_path: str,
    scraper: XHSCommentScraper,
    note_manual_used: bool,
    logger: logging.Logger,
) -> Tuple[bool, XHSCommentScraper, Dict[str, int]]:
    queue_settings = config["crawler"]["queue"]
    comment_settings = config["crawler"]["comments"]
    daemon_settings = config["crawler"]["daemon"]
    notify_config = config.get("notify") or {}
    summary_dir = config["output"]["summary_dir"]
    stats = {"crawled": 0, "unavailable": 0, "failed": 0, "no_comments": 0}

    pending_notes = claim_pending_notes(
        limit=int(queue_settings.get("pending_batch_size", 5)),
        db_path=db_path,
    )

    if not pending_notes:
        logger.info("当前没有 pending 状态的笔记。")
        return note_manual_used, scraper, stats

    summarizer = build_summarizer()

    for index, note in enumerate(pending_notes, 1):
        access_url = note.get("raw_url") or note.get("clean_url") or ""
        manual_confirm = bool(daemon_settings.get("manual_confirm_first_note", True)) and not note_manual_used
        if manual_confirm:
            note_manual_used = True

        logger.info("开始处理笔记 %s", access_url)

        try:
            comments = scraper.scrape_comments(
                access_url,
                max_comments=int(comment_settings.get("max_comments_per_note", 50)),
                scroll_pause=int(comment_settings.get("scroll_pause_seconds", 3)),
                wait_for_login=30 if manual_confirm else 0,
                manual_confirm=manual_confirm,
            )
            current_status = scraper.last_status or ("crawled" if comments else "no_comments")
            current_reason = scraper.last_fail_reason or ""

            if current_status == "unavailable":
                save_comments(note["id"], note.get("clean_url") or access_url, [], db_path)
                summary = summarizer.summarize_unavailable(note, current_reason)
                save_summary(note, summary, db_path)
                mark_note_unavailable(note["id"], current_reason, db_path)
                dashboard_stats = get_daily_dashboard_stats(db_path)
                summary_path = notify_summary(
                    note,
                    summary,
                    notify_config=notify_config,
                    dashboard_stats=dashboard_stats,
                    summary_dir=summary_dir,
                )
                stats["unavailable"] += 1
                logger.info("笔记 unavailable：%s，摘要文件 %s", access_url, summary_path)
            elif comments:
                save_comments(note["id"], note.get("clean_url") or access_url, comments, db_path)
                summary = summarizer.summarize(note, comments)
                save_summary(note, summary, db_path)
                mark_note_crawled(note["id"], db_path)
                dashboard_stats = get_daily_dashboard_stats(db_path)
                summary_path = notify_summary(
                    note,
                    summary,
                    notify_config=notify_config,
                    dashboard_stats=dashboard_stats,
                    summary_dir=summary_dir,
                )
                stats["crawled"] += 1
                logger.info(
                    "笔记处理完成：%s，评论 %s 条，摘要文件 %s",
                    access_url,
                    len(comments),
                    summary_path,
                )
            else:
                save_comments(note["id"], note.get("clean_url") or access_url, [], db_path)
                summary = summarizer.summarize_no_comments(note, current_reason)
                save_summary(note, summary, db_path)
                mark_note_no_comments(note["id"], current_reason, db_path)
                dashboard_stats = get_daily_dashboard_stats(db_path)
                summary_path = notify_summary(
                    note,
                    summary,
                    notify_config=notify_config,
                    dashboard_stats=dashboard_stats,
                    summary_dir=summary_dir,
                )
                stats["no_comments"] += 1
                logger.info("笔记未获取到评论：%s，摘要文件 %s", access_url, summary_path)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            reason = str(error).strip() or error.__class__.__name__
            mark_note_failed(note["id"], reason, db_path)
            stats["failed"] += 1
            logger.exception("笔记处理失败：%s", access_url)

            if should_recreate_driver(error):
                logger.info("浏览器会话异常，准备重建评论抓取实例。")
                scraper.close()
                scraper = XHSCommentScraper(headless=False)

        if index < len(pending_notes):
            sleep_seconds = int(comment_settings.get("interval_between_notes_seconds", 60))
            if sleep_seconds > 0:
                logger.info("评论阶段等待 %s 秒后继续下一篇。", sleep_seconds)
                time.sleep(sleep_seconds)

    return note_manual_used, scraper, stats


def main():
    config = load_config()
    logger = setup_logger(config["output"]["log_dir"])
    logger.info("crawler_daemon 启动。按 Ctrl+C 可停止。")

    search_manual_used = False
    note_manual_used = False
    scraper = XHSCommentScraper(headless=False)

    try:
        while True:
            config = load_config()
            db_path = str(config["output"]["db_path"])
            ensure_directory(Path(db_path).parent)
            ensure_directory(config["output"]["summary_dir"])
            init_db(db_path)

            search_manual_used, search_stats = collect_and_enqueue_notes(
                config=config,
                db_path=db_path,
                search_manual_used=search_manual_used,
                logger=logger,
            )
            note_manual_used, scraper, process_stats = process_pending_notes(
                config=config,
                db_path=db_path,
                scraper=scraper,
                note_manual_used=note_manual_used,
                logger=logger,
            )

            sleep_seconds = int(config["crawler"]["daemon"].get("loop_sleep_seconds", 300))
            logger.info(
                "本轮统计：发现笔记 %s，成功采集 %s，unavailable %s，failed %s，no_comments %s。",
                search_stats.get("found", 0),
                process_stats.get("crawled", 0),
                search_stats.get("unavailable", 0) + process_stats.get("unavailable", 0),
                process_stats.get("failed", 0),
                process_stats.get("no_comments", 0),
            )
            logger.info("本轮完成，%s 秒后开始下一轮。", sleep_seconds)
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        logger.info("收到停止信号，crawler_daemon 即将退出。")
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
