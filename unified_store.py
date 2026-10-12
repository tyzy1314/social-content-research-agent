#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台统一数据层（第四轮）。

在**保留**原有小红书表（``content_items`` / ``content_comments`` / ``comments``）
的前提下新增三张统一表，满足第八节要求：

* ``platform_content``  —— 跨平台内容（视频/笔记/动态）
* ``platform_comments`` —— 跨平台评论（含一级/二级关系）
* ``platform_runs``     —— 采集运行审计
* ``access_records``    —— 第五轮：统一接入记录（平台/接入模式/能力/来源/结果/失败原因/时间/引用/任务ID）

关键约束：
1. 唯一键为 ``(platform, platform_content_id)``，不同平台相同原始 ID 不冲突。
2. 迁移前**先备份**数据库文件（可验证、可回滚）。
3. 写入使用 UPSERT，重复执行不会无限插入重复记录。
4. 来源可追溯：``source_method`` / ``collection_run_id`` / ``provenance``。
5. 各平台统计字段存于 ``engagement_metrics`` JSON，**不做跨平台混算**。
6. 原有表不动，既有分析工具继续可用。
7. 每一次能力访问都在 ``access_records`` 留痕（成功与失败都记录）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 3

CREATE_CONTENT = """
CREATE TABLE IF NOT EXISTS platform_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_content_id TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    author_ref TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    engagement_metrics TEXT NOT NULL DEFAULT '{}',
    raw_metadata TEXT NOT NULL DEFAULT '{}',
    source_method TEXT NOT NULL DEFAULT '',
    collection_run_id TEXT NOT NULL DEFAULT '',
    UNIQUE(platform, platform_content_id)
);
"""

CREATE_COMMENTS = """
CREATE TABLE IF NOT EXISTS platform_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_comment_id TEXT NOT NULL,
    platform_content_id TEXT NOT NULL DEFAULT '',
    parent_comment_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    author_ref TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    engagement_metrics TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform, platform_comment_id)
);
"""

CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS platform_runs (
    run_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT '',
    access_method TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_inserted INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT ''
);
"""

# 第五轮：统一接入记录表 —— 每次访问都必须留下可审计的痕迹
CREATE_ACCESS_RECORDS = """
CREATE TABLE IF NOT EXISTS access_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    capability TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    record_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT '',
    citation TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_pc_platform ON platform_content(platform);",
    "CREATE INDEX IF NOT EXISTS idx_pcm_platform ON platform_comments(platform);",
    "CREATE INDEX IF NOT EXISTS idx_pcm_content ON platform_comments(platform, platform_content_id);",
    "CREATE INDEX IF NOT EXISTS idx_pcm_parent ON platform_comments(platform, parent_comment_id);",
    "CREATE INDEX IF NOT EXISTS idx_ar_task ON access_records(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_ar_platform ON access_records(platform, capability);",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup_database(db_path: str | Path) -> str:
    """迁移前备份数据库，返回备份文件路径（可验证、可回滚）。"""
    source = Path(db_path)
    if not source.exists():
        return ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = source.with_name(f"{source.stem}.backup_{stamp}{source.suffix}")
    shutil.copy2(source, target)
    return str(target)


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def migrate(db_path: str | Path, *, with_backup: bool = True) -> Dict[str, Any]:
    """创建统一表（幂等）。返回迁移报告（含备份路径）。"""
    path = Path(db_path)
    backup = ""
    if with_backup and path.exists():
        backup = backup_database(path)
    with closing(connect(path)) as connection:
        connection.execute(CREATE_CONTENT)
        connection.execute(CREATE_COMMENTS)
        connection.execute(CREATE_RUNS)
        connection.execute(CREATE_ACCESS_RECORDS)
        for statement in INDEXES:
            connection.execute(statement)
        connection.commit()
    return {
        "db_path": str(path),
        "backup": backup,
        "schema_version": SCHEMA_VERSION,
        "tables": ["platform_content", "platform_comments", "platform_runs", "access_records"],
    }


def ensure_unified_store(db_path: str | Path) -> Dict[str, Any]:
    """Create missing unified tables, backing up an existing database only once."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    required = {"platform_content", "platform_comments", "platform_runs", "access_records"}
    if path.exists():
        with closing(connect(path)) as connection:
            existing = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        if required.issubset(existing):
            return {
                "db_path": str(path),
                "backup": "",
                "schema_version": SCHEMA_VERSION,
                "tables": sorted(required),
            }
        return migrate(path, with_backup=True)
    return migrate(path, with_backup=False)


# --------------------------------------------------------------------------- #
# 运行审计
# --------------------------------------------------------------------------- #
def start_run(
    db_path: str | Path, *, run_id: str, platform: str, task_type: str = "", access_method: str = ""
) -> None:
    with closing(connect(db_path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO platform_runs(run_id, platform, task_type, access_method, started_at, status)"
            " VALUES(?,?,?,?,?,'running')",
            (run_id, platform, task_type, access_method, _now()),
        )
        connection.commit()


def finish_run(
    db_path: str | Path, *, run_id: str, status: str, seen: int = 0, inserted: int = 0,
    updated: int = 0, error_type: str = "",
) -> None:
    with closing(connect(db_path)) as connection:
        connection.execute(
            "UPDATE platform_runs SET finished_at=?, status=?, records_seen=?, records_inserted=?,"
            " records_updated=?, error_type=? WHERE run_id=?",
            (_now(), status, seen, inserted, updated, error_type, run_id),
        )
        connection.commit()


# --------------------------------------------------------------------------- #
# 幂等写入
# --------------------------------------------------------------------------- #
def upsert_content(db_path: str | Path, records: Iterable[Dict[str, Any]], *, run_id: str = "") -> Dict[str, int]:
    """UPSERT 内容记录；返回 {'seen','inserted','updated'}。"""
    inserted = updated = seen = 0
    with closing(connect(db_path)) as connection:
        for record in records:
            seen += 1
            key = (record.get("platform"), record.get("platform_content_id"))
            existing = connection.execute(
                "SELECT id FROM platform_content WHERE platform=? AND platform_content_id=?", key
            ).fetchone()
            connection.execute(
                "INSERT INTO platform_content(platform, platform_content_id, content_type, title, text,"
                " source_url, author_ref, published_at, collected_at, engagement_metrics, raw_metadata,"
                " source_method, collection_run_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(platform, platform_content_id) DO UPDATE SET"
                " content_type=excluded.content_type, title=excluded.title, text=excluded.text,"
                " source_url=excluded.source_url, author_ref=excluded.author_ref,"
                " published_at=excluded.published_at, engagement_metrics=excluded.engagement_metrics,"
                " raw_metadata=excluded.raw_metadata, source_method=excluded.source_method,"
                " collection_run_id=excluded.collection_run_id",
                (
                    record.get("platform", ""),
                    str(record.get("platform_content_id", "")),
                    record.get("content_type", ""),
                    record.get("title", ""),
                    record.get("text", ""),
                    record.get("source_url", ""),
                    record.get("author_ref", ""),
                    record.get("published_at", ""),
                    _now(),
                    json.dumps(record.get("engagement_metrics") or {}, ensure_ascii=False),
                    json.dumps(record.get("raw_metadata") or {}, ensure_ascii=False),
                    record.get("source_method", ""),
                    run_id,
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
        connection.commit()
    return {"seen": seen, "inserted": inserted, "updated": updated}


def upsert_comments(db_path: str | Path, records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """UPSERT 评论记录（唯一键 platform + platform_comment_id）。"""
    inserted = updated = seen = 0
    with closing(connect(db_path)) as connection:
        for record in records:
            seen += 1
            key = (record.get("platform"), record.get("platform_comment_id"))
            if not key[1]:
                continue
            existing = connection.execute(
                "SELECT id FROM platform_comments WHERE platform=? AND platform_comment_id=?", key
            ).fetchone()
            connection.execute(
                "INSERT INTO platform_comments(platform, platform_comment_id, platform_content_id,"
                " parent_comment_id, content, author_ref, published_at, collected_at, engagement_metrics, provenance)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(platform, platform_comment_id) DO UPDATE SET"
                " platform_content_id=excluded.platform_content_id, parent_comment_id=excluded.parent_comment_id,"
                " content=excluded.content, author_ref=excluded.author_ref, published_at=excluded.published_at,"
                " engagement_metrics=excluded.engagement_metrics, provenance=excluded.provenance",
                (
                    record.get("platform", ""),
                    str(record.get("platform_comment_id")),
                    str(record.get("platform_content_id", "")),
                    str(record.get("parent_comment_id", "")),
                    record.get("content", ""),
                    record.get("author_ref", ""),
                    record.get("published_at", ""),
                    _now(),
                    json.dumps(record.get("engagement_metrics") or {}, ensure_ascii=False),
                    json.dumps(record.get("provenance") or {}, ensure_ascii=False),
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
        connection.commit()
    return {"seen": seen, "inserted": inserted, "updated": updated}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def platform_counts(db_path: str | Path) -> List[Dict[str, Any]]:
    """按平台统计内容与评论数量（不做跨平台指标混算）。"""
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT platform, COUNT(*) AS content_count FROM platform_content GROUP BY platform ORDER BY platform"
        ).fetchall()
        comments = {
            row["platform"]: row["n"]
            for row in connection.execute(
                "SELECT platform, COUNT(*) AS n FROM platform_comments GROUP BY platform"
            ).fetchall()
        }
    return [
        {"platform": row["platform"], "content_count": row["content_count"],
         "comment_count": comments.get(row["platform"], 0)}
        for row in rows
    ]


def fetch_comments(
    db_path: str | Path, *, platform: str = "", limit: int = 500
) -> List[Dict[str, Any]]:
    """读取统一评论（可按平台筛选），返回 dict 列表。"""
    sql = "SELECT * FROM platform_comments"
    params: List[Any] = []
    if platform:
        sql += " WHERE platform=?"
        params.append(platform)
    sql += " ORDER BY id LIMIT ?"
    params.append(int(limit))
    with closing(connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def fetch_content(db_path: str | Path, *, platform: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM platform_content"
    params: List[Any] = []
    if platform:
        sql += " WHERE platform=?"
        params.append(platform)
    sql += " ORDER BY id LIMIT ?"
    params.append(int(limit))
    with closing(connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- #
# 第五轮：统一接入记录（成功与失败都留痕）
# --------------------------------------------------------------------------- #
def record_access(
    db_path: str | Path,
    *,
    task_id: str,
    platform: str,
    capability: str,
    access_mode: str,
    status: str,
    ok: bool,
    source: str = "",
    record_count: int = 0,
    failure_reason: str = "",
    citation: Dict[str, Any] | None = None,
    checked_at: str = "",
) -> int:
    """写入一条接入记录（append-only，不做更新，保证可审计）。

    记录的九个要素：平台、接入模式、能力、来源、结果状态、失败原因、
    更新时间、原始引用、任务 ID。
    """
    with closing(connect(db_path)) as connection:
        cursor = connection.execute(
            "INSERT INTO access_records(task_id, platform, capability, access_mode, status, ok,"
            " source, record_count, failure_reason, citation, checked_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, platform, capability, access_mode, status,
                1 if ok else 0, source, int(record_count), failure_reason,
                json.dumps(citation or {}, ensure_ascii=False),
                checked_at or _now(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid or 0)


def list_access_records(
    db_path: str | Path, *, task_id: str = "", platform: str = "", limit: int = 200
) -> List[Dict[str, Any]]:
    """读取接入记录（可按任务/平台筛选）。"""
    sql = "SELECT * FROM access_records"
    clauses: List[str] = []
    params: List[Any] = []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if platform:
        clauses.append("platform=?")
        params.append(platform)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with closing(connect(db_path)) as connection:
        rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
    for row in rows:
        try:
            row["citation"] = json.loads(row.get("citation") or "{}")
        except json.JSONDecodeError:
            row["citation"] = {}
        row["ok"] = bool(row.get("ok"))
    return rows


# --------------------------------------------------------------------------- #
# 历史数据导入（保留原有表，仅复制到统一表）
# --------------------------------------------------------------------------- #
def import_xhs_history(
    db_path: str | Path,
    *,
    keyword: str = "",
    content_item_ids: Iterable[int] | None = None,
    run_id: str = "xhs-history-import",
) -> Dict[str, Any]:
    """Copy XHS legacy rows into unified tables without changing legacy rows.

    ``content_item_ids`` limits the import to specific legacy rows from one live
    browser run. ``None`` preserves the original whole-history import behavior.
    """
    selected_ids = None
    if content_item_ids is not None:
        selected_ids = sorted({int(value) for value in content_item_ids if int(value) > 0})
        if not selected_ids:
            return {
                "content": {"seen": 0, "inserted": 0, "updated": 0},
                "comments": {"seen": 0, "inserted": 0, "updated": 0},
            }

    content_rows: List[Dict[str, Any]] = []
    comment_rows: List[Dict[str, Any]] = []
    with closing(connect(db_path)) as connection:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "content_items" in tables:
            sql = (
                "SELECT id, keyword, note_title, title, note_content, body, clean_url, source_url,"
                " author_name, publish_time, like_count, comment_count_display FROM content_items"
            )
            clauses: List[str] = []
            params: List[Any] = []
            if keyword:
                clauses.append("keyword=?")
                params.append(keyword)
            if selected_ids is not None:
                clauses.append("id IN (" + ",".join("?" for _ in selected_ids) + ")")
                params.extend(selected_ids)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            for row in connection.execute(sql, params).fetchall():
                content_rows.append({
                    "platform": "xiaohongshu",
                    "platform_content_id": f"xhs_{row['id']}",
                    "content_type": "note",
                    "title": row["note_title"] or row["title"] or "",
                    "text": row["note_content"] or row["body"] or "",
                    "source_url": row["clean_url"] or row["source_url"] or "",
                    "author_ref": row["author_name"] or "",
                    "published_at": row["publish_time"] or "",
                    "engagement_metrics": {
                        "like": row["like_count"] or "",
                        "comment_display": row["comment_count_display"] or "",
                    },
                    "raw_metadata": {"keyword": row["keyword"], "legacy_id": row["id"]},
                    "source_method": "xhs_legacy_import",
                })
        if "content_comments" in tables:
            comment_sql = (
                "SELECT id, content_item_id, username, content, publish_time, likes FROM content_comments"
            )
            comment_params: List[Any] = []
            if selected_ids is not None:
                comment_sql += " WHERE content_item_id IN (" + ",".join("?" for _ in selected_ids) + ")"
                comment_params.extend(selected_ids)
            for row in connection.execute(comment_sql, comment_params).fetchall():
                comment_rows.append({
                    "platform": "xiaohongshu",
                    "platform_comment_id": f"xhs_c_{row['id']}",
                    "platform_content_id": f"xhs_{row['content_item_id']}",
                    "parent_comment_id": "",
                    "content": row["content"] or "",
                    "author_ref": row["username"] or "",
                    "published_at": row["publish_time"] or "",
                    "engagement_metrics": {"like": row["likes"] or ""},
                    "provenance": {"source_method": "xhs_legacy_import", "legacy_id": row["id"],
                                   "collection_run_id": run_id},
                })
    content_stats = upsert_content(db_path, content_rows, run_id=run_id)
    comment_stats = upsert_comments(db_path, comment_rows)
    return {"content": content_stats, "comments": comment_stats}
