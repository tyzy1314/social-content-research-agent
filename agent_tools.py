#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real tool wiring for the agent runtime: wraps existing repo entry points.

Every tool delegates to the same functions the existing CLI subcommands already
use (``db.py``, ``crawl_planner.py``, ``discovery.keyword_extractor``,
``export_utils.py``); this module never reimplements their logic and never edits
them. :func:`build_default_registry` is the single place where model-visible tool
metadata (names, descriptions, JSON Schemas, permission tags) is declared.

DB path resolution reuses the repo's own conventions: an explicit ``db_path``
argument wins, then the CLI ``--db`` override (:func:`set_db_path`), then
``output.db_path`` from ``config.yaml`` (loaded through ``main.load_config`` —
no second config parser), then ``db.DEFAULT_DB_PATH``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agent.errors import ToolValidationError
from agent.state import AgentStore
from agent.tools import ToolRegistry, spec_from_function

from crawl_planner import build_crawl_plan
from db import DEFAULT_DB_PATH, get_daily_dashboard_stats, get_enabled_keywords
from discovery.keyword_extractor import extract_keywords_from_db, write_discovered_keywords_outputs
from export_utils import export_dataset

#: CLI ``--db`` override applied by ``main.py agent``; ``None`` = resolve from config.
_DB_PATH_OVERRIDE: str | None = None

#: ``db_query`` may only run statements starting with SELECT or WITH.
_READONLY_STATEMENT = re.compile(r"^(?:SELECT|WITH)\b", re.IGNORECASE)

#: Hard row cap returned to the model by ``db_query``.
_QUERY_ROW_LIMIT = 200

#: Tables counted by ``db_stats`` (legacy ``comments`` kept alongside ``content_comments``).
_STATS_TABLES = ("content_items", "comments", "content_comments", "keywords", "crawl_runs")


def set_db_path(db_path: str | None) -> None:
    """Override the database the tools operate on (called by the ``agent`` CLI with ``--db``).

    Args:
        db_path: Absolute or relative path to the crawler SQLite file; empty/None clears
            the override so resolution falls back to config/defaults.
    """
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = db_path or None


def build_default_registry(store: AgentStore) -> ToolRegistry:
    """Register the first-release real tools against the existing repo modules.

    Args:
        store: Run-state store for the agent loop. Accepted per the frozen wiring
            contract; the tool handlers never touch run state themselves — the
            runtime owns per-run budget accounting through ``InvocationBudget``.

    Returns:
        A registry holding the seven real tools with their permission tags.
    """
    registry = ToolRegistry()
    registry.register(
        spec_from_function(
            db_stats,
            description=(
                "Count rows in the crawler SQLite database tables: content_items, comments, "
                "content_comments, keywords, crawl_runs. Read-only."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            db_query,
            description=(
                "Run one read-only SQL statement (must start with SELECT or WITH) against the "
                f"crawler database and return up to {_QUERY_ROW_LIMIT} rows as JSON objects. Read-only."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            db_keywords,
            description="List the currently enabled crawl keywords from the crawler database. Read-only.",
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            plan_crawl,
            description=(
                "Build a compliant crawl plan (platform quotas, keyword sources, permission audit) "
                "without collecting anything. Read-only planning."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            discover_keywords,
            description=(
                "Mine topic keywords from already-collected content, persist them to the "
                "discovered_keywords table, and write the markdown/CSV discovery report. "
                "Side-effecting; requires approval."
            ),
            mutates=True,
            tags=("db", "write"),
        )
    )
    registry.register(
        spec_from_function(
            run_analysis,
            description=(
                "Compute the daily collection dashboard statistics (counts by status, comments, "
                "keywords) from the crawler database. Requires approval."
            ),
            mutates=True,
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            export_data,
            description=(
                "Export a database table (content, comments, keywords, crawl_runs, site_profiles, "
                "dedupe_groups, or all) to CSV or XLSX. Writes files; output_path must stay inside "
                "the allowed output directories. Requires approval."
            ),
            mutates=True,
            tags=("write",),
        )
    )
    registry.register(
        spec_from_function(
            analyze_comments,
            description=(
                "Analyse stored comments, optionally filtered by keyword: returns sentiment split "
                "(lexicon baseline), mined topics, negative-opinion examples and traceable evidence "
                "with comment ids. Read-only, grounded in real rows — it states explicitly when the "
                "data is insufficient instead of inventing feedback. Use this for content/opinion research."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            write_report,
            description=(
                "Write a markdown analysis report to outputs/reports/ and return its path. "
                "Writes a file; the filename is sanitised and confined to that directory. Requires approval."
            ),
            mutates=True,
            tags=("write",),
        )
    )
    # ---- 多平台能力（第四轮）----
    registry.register(
        spec_from_function(
            list_platforms,
            description=(
                "List every registered platform with its real capabilities, access method and "
                "verification grade (LIVE_VERIFIED/DATA_VERIFIED/BLOCKED/NOT_SUPPORTED). "
                "Use this first to decide which platform a request can actually use. Read-only."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            get_platform_capabilities,
            description="Return one platform's declared capabilities, access method and verification grade. Read-only.",
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            check_platform_access,
            description=(
                "Check whether a platform capability is usable now: OK / AUTH_REQUIRED / "
                "PERMISSION_DENIED / UNAVAILABLE / NOT_SUPPORTED. Never a silent empty success. Read-only."
            ),
            tags=("read",),
        )
    )
    # ---- 第五轮：免登录接入与自动降级 ----
    registry.register(
        spec_from_function(
            probe_platform_capabilities,
            description=(
                "Probe every capability of one platform and return a runtime status matrix "
                "(AVAILABLE/AUTH_REQUIRED/RATE_LIMITED/BLOCKED/NOT_SUPPORTED/UNKNOWN). Status is measured, "
                "not hardcoded. Stops immediately on 401/403/412/429. Read-only."
            ),
            tags=("read", "network"),
        )
    )
    registry.register(
        spec_from_function(
            resolve_platform_access,
            description=(
                "Decide and run the best access path for one platform capability, degrading automatically: "
                "login-free public read -> existing local real data -> authorised API/session -> explicit "
                "missing-data marking. Records every attempt with its reason. Never fabricates data."
            ),
            mutates=True,
            tags=("read", "network", "write"),
        )
    )
    registry.register(
        spec_from_function(
            analyze_platform_content,
            description=(
                "Analyse one platform's stored comments: sentiment split, mined topics, negative examples "
                "and traceable evidence. Optional keyword filter keeps comparisons like-for-like. Read-only."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            compare_platforms,
            description=(
                "Compare several platforms on the SAME topic: sentiment proportions and topic "
                "differences only (engagement metrics are never mixed across platforms). Unavailable "
                "platforms are reported, never guessed. Read-only."
            ),
            tags=("read",),
        )
    )
    registry.register(
        spec_from_function(
            collect_platform_comments,
            description=(
                "Collect content and comments from a platform into the unified store (idempotent; "
                "failing platforms are isolated). Accesses an external platform, so it is "
                "approval-gated and constrained by the adapter. Requires approval."
            ),
            mutates=True,
            tags=("write", "network"),
        )
    )
    registry.register(
        spec_from_function(
            generate_cross_platform_report,
            description=(
                "Generate a cross-platform research report (JSON + Markdown) with automatic "
                "verification of counts and ratios. Writes files. Requires approval."
            ),
            mutates=True,
            tags=("write",),
        )
    )
    return registry


def db_stats(db_path: str = "") -> dict[str, int]:
    """Count rows in the core crawler tables; a missing table counts as 0.

    Opens the database through the repo's path resolution and runs every COUNT(*)
    on a SQLite connection opened in read-only URI mode (``mode=ro``).

    Args:
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Mapping of table name to row count for the tables in ``_STATS_TABLES``.
    """
    with closing(_open_readonly(_resolve_db_path(db_path))) as connection:
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts: dict[str, int] = {}
        for table in _STATS_TABLES:
            if table in tables:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            else:
                counts[table] = 0
    return counts


def db_query(sql: str, db_path: str = "") -> list[dict[str, Any]]:
    """Run one read-only SELECT/WITH statement and return rows as dicts.

    The connection is opened in SQLite read-only URI mode (``mode=ro``), and the
    statement must start (after stripping) with SELECT or WITH — anything else
    raises ``ToolValidationError`` before the database is touched. At most
    ``_QUERY_ROW_LIMIT`` rows are returned.

    Args:
        sql: The SELECT or WITH statement to execute.
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Row dicts, capped at ``_QUERY_ROW_LIMIT`` rows.

    Raises:
        ToolValidationError: The statement does not start with SELECT or WITH.
    """
    statement = sql.strip()
    if not _READONLY_STATEMENT.match(statement):
        raise ToolValidationError("db_query only accepts statements starting with SELECT or WITH")
    with closing(_open_readonly(_resolve_db_path(db_path))) as connection:
        rows = connection.execute(statement).fetchmany(_QUERY_ROW_LIMIT)
    return [dict(row) for row in rows]


def db_keywords(db_path: str = "", limit: int = 50) -> list[str]:
    """List enabled crawl keywords via the existing ``db.get_enabled_keywords``.

    Args:
        db_path: Crawler database path; empty resolves via override/config/default.
        limit: Maximum number of keyword strings returned.

    Returns:
        Enabled keyword strings, at most ``limit`` of them.
    """
    rows = get_enabled_keywords(db_path=_resolve_db_path(db_path))
    return [str(row["keyword"]) for row in rows[: max(0, int(limit))]]


def plan_crawl(
    mode: str = "test",
    total_limit: int = 10,
    platform: str = "all",
    seed: str = "",
    db_path: str = "",
) -> dict[str, Any]:
    """Build a compliant crawl plan without collecting anything.

    Wraps ``crawl_planner.build_crawl_plan`` — the planner entry point behind
    ``main.py plan-crawl`` (the architecture table's ``plan_crawl`` name). The
    returned dict is ``CrawlPlan.asdict()`` and already embeds the permission
    audit under the ``permission_audit`` key.

    Args:
        mode: Planning mode — ``test``, ``auto`` or ``prod``.
        total_limit: Total keyword/quota budget to allocate across platforms.
        platform: Platform filter (``all`` or a single adapter name).
        seed: Optional temporary seed keyword.
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Serialisable crawl plan including the permission audit.
    """
    plan = build_crawl_plan(
        _repo_config(),
        db_path=_resolve_db_path(db_path),
        mode=mode,
        total_limit=total_limit,
        platform_filter=platform,
        seed=seed,
    )
    return plan.asdict()


def discover_keywords(top: int = 50, db_path: str = "") -> dict[str, Any]:
    """Mine topic keywords from collected content and persist the discovery report.

    Wraps the discovery module's entry points: ``extract_keywords_from_db``
    (which scores terms and writes them to the ``discovered_keywords`` table)
    followed by ``write_discovered_keywords_outputs`` (markdown/CSV report under
    ``reports/``). Side-effecting — approval-gated by the runtime.

    Args:
        top: Number of top terms to keep.
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Summary with count, top terms, and the written report paths.
    """
    path = _resolve_db_path(db_path)
    records = extract_keywords_from_db(db_path=path, top_n=top)
    outputs = write_discovered_keywords_outputs(records, output_dir="reports")
    return {
        "count": len(records),
        "top_terms": [str(record["term"]) for record in records[:10]],
        "markdown": outputs["markdown"],
        "csv": outputs["csv"],
    }


def run_analysis(day: str = "", db_path: str = "") -> dict[str, Any]:
    """Compute the collection dashboard via ``db.get_daily_dashboard_stats``.

    This is the core function behind ``main.py analyze`` (reused directly, never
    shelled out). Day-scoped: ``day`` empty means *today*, which legitimately
    returns zeros when nothing was collected today — pass an explicit date
    (``YYYY-MM-DD``) to inspect a day that has data.

    Args:
        day: Date to report on (``YYYY-MM-DD``); empty = today.
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Serialisable dashboard statistics (status counts, totals, keywords).
    """
    return dict(get_daily_dashboard_stats(db_path=_resolve_db_path(db_path), day=day or None))


def export_data(
    table: str = "content",
    output_format: str = "csv",
    output_path: str | None = None,
    limit: int = 1000,
    db_path: str = "",
) -> dict[str, Any]:
    """Export a table (or all tables) to CSV/XLSX via ``export_utils.export_dataset``.

    Side-effecting — approval-gated, and the ``output_path`` argument is
    path-checked by the ``PermissionGate`` (any value must resolve inside the
    configured output directories; leave it unset for the default location).

    Args:
        table: ``content``, ``comments``, ``keywords``, ``crawl_runs``,
            ``site_profiles``, ``dedupe_groups`` or ``all``.
        output_format: ``csv`` or ``xlsx``.
        output_path: Destination file/directory; None uses the exporter default.
        limit: Maximum rows exported per table.
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        ``{"paths": [...]}`` listing the files that were written.
    """
    result = export_dataset(
        table=table,
        output_format=output_format,
        output_path=output_path,
        limit=limit,
        db_path=_resolve_db_path(db_path),
    )
    return {"paths": [str(path) for path in result["paths"]]}


def analyze_comments(keyword: str = "", limit: int = 200, db_path: str = "") -> dict[str, Any]:
    """Analyse stored comments (optionally for one keyword) with real data and evidence.

    Reads real rows from ``content_comments`` joined to ``content_items`` — it never
    fabricates comment text. Each comment is classified with the project's own
    lexicon baseline (``sentiment_analyzer.classify_sentiment``, reused rather than
    reimplemented), topics are mined with jieba, and the result carries traceable
    evidence (comment ids + snippets) so conclusions can be checked against source rows.

    When nothing matches, the result reports **exactly where the data is insufficient**
    (and lists the keywords that do exist) instead of guessing or inventing feedback.

    Args:
        keyword: Filter comments to content collected under this keyword; empty = all.
        limit: Maximum comments to analyse (capped at 500).
        db_path: Crawler database path; empty resolves via override/config/default.

    Returns:
        Structured result: ``data_scope``, ``sentiment`` (counts + ratios + method),
        ``topics``, ``negative_examples``, ``evidence``, and ``caveats``. A failed
        lookup returns ``ok=False`` with ``reason="insufficient_data"``.
    """
    path = _resolve_db_path(db_path)
    cap = max(1, min(int(limit), 500))
    sql = (
        "SELECT cc.id AS id, cc.username AS username, cc.content AS content, "
        "cc.likes AS likes, cc.publish_time AS publish_time, ci.keyword AS keyword "
        "FROM content_comments cc JOIN content_items ci ON ci.id = cc.content_item_id"
    )
    params: list[Any] = []
    if keyword.strip():
        sql += " WHERE ci.keyword = ?"
        params.append(keyword.strip())
    sql += " ORDER BY cc.id DESC LIMIT ?"
    params.append(cap)

    with closing(_open_readonly(path)) as connection:
        rows = [dict(row) for row in connection.execute(sql, params)]
        available = [
            str(row["keyword"])
            for row in connection.execute(
                "SELECT DISTINCT keyword FROM content_items WHERE keyword <> '' ORDER BY keyword"
            )
        ]

    scope: dict[str, Any] = {
        "keyword_filter": keyword.strip() or None,
        "tables": ["content_comments", "content_items"],
        "row_cap": cap,
        "available_keywords": available,
    }

    if not rows:
        return {
            "ok": False,
            "reason": "insufficient_data",
            "message": (
                f"未找到与关键词 '{keyword.strip()}' 匹配的评论：content_comments 关联 "
                f"content_items 后结果为空。库中现有关键词：{available}"
                if keyword.strip()
                else "content_comments 表中没有可分析的评论数据。"
            ),
            "data_scope": scope,
            "suggestion": "请换用 available_keywords 中的关键词，或先执行采集。",
        }

    # Reuse the project's sentiment baseline (lazy import: pulls pandas/jieba).
    from sentiment_analyzer import classify_sentiment

    sentiments = [classify_sentiment(row["content"]) for row in rows]
    counts = {"正面": 0, "负面": 0, "中性": 0}
    for label in sentiments:
        counts[label] += 1
    total = len(rows)

    try:
        # jieba's first import prints its dictionary banner to stdout, which would
        # corrupt `--json` output; suppress it for the duration of the import.
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            import jieba.analyse

        topics = [
            {"term": term, "weight": round(float(weight), 4)}
            for term, weight in jieba.analyse.extract_tags(
                " ".join(str(row["content"]) for row in rows), topK=15, withWeight=True
            )
        ]
    except Exception:  # topic mining is an enhancement; never fail the whole analysis
        topics = []

    def _snippet(text: str, size: int = 80) -> str:
        body = str(text).strip().replace("\n", " ")
        return body if len(body) <= size else body[: size - 3] + "..."

    negative_examples = [
        {"comment_id": row["id"], "keyword": row["keyword"], "snippet": _snippet(row["content"])}
        for row, label in zip(rows, sentiments)
        if label == "负面"
    ][:10]
    evidence = [
        {"comment_id": row["id"], "keyword": row["keyword"], "snippet": _snippet(row["content"])}
        for row in rows[:10]
    ]

    return {
        "ok": True,
        "data_scope": {**scope, "rows_analyzed": total, "sample_is_complete": total < cap},
        "sentiment": {
            "counts": counts,
            "ratios": {label: f"{count / total * 100:.1f}%" for label, count in counts.items()},
            "method": "lexicon baseline (sentiment_analyzer.classify_sentiment) — rule-based, not a trained model",
        },
        "topics": topics,
        "negative_examples": negative_examples,
        "evidence": evidence,
        "caveats": [
            "情感为词典规则基线，无法识别否定、反讽与上下文，结论仅供参考而非事实判定。",
            f"样本取自最近 {total} 条匹配评论（上限 {cap}），不等于全网舆情。",
        ],
    }


def write_report(title: str, content: str, filename: str = "") -> dict[str, Any]:
    """Write a markdown analysis report into ``outputs/reports/``.

    Approval-gated and path-confined: the filename is sanitised to word characters
    so it cannot escape the reports directory, and the resolved path is re-checked.

    Args:
        title: Report title (rendered as the H1).
        content: Markdown body.
        filename: Destination file name; ``.md`` is appended when missing.

    Returns:
        ``{"path", "title", "bytes"}`` for the written file.

    Raises:
        ToolValidationError: The sanitised path escapes the reports directory.
    """
    safe = re.sub(r"[^\w.\-]", "_", (filename or "").strip(), flags=re.UNICODE)
    if not safe:
        safe = "agent_report"
    if not safe.lower().endswith(".md"):
        safe += ".md"
    directory = Path("outputs/reports")
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / safe).resolve()
    if directory.resolve() not in target.parents:
        raise ToolValidationError("write_report: filename escapes the reports directory")
    target.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
    return {"path": str(target), "title": title, "bytes": target.stat().st_size}


def list_platforms() -> list[dict[str, Any]]:
    """List every registered platform with its **real** capabilities and verification grade.

    Returns the honest status per platform (LIVE_VERIFIED / DATA_VERIFIED /
    CONTRACT_VERIFIED / BLOCKED / NOT_SUPPORTED) — never a flat "supported" flag.
    """
    from platforms import list_platforms as _list

    return _list()


def get_platform_capabilities(platform: str) -> dict[str, Any]:
    """Return one platform's declared capabilities, access method and verification grade.

    Args:
        platform: Platform key, e.g. ``xiaohongshu`` / ``bilibili`` / ``douyin``.
    """
    from platforms import get_adapter

    try:
        adapter = get_adapter(platform)
    except Exception as exc:
        return {"platform": platform, "error": f"{type(exc).__name__}: {exc}"}
    info = adapter.platform_info().as_dict()
    info["declared_capabilities"] = list(adapter.capabilities())
    return info


def check_platform_access(platform: str, capability: str = "", live: bool = True) -> dict[str, Any]:
    """Check whether a platform capability is actually usable right now.

    Returns ``OK`` / ``AUTH_REQUIRED`` / ``RATE_LIMITED`` / ``PERMISSION_DENIED`` /
    ``UNAVAILABLE`` / ``NOT_SUPPORTED`` — never a silent empty success.

    Args:
        platform: Platform key.
        capability: Capability name, e.g. ``comments`` / ``content_detail`` / ``search_content``.
        live: Perform a real request. When False the result is marked ``probed=False``
            and must never be reported as a successful real access.
    """
    from platform_service import check_platform_access as _check

    return _check(platform, capability, live=live)


def probe_platform_capabilities(platform: str, live: bool = True) -> dict[str, Any]:
    """Probe **every** capability of one platform and return its runtime status matrix.

    Status is measured, not hardcoded: ``AVAILABLE`` / ``AUTH_REQUIRED`` /
    ``RATE_LIMITED`` / ``BLOCKED`` / ``NOT_SUPPORTED`` / ``UNKNOWN``.

    When a probe hits 401/403/412/429 it stops immediately and records the reason —
    it never rotates IP/UA/accounts and never forges requests.

    Args:
        platform: Platform key.
        live: Issue real requests. False -> every capability is marked
            ``probed=False`` with an explicit "not probed" reason.
    """
    from platforms import get_adapter

    try:
        adapter = get_adapter(platform)
    except Exception as exc:
        return {"platform": platform, "error": f"{type(exc).__name__}: {exc}"}
    matrix = adapter.capability_matrix(live=live)
    return {
        "platform": platform,
        "display_name": getattr(adapter, "platform_name", platform),
        "default_access_mode": adapter.default_access_mode(),
        "access_modes": list(adapter.access_modes()),
        "login_free_capabilities": list(adapter.login_free_capabilities()),
        "capabilities": matrix,
    }


def resolve_platform_access(
    platform: str,
    capability: str = "comments",
    content_id: str = "",
    keyword: str = "",
    limit: int = 50,
    allow_live: bool = True,
    collect: bool = True,
) -> dict[str, Any]:
    """Decide and run the best available access path for one platform capability.

    Automatic degradation chain (each step recorded with its reason):
    login-free public read -> existing local real data -> authorised API/session
    -> explicit "missing data" marking.

    Never fabricates data: if every path fails the result carries ``status`` and
    ``failure_reason``, plus actionable ``auth_guidance`` when a credential is the blocker.

    Args:
        platform: Platform key.
        capability: Capability name (default ``comments``).
        content_id: Explicit content id/URL — the preferred login-free input for
            public read-only collection when platform search is rate limited.
        keyword: Topic keyword (used for local filtering or platforms with working search).
        limit: Desired number of records.
        allow_live: Allow real network requests.
        collect: Actually pull data when the public path is available.
    """
    from platform_service import resolve_platform_access as _resolve

    return _resolve(
        _resolve_db_path(""),
        platform,
        capability,
        content_id=content_id,
        keyword=keyword,
        limit=limit,
        allow_live=allow_live,
        collect=collect,
    )


def collect_platform_comments(
    platform: str = "bilibili",
    keyword: str = "",
    content_id: str = "",
    comment_limit: int = 30,
    include_replies: bool = True,
    collect_barrage: bool = False,
) -> dict[str, Any]:
    """Collect content and comments from a platform into the unified store.

    Idempotent: repeated collection updates existing rows instead of duplicating.
    A failing platform is isolated — it never pollutes other platforms' data.

    Args:
        platform: Target platform key.
        keyword: Search keyword (used when ``content_id`` is empty).
        content_id: Specific content id (takes precedence over ``keyword``).
        comment_limit: Max comments per content.
        include_replies: Also fetch second-level replies.
        collect_barrage: Also fetch danmaku (only where supported).
    """
    from platform_service import collect_platform_comments as _collect

    return _collect(
        _resolve_db_path(""),
        platform,
        content_id=content_id,
        keyword=keyword,
        comment_limit=comment_limit,
        include_replies=include_replies,
        collect_barrage=collect_barrage,
        topic_label=keyword,
    )


def analyze_platform_content(platform: str, keyword: str = "", limit: int = 500) -> dict[str, Any]:
    """Analyse one platform's stored comments (sentiment, topics, evidence).

    Args:
        platform: Platform key.
        keyword: Optional topic filter (compares like-for-like across platforms).
        limit: Max comments analysed.
    """
    from cross_platform import analyze_platform

    return analyze_platform(_resolve_db_path(""), platform, keyword=keyword, limit=limit)


def compare_platforms(platforms: str, keyword: str = "", limit: int = 500) -> dict[str, Any]:
    """Compare several platforms on the **same topic** (comparable dimensions only).

    Args:
        platforms: Comma-separated platform keys, e.g. ``"xiaohongshu,bilibili"``.
        keyword: Topic filter; both platforms are restricted to it when supported.
        limit: Max comments per platform.
    """
    from cross_platform import compare_platforms as _compare

    names = [part.strip() for part in (platforms or "").split(",") if part.strip()]
    return _compare(_resolve_db_path(""), names, keyword=keyword, limit=limit)


def generate_cross_platform_report(
    platforms: str, keyword: str = "", title: str = "跨平台内容研究报告"
) -> dict[str, Any]:
    """Generate a cross-platform research report (JSON + Markdown) with self-verification.

    Writes files; the filename is generated and confined to ``outputs/reports/``.

    Args:
        platforms: Comma-separated platform keys.
        keyword: Topic filter.
        title: Report title.
    """
    from cross_platform import compare_platforms as _compare
    from cross_platform import generate_report

    names = [part.strip() for part in (platforms or "").split(",") if part.strip()]
    comparison = _compare(_resolve_db_path(""), names, keyword=keyword)
    return generate_report(comparison, title=title)


def _repo_config() -> dict[str, object]:
    """Load ``config.yaml`` through the existing ``main.load_config`` (no second parser)."""
    from main import load_config  # lazy: main dispatches to agent.cli lazily; keeps import graph acyclic

    return load_config()


def _resolve_db_path(db_path: str) -> str:
    """Resolve the crawler DB path: explicit arg > CLI override > config > repo default."""
    if db_path:
        return db_path
    if _DB_PATH_OVERRIDE:
        return _DB_PATH_OVERRIDE
    output_config = _repo_config().get("output") or {}
    resolved = str(output_config.get("db_path") or "")
    return resolved or str(DEFAULT_DB_PATH)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the crawler database in SQLite read-only URI mode (``mode=ro``).

    Args:
        db_path: Path of an existing SQLite file.

    Returns:
        A connection with ``Row`` row factory; the file must already exist.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection
