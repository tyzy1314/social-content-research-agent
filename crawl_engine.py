#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用采集引擎组件：队列、限速、重试、批量写入和统计。"""

from __future__ import annotations

import random
import csv
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, TypeVar

from crawl_guard import CrawlGuard, RiskDetectionResult
from db import (
    claim_crawl_queue,
    enqueue_crawl_url,
    save_content_comments,
    save_summary,
    update_crawl_queue_status,
    upsert_content_item,
)
from models import ContentComment, ContentItem

T = TypeVar("T")
GUARD_STOP_STATUSES = {"blocked", "login_required", "captcha_required", "rate_limited"}


def _resume_after_xhs_user_action(adapter, risk_result: RiskDetectionResult) -> RiskDetectionResult:
    """Allow one manual login/verification attempt; never resume after rate limiting."""
    if risk_result.status not in {"login_required", "captcha_required"}:
        return risk_result
    if bool(getattr(adapter, "headless", False)):
        return risk_result
    wait_for_login = getattr(adapter, "wait_user_login", None)
    if not callable(wait_for_login):
        return risk_result
    try:
        wait_for_login()
    except EOFError:
        print("当前终端无法接收登录确认；请在交互式终端手动登录后重新运行。")
        return risk_result
    return adapter.detect_current_risk()
BILIBILI_ITEM_CSV_FIELDNAMES = [
    "序号",
    "平台",
    "内容ID",
    "标题",
    "正文/简介",
    "作者",
    "发布时间",
    "播放量",
    "点赞数",
    "投币数",
    "收藏数",
    "评论数展示值",
    "弹幕数",
    "链接",
    "页面状态",
    "失败原因",
    "采集时间",
]
BILIBILI_COMMENT_CSV_FIELDNAMES = [
    "序号",
    "平台",
    "内容ID",
    "标题",
    "链接",
    "评论用户",
    "评论内容",
    "发布时间",
    "点赞数",
    "采集时间",
]


class RateLimiter:
    def __init__(self, min_delay_seconds: float = 1.5, max_delay_seconds: float = 5.0):
        self.min_delay_seconds = max(0.0, float(min_delay_seconds))
        self.max_delay_seconds = max(self.min_delay_seconds, float(max_delay_seconds))
        self._last_at = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last_at
        delay = random.uniform(self.min_delay_seconds, self.max_delay_seconds)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_at = time.time()


class RetryWithBackoff:
    def __init__(self, max_retries: int = 3, base_delay_seconds: float = 1.0, max_delay_seconds: float = 30.0):
        self.max_retries = max(0, int(max_retries))
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(self.base_delay_seconds, float(max_delay_seconds))

    def run(self, func: Callable[[], T], *, should_retry: Callable[[Exception], bool] | None = None) -> T:
        attempt = 0
        while True:
            try:
                return func()
            except Exception as exc:
                if should_retry and not should_retry(exc):
                    raise
                if attempt >= self.max_retries:
                    raise
                delay = min(self.max_delay_seconds, self.base_delay_seconds * (2**attempt))
                time.sleep(delay + random.uniform(0, 0.5))
                attempt += 1


@dataclass
class CrawlerRunStats:
    scanned_count: int = 0
    detail_attempts: int = 0
    detail_successes: int = 0
    comment_attempts: int = 0
    new_items_count: int = 0
    duplicate_count: int = 0
    failed_count: int = 0
    unavailable_count: int = 0
    no_comments_count: int = 0
    skipped_non_note_count: int = 0
    skipped_author_profile_count: int = 0
    fast_skip_count: int = 0
    total_comments: int = 0
    href_cards: int = 0
    clickable_cards: int = 0
    clicked_without_href_success: int = 0
    clicked_without_href_failed: int = 0
    clicked_profile: int = 0
    blocked_count: int = 0
    login_required_count: int = 0
    captcha_required_count: int = 0
    rate_limited_count: int = 0
    skipped_robots_count: int = 0
    circuit_breaker_triggered: bool = False
    cooldown_platforms: List[str] = field(default_factory=list)
    top_block_reasons: List[Dict[str, object]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def record_risk(self, result: RiskDetectionResult):
        if not result.blocked:
            return
        self.blocked_count += 1
        if result.status == "login_required":
            self.login_required_count += 1
        elif result.status == "captcha_required":
            self.captcha_required_count += 1
        elif result.status == "rate_limited":
            self.rate_limited_count += 1
        elif result.status == "skipped_robots":
            self.skipped_robots_count += 1

    def asdict(self) -> Dict[str, object]:
        elapsed_seconds = max(0.001, time.time() - self.started_at)
        payload = asdict(self)
        payload["elapsed_seconds"] = round(elapsed_seconds, 2)
        payload["items_per_minute"] = round((self.new_items_count / elapsed_seconds) * 60, 2)
        return payload


class URLQueue:
    def __init__(self, platform: str, *, db_path: str | None = None, max_retries: int = 3):
        self.platform = platform
        self.db_path = db_path
        self.max_retries = max_retries

    def enqueue_many(self, urls: Iterable[str], *, seed: str = "", priority: int = 0) -> int:
        count = 0
        for url in urls:
            if not url:
                continue
            enqueue_crawl_url(
                platform=self.platform,
                url_or_id=url,
                source_url=url,
                seed=seed,
                priority=priority,
                max_retries=self.max_retries,
                db_path=self.db_path,
            )
            count += 1
        return count

    def claim(self, limit: int = 10) -> List[Dict[str, object]]:
        return claim_crawl_queue(platform=self.platform, limit=limit, db_path=self.db_path)

    def mark_done(self, queue_id: int):
        update_crawl_queue_status(queue_id, status="done", db_path=self.db_path)

    def mark_failed(self, queue_id: int, error: str, *, retry_delay_seconds: int = 60):
        next_retry_at = (datetime.now() + timedelta(seconds=retry_delay_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        update_crawl_queue_status(
            queue_id,
            status="failed",
            error=error,
            next_retry_at=next_retry_at,
            db_path=self.db_path,
        )

    def mark_blocked(self, queue_id: int, status: str, reason: str):
        update_crawl_queue_status(
            queue_id,
            status=status,
            error=reason,
            blocked_reason=reason,
            db_path=self.db_path,
        )


class CrawlerEngine:
    def __init__(self, adapter, *, crawler_config: dict | None = None, db_path: str | None = None):
        self.adapter = adapter
        self.crawler_config = crawler_config or {}
        self.db_path = db_path
        self.guard: CrawlGuard = adapter.guard
        self.rate_limiter = RateLimiter(
            self.crawler_config.get("min_delay_seconds", 1.5),
            self.crawler_config.get("max_delay_seconds", 5),
        )
        self.retry = RetryWithBackoff(max_retries=self.crawler_config.get("max_retries", 3))
        self.stats = CrawlerRunStats()

    def save_items_batch(self, items: Sequence[ContentItem]) -> List[int]:
        ids = []
        for item in items:
            ids.append(upsert_content_item(db_path=self.db_path, **item.to_db_kwargs()))
        return ids

    def save_comments_batch(self, item_id: int, item: ContentItem, comments: Sequence[ContentComment]) -> int:
        return save_content_comments(
            content_item_id=item_id,
            platform=item.platform,
            content_id=item.content_id,
            source_url=item.source_url,
            comments=[comment.asdict() for comment in comments],
            db_path=self.db_path,
        )

    def guard_stats(self) -> Dict[str, object]:
        guard_stats = self.guard.stats()
        self.stats.circuit_breaker_triggered = bool(guard_stats.get("circuit_breaker_triggered"))
        self.stats.cooldown_platforms = list(guard_stats.get("cooldown_platforms") or [])
        self.stats.top_block_reasons = list(guard_stats.get("top_block_reasons") or [])
        return self.stats.asdict()

    def run_bilibili_test_stream(
        self,
        *,
        keyword: str = "",
        direct_url: str = "",
        stream_config: Dict[str, object],
        output_config: Dict[str, object],
        ai_config: Dict[str, object] | None = None,
    ) -> Dict[str, object]:
        from notifier import ensure_directory, write_stream_run_report
        from summarizer import build_summarizer

        success_target = int(stream_config["success_target"])
        max_candidates = int(stream_config["max_candidates"])
        max_comments = int(stream_config.get("max_comments_per_item") or stream_config["max_comments_per_note"])
        interval_seconds = int(stream_config.get("sleep_between_items_seconds") or stream_config["sleep_between_notes_seconds"])
        write_db = bool(stream_config.get("write_db", True))
        generate_summary = bool(stream_config.get("generate_summary", True))

        if direct_url:
            success_target = min(success_target, 1)
            max_candidates = 1

        db_path = str(output_config.get("db_path") or self.db_path or "data/xhs_crawler.db")
        csv_dir = ensure_directory(output_config.get("csv_dir") or "outputs/csv")
        log_dir = ensure_directory(output_config.get("log_dir") or "outputs/logs")
        summary_dir = ensure_directory(output_config.get("summary_dir") or "outputs/summaries")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        items_csv_path = Path(csv_dir) / f"bilibili_items_{timestamp}.csv"
        comments_csv_path = Path(csv_dir) / f"bilibili_comments_{timestamp}.csv"
        failed_log_path = Path(log_dir) / f"bilibili_failed_{timestamp}.txt"
        self._ensure_csv_file(items_csv_path, BILIBILI_ITEM_CSV_FIELDNAMES)
        self._ensure_csv_file(comments_csv_path, BILIBILI_COMMENT_CSV_FIELDNAMES)
        summarizer = build_summarizer(ai_config or {"enabled": False})
        run_label = keyword or direct_url or "direct_url"

        note_reports: List[Dict[str, object]] = []
        failed_records: List[Dict[str, str]] = []
        unavailable_records: List[Dict[str, str]] = []

        try:
            if direct_url:
                candidates = [
                    {
                        "title": "",
                        "href": direct_url,
                        "raw_text": direct_url,
                        "candidate_type": "video",
                        "signature": direct_url,
                    }
                ]
            else:
                self.adapter.open_search_page(keyword)
                candidates = self.adapter.iter_search_candidates(limit=max_candidates)

            for candidate in candidates:
                if self.stats.new_items_count >= success_target or self.stats.scanned_count >= max_candidates:
                    break
                can_continue, stop_reason = self.guard.can_continue()
                if not can_continue:
                    print(f"platform_guard: stopped | reason: {stop_reason}")
                    break

                self.stats.scanned_count += 1
                candidate_type = str(candidate.get("candidate_type") or "skipped_non_item")
                href = str(candidate.get("href") or "")
                title_hint = str(candidate.get("title") or "")
                print("\n" + "=" * 60)
                print(f"Bilibili 候选 {self.stats.scanned_count}/{max_candidates}")
                print(f"candidate_type: {candidate_type}")
                print(f"title: {title_hint or '未提取到标题'}")
                print(f"href: {href or '无'}")
                print("=" * 60)

                if candidate_type != "video":
                    self.stats.skipped_non_note_count += 1
                    self.stats.fast_skip_count += 1
                    print("page_status: skipped_non_item | reason: search_candidate_not_video")
                    self._write_bilibili_failed(failed_log_path, href, "skipped_non_item", "search_candidate_not_video")
                    continue

                self.guard.record_request()
                try:
                    open_result = self.retry.run(lambda: self.adapter.open_candidate(candidate))
                except Exception as error:
                    open_result = {"ok": False, "reason": str(error) or error.__class__.__name__}
                if not open_result.get("ok"):
                    reason = str(open_result.get("reason") or "open_failed")
                    self.stats.failed_count += 1
                    self.guard.record_failure(reason)
                    failed_records.append({"url": href, "status": "failed", "reason": reason})
                    self._write_bilibili_failed(failed_log_path, href, "failed", reason)
                    print(f"page_status: failed | reason: {reason}")
                    continue

                classification = self.adapter.classify_current_page()
                status = str(classification.get("status") or "unknown")
                reason = str(classification.get("reason") or "")
                current_url = self.adapter.current_url()

                if self._should_prompt_for_bilibili_user_action(classification):
                    print("请在浏览器中手动完成登录或验证，然后按 Enter 继续。")
                    try:
                        input()
                        classification = self.adapter.classify_current_page()
                        status = str(classification.get("status") or "unknown")
                        reason = str(classification.get("reason") or "")
                        current_url = self.adapter.current_url()
                    except EOFError:
                        print("当前终端无法读取 Enter 确认，保留原状态并停止当前任务。")

                if status in GUARD_STOP_STATUSES:
                    risk_result = classification.get("risk_result")
                    if isinstance(risk_result, RiskDetectionResult):
                        self._record_guard_stop(risk_result)
                    else:
                        self.guard.record_failure(reason or status)
                    item_data = self._minimal_bilibili_item(
                        keyword=run_label,
                        title=title_hint,
                        url=current_url or href,
                        status=status,
                        reason=reason or status,
                    )
                    self._write_bilibili_item_row(items_csv_path, item_data, status=status, reason=reason or status)
                    self._write_bilibili_failed(failed_log_path, current_url or href, status, reason or status)
                    failed_records.append({"url": current_url or href, "status": status, "reason": reason or status})
                    self._persist_bilibili_item(
                        item_data=item_data,
                        comments=[],
                        status=status,
                        reason=reason or status,
                        write_db=write_db,
                        db_path=db_path,
                        generate_summary=False,
                        summarizer=summarizer,
                        note_reports=note_reports,
                    )
                    print(f"page_status: {status} | reason: {reason or status}")
                    break

                if status in {"unavailable", "skipped_non_item", "unknown"}:
                    if status == "unavailable":
                        self.stats.unavailable_count += 1
                        unavailable_records.append({"url": current_url or href, "status": status, "reason": reason})
                    elif status == "skipped_non_item":
                        self.stats.skipped_non_note_count += 1
                    else:
                        self.stats.failed_count += 1
                        self.guard.record_failure(reason or status)
                        failed_records.append({"url": current_url or href, "status": "failed", "reason": reason or status})
                    item_data = self._minimal_bilibili_item(
                        keyword=run_label,
                        title=title_hint,
                        url=current_url or href,
                        status=status,
                        reason=reason or status,
                    )
                    self._write_bilibili_item_row(items_csv_path, item_data, status=status, reason=reason or status)
                    self._write_bilibili_failed(failed_log_path, current_url or href, status, reason or status)
                    self._persist_bilibili_item(
                        item_data=item_data,
                        comments=[],
                        status=status if status != "unknown" else "failed",
                        reason=reason or status,
                        write_db=write_db,
                        db_path=db_path,
                        generate_summary=generate_summary and status == "unavailable",
                        summarizer=summarizer,
                        note_reports=note_reports,
                    )
                    print(f"page_status: {status} | reason: {reason or status}")
                    continue

                item_data = self.adapter.scrape_current_page_item()
                item_data["keyword"] = run_label
                item_data["search_title"] = title_hint
                item_data["status"] = "crawled"
                comments = self.adapter.scrape_current_page_comments(limit=max_comments)
                if not comments:
                    self.stats.no_comments_count += 1

                self.stats.new_items_count += 1
                self.stats.total_comments += len(comments)
                self.guard.record_success()
                self._write_bilibili_item_row(items_csv_path, item_data, status="crawled", reason="")
                self._write_bilibili_comment_rows(
                    comments_csv_path,
                    item_data=item_data,
                    comments=comments,
                    start_index=self.stats.total_comments - len(comments) + 1,
                )
                self._persist_bilibili_item(
                    item_data=item_data,
                    comments=comments,
                    status="crawled",
                    reason="",
                    write_db=write_db,
                    db_path=db_path,
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                    note_reports=note_reports,
                )
                print(f"page_status: crawled | 当前评论数：{len(comments)} | 累计评论数：{self.stats.total_comments}")

                if self.stats.new_items_count < success_target and self.stats.scanned_count < max_candidates:
                    print(f"等待 {interval_seconds} 秒后继续下一个视频...")
                    time.sleep(max(1, interval_seconds))
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，正在优雅退出并保留已采集结果。")
        finally:
            self.adapter.close()

        guard_stats = self.guard.stats()
        run_stats = {
            "platform": "bilibili",
            "scanned_candidates": self.stats.scanned_count,
            "crawled": self.stats.new_items_count,
            "unavailable": self.stats.unavailable_count,
            "no_comments": self.stats.no_comments_count,
            "failed": self.stats.failed_count,
            "skipped_non_note": self.stats.skipped_non_note_count,
            "skipped_author_profile": self.stats.skipped_author_profile_count,
            "total_comments": self.stats.total_comments,
            "clicked_profile": self.stats.clicked_profile,
            "fast_skip_count": self.stats.fast_skip_count,
            "unavailable_records": unavailable_records,
            "failed_records": failed_records,
            **guard_stats,
        }
        if generate_summary:
            write_stream_run_report(
                keyword=run_label,
                run_stats=run_stats,
                note_reports=note_reports,
                summary_dir=summary_dir,
            )

        print("\n" + "=" * 60)
        print("bilibili crawler engine test 采集结束")
        print(f"scanned_candidates：{run_stats['scanned_candidates']}")
        print(f"crawled：{run_stats['crawled']}")
        print(f"unavailable：{run_stats['unavailable']}")
        print(f"no_comments：{run_stats['no_comments']}")
        print(f"failed：{run_stats['failed']}")
        print(f"skipped_non_item：{run_stats['skipped_non_note']}")
        print(f"total_comments：{run_stats['total_comments']}")
        print(f"blocked_count：{guard_stats.get('blocked_count', 0)}")
        print(f"login_required_count：{guard_stats.get('login_required_count', 0)}")
        print(f"captcha_required_count：{guard_stats.get('captcha_required_count', 0)}")
        print(f"rate_limited_count：{guard_stats.get('rate_limited_count', 0)}")
        print(f"circuit_breaker_triggered：{guard_stats.get('circuit_breaker_triggered', False)}")
        print(f"视频 CSV：{items_csv_path}")
        print(f"评论 CSV：{comments_csv_path}")
        print(f"失败日志：{failed_log_path}")
        if generate_summary:
            print(f"总结目录：{summary_dir}")
        return run_stats

    def _should_prompt_for_bilibili_user_action(self, classification: Dict[str, object]) -> bool:
        status = str(classification.get("status") or "")
        if status not in {"login_required", "captcha_required"}:
            return False
        risk_result = classification.get("risk_result")
        if isinstance(risk_result, RiskDetectionResult):
            return risk_result.action == "require_user_action"
        return True

    def _minimal_bilibili_item(
        self,
        *,
        keyword: str,
        title: str,
        url: str,
        status: str,
        reason: str,
    ) -> Dict[str, object]:
        from platforms.bilibili import canonicalize_bilibili_video_url, extract_bilibili_content_id

        source_url = canonicalize_bilibili_video_url(url) or url
        return {
            "platform": "bilibili",
            "keyword": keyword,
            "content_id": extract_bilibili_content_id(source_url),
            "source_url": source_url,
            "clean_url": source_url,
            "raw_url": url,
            "title": title,
            "note_title": title,
            "body": "",
            "note_content": "",
            "author_name": "",
            "publish_time": "",
            "view_count": "",
            "like_count": "",
            "coin_count": "",
            "collect_count": "",
            "favorite_count": "",
            "comment_count_display": "",
            "share_count": "",
            "danmaku_count": "",
            "is_video": True,
            "status": status,
            "fail_reason": reason,
            "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_metadata": {"status": status, "reason": reason},
        }

    def _write_bilibili_item_row(self, csv_path: Path, item_data: Dict[str, object], *, status: str, reason: str) -> None:
        row = {
            "序号": self.stats.scanned_count,
            "平台": "bilibili",
            "内容ID": item_data.get("content_id") or "",
            "标题": item_data.get("title") or item_data.get("note_title") or "",
            "正文/简介": item_data.get("body") or item_data.get("note_content") or "",
            "作者": item_data.get("author_name") or "",
            "发布时间": item_data.get("publish_time") or "",
            "播放量": item_data.get("view_count") or "",
            "点赞数": item_data.get("like_count") or "",
            "投币数": item_data.get("coin_count") or "",
            "收藏数": item_data.get("favorite_count") or item_data.get("collect_count") or "",
            "评论数展示值": item_data.get("comment_count_display") or "",
            "弹幕数": item_data.get("danmaku_count") or "",
            "链接": item_data.get("source_url") or item_data.get("clean_url") or item_data.get("raw_url") or "",
            "页面状态": status,
            "失败原因": reason,
            "采集时间": item_data.get("crawled_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._append_csv_rows(csv_path, BILIBILI_ITEM_CSV_FIELDNAMES, [row])

    def _write_bilibili_comment_rows(
        self,
        csv_path: Path,
        *,
        item_data: Dict[str, object],
        comments: Sequence[Dict[str, object]],
        start_index: int,
    ) -> None:
        rows = []
        for index, comment in enumerate(comments, 1):
            rows.append(
                {
                    "序号": start_index + index - 1,
                    "平台": "bilibili",
                    "内容ID": item_data.get("content_id") or comment.get("content_id") or "",
                    "标题": item_data.get("title") or item_data.get("note_title") or "",
                    "链接": item_data.get("source_url") or item_data.get("clean_url") or "",
                    "评论用户": comment.get("username") or "",
                    "评论内容": comment.get("content") or "",
                    "发布时间": comment.get("publish_time") or "",
                    "点赞数": comment.get("likes") or "",
                    "采集时间": comment.get("crawled_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        self._append_csv_rows(csv_path, BILIBILI_COMMENT_CSV_FIELDNAMES, rows)

    def _append_csv_rows(self, csv_path: Path, fieldnames: Sequence[str], rows: Sequence[Dict[str, object]]) -> None:
        if not rows:
            return
        self._ensure_csv_file(csv_path, fieldnames)
        with csv_path.open("a", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writerows(rows)

    def _ensure_csv_file(self, csv_path: Path, fieldnames: Sequence[str]) -> None:
        if csv_path.exists():
            return
        with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()

    def _write_bilibili_failed(self, log_path: Path, url: str, status: str, reason: str) -> None:
        with log_path.open("a", encoding="utf-8") as file:
            file.write(f"{url}\t{status}\t{reason}\n")

    def _persist_bilibili_item(
        self,
        *,
        item_data: Dict[str, object],
        comments: Sequence[Dict[str, object]],
        status: str,
        reason: str,
        write_db: bool,
        db_path: str,
        generate_summary: bool,
        summarizer,
        note_reports: List[Dict[str, object]],
    ) -> None:
        if not write_db:
            return

        source_url = str(item_data.get("source_url") or item_data.get("clean_url") or item_data.get("raw_url") or "").strip()
        if not source_url:
            return
        raw_metadata = dict(item_data.get("raw_metadata") or {})
        raw_metadata.update(
            {
                "coin_count": item_data.get("coin_count") or "",
                "danmaku_count": item_data.get("danmaku_count") or "",
                "duration": item_data.get("duration") or "",
                "tags": item_data.get("tags") or [],
            }
        )
        item_id = upsert_content_item(
            platform="bilibili",
            content_id=str(item_data.get("content_id") or ""),
            keyword=str(item_data.get("keyword") or ""),
            clean_url=source_url,
            raw_url=str(item_data.get("raw_url") or source_url),
            source_url=source_url,
            search_title=str(item_data.get("search_title") or item_data.get("title") or ""),
            title=str(item_data.get("title") or item_data.get("note_title") or ""),
            note_title=str(item_data.get("note_title") or item_data.get("title") or ""),
            note_content=str(item_data.get("note_content") or item_data.get("body") or ""),
            body=str(item_data.get("body") or item_data.get("note_content") or ""),
            raw_note_text=str(item_data.get("raw_note_text") or item_data.get("body") or item_data.get("note_content") or ""),
            author_name=str(item_data.get("author_name") or ""),
            publish_time=str(item_data.get("publish_time") or ""),
            like_count=str(item_data.get("like_count") or ""),
            collect_count=str(item_data.get("collect_count") or item_data.get("favorite_count") or ""),
            favorite_count=str(item_data.get("favorite_count") or item_data.get("collect_count") or ""),
            view_count=str(item_data.get("view_count") or ""),
            comment_count_display=str(item_data.get("comment_count_display") or ""),
            share_count=str(item_data.get("share_count") or ""),
            image_count=0,
            is_video=True,
            status=status,
            fail_reason=reason,
            last_error=reason,
            blocked_reason=reason if status in GUARD_STOP_STATUSES else "",
            raw_metadata=raw_metadata,
            db_path=db_path,
        )
        if item_id and comments:
            save_content_comments(
                content_item_id=item_id,
                platform="bilibili",
                content_id=str(item_data.get("content_id") or ""),
                source_url=source_url,
                comments=comments,
                db_path=db_path,
            )

        if not item_id or not generate_summary:
            return

        summary_note = {
            **item_data,
            "id": item_id,
            "platform": "bilibili",
            "clean_url": source_url,
            "raw_url": str(item_data.get("raw_url") or source_url),
            "note_title": item_data.get("note_title") or item_data.get("title") or "",
            "note_content": item_data.get("note_content") or item_data.get("body") or "",
            "status": status,
            "fail_reason": reason,
        }
        if status == "unavailable":
            summary = summarizer.summarize_unavailable(summary_note, reason)
        elif not comments:
            summary = summarizer.summarize_no_comments(summary_note, reason or "页面无可见评论")
        else:
            summary = summarizer.summarize_note_and_comments(summary_note, comments, use_ai=summarizer.ai_enabled)
        save_summary(summary_note, summary, db_path)
        note_reports.append(
            {
                "note": summary_note,
                "summary": summary,
                "status": status,
                "reason": reason,
                "comment_count": len(comments),
            }
        )

    def run_xhs_test_stream(
        self,
        *,
        keyword: str,
        stream_config: Dict[str, object],
        output_config: Dict[str, object],
        ai_config: Dict[str, object] | None = None,
    ) -> Dict[str, object]:
        from notifier import ensure_directory, write_stream_run_report
        from search_stream_scrape import (
            COMMENT_CSV_FIELDNAMES,
            NOTE_CSV_FIELDNAMES,
            append_failure_row,
            append_rows,
            build_comment_csv_rows,
            build_note_csv_row,
            persist_note_result,
            should_skip_by_db,
            wait_before_next,
        )
        from summarizer import build_summarizer

        success_target = int(stream_config["success_target"])
        max_candidates = int(stream_config["max_candidates"])
        max_comments = int(stream_config["max_comments_per_note"])
        interval_seconds = int(stream_config["sleep_between_notes_seconds"])
        fast_skip_seconds = int(stream_config.get("fast_skip_sleep_seconds", 1))
        write_db = bool(stream_config.get("write_db", True))
        generate_summary = bool(stream_config.get("generate_summary", True))

        db_path = str(output_config.get("db_path") or self.db_path or "data/xhs_crawler.db")
        csv_dir = ensure_directory(output_config.get("csv_dir") or "outputs/csv")
        log_dir = ensure_directory(output_config.get("log_dir") or "outputs/logs")
        summary_dir = ensure_directory(output_config.get("summary_dir") or "outputs/summaries")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        comments_csv_path = Path(csv_dir) / f"stream_comments_{timestamp}.csv"
        notes_csv_path = Path(csv_dir) / f"stream_notes_{timestamp}.csv"
        failed_log_path = Path(log_dir) / f"stream_failed_{timestamp}.txt"
        summarizer = build_summarizer(ai_config or {"enabled": False})

        processed_signatures = set()
        processed_clean_urls = set()
        note_row_index = 0
        idle_scrolls = 0
        max_idle_scrolls = max(8, max_candidates * 2)
        note_reports: List[Dict[str, object]] = []
        failed_records: List[Dict[str, str]] = []
        unavailable_records: List[Dict[str, str]] = []

        try:
            search_url = self.adapter.open_search_page(keyword)
            search_risk = self.adapter.detect_current_risk()
            search_risk = _resume_after_xhs_user_action(self.adapter, search_risk)
            if search_risk.blocked and search_risk.status in GUARD_STOP_STATUSES:
                self._record_guard_stop(search_risk)
                print(f"search_page_status: {search_risk.status} | reason: {search_risk.reason}")
                return self._finalize_xhs_stream(
                    keyword=keyword,
                    note_reports=note_reports,
                    failed_records=failed_records,
                    unavailable_records=unavailable_records,
                    summary_dir=summary_dir,
                    generate_summary=generate_summary,
                    notes_csv_path=notes_csv_path,
                    comments_csv_path=comments_csv_path,
                    failed_log_path=failed_log_path,
                )

            self.adapter.wait_search_ready()
            search_risk = self.adapter.detect_current_risk()
            if search_risk.blocked and search_risk.status in GUARD_STOP_STATUSES:
                self._record_guard_stop(search_risk)
                print(f"search_page_status: {search_risk.status} | reason: {search_risk.reason}")
                return self._finalize_xhs_stream(
                    keyword=keyword,
                    note_reports=note_reports,
                    failed_records=failed_records,
                    unavailable_records=unavailable_records,
                    summary_dir=summary_dir,
                    generate_summary=generate_summary,
                    notes_csv_path=notes_csv_path,
                    comments_csv_path=comments_csv_path,
                    failed_log_path=failed_log_path,
                )

            while (
                self.stats.new_items_count < success_target
                and self.stats.scanned_count < max_candidates
                and idle_scrolls < max_idle_scrolls
            ):
                can_continue, stop_reason = self.guard.can_continue()
                if not can_continue:
                    print(f"platform_guard: stopped | reason: {stop_reason}")
                    break

                cards = self.adapter.list_search_candidates()
                next_card = None
                for card in cards:
                    signature = str(card.get("signature") or "")
                    if signature in processed_signatures:
                        continue
                    processed_signatures.add(signature)
                    self.stats.scanned_count += 1

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
                        self.stats.href_cards += 1
                    elif candidate_type == "clickable_card":
                        self.stats.clickable_cards += 1
                    else:
                        self.stats.skipped_non_note_count += 1
                        self.stats.fast_skip_count += 1
                        print(f"page_status: skipped_non_note | reason: {skip_reason or 'non_note_candidate'}")
                        if self.stats.scanned_count >= max_candidates:
                            break
                        continue

                    clean_hint = str(card.get("clean_url_hint") or "")
                    if clean_hint and clean_hint in processed_clean_urls:
                        continue
                    if write_db and clean_hint and should_skip_by_db(clean_hint, db_path):
                        processed_clean_urls.add(clean_hint)
                        self.stats.duplicate_count += 1
                        print(f"数据库中已存在终态笔记，跳过：{clean_hint}")
                        if self.stats.scanned_count >= max_candidates:
                            break
                        continue

                    next_card = card
                    break

                if self.stats.new_items_count >= success_target or self.stats.scanned_count >= max_candidates:
                    break

                if not next_card:
                    idle_scrolls += 1
                    print(f"当前没有新的可处理笔记卡片，滚动搜索页继续加载，第 {idle_scrolls}/{max_idle_scrolls} 次。")
                    self.adapter.scroll_search_results()
                    continue

                idle_scrolls = 0
                title_hint = str(next_card.get("title_text") or "")
                clean_hint = str(next_card.get("clean_url_hint") or "")
                href_hint = str(next_card.get("href") or "")
                print("\n" + "=" * 60)
                print(f"扫描候选 {self.stats.scanned_count}/{max_candidates}")
                print(f"当前成功笔记 {self.stats.new_items_count}/{success_target}")
                print(f"搜索页标题：{title_hint or '未提取到标题'}")
                print(f"搜索页 href：{href_hint}")
                print("=" * 60)

                self.guard.record_request()
                open_result = self._open_candidate_with_retry(next_card)
                if not open_result.get("ok"):
                    self._record_failed_click(
                        failed_log_path=failed_log_path,
                        failed_records=failed_records,
                        next_card=next_card,
                        href_hint=href_hint,
                        clean_hint=clean_hint,
                        reason=str(open_result.get("reason") or "click_failed"),
                    )
                    if self._should_wait_between_items(max_candidates, success_target):
                        wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                    continue

                navigation_mode = str(open_result.get("navigation_mode") or "same_tab")
                original_window = str(open_result.get("original_window") or "")
                raw_url = self.adapter.current_url()
                print(f"click_result_url: {raw_url or '无'}")

                if "/user/profile/" in raw_url:
                    self.stats.clicked_profile += 1
                    self.stats.skipped_author_profile_count += 1
                    self.stats.fast_skip_count += 1
                    print("page_status: skipped_author_profile | reason: clicked_author_profile")
                    self.adapter.return_to_search_page(search_url, navigation_mode, original_window)
                    if self._should_wait_between_items(max_candidates, success_target):
                        wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                    continue

                if "/explore/" not in raw_url:
                    if str(next_card.get("candidate_type") or "") == "clickable_card":
                        self.stats.clicked_without_href_failed += 1
                        self.stats.skipped_non_note_count += 1
                        self.stats.fast_skip_count += 1
                        print("page_status: skipped_non_note | reason: click_did_not_enter_explore")
                    else:
                        reason = "click_did_not_enter_explore"
                        self.stats.failed_count += 1
                        self.stats.fast_skip_count += 1
                        self.guard.record_failure(reason)
                        failed_records.append({"url": href_hint or clean_hint, "status": "failed_click", "reason": reason})
                        append_failure_row(failed_log_path, href_hint or clean_hint, "failed_click", reason)
                        print(f"page_status: failed_click | reason: {reason}")
                    self.adapter.return_to_search_page(search_url, navigation_mode, original_window)
                    if self._should_wait_between_items(max_candidates, success_target):
                        wait_before_next(interval_seconds, fast_skip=True, fast_skip_seconds=fast_skip_seconds)
                    continue

                if str(next_card.get("candidate_type") or "") == "clickable_card":
                    self.stats.clicked_without_href_success += 1

                clean_url = self._canonicalize(raw_url) or clean_hint
                if clean_url and clean_url in processed_clean_urls:
                    print(f"当前笔记已在本轮处理过，跳过：{clean_url}")
                    self.adapter.return_to_search_page(search_url, navigation_mode, original_window)
                    continue
                if write_db and clean_url and should_skip_by_db(clean_url, db_path):
                    processed_clean_urls.add(clean_url)
                    self.stats.duplicate_count += 1
                    print(f"数据库中已存在终态笔记，跳过：{clean_url}")
                    self.adapter.return_to_search_page(search_url, navigation_mode, original_window)
                    continue

                stop_after_current = self._process_open_note(
                    keyword=keyword,
                    title_hint=title_hint,
                    clean_url=clean_url,
                    raw_url=raw_url,
                    notes_csv_path=notes_csv_path,
                    comments_csv_path=comments_csv_path,
                    failed_log_path=failed_log_path,
                    failed_records=failed_records,
                    unavailable_records=unavailable_records,
                    note_reports=note_reports,
                    processed_clean_urls=processed_clean_urls,
                    note_row_index_ref=[note_row_index],
                    max_comments=max_comments,
                    write_db=write_db,
                    db_path=db_path,
                    generate_summary=generate_summary,
                    summarizer=summarizer,
                )
                note_row_index = len(note_reports)

                self.adapter.return_to_search_page(search_url, navigation_mode, original_window)
                if stop_after_current or self.guard.state.stopped:
                    print(f"platform_guard: stopped | reason: {self.guard.state.stop_reason}")
                    break
                if self._should_wait_between_items(max_candidates, success_target):
                    wait_before_next(interval_seconds, fast_skip_seconds=fast_skip_seconds)
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，正在优雅退出并保留已采集结果。")
        finally:
            self.adapter.close()

        return self._finalize_xhs_stream(
            keyword=keyword,
            note_reports=note_reports,
            failed_records=failed_records,
            unavailable_records=unavailable_records,
            summary_dir=summary_dir,
            generate_summary=generate_summary,
            notes_csv_path=notes_csv_path,
            comments_csv_path=comments_csv_path,
            failed_log_path=failed_log_path,
        )

    def _open_candidate_with_retry(self, next_card: Dict[str, object]) -> Dict[str, object]:
        try:
            return self.retry.run(lambda: self.adapter.open_candidate(next_card))
        except Exception as error:
            return {"ok": False, "reason": str(error) or error.__class__.__name__}

    def _record_failed_click(
        self,
        *,
        failed_log_path: Path,
        failed_records: List[Dict[str, str]],
        next_card: Dict[str, object],
        href_hint: str,
        clean_hint: str,
        reason: str,
    ) -> None:
        from search_stream_scrape import append_failure_row

        self.stats.failed_count += 1
        self.stats.fast_skip_count += 1
        self.guard.record_failure(reason)
        if str(next_card.get("candidate_type") or "") == "clickable_card":
            self.stats.clicked_without_href_failed += 1
        failed_records.append({"url": href_hint or clean_hint, "status": "failed_click", "reason": reason})
        append_failure_row(failed_log_path, href_hint or clean_hint, "failed_click", reason)
        print("click_result_url: 无")
        print(f"page_status: failed_click | reason: {reason}")

    def _process_open_note(
        self,
        *,
        keyword: str,
        title_hint: str,
        clean_url: str,
        raw_url: str,
        notes_csv_path: Path,
        comments_csv_path: Path,
        failed_log_path: Path,
        failed_records: List[Dict[str, str]],
        unavailable_records: List[Dict[str, str]],
        note_reports: List[Dict[str, object]],
        processed_clean_urls: set,
        note_row_index_ref: List[int],
        max_comments: int,
        write_db: bool,
        db_path: str,
        generate_summary: bool,
        summarizer,
    ) -> bool:
        from browser_utils import has_comment_area, is_available_note_page
        from search_stream_scrape import (
            COMMENT_CSV_FIELDNAMES,
            NOTE_CSV_FIELDNAMES,
            append_failure_row,
            append_rows,
            build_comment_csv_rows,
            build_note_csv_row,
            persist_note_result,
        )

        self.stats.detail_attempts += 1
        risk_result = self.adapter.detect_current_risk(expected_url_contains="/explore/")
        if risk_result.blocked and risk_result.status in GUARD_STOP_STATUSES:
            self._record_guard_stop(risk_result)
            reason = risk_result.reason or risk_result.status
            note_data = self._minimal_note_data(keyword, title_hint, clean_url or raw_url, raw_url)
            note_row_index_ref[0] += 1
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, risk_result.status, reason)])
            append_failure_row(failed_log_path, raw_url or clean_url, risk_result.status, reason)
            report = persist_note_result(
                db_enabled=write_db,
                db_path=db_path,
                note_data=note_data,
                status=risk_result.status,
                reason=reason,
                comments=[],
                generate_summary=generate_summary,
                summarizer=summarizer,
            )
            if report:
                note_reports.append(report)
            print(f"page_status: {risk_result.status} | reason: {reason} | action: {risk_result.action}")
            return True

        note_data = self.adapter.scrape_current_note_content(keyword=keyword, search_title=title_hint, raw_url=raw_url)
        note_data["clean_url"] = note_data.get("clean_url") or clean_url or raw_url
        note_data["raw_url"] = note_data.get("raw_url") or raw_url or clean_url
        if note_data.get("clean_url"):
            processed_clean_urls.add(str(note_data.get("clean_url")))

        unavailable, unavailable_reason = self.adapter.is_current_note_unavailable(debug_filename=f"debug_stream_{self.stats.scanned_count}.html")
        if unavailable:
            self.stats.unavailable_count += 1
            note_row_index_ref[0] += 1
            unavailable_records.append({"url": note_data.get("raw_url") or note_data.get("clean_url") or "", "status": "unavailable", "reason": unavailable_reason})
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, "unavailable", unavailable_reason)])
            append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "unavailable", unavailable_reason)
            report = persist_note_result(
                db_enabled=write_db,
                db_path=db_path,
                note_data=note_data,
                status="unavailable",
                reason=unavailable_reason,
                comments=[],
                generate_summary=generate_summary,
                summarizer=summarizer,
            )
            if report:
                note_reports.append(report)
            print(f"page_status: unavailable | reason: {unavailable_reason}")
            return False

        self.stats.detail_successes += 1
        comments: List[Dict[str, str]] = []
        comment_attempted = False
        current_status = "failed"
        current_reason = "unknown_failure"

        try:
            driver = self.adapter.scraper.driver
            available_page = is_available_note_page(driver)
            visible_comments = has_comment_area(driver)
            risk_result = self.adapter.detect_current_risk(expected_url_contains="/explore/")
            if risk_result.blocked and risk_result.status in GUARD_STOP_STATUSES:
                self._record_guard_stop(risk_result)
                current_status = risk_result.status
                current_reason = risk_result.reason or risk_result.status
            elif not available_page and not visible_comments:
                current_status = "failed"
                current_reason = "page_state_unknown"
            else:
                self.stats.comment_attempts += 1
                comment_attempted = True
                comments = self.adapter.scrape_current_comments(
                    note_url=str(note_data.get("clean_url") or note_data.get("raw_url") or ""),
                    max_comments=max_comments,
                )
                scraper = self.adapter.scraper
                current_status = scraper.last_status or ("crawled" if comments else "no_comments")
                current_reason = scraper.last_fail_reason or ""
        except KeyboardInterrupt:
            raise
        except Exception as error:
            comments = []
            current_status = "failed"
            current_reason = str(error).strip() or error.__class__.__name__

        note_row_index_ref[0] += 1
        if current_status == "crawled" and comments:
            self.stats.new_items_count += 1
            comment_rows = build_comment_csv_rows(
                start_index=self.stats.total_comments + 1,
                keyword=keyword,
                search_title=str(note_data.get("search_title") or ""),
                note_title=str(note_data.get("note_title") or ""),
                clean_url=str(note_data.get("clean_url") or ""),
                raw_url=str(note_data.get("raw_url") or ""),
                comments=comments,
            )
            append_rows(comments_csv_path, COMMENT_CSV_FIELDNAMES, comment_rows)
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, "crawled", "")])
            self.stats.total_comments += len(comment_rows)
            self.guard.record_success()
            print(f"page_status: crawled | 当前评论数：{len(comments)} | 累计评论数：{self.stats.total_comments}")
        elif current_status == "unavailable":
            self.stats.unavailable_count += 1
            current_reason = current_reason or "网页端不可访问或 App-only"
            unavailable_records.append({"url": note_data.get("raw_url") or note_data.get("clean_url") or "", "status": "unavailable", "reason": current_reason})
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, "unavailable", current_reason)])
            append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "unavailable", current_reason)
            print(f"page_status: unavailable | reason: {current_reason}")
        elif current_status == "no_comments":
            self.stats.no_comments_count += 1
            current_reason = current_reason or "未找到评论或解析失败"
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, "no_comments", current_reason)])
            append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "no_comments", current_reason)
            print(f"page_status: no_comments | reason: {current_reason}")
        elif current_status in GUARD_STOP_STATUSES:
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, current_status, current_reason)])
            append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", current_status, current_reason)
            print(f"page_status: {current_status} | reason: {current_reason}")
        else:
            self.stats.failed_count += 1
            self.guard.record_failure(current_reason)
            failed_records.append({"url": note_data.get("raw_url") or note_data.get("clean_url") or "", "status": "failed", "reason": current_reason})
            append_rows(notes_csv_path, NOTE_CSV_FIELDNAMES, [build_note_csv_row(note_row_index_ref[0], note_data, "failed", current_reason)])
            append_failure_row(failed_log_path, note_data.get("raw_url") or note_data.get("clean_url") or "", "failed", current_reason)
            print(f"page_status: failed | reason: {current_reason}")

        report = persist_note_result(
            db_enabled=write_db,
            db_path=db_path,
            note_data=note_data,
            status=current_status,
            reason=current_reason,
            comments=comments,
            generate_summary=generate_summary,
            summarizer=summarizer,
        )
        if report:
            report["detail_read"] = True
            report["comments_attempted"] = comment_attempted
            note_reports.append(report)
        return current_status in GUARD_STOP_STATUSES

    def _record_guard_stop(self, risk_result: RiskDetectionResult) -> None:
        self.guard.record_detection(risk_result)
        self.stats.record_risk(risk_result)

    def _minimal_note_data(self, keyword: str, title_hint: str, clean_url: str, raw_url: str) -> Dict[str, object]:
        return {
            "keyword": keyword,
            "search_title": title_hint,
            "clean_url": clean_url,
            "raw_url": raw_url or clean_url,
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

    def _canonicalize(self, url: str) -> str:
        from browser_utils import canonicalize_explore_url

        return canonicalize_explore_url(url) or ""

    def _should_wait_between_items(self, max_candidates: int, success_target: int) -> bool:
        return self.stats.scanned_count < max_candidates and self.stats.new_items_count < success_target

    def _finalize_xhs_stream(
        self,
        *,
        keyword: str,
        note_reports: Sequence[Dict[str, object]],
        failed_records: Sequence[Dict[str, str]],
        unavailable_records: Sequence[Dict[str, str]],
        summary_dir: Path,
        generate_summary: bool,
        notes_csv_path: Path,
        comments_csv_path: Path,
        failed_log_path: Path,
    ) -> Dict[str, object]:
        from notifier import write_stream_run_report

        collected_legacy_ids: list[int] = []
        for report in note_reports:
            note = report.get("note") or {}
            if not report.get("detail_read") or not isinstance(note, dict):
                continue
            try:
                legacy_id = int(note.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if legacy_id > 0 and legacy_id not in collected_legacy_ids:
                collected_legacy_ids.append(legacy_id)
        guard_stats = self.guard.stats()
        run_stats = {
            "platform": getattr(self.adapter, "platform_name", ""),
            "scanned_candidates": self.stats.scanned_count,
            "detail_attempts": self.stats.detail_attempts,
            "detail_successes": self.stats.detail_successes,
            "comment_attempts": self.stats.comment_attempts,
            "crawled": self.stats.new_items_count,
            "unavailable": self.stats.unavailable_count,
            "no_comments": self.stats.no_comments_count,
            "failed": self.stats.failed_count,
            "skipped_non_note": self.stats.skipped_non_note_count,
            "skipped_author_profile": self.stats.skipped_author_profile_count,
            "total_comments": self.stats.total_comments,
            "collected_legacy_ids": collected_legacy_ids,
            "href_cards": self.stats.href_cards,
            "clickable_cards": self.stats.clickable_cards,
            "clicked_without_href_success": self.stats.clicked_without_href_success,
            "clicked_without_href_failed": self.stats.clicked_without_href_failed,
            "clicked_profile": self.stats.clicked_profile,
            "fast_skip_count": self.stats.fast_skip_count,
            "unavailable_records": list(unavailable_records),
            "failed_records": list(failed_records),
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
        print("crawler engine test 采集结束")
        print(f"scanned_candidates：{run_stats['scanned_candidates']}")
        print(f"crawled：{run_stats['crawled']}")
        print(f"unavailable：{run_stats['unavailable']}")
        print(f"no_comments：{run_stats['no_comments']}")
        print(f"failed：{run_stats['failed']}")
        print(f"href_cards：{run_stats['href_cards']}")
        print(f"clickable_cards：{run_stats['clickable_cards']}")
        print(f"clicked_without_href_success：{run_stats['clicked_without_href_success']}")
        print(f"clicked_without_href_failed：{run_stats['clicked_without_href_failed']}")
        print(f"clicked_profile：{run_stats['clicked_profile']}")
        print(f"skipped_author_profile：{run_stats['skipped_author_profile']}")
        print(f"fast_skip_count：{run_stats['fast_skip_count']}")
        print(f"blocked_count：{guard_stats.get('blocked_count', 0)}")
        print(f"login_required_count：{guard_stats.get('login_required_count', 0)}")
        print(f"captcha_required_count：{guard_stats.get('captcha_required_count', 0)}")
        print(f"rate_limited_count：{guard_stats.get('rate_limited_count', 0)}")
        print(f"circuit_breaker_triggered：{guard_stats.get('circuit_breaker_triggered', False)}")
        print(f"skipped_non_note：{run_stats['skipped_non_note']}")
        print(f"total_comments：{run_stats['total_comments']}")
        print(f"笔记 CSV：{notes_csv_path}")
        print(f"评论 CSV：{comments_csv_path}")
        print(f"失败日志：{failed_log_path}")
        if generate_summary:
            print(f"总结目录：{summary_dir}")
        return run_stats
