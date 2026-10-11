#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看多平台采集队列 SQLite 数据库状态。默认读 ``data/xhs_crawler.db``。"""

from __future__ import annotations

import argparse
from pathlib import Path

from db import (
    DEFAULT_DB_PATH,
    get_connection,
    get_note_status_counts,
    get_recent_notes_by_status,
    init_db,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inspect content_items / comments queue database (multi-platform)."
    )
    parser.add_argument("--failed", action="store_true", help="仅查看 failed 条目")
    parser.add_argument("--unavailable", action="store_true", help="仅查看 unavailable 条目")
    parser.add_argument("--pending", action="store_true", help="仅查看 pending 条目")
    parser.add_argument(
        "--blocked",
        action="store_true",
        help="仅查看 blocked / login_required / captcha_required / rate_limited 条目",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="可选：仅限定某个平台的记录（如 xiaohongshu / weibo）",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"DB 文件路径，默认 {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="近期记录列表的最大条数，默认 10",
    )
    return parser.parse_args()


def resolve_status_filter(args) -> list[str]:
    if args.failed:
        return ["failed"]
    if args.unavailable:
        return ["unavailable"]
    if args.pending:
        return ["pending"]
    if args.blocked:
        return ["blocked", "login_required", "captcha_required", "rate_limited"]
    return ["failed", "unavailable", "blocked"]


def _format_db_label(db_path: str) -> str:
    try:
        return Path(db_path).resolve().name
    except OSError:
        return db_path


def _print_platform_breakdown(connection, platform_filter: str | None) -> None:
    if platform_filter:
        rows = connection.execute(
            "SELECT platform, status, COUNT(*) AS c FROM content_items "
            "WHERE platform = ? GROUP BY platform, status ORDER BY platform, status",
            (platform_filter,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT platform, status, COUNT(*) AS c FROM content_items "
            "GROUP BY platform, status ORDER BY platform, status"
        ).fetchall()

    if not rows:
        print("  暂无记录")
        return

    current_platform: str | None = None
    for row in rows:
        platform = row["platform"] or "<unknown>"
        status = row["status"]
        count = row["c"]
        if platform != current_platform:
            print(f"  [{platform}]")
            current_platform = platform
        print(f"    - {status}: {count}")


def _safe_count(connection, table_name: str) -> int:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    if not row:
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _print_crawl_queue_breakdown(connection) -> None:
    rows = connection.execute(
        """
        SELECT platform, status, COUNT(*) AS c
        FROM crawl_queue
        GROUP BY platform, status
        ORDER BY platform, status
        """
    ).fetchall()
    if not rows:
        print("  暂无记录")
        return
    for row in rows:
        print(f"  - {row['platform']}/{row['status']}: {row['c']}")


def _print_recent_content_items(connection, limit: int = 10) -> None:
    rows = connection.execute(
        """
        SELECT id, platform, status, title, note_title, source_url, clean_url, updated_at
        FROM content_items
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("  暂无记录")
        return
    for row in rows:
        title = row["title"] or row["note_title"] or ""
        url = row["source_url"] or row["clean_url"] or ""
        print(f"  - #{row['id']} [{row['platform']}/{row['status']}] {title}")
        print(f"    {url}")
        print(f"    updated_at: {row['updated_at']}")


def _print_recent_problem_items(connection, limit: int = 10) -> None:
    statuses = ("blocked", "failed", "rate_limited", "captcha_required", "login_required")
    placeholders = ",".join("?" for _ in statuses)
    rows = connection.execute(
        f"""
        SELECT id, platform, status, title, note_title, source_url, clean_url,
               fail_reason, blocked_reason, last_error, updated_at
        FROM content_items
        WHERE status IN ({placeholders})
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (*statuses, limit),
    ).fetchall()
    if not rows:
        print("  暂无记录")
        return
    for row in rows:
        title = row["title"] or row["note_title"] or ""
        reason = row["fail_reason"] or row["blocked_reason"] or row["last_error"] or ""
        url = row["source_url"] or row["clean_url"] or ""
        print(f"  - #{row['id']} [{row['platform']}/{row['status']}] {title}")
        print(f"    {url}")
        print(f"    reason: {reason}")
        print(f"    updated_at: {row['updated_at']}")


def main():
    args = parse_args()
    init_db(args.db)

    with get_connection(args.db) as connection:
        keyword_count = connection.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
        notes_count = _safe_count(connection, "notes")
        comments_count = _safe_count(connection, "comments")
        content_items_count = _safe_count(connection, "content_items")
        content_comments_count = _safe_count(connection, "content_comments")
        discovered_keywords_count = _safe_count(connection, "discovered_keywords")
        summaries_count = connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        platforms = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT platform FROM content_items ORDER BY platform"
            ).fetchall()
        ]
        platform_count_pairs = connection.execute(
            "SELECT platform, COUNT(*) AS c FROM content_items GROUP BY platform ORDER BY platform"
        ).fetchall()

        print("=" * 60)
        print(f"DB 状态概览 ({_format_db_label(args.db)})")
        print("=" * 60)
        print(f"keywords 数量: {keyword_count}")
        print(f"旧 notes 数量（兼容视图/表）: {notes_count}")
        print(f"旧 comments 数量: {comments_count}")
        print(f"content_items 数量: {content_items_count}")
        print(f"content_comments 数量: {content_comments_count}")
        print(f"discovered_keywords 数量: {discovered_keywords_count}")
        print(f"平台覆盖: {', '.join(platforms) or '无'}")
        print("content_items 按平台分组:")
        for row in platform_count_pairs:
            print(f"  - {row['platform']}: {row['c']}")
        print("content_items 按 (platform, status) 分组:")
        _print_platform_breakdown(connection, args.platform)
        print("crawl_queue 按 (platform, status) 分组:")
        _print_crawl_queue_breakdown(connection)

    overall_status_counts = get_note_status_counts(args.db)
    print("content_items 按总 status 分组:")
    for status in sorted(overall_status_counts):
        print(f"  - {status}: {overall_status_counts[status]}")
    print(f"comments 总数: {comments_count}")
    print(f"summaries 总数: {summaries_count}")
    print()

    with get_connection(args.db) as connection:
        print("最近 10 条 content_items:")
        _print_recent_content_items(connection, limit=10)
        print()
        print("最近 10 条 blocked / failed / rate_limited / captcha_required / login_required:")
        _print_recent_problem_items(connection, limit=10)
        print()

    filter_statuses = resolve_status_filter(args)
    recent_notes = get_recent_notes_by_status(filter_statuses, limit=args.limit, db_path=args.db)
    if args.platform:
        recent_notes = [n for n in recent_notes if (n.get("platform") or "") == args.platform]

    label = " / ".join(filter_statuses)
    if args.platform:
        label = f"{args.platform} | {label}"
    print(f"最近 {len(recent_notes)} 条 [{label}]:")

    if not recent_notes:
        print("  暂无记录")
        return

    for note in recent_notes:
        print(
            f"- [{note.get('platform') or '?'}/{note.get('status')}] "
            f"{note.get('clean_url') or note.get('raw_url')}"
        )
        print(f"  关键词: {note.get('keyword') or ''}")
        print(f"  标题: {note.get('title') or note.get('note_title') or ''}")
        print(f"  原因: {note.get('fail_reason') or ''}")
        print(f"  更新时间: {note.get('updated_at') or ''}")


if __name__ == "__main__":
    main()
