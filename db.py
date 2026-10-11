#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 数据库初始化与读写工具。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from dedupe import content_hashes, normalized_text_hash, simhash_distance, simhash_text

DEFAULT_DB_PATH = Path("data") / "xhs_crawler.db"
DEFAULT_NOTE_STATUSES = (
    "pending",
    "processing",
    "done",
    "collected",
    "available",
    "crawled",
    "failed",
    "unavailable",
    "no_comments",
    "skipped",
    "skipped_non_item",
    "skipped_non_note",
    "skipped_author_profile",
    "blocked",
    "login_required",
    "captcha_required",
    "rate_limited",
    "skipped_robots",
)
PRESERVE_ON_PENDING_STATUSES = {
    "processing",
    "crawled",
    "unavailable",
    "no_comments",
    "failed",
    "skipped_non_note",
    "skipped_non_item",
    "skipped_author_profile",
    "blocked",
    "login_required",
    "captcha_required",
    "rate_limited",
    "skipped_robots",
}
REQUEUE_LIKE_STATUSES = {"pending", "processing", "collected", "available"}

KEYWORDS_SQL = """
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_search_at TEXT
);
"""

CONTENT_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id INTEGER,
    keyword TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'xiaohongshu',
    content_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    search_title TEXT DEFAULT '',
    clean_url TEXT NOT NULL UNIQUE,
    raw_url TEXT NOT NULL DEFAULT '',
    note_title TEXT DEFAULT '',
    note_content TEXT DEFAULT '',
    body TEXT DEFAULT '',
    raw_note_text TEXT DEFAULT '',
    author_name TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    like_count TEXT DEFAULT '',
    collect_count TEXT DEFAULT '',
    favorite_count TEXT DEFAULT '',
    view_count TEXT DEFAULT '',
    comment_count_display TEXT DEFAULT '',
    share_count TEXT DEFAULT '',
    image_count INTEGER NOT NULL DEFAULT 0,
    is_video INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    fail_reason TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    blocked_reason TEXT DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT DEFAULT '',
    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
    normalized_text_hash TEXT NOT NULL DEFAULT '',
    simhash TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    last_attempt_at TEXT,
    crawled_at TEXT,
    updated_at TEXT NOT NULL,
    title TEXT DEFAULT '',
    FOREIGN KEY(keyword_id) REFERENCES keywords(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_items_clean_url
ON content_items(clean_url);

CREATE INDEX IF NOT EXISTS idx_content_items_status_discovered_at
ON content_items(status, discovered_at);
"""

# 依赖 platform / content_id 列的索引：必须在老 DB 补列之后才能创建。
CONTENT_ITEMS_PLATFORM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_content_items_platform_status
ON content_items(platform, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_items_platform_content_id
ON content_items(platform, content_id) WHERE content_id <> '';

CREATE INDEX IF NOT EXISTS idx_content_items_hash
ON content_items(normalized_text_hash);
"""

CONTENT_COMMENTS_SQL = """
CREATE TABLE IF NOT EXISTS content_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT '',
    content_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    comment_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    publish_time TEXT NOT NULL DEFAULT '',
    likes TEXT NOT NULL DEFAULT '',
    crawled_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
    normalized_text_hash TEXT NOT NULL DEFAULT '',
    simhash TEXT NOT NULL DEFAULT '',
    UNIQUE(content_item_id, content, publish_time, username),
    FOREIGN KEY(content_item_id) REFERENCES content_items(id)
);

CREATE INDEX IF NOT EXISTS idx_content_comments_platform_content
ON content_comments(platform, content_id);
"""

CONTENT_COMMENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_content_comments_hash
ON content_comments(normalized_text_hash);
"""

CRAWL_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    url_or_id TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    content_id TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT NOT NULL DEFAULT '',
    blocked_reason TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, url_or_id)
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_status_priority
ON crawl_queue(platform, status, priority, id);
"""

DISCOVERED_KEYWORDS_SQL = """
CREATE TABLE IF NOT EXISTS discovered_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'all',
    frequency INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    platforms_json TEXT NOT NULL DEFAULT '[]',
    source_count INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(term, platform)
);

CREATE INDEX IF NOT EXISTS idx_discovered_keywords_score
ON discovered_keywords(score DESC, frequency DESC);
"""

CRAWL_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL DEFAULT 'manual',
    mode TEXT NOT NULL DEFAULT 'test',
    status TEXT NOT NULL DEFAULT 'planned',
    plan_json TEXT NOT NULL DEFAULT '{}',
    stats_json TEXT NOT NULL DEFAULT '{}',
    audit_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_created
ON crawl_runs(created_at DESC);
"""

SITE_PROFILES_SQL = """
CREATE TABLE IF NOT EXISTS site_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL UNIQUE,
    supported_channels_json TEXT NOT NULL DEFAULT '[]',
    selector_observations_json TEXT NOT NULL DEFAULT '{}',
    last_failure_reason TEXT NOT NULL DEFAULT '',
    robots_status TEXT NOT NULL DEFAULT 'unknown',
    recommended_min_delay_seconds REAL NOT NULL DEFAULT 1.5,
    recommended_max_delay_seconds REAL NOT NULL DEFAULT 5.0,
    health_score REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active',
    debug_artifacts_json TEXT NOT NULL DEFAULT '[]',
    tuning_notes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEDUPE_GROUPS_SQL = """
CREATE TABLE IF NOT EXISTS dedupe_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL UNIQUE,
    primary_content_item_id INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT 'all',
    normalized_text_hash TEXT NOT NULL DEFAULT '',
    simhash TEXT NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    representative_title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(primary_content_item_id) REFERENCES content_items(id)
);

CREATE INDEX IF NOT EXISTS idx_dedupe_groups_hash
ON dedupe_groups(normalized_text_hash);
"""

SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
);
"""

# 兼容旧名称导入（外部代码若仍 import NOTES_SQL 不会报错）
NOTES_SQL = CONTENT_ITEMS_SQL

COMMENTS_SQL = """
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    note_url TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    publish_time TEXT NOT NULL DEFAULT '',
    likes TEXT NOT NULL DEFAULT '',
    crawled_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(note_id, content, publish_time, username),
    FOREIGN KEY(note_id) REFERENCES content_items(id)
);

CREATE INDEX IF NOT EXISTS idx_comments_note_id
ON comments(note_id);
"""

SUMMARIES_SQL = """
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL UNIQUE,
    note_url TEXT NOT NULL,
    keyword TEXT NOT NULL DEFAULT '',
    note_title TEXT NOT NULL DEFAULT '',
    summary_markdown TEXT NOT NULL,
    comment_count INTEGER NOT NULL DEFAULT 0,
    top_keywords_json TEXT NOT NULL DEFAULT '[]',
    focus_points_json TEXT NOT NULL DEFAULT '[]',
    representative_comments_json TEXT NOT NULL DEFAULT '[]',
    sentiment TEXT NOT NULL DEFAULT '中性',
    one_line_summary TEXT NOT NULL DEFAULT '',
    content_summary TEXT NOT NULL DEFAULT '',
    comment_summary TEXT NOT NULL DEFAULT '',
    ai_summary TEXT NOT NULL DEFAULT '',
    ai_provider TEXT NOT NULL DEFAULT '',
    ai_model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(note_id) REFERENCES content_items(id)
);
"""

CONTENT_ITEMS_REQUIRED_COLUMNS = {
    "platform": "TEXT NOT NULL DEFAULT 'xiaohongshu'",
    "content_id": "TEXT NOT NULL DEFAULT ''",
    "source_url": "TEXT NOT NULL DEFAULT ''",
    "search_title": "TEXT DEFAULT ''",
    "note_title": "TEXT DEFAULT ''",
    "note_content": "TEXT DEFAULT ''",
    "body": "TEXT DEFAULT ''",
    "raw_note_text": "TEXT DEFAULT ''",
    "author_name": "TEXT DEFAULT ''",
    "publish_time": "TEXT DEFAULT ''",
    "like_count": "TEXT DEFAULT ''",
    "collect_count": "TEXT DEFAULT ''",
    "favorite_count": "TEXT DEFAULT ''",
    "view_count": "TEXT DEFAULT ''",
    "comment_count_display": "TEXT DEFAULT ''",
    "share_count": "TEXT DEFAULT ''",
    "image_count": "INTEGER NOT NULL DEFAULT 0",
    "is_video": "INTEGER NOT NULL DEFAULT 0",
    "title": "TEXT DEFAULT ''",
    "last_error": "TEXT DEFAULT ''",
    "blocked_reason": "TEXT DEFAULT ''",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "next_retry_at": "TEXT DEFAULT ''",
    "raw_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    "normalized_text_hash": "TEXT NOT NULL DEFAULT ''",
    "simhash": "TEXT NOT NULL DEFAULT ''",
}

CRAWL_QUEUE_REQUIRED_COLUMNS = {
    "last_error": "TEXT NOT NULL DEFAULT ''",
    "blocked_reason": "TEXT NOT NULL DEFAULT ''",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "next_retry_at": "TEXT NOT NULL DEFAULT ''",
    "last_attempt_at": "TEXT NOT NULL DEFAULT ''",
}

CONTENT_COMMENTS_REQUIRED_COLUMNS = {
    "normalized_text_hash": "TEXT NOT NULL DEFAULT ''",
    "simhash": "TEXT NOT NULL DEFAULT ''",
}

# 兼容旧名称
NOTES_REQUIRED_COLUMNS = CONTENT_ITEMS_REQUIRED_COLUMNS

SUMMARIES_REQUIRED_COLUMNS = {
    "note_title": "TEXT NOT NULL DEFAULT ''",
    "content_summary": "TEXT NOT NULL DEFAULT ''",
    "comment_summary": "TEXT NOT NULL DEFAULT ''",
    "ai_summary": "TEXT NOT NULL DEFAULT ''",
    "ai_provider": "TEXT NOT NULL DEFAULT ''",
    "ai_model": "TEXT NOT NULL DEFAULT ''",
}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path) if db_path else DEFAULT_DB_PATH


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _get_existing_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_columns(
    connection: sqlite3.Connection,
    table_name: str,
    required_columns: Dict[str, str],
):
    existing_columns = _get_existing_columns(connection, table_name)
    for column_name, column_sql in required_columns.items():
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def _legacy_rename_notes_to_content_items(connection: sqlite3.Connection) -> bool:
    """将历史 ``notes`` 物理表重命名为 ``content_items``。

    幂等：当 ``content_items`` 已存在或 ``notes`` 不存在时直接返回 ``False``。
    必须在 ``CREATE TABLE IF NOT EXISTS content_items`` 之前调用，否则会出现
    新空表与旧数据表并存的尴尬局面。
    """
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "notes" not in table_names or "content_items" in table_names:
        return False
    # 先删除老索引，避免随后 CREATE 新索引时命名冲突或留下死索引
    for legacy_index in ("idx_notes_clean_url", "idx_notes_status_discovered_at"):
        try:
            connection.execute(f"DROP INDEX IF EXISTS {legacy_index}")
        except sqlite3.Error:
            pass
    connection.execute("ALTER TABLE notes RENAME TO content_items")
    return True


def extract_xhs_content_id(clean_url: str) -> str:
    """从小红书清洗后 URL 推导 ``content_id``，兼容 ``/explore/`` 与 ``/discovery/item/``。"""
    if not clean_url:
        return ""
    for marker in ("/explore/", "/discovery/item/"):
        if marker in clean_url:
            tail = clean_url.split(marker, 1)[1]
            return tail.split("?", 1)[0].split("/", 1)[0].strip()
    tail = clean_url.rstrip("/").rsplit("/", 1)[-1]
    return tail.split("?", 1)[0].strip()


def migrate_legacy_schema(connection: sqlite3.Connection) -> Dict[str, int]:
    """幂等地为 ``content_items`` 老数据回填 platform / content_id / source_url / body 等列。

    需要在所有 ``CREATE TABLE`` 与 ``_ensure_columns`` 之后调用。
    """
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "content_items" not in table_names:
        return {"backfilled": 0}

    cursor = connection.execute(
        """
        UPDATE content_items
        SET
            platform = CASE
                WHEN platform IS NULL OR platform = '' THEN 'xiaohongshu'
                ELSE platform
            END,
            content_id = CASE
                WHEN content_id IS NOT NULL AND content_id <> '' THEN content_id
                WHEN clean_url LIKE '%/explore/%' THEN
                    SUBSTR(clean_url, INSTR(clean_url, '/explore/') + 9)
                WHEN clean_url LIKE '%/discovery/item/%' THEN
                    SUBSTR(clean_url, INSTR(clean_url, '/discovery/item/') + 16)
                ELSE clean_url
            END,
            source_url = CASE
                WHEN source_url IS NOT NULL AND source_url <> '' THEN source_url
                WHEN raw_url IS NOT NULL AND raw_url <> '' THEN raw_url
                ELSE clean_url
            END,
            body = CASE
                WHEN body IS NOT NULL AND body <> '' THEN body
                ELSE COALESCE(note_content, '')
            END,
            favorite_count = CASE
                WHEN favorite_count IS NOT NULL AND favorite_count <> '' THEN favorite_count
                ELSE COALESCE(collect_count, '')
            END,
            raw_metadata_json = CASE
                WHEN raw_metadata_json IS NULL OR raw_metadata_json = '' THEN '{}'
                ELSE raw_metadata_json
            END
        WHERE
            (platform IS NULL OR platform = '')
            OR (content_id IS NULL OR content_id = '')
            OR (source_url IS NULL OR source_url = '')
            OR (body IS NULL OR body = '')
            OR (favorite_count IS NULL OR favorite_count = '')
            OR (raw_metadata_json IS NULL OR raw_metadata_json = '')
        """
    )
    backfilled = cursor.rowcount or 0

    # 回填完成后再建依赖新列的索引（幂等）。
    connection.executescript(CONTENT_ITEMS_PLATFORM_INDEX_SQL)
    return {"backfilled": backfilled}


def backfill_legacy_comments_to_content_comments(connection: sqlite3.Connection) -> int:
    """将旧 ``comments`` 表可重复回填到 ``content_comments``。"""
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "comments" not in table_names or "content_comments" not in table_names:
        return 0
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO content_comments (
            content_item_id, platform, content_id, source_url, comment_id,
            username, content, raw_text, publish_time, likes,
            crawled_at, created_at, raw_metadata_json
        )
        SELECT
            c.note_id,
            COALESCE(i.platform, 'xiaohongshu'),
            COALESCE(i.content_id, ''),
            COALESCE(i.source_url, i.clean_url, c.note_url),
            '',
            COALESCE(c.username, ''),
            COALESCE(c.content, ''),
            COALESCE(c.raw_text, ''),
            COALESCE(c.publish_time, ''),
            COALESCE(c.likes, ''),
            COALESCE(c.crawled_at, ''),
            COALESCE(c.created_at, ''),
            '{}'
        FROM comments c
        LEFT JOIN content_items i ON i.id = c.note_id
        WHERE COALESCE(c.content, '') <> ''
        """
    )
    return cursor.rowcount or 0


def backfill_dedupe_fields(connection: sqlite3.Connection) -> Dict[str, int]:
    """Backfill exact hash and SimHash values for existing rows."""
    item_updates = 0
    comment_updates = 0
    item_rows = connection.execute(
        """
        SELECT id, title, note_title, body, note_content, author_name, publish_time
        FROM content_items
        WHERE normalized_text_hash = '' OR simhash = ''
        """
    ).fetchall()
    for row in item_rows:
        text_hash, simhash_value = content_hashes(
            title=row["title"] or row["note_title"] or "",
            body=row["body"] or row["note_content"] or "",
            author_name=row["author_name"] or "",
            publish_time=row["publish_time"] or "",
        )
        connection.execute(
            """
            UPDATE content_items
            SET normalized_text_hash = ?, simhash = ?
            WHERE id = ?
            """,
            (text_hash, simhash_value, row["id"]),
        )
        item_updates += 1

    comment_rows = connection.execute(
        """
        SELECT id, username, content, raw_text, publish_time
        FROM content_comments
        WHERE normalized_text_hash = '' OR simhash = ''
        """
    ).fetchall()
    for row in comment_rows:
        text = " ".join(
            str(row[field] or "").strip()
            for field in ("username", "content", "raw_text", "publish_time")
            if str(row[field] or "").strip()
        )
        connection.execute(
            """
            UPDATE content_comments
            SET normalized_text_hash = ?, simhash = ?
            WHERE id = ?
            """,
            (normalized_text_hash(text), simhash_text(text), row["id"]),
        )
        comment_updates += 1

    refresh_dedupe_groups(connection)
    return {"content_items": item_updates, "content_comments": comment_updates}


def refresh_dedupe_groups(connection: sqlite3.Connection, *, include_near: bool = True) -> int:
    """Record exact duplicate groups based on normalized text hashes."""
    timestamp = now_str()
    saved = 0
    rows = connection.execute(
        """
        SELECT normalized_text_hash,
               MIN(id) AS primary_content_item_id,
               COUNT(*) AS item_count,
               MIN(simhash) AS simhash,
               MIN(platform) AS platform,
               MIN(COALESCE(NULLIF(title, ''), NULLIF(note_title, ''), clean_url)) AS representative_title
        FROM content_items
        WHERE normalized_text_hash <> ''
        GROUP BY normalized_text_hash
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in rows:
        group_key = f"exact:{row['normalized_text_hash']}"
        connection.execute(
            """
            INSERT INTO dedupe_groups (
                group_key, primary_content_item_id, platform, normalized_text_hash,
                simhash, item_count, representative_title, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_key) DO UPDATE SET
                primary_content_item_id = excluded.primary_content_item_id,
                platform = excluded.platform,
                normalized_text_hash = excluded.normalized_text_hash,
                simhash = excluded.simhash,
                item_count = excluded.item_count,
                representative_title = excluded.representative_title,
                updated_at = excluded.updated_at
            """,
            (
                group_key,
                int(row["primary_content_item_id"] or 0),
                row["platform"] or "all",
                row["normalized_text_hash"] or "",
                row["simhash"] or "",
                int(row["item_count"] or 0),
                row["representative_title"] or "",
                timestamp,
                timestamp,
            ),
        )
        saved += 1
    if not include_near:
        return saved
    near_rows = connection.execute(
        """
        SELECT id, platform, simhash,
               COALESCE(NULLIF(title, ''), NULLIF(note_title, ''), clean_url) AS representative_title
        FROM content_items
        WHERE simhash <> ''
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    visited: set[int] = set()
    for index, row in enumerate(near_rows):
        row_id = int(row["id"])
        if row_id in visited:
            continue
        group = [row]
        for other in near_rows[index + 1 :]:
            other_id = int(other["id"])
            if other_id in visited:
                continue
            if simhash_distance(row["simhash"], other["simhash"]) <= 3:
                group.append(other)
        if len(group) <= 1:
            continue
        primary_id = min(int(item["id"]) for item in group)
        primary = next(item for item in group if int(item["id"]) == primary_id)
        group_key = f"near:{primary_id}:{primary['simhash']}"
        visited.update(int(item["id"]) for item in group)
        connection.execute(
            """
            INSERT INTO dedupe_groups (
                group_key, primary_content_item_id, platform, normalized_text_hash,
                simhash, item_count, representative_title, created_at, updated_at
            )
            VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(group_key) DO UPDATE SET
                primary_content_item_id = excluded.primary_content_item_id,
                platform = excluded.platform,
                simhash = excluded.simhash,
                item_count = excluded.item_count,
                representative_title = excluded.representative_title,
                updated_at = excluded.updated_at
            """,
            (
                group_key,
                primary_id,
                primary["platform"] or "all",
                primary["simhash"] or "",
                len(group),
                primary["representative_title"] or "",
                timestamp,
                timestamp,
            ),
        )
        saved += 1
    return saved


def _ensure_notes_compat_view(connection: sqlite3.Connection):
    """为只读旧 SQL 保留 ``notes`` 名称。写入请继续使用兼容 API。"""
    relation = connection.execute(
        "SELECT type FROM sqlite_master WHERE name = 'notes'"
    ).fetchone()
    if relation:
        return
    connection.execute("CREATE VIEW IF NOT EXISTS notes AS SELECT * FROM content_items")


def init_db(db_path: str | Path | None = None):
    with get_connection(db_path) as connection:
        # 1) 物理表重命名（必须在 CREATE TABLE 之前）
        _legacy_rename_notes_to_content_items(connection)
        # 2) 创建/确保表与索引存在
        connection.executescript(KEYWORDS_SQL)
        connection.executescript(CONTENT_ITEMS_SQL)
        connection.executescript(COMMENTS_SQL)
        connection.executescript(CONTENT_COMMENTS_SQL)
        connection.executescript(CRAWL_QUEUE_SQL)
        connection.executescript(DISCOVERED_KEYWORDS_SQL)
        connection.executescript(CRAWL_RUNS_SQL)
        connection.executescript(SITE_PROFILES_SQL)
        connection.executescript(DEDUPE_GROUPS_SQL)
        connection.executescript(SCHEMA_MIGRATIONS_SQL)
        connection.executescript(SUMMARIES_SQL)
        # 3) 补齐缺失列
        _ensure_columns(connection, "content_items", CONTENT_ITEMS_REQUIRED_COLUMNS)
        _ensure_columns(connection, "content_comments", CONTENT_COMMENTS_REQUIRED_COLUMNS)
        _ensure_columns(connection, "crawl_queue", CRAWL_QUEUE_REQUIRED_COLUMNS)
        _ensure_columns(connection, "summaries", SUMMARIES_REQUIRED_COLUMNS)
        connection.executescript(CONTENT_COMMENTS_INDEX_SQL)
        # 4) 回填老数据
        migrate_legacy_schema(connection)
        backfill_legacy_comments_to_content_comments(connection)
        backfill_dedupe_fields(connection)
        _ensure_notes_compat_view(connection)
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version, description, applied_at)
            VALUES (?, ?, ?)
            """,
            ("2026-04-29-content-framework", "add generic content tables and crawl guard state", now_str()),
        )
        connection.commit()


def sync_keywords(keywords: Sequence[str], db_path: str | Path | None = None):
    normalized_keywords = [keyword.strip() for keyword in keywords if keyword and keyword.strip()]
    timestamp = now_str()

    with get_connection(db_path) as connection:
        if normalized_keywords:
            placeholders = ",".join("?" for _ in normalized_keywords)
            connection.execute(
                f"UPDATE keywords SET enabled = 0, updated_at = ? WHERE keyword NOT IN ({placeholders})",
                [timestamp, *normalized_keywords],
            )
        else:
            connection.execute("UPDATE keywords SET enabled = 0, updated_at = ?", (timestamp,))

        for keyword in normalized_keywords:
            connection.execute(
                """
                INSERT INTO keywords (keyword, enabled, created_at, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(keyword) DO UPDATE SET
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (keyword, timestamp, timestamp),
            )
        connection.commit()


def get_enabled_keywords(db_path: str | Path | None = None) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM keywords WHERE enabled = 1 ORDER BY keyword ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def touch_keyword_search(keyword: str, db_path: str | Path | None = None):
    timestamp = now_str()
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE keywords SET last_search_at = ?, updated_at = ? WHERE keyword = ?",
            (timestamp, timestamp, keyword),
        )
        connection.commit()


def upsert_content_item(
    *,
    platform: str = "xiaohongshu",
    content_id: str = "",
    keyword: str = "",
    clean_url: str,
    raw_url: str = "",
    source_url: str = "",
    search_title: str = "",
    title: str = "",
    note_title: str = "",
    note_content: str = "",
    body: str = "",
    raw_note_text: str = "",
    author_name: str = "",
    publish_time: str = "",
    like_count: str = "",
    collect_count: str = "",
    favorite_count: str = "",
    view_count: str = "",
    comment_count_display: str = "",
    share_count: str = "",
    image_count: int = 0,
    is_video: bool | int = False,
    status: str = "pending",
    fail_reason: str = "",
    last_error: str = "",
    blocked_reason: str = "",
    raw_metadata: Optional[Dict[str, object]] = None,
    db_path: str | Path | None = None,
) -> int:
    """统一 upsert 内容条目（多平台规范入口）。返回行 id；若失败返回 0。

    - ``platform`` 缺省 ``xiaohongshu``，配合 ``(platform, content_id)`` 唯一索引去重。
    - ``content_id`` 留空时按平台规则从 ``clean_url`` 推导（XHS 走 ``extract_xhs_content_id``）。
    - 状态保护：旧行处于终态/风控态时，新 pending/processing 不会覆盖。
    """
    timestamp = now_str()
    normalized_status = status if status in DEFAULT_NOTE_STATUSES else "pending"
    note_title_value = (note_title or title or "").strip()
    legacy_title = note_title_value or (search_title or "").strip()
    fail_reason_value = (fail_reason or "")[:1000]
    image_count_value = int(image_count or 0)
    is_video_value = 1 if bool(is_video) else 0

    platform_value = (platform or "xiaohongshu").strip().lower()
    clean_url_value = (clean_url or source_url or raw_url or "").strip()
    source_url_value = (source_url or raw_url or clean_url_value).strip()
    if not clean_url_value:
        return 0
    body_value = (body or note_content or "").strip()

    content_id_value = (content_id or "").strip()
    if not content_id_value:
        if platform_value == "xiaohongshu":
            content_id_value = extract_xhs_content_id(clean_url_value)
        else:
            tail = clean_url_value.rstrip("/").rsplit("/", 1)[-1]
            content_id_value = tail.split("?", 1)[0].strip()

    favorite_count_value = (favorite_count or collect_count or "").strip()
    normalized_text_hash_value, simhash_value = content_hashes(
        title=legacy_title,
        body=body_value or note_content,
        author_name=author_name,
        publish_time=publish_time,
    )

    if raw_metadata:
        try:
            raw_metadata_json = json.dumps(raw_metadata, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_metadata_json = "{}"
    else:
        raw_metadata_json = "{}"

    with get_connection(db_path) as connection:
        keyword_id = None
        if keyword:
            keyword_row = connection.execute(
                "SELECT id FROM keywords WHERE keyword = ?",
                (keyword,),
            ).fetchone()
            keyword_id = keyword_row["id"] if keyword_row else None

        existing_row = connection.execute(
            "SELECT id, status, fail_reason FROM content_items WHERE clean_url = ?",
            (clean_url_value,),
        ).fetchone()

        # 同一条内容可能有多种 URL 写法（如 b23.tv 短链、/discovery/item/ 与 /explore/）。
        # 这里先对齐 (platform, content_id) 唯一索引，避免 INSERT 只声明 ON CONFLICT(clean_url)
        # 时触发 IntegrityError 中断采集。
        if content_id_value:
            content_id_row = connection.execute(
                "SELECT id, clean_url, status, fail_reason FROM content_items WHERE platform = ? AND content_id = ?",
                (platform_value, content_id_value),
            ).fetchone()
            if content_id_row:
                if not existing_row:
                    # 该内容已用别的 clean_url 入库：复用旧行的 clean_url 走 upsert 更新。
                    clean_url_value = content_id_row["clean_url"]
                    existing_row = content_id_row
                elif int(content_id_row["id"]) != int(existing_row["id"]):
                    # (platform, content_id) 已被另一行占用：本行保留原 content_id，不再改写。
                    content_id_value = ""

        status_to_store = normalized_status
        fail_reason_to_store = fail_reason_value
        last_error_value = (last_error or fail_reason_value or "")[:1000]
        blocked_reason_value = (blocked_reason or (fail_reason_value if normalized_status in {"blocked", "login_required", "captcha_required", "rate_limited"} else ""))[:1000]

        if existing_row:
            existing_status = existing_row["status"]
            if existing_status in PRESERVE_ON_PENDING_STATUSES and normalized_status in REQUEUE_LIKE_STATUSES:
                status_to_store = existing_status
                fail_reason_to_store = existing_row["fail_reason"] or ""

        connection.execute(
            """
            INSERT INTO content_items (
                keyword_id, keyword, platform, content_id, source_url,
                search_title, clean_url, raw_url, note_title,
                note_content, body, raw_note_text, author_name, publish_time, like_count,
                collect_count, favorite_count, view_count, comment_count_display, share_count,
                image_count, is_video, status, fail_reason, last_error, blocked_reason, raw_metadata_json,
                normalized_text_hash, simhash,
                discovered_at, updated_at, title
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clean_url) DO UPDATE SET
                keyword_id = excluded.keyword_id,
                keyword = excluded.keyword,
                platform = CASE
                    WHEN excluded.platform <> '' THEN excluded.platform
                    ELSE content_items.platform
                END,
                content_id = CASE
                    WHEN excluded.content_id <> '' THEN excluded.content_id
                    ELSE content_items.content_id
                END,
                source_url = CASE
                    WHEN excluded.source_url <> '' THEN excluded.source_url
                    ELSE content_items.source_url
                END,
                search_title = CASE
                    WHEN excluded.search_title <> '' THEN excluded.search_title
                    ELSE content_items.search_title
                END,
                raw_url = CASE
                    WHEN excluded.raw_url <> '' THEN excluded.raw_url
                    ELSE content_items.raw_url
                END,
                note_title = CASE
                    WHEN excluded.note_title <> '' THEN excluded.note_title
                    ELSE content_items.note_title
                END,
                note_content = CASE
                    WHEN excluded.note_content <> '' THEN excluded.note_content
                    ELSE content_items.note_content
                END,
                body = CASE
                    WHEN excluded.body <> '' THEN excluded.body
                    ELSE content_items.body
                END,
                raw_note_text = CASE
                    WHEN excluded.raw_note_text <> '' THEN excluded.raw_note_text
                    ELSE content_items.raw_note_text
                END,
                author_name = CASE
                    WHEN excluded.author_name <> '' THEN excluded.author_name
                    ELSE content_items.author_name
                END,
                publish_time = CASE
                    WHEN excluded.publish_time <> '' THEN excluded.publish_time
                    ELSE content_items.publish_time
                END,
                like_count = CASE
                    WHEN excluded.like_count <> '' THEN excluded.like_count
                    ELSE content_items.like_count
                END,
                collect_count = CASE
                    WHEN excluded.collect_count <> '' THEN excluded.collect_count
                    ELSE content_items.collect_count
                END,
                favorite_count = CASE
                    WHEN excluded.favorite_count <> '' THEN excluded.favorite_count
                    ELSE content_items.favorite_count
                END,
                view_count = CASE
                    WHEN excluded.view_count <> '' THEN excluded.view_count
                    ELSE content_items.view_count
                END,
                comment_count_display = CASE
                    WHEN excluded.comment_count_display <> '' THEN excluded.comment_count_display
                    ELSE content_items.comment_count_display
                END,
                share_count = CASE
                    WHEN excluded.share_count <> '' THEN excluded.share_count
                    ELSE content_items.share_count
                END,
                image_count = CASE
                    WHEN excluded.image_count > 0 THEN excluded.image_count
                    ELSE content_items.image_count
                END,
                is_video = CASE
                    WHEN excluded.is_video > 0 THEN excluded.is_video
                    ELSE content_items.is_video
                END,
                status = CASE
                    WHEN content_items.status IN (
                        'processing', 'crawled', 'done', 'unavailable', 'no_comments',
                        'failed', 'skipped', 'skipped_non_item', 'skipped_non_note', 'skipped_author_profile',
                        'skipped_robots', 'blocked', 'login_required', 'captcha_required',
                        'rate_limited'
                    )
                         AND excluded.status IN ('pending', 'processing', 'collected', 'available')
                        THEN content_items.status
                    WHEN content_items.status IN ('crawled', 'done')
                         AND excluded.status IN (
                             'blocked', 'login_required', 'captcha_required',
                             'rate_limited', 'skipped_robots'
                         )
                        THEN content_items.status
                    ELSE excluded.status
                END,
                fail_reason = CASE
                    WHEN content_items.status IN (
                        'processing', 'crawled', 'done', 'unavailable', 'no_comments',
                        'failed', 'skipped', 'skipped_non_item', 'skipped_non_note', 'skipped_author_profile',
                        'skipped_robots', 'blocked', 'login_required', 'captcha_required',
                        'rate_limited'
                    )
                         AND excluded.status IN ('pending', 'processing', 'collected', 'available')
                        THEN content_items.fail_reason
                    WHEN content_items.status IN ('crawled', 'done')
                         AND excluded.status IN (
                             'blocked', 'login_required', 'captcha_required',
                             'rate_limited', 'skipped_robots'
                         )
                        THEN content_items.fail_reason
                    ELSE excluded.fail_reason
                END,
                last_error = CASE
                    WHEN excluded.last_error <> '' THEN excluded.last_error
                    ELSE content_items.last_error
                END,
                blocked_reason = CASE
                    WHEN excluded.blocked_reason <> '' THEN excluded.blocked_reason
                    ELSE content_items.blocked_reason
                END,
                raw_metadata_json = CASE
                    WHEN excluded.raw_metadata_json <> '{}' AND excluded.raw_metadata_json <> ''
                        THEN excluded.raw_metadata_json
                    ELSE content_items.raw_metadata_json
                END,
                normalized_text_hash = CASE
                    WHEN excluded.normalized_text_hash <> '' THEN excluded.normalized_text_hash
                    ELSE content_items.normalized_text_hash
                END,
                simhash = CASE
                    WHEN excluded.simhash <> '' THEN excluded.simhash
                    ELSE content_items.simhash
                END,
                title = CASE
                    WHEN excluded.title <> '' THEN excluded.title
                    ELSE content_items.title
                END,
                updated_at = excluded.updated_at
            """,
            (
                keyword_id,
                keyword or "",
                platform_value,
                content_id_value,
                source_url_value,
                (search_title or "").strip(),
                clean_url_value,
                (raw_url or clean_url_value).strip(),
                note_title_value,
                (note_content or "").strip(),
                body_value,
                (raw_note_text or "").strip(),
                (author_name or "").strip(),
                (publish_time or "").strip(),
                (like_count or "").strip(),
                (collect_count or "").strip(),
                favorite_count_value,
                (view_count or "").strip(),
                (comment_count_display or "").strip(),
                (share_count or "").strip(),
                image_count_value,
                is_video_value,
                status_to_store,
                fail_reason_to_store,
                last_error_value,
                blocked_reason_value,
                raw_metadata_json,
                normalized_text_hash_value,
                simhash_value,
                timestamp,
                timestamp,
                legacy_title,
            ),
        )
        row = connection.execute(
            "SELECT id FROM content_items WHERE clean_url = ?",
            (clean_url_value,),
        ).fetchone()
        if normalized_text_hash_value:
            refresh_dedupe_groups(connection, include_near=False)
        connection.commit()
        return int(row["id"]) if row else 0


def upsert_note(**kwargs) -> int:
    """向后兼容包装：旧调用未传 ``platform``/``content_id`` 时默认走小红书。"""
    kwargs.setdefault("platform", "xiaohongshu")
    return upsert_content_item(**kwargs)


def save_search_results(
    keyword: str,
    results: Iterable[Dict[str, str]],
    db_path: str | Path | None = None,
) -> int:
    count = 0
    for item in results:
        clean_url = (item.get("clean_url") or "").strip()
        raw_url = (item.get("raw_url") or clean_url).strip()
        if not clean_url or not raw_url:
            continue
        upsert_note(
            keyword=keyword,
            clean_url=clean_url,
            raw_url=raw_url,
            search_title=(item.get("title_text") or item.get("标题文本") or "").strip(),
            note_title=(item.get("note_title") or "").strip(),
            note_content=(item.get("note_content") or "").strip(),
            raw_note_text=(item.get("raw_note_text") or "").strip(),
            author_name=(item.get("author_name") or "").strip(),
            publish_time=(item.get("publish_time") or "").strip(),
            like_count=(item.get("like_count") or "").strip(),
            collect_count=(item.get("collect_count") or "").strip(),
            comment_count_display=(item.get("comment_count_display") or "").strip(),
            share_count=(item.get("share_count") or "").strip(),
            image_count=int(item.get("image_count") or 0),
            is_video=bool(item.get("is_video")),
            status=(item.get("status") or "pending").strip(),
            fail_reason=(item.get("fail_reason") or "").strip(),
            db_path=db_path,
        )
        count += 1
    return count


def get_note_by_clean_url(
    clean_url: str,
    db_path: str | Path | None = None,
) -> Dict[str, object] | None:
    if not clean_url:
        return None
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM content_items WHERE clean_url = ?",
            (clean_url,),
        ).fetchone()
    return dict(row) if row else None


def claim_pending_notes(limit: int = 10, db_path: str | Path | None = None) -> List[Dict[str, object]]:
    timestamp = now_str()
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM content_items
            WHERE status = 'pending'
            ORDER BY discovered_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        if not rows:
            return []

        note_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in note_ids)
        connection.execute(
            f"""
            UPDATE content_items
            SET status = 'processing',
                last_attempt_at = ?,
                updated_at = ?,
                fail_reason = ''
            WHERE id IN ({placeholders})
            """,
            [timestamp, timestamp, *note_ids],
        )
        connection.commit()

    refreshed_rows = []
    with get_connection(db_path) as connection:
        for note_id in note_ids:
            row = connection.execute("SELECT * FROM content_items WHERE id = ?", (note_id,)).fetchone()
            if row:
                refreshed_rows.append(dict(row))
    return refreshed_rows


def update_note_status(
    note_id: int,
    status: str,
    reason: str = "",
    *,
    last_error: str = "",
    blocked_reason: str = "",
    db_path: str | Path | None = None,
):
    normalized_status = status if status in DEFAULT_NOTE_STATUSES else "failed"
    timestamp = now_str()
    fail_reason = (reason or "")[:1000]
    crawled_at = timestamp if normalized_status == "crawled" else None
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE content_items
            SET status = ?,
                fail_reason = ?,
                last_error = ?,
                blocked_reason = ?,
                crawled_at = CASE
                    WHEN ? IS NOT NULL THEN ?
                    ELSE crawled_at
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalized_status,
                fail_reason,
                (last_error or fail_reason)[:1000],
                (blocked_reason or (fail_reason if normalized_status in {"blocked", "login_required", "captcha_required", "rate_limited"} else ""))[:1000],
                crawled_at,
                crawled_at,
                timestamp,
                note_id,
            ),
        )
        connection.commit()


def mark_note_crawled(note_id: int, db_path: str | Path | None = None):
    update_note_status(note_id, "crawled", reason="", db_path=db_path)


def mark_note_failed(note_id: int, reason: str, db_path: str | Path | None = None):
    update_note_status(note_id, "failed", reason=reason or "未知错误", db_path=db_path)


def mark_note_unavailable(note_id: int, reason: str, db_path: str | Path | None = None):
    update_note_status(note_id, "unavailable", reason=reason or "网页端不可访问或 App-only", db_path=db_path)


def mark_note_no_comments(note_id: int, reason: str = "", db_path: str | Path | None = None):
    update_note_status(note_id, "no_comments", reason=reason or "未找到评论或解析失败", db_path=db_path)


def mark_note_skipped_non_note(note_id: int, reason: str = "", db_path: str | Path | None = None):
    update_note_status(note_id, "skipped_non_note", reason=reason or "non_note_candidate", db_path=db_path)


def mark_note_skipped_author_profile(note_id: int, reason: str = "", db_path: str | Path | None = None):
    update_note_status(note_id, "skipped_author_profile", reason=reason or "clicked_author_profile", db_path=db_path)


def mark_note_blocked(note_id: int, status: str = "blocked", reason: str = "", db_path: str | Path | None = None):
    final_status = status if status in DEFAULT_NOTE_STATUSES else "blocked"
    update_note_status(
        note_id,
        final_status,
        reason=reason or final_status,
        last_error=reason or final_status,
        blocked_reason=reason or final_status,
        db_path=db_path,
    )


def save_comments(
    note_id: int,
    note_url: str,
    comments: Sequence[Dict[str, str]],
    db_path: str | Path | None = None,
):
    timestamp = now_str()
    with get_connection(db_path) as connection:
        item_row = connection.execute(
            "SELECT platform, content_id, source_url, clean_url FROM content_items WHERE id = ?",
            (note_id,),
        ).fetchone()
        connection.execute("DELETE FROM comments WHERE note_id = ?", (note_id,))
        connection.execute("DELETE FROM content_comments WHERE content_item_id = ?", (note_id,))
        for comment in comments:
            username = (comment.get("用户名") or comment.get("username") or "").strip()
            content = (comment.get("评论内容") or comment.get("content") or "").strip()
            raw_text = (comment.get("原始文本") or comment.get("raw_text") or "").strip()
            publish_time = (comment.get("发布时间") or comment.get("publish_time") or "").strip()
            likes = (comment.get("点赞数") or comment.get("likes") or "").strip()
            crawled_at = (comment.get("爬取时间") or comment.get("crawled_at") or timestamp).strip()
            comment_fingerprint = " ".join(part for part in (username, content, raw_text, publish_time) if part)
            comment_hash = normalized_text_hash(comment_fingerprint)
            comment_simhash = simhash_text(comment_fingerprint)
            connection.execute(
                """
                INSERT OR IGNORE INTO comments (
                    note_id, note_url, username, content, raw_text,
                    publish_time, likes, crawled_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    note_url,
                    username,
                    content,
                    raw_text,
                    publish_time,
                    likes,
                    crawled_at,
                    timestamp,
                ),
            )
            if item_row and content:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO content_comments (
                        content_item_id, platform, content_id, source_url, comment_id,
                        username, content, raw_text, publish_time, likes,
                        crawled_at, created_at, raw_metadata_json, normalized_text_hash, simhash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        item_row["platform"] or "xiaohongshu",
                        item_row["content_id"] or "",
                        item_row["source_url"] or item_row["clean_url"] or note_url,
                        (comment.get("comment_id") or "").strip(),
                        username,
                        content,
                        raw_text,
                        publish_time,
                        likes,
                        crawled_at,
                        timestamp,
                        "{}",
                        comment_hash,
                        comment_simhash,
                    ),
                )
        connection.commit()


def save_content_comments(
    *,
    content_item_id: int,
    platform: str,
    content_id: str = "",
    source_url: str = "",
    comments: Sequence[Dict[str, object]],
    db_path: str | Path | None = None,
) -> int:
    timestamp = now_str()
    saved = 0
    with get_connection(db_path) as connection:
        for comment in comments:
            raw_metadata = comment.get("raw_metadata") or {}
            try:
                raw_metadata_json = json.dumps(raw_metadata, ensure_ascii=False)
            except (TypeError, ValueError):
                raw_metadata_json = "{}"
            comment_fingerprint = " ".join(
                str(comment.get(field) or "").strip()
                for field in ("username", "content", "raw_text", "publish_time")
                if str(comment.get(field) or "").strip()
            )
            comment_hash = normalized_text_hash(comment_fingerprint)
            comment_simhash = simhash_text(comment_fingerprint)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO content_comments (
                    content_item_id, platform, content_id, source_url, comment_id,
                    username, content, raw_text, publish_time, likes,
                    crawled_at, created_at, raw_metadata_json, normalized_text_hash, simhash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_item_id,
                    platform,
                    content_id,
                    source_url,
                    str(comment.get("comment_id") or ""),
                    str(comment.get("username") or ""),
                    str(comment.get("content") or ""),
                    str(comment.get("raw_text") or ""),
                    str(comment.get("publish_time") or ""),
                    str(comment.get("likes") or ""),
                    str(comment.get("crawled_at") or timestamp),
                    timestamp,
                    raw_metadata_json,
                    comment_hash,
                    comment_simhash,
                ),
            )
            saved += int(cursor.rowcount or 0)
        connection.commit()
    return saved


def save_summary(
    note: Dict[str, object],
    summary: Dict[str, object],
    db_path: str | Path | None = None,
):
    timestamp = now_str()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO summaries (
                note_id, note_url, keyword, note_title, summary_markdown, comment_count,
                top_keywords_json, focus_points_json, representative_comments_json,
                sentiment, one_line_summary, content_summary, comment_summary,
                ai_summary, ai_provider, ai_model, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                note_url = excluded.note_url,
                keyword = excluded.keyword,
                note_title = excluded.note_title,
                summary_markdown = excluded.summary_markdown,
                comment_count = excluded.comment_count,
                top_keywords_json = excluded.top_keywords_json,
                focus_points_json = excluded.focus_points_json,
                representative_comments_json = excluded.representative_comments_json,
                sentiment = excluded.sentiment,
                one_line_summary = excluded.one_line_summary,
                content_summary = excluded.content_summary,
                comment_summary = excluded.comment_summary,
                ai_summary = excluded.ai_summary,
                ai_provider = excluded.ai_provider,
                ai_model = excluded.ai_model,
                updated_at = excluded.updated_at
            """,
            (
                note["id"],
                note.get("clean_url") or note.get("raw_url") or "",
                note.get("keyword") or "",
                note.get("note_title") or note.get("title") or note.get("search_title") or "",
                summary.get("summary_markdown") or "",
                int(summary.get("comment_count") or 0),
                json.dumps(summary.get("top_keywords") or [], ensure_ascii=False),
                json.dumps(summary.get("focus_points") or [], ensure_ascii=False),
                json.dumps(summary.get("representative_comments") or [], ensure_ascii=False),
                summary.get("sentiment") or "中性",
                summary.get("one_line_summary") or "",
                summary.get("content_summary") or "",
                summary.get("comment_summary") or "",
                summary.get("ai_summary") or "",
                summary.get("ai_provider") or "",
                summary.get("ai_model") or "",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()


def get_note_comments(note_id: int, db_path: str | Path | None = None) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM comments WHERE note_id = ? ORDER BY id ASC",
            (note_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_note_status_counts(db_path: str | Path | None = None) -> Dict[str, int]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM content_items GROUP BY status ORDER BY status ASC"
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def get_platform_status_counts(
    platform: str,
    *,
    db_path: str | Path | None = None,
    day: str | None = None,
) -> Dict[str, int]:
    date_prefix = day or datetime.now().strftime("%Y-%m-%d")
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM content_items
            WHERE platform = ?
              AND updated_at LIKE ?
            GROUP BY status
            ORDER BY status ASC
            """,
            ((platform or "").strip().lower(), f"{date_prefix}%"),
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def get_recent_notes_by_status(
    statuses: Sequence[str],
    limit: int = 10,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    normalized = [status for status in statuses if status]
    if not normalized:
        return []

    placeholders = ",".join("?" for _ in normalized)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, keyword, platform, content_id, search_title, title, note_title,
                   clean_url, raw_url, status, fail_reason, updated_at
            FROM content_items
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            [*normalized, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def get_daily_dashboard_stats(
    db_path: str | Path | None = None,
    day: str | None = None,
) -> Dict[str, object]:
    date_prefix = day or datetime.now().strftime("%Y-%m-%d")
    with get_connection(db_path) as connection:
        notes_discovered = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE discovered_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        crawled_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'crawled' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        unavailable_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'unavailable' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        failed_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'failed' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        no_comments_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'no_comments' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        skipped_non_note_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'skipped_non_note' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        skipped_author_profile_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'skipped_author_profile' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        blocked_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'blocked' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        login_required_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'login_required' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        captcha_required_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'captcha_required' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        rate_limited_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'rate_limited' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        skipped_robots_count = connection.execute(
            "SELECT COUNT(*) FROM content_items WHERE status = 'skipped_robots' AND updated_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        comment_total = connection.execute(
            "SELECT COUNT(*) FROM comments WHERE created_at LIKE ?",
            (f"{date_prefix}%",),
        ).fetchone()[0]
        unavailable_rows = connection.execute(
            """
            SELECT clean_url, raw_url, note_title, search_title, keyword, fail_reason
            FROM content_items
            WHERE status = 'unavailable' AND updated_at LIKE ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (f"{date_prefix}%",),
        ).fetchall()
        failed_rows = connection.execute(
            """
            SELECT clean_url, raw_url, note_title, search_title, keyword, fail_reason
            FROM content_items
            WHERE status = 'failed' AND updated_at LIKE ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (f"{date_prefix}%",),
        ).fetchall()

    return {
        "date": date_prefix,
        "notes_discovered": notes_discovered,
        "notes_crawled": crawled_count,
        "comments_total": comment_total,
        "notes_unavailable": unavailable_count,
        "notes_failed": failed_count,
        "notes_no_comments": no_comments_count,
        "notes_skipped_non_note": skipped_non_note_count,
        "notes_skipped_author_profile": skipped_author_profile_count,
        "notes_blocked": blocked_count,
        "notes_login_required": login_required_count,
        "notes_captcha_required": captcha_required_count,
        "notes_rate_limited": rate_limited_count,
        "notes_skipped_robots": skipped_robots_count,
        "unavailable_notes": [dict(row) for row in unavailable_rows],
        "failed_notes": [dict(row) for row in failed_rows],
    }


def enqueue_crawl_url(
    *,
    platform: str,
    url_or_id: str,
    source_url: str = "",
    content_id: str = "",
    seed: str = "",
    priority: int = 0,
    max_retries: int = 3,
    db_path: str | Path | None = None,
) -> int:
    timestamp = now_str()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO crawl_queue (
                platform, url_or_id, source_url, content_id, seed, status,
                priority, max_retries, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(platform, url_or_id) DO UPDATE SET
                source_url = CASE
                    WHEN excluded.source_url <> '' THEN excluded.source_url
                    ELSE crawl_queue.source_url
                END,
                content_id = CASE
                    WHEN excluded.content_id <> '' THEN excluded.content_id
                    ELSE crawl_queue.content_id
                END,
                seed = CASE
                    WHEN excluded.seed <> '' THEN excluded.seed
                    ELSE crawl_queue.seed
                END,
                priority = CASE
                    WHEN excluded.priority > crawl_queue.priority THEN excluded.priority
                    ELSE crawl_queue.priority
                END,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                url_or_id,
                source_url,
                content_id,
                seed,
                int(priority or 0),
                int(max_retries or 3),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT id FROM crawl_queue WHERE platform = ? AND url_or_id = ?",
            (platform, url_or_id),
        ).fetchone()
    return int(row["id"]) if row else 0


def claim_crawl_queue(
    *,
    platform: str,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    timestamp = now_str()
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM crawl_queue
            WHERE platform = ?
              AND status = 'pending'
              AND (next_retry_at = '' OR next_retry_at <= ?)
            ORDER BY priority DESC, id ASC
            LIMIT ?
            """,
            (platform, timestamp, int(limit or 10)),
        ).fetchall()
        queue_ids = [row["id"] for row in rows]
        if queue_ids:
            placeholders = ",".join("?" for _ in queue_ids)
            connection.execute(
                f"""
                UPDATE crawl_queue
                SET status = 'processing',
                    last_attempt_at = ?,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [timestamp, timestamp, *queue_ids],
            )
            connection.commit()

        refreshed = []
        for queue_id in queue_ids:
            row = connection.execute("SELECT * FROM crawl_queue WHERE id = ?", (queue_id,)).fetchone()
            if row:
                refreshed.append(dict(row))
    return refreshed


def update_crawl_queue_status(
    queue_id: int,
    *,
    status: str,
    error: str = "",
    blocked_reason: str = "",
    next_retry_at: str = "",
    db_path: str | Path | None = None,
):
    timestamp = now_str()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE crawl_queue
            SET status = ?,
                last_error = ?,
                blocked_reason = ?,
                next_retry_at = ?,
                retry_count = CASE
                    WHEN ? IN ('failed', 'rate_limited') THEN retry_count + 1
                    ELSE retry_count
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (status, error[:1000], blocked_reason[:1000], next_retry_at, status, timestamp, queue_id),
        )
        connection.commit()


def save_discovered_keywords(
    records: Sequence[Dict[str, object]],
    db_path: str | Path | None = None,
) -> int:
    timestamp = now_str()
    saved = 0
    with get_connection(db_path) as connection:
        for record in records:
            term = str(record.get("term") or "").strip()
            if not term:
                continue
            platforms = record.get("platforms") or []
            if isinstance(platforms, str):
                platforms = [platforms]
            platform = str(record.get("platform") or "all").strip() or "all"
            try:
                platforms_json = json.dumps(list(platforms), ensure_ascii=False)
            except (TypeError, ValueError):
                platforms_json = "[]"
            connection.execute(
                """
                INSERT INTO discovered_keywords (
                    term, platform, frequency, score, platforms_json,
                    source_count, last_seen, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(term, platform) DO UPDATE SET
                    frequency = excluded.frequency,
                    score = excluded.score,
                    platforms_json = excluded.platforms_json,
                    source_count = excluded.source_count,
                    last_seen = excluded.last_seen,
                    updated_at = excluded.updated_at
                """,
                (
                    term,
                    platform,
                    int(record.get("frequency") or 0),
                    float(record.get("score") or 0),
                    platforms_json,
                    int(record.get("source_count") or 0),
                    str(record.get("last_seen") or ""),
                    timestamp,
                    timestamp,
                ),
            )
            saved += 1
        connection.commit()
    return saved


def create_crawl_run(
    *,
    run_type: str,
    mode: str,
    status: str = "planned",
    plan: Dict[str, object] | None = None,
    audit: Dict[str, object] | None = None,
    db_path: str | Path | None = None,
) -> int:
    timestamp = now_str()
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO crawl_runs (
                run_type, mode, status, plan_json, stats_json, audit_json,
                started_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?)
            """,
            (
                run_type,
                mode,
                status,
                _json_dumps(plan or {}),
                _json_dumps(audit or {}),
                timestamp if status in {"running", "planned"} else "",
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid or 0)


def update_crawl_run(
    run_id: int,
    *,
    status: str,
    stats: Dict[str, object] | None = None,
    audit: Dict[str, object] | None = None,
    db_path: str | Path | None = None,
):
    timestamp = now_str()
    finished_at = timestamp if status in {"completed", "failed", "stopped"} else ""
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE crawl_runs
            SET status = ?,
                stats_json = ?,
                audit_json = CASE
                    WHEN ? <> '{}' THEN ?
                    ELSE audit_json
                END,
                finished_at = CASE
                    WHEN ? <> '' THEN ?
                    ELSE finished_at
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                _json_dumps(stats or {}),
                _json_dumps(audit or {}),
                _json_dumps(audit or {}),
                finished_at,
                finished_at,
                timestamp,
                int(run_id),
            ),
        )
        connection.commit()


def list_crawl_runs(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM crawl_runs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit or 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_site_profile(
    *,
    platform: str,
    supported_channels: Sequence[str] | None = None,
    selector_observations: Dict[str, object] | None = None,
    last_failure_reason: str = "",
    robots_status: str = "unknown",
    recommended_min_delay_seconds: float = 1.5,
    recommended_max_delay_seconds: float = 5.0,
    health_score: float = 1.0,
    status: str = "active",
    debug_artifacts: Sequence[str] | None = None,
    tuning_notes: Sequence[str] | None = None,
    db_path: str | Path | None = None,
) -> int:
    timestamp = now_str()
    platform_value = (platform or "").strip().lower()
    if not platform_value:
        return 0
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO site_profiles (
                platform, supported_channels_json, selector_observations_json,
                last_failure_reason, robots_status, recommended_min_delay_seconds,
                recommended_max_delay_seconds, health_score, status, debug_artifacts_json,
                tuning_notes_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                supported_channels_json = excluded.supported_channels_json,
                selector_observations_json = excluded.selector_observations_json,
                last_failure_reason = excluded.last_failure_reason,
                robots_status = excluded.robots_status,
                recommended_min_delay_seconds = excluded.recommended_min_delay_seconds,
                recommended_max_delay_seconds = excluded.recommended_max_delay_seconds,
                health_score = excluded.health_score,
                status = excluded.status,
                debug_artifacts_json = excluded.debug_artifacts_json,
                tuning_notes_json = excluded.tuning_notes_json,
                updated_at = excluded.updated_at
            """,
            (
                platform_value,
                _json_dumps(list(supported_channels or [])),
                _json_dumps(selector_observations or {}),
                last_failure_reason[:1000],
                robots_status or "unknown",
                float(recommended_min_delay_seconds),
                float(recommended_max_delay_seconds),
                float(health_score),
                status or "active",
                _json_dumps(list(debug_artifacts or [])),
                _json_dumps(list(tuning_notes or [])),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT id FROM site_profiles WHERE platform = ?", (platform_value,)).fetchone()
    return int(row["id"]) if row else 0


def get_site_profile(platform: str, db_path: str | Path | None = None) -> Dict[str, object] | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM site_profiles WHERE platform = ?",
            ((platform or "").strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def list_site_profiles(db_path: str | Path | None = None) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM site_profiles
            ORDER BY platform ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_discovered_keywords(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM discovered_keywords
            ORDER BY score DESC, frequency DESC, updated_at DESC
            LIMIT ?
            """,
            (int(limit or 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_content_comments(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM content_comments
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit or 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_dedupe_groups(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        refresh_dedupe_groups(connection)
        connection.commit()
        rows = connection.execute(
            """
            SELECT *
            FROM dedupe_groups
            ORDER BY item_count DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit or 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_content_items(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM content_items
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit or 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def _json_dumps(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"
