#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台采集编排（第四轮第十一节）。

职责：把「适配器调用 → 统一库持久化 → 运行审计」串起来，并保证：
* 幂等写入（重复采集不产生重复记录）；
* 错误隔离（某平台失败不污染其他平台数据）；
* 运行审计（``platform_runs`` 记录 seen/inserted/updated/error_type）；
* 受限能力如实返回状态，不静默成功。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

from platform_dispatch import resolve_access
from platforms import get_adapter
from platforms.capabilities import (
    ACCESS_OK, AUTH_REQUIRED, BLOCKED, MODE_AUTHORIZED_SESSION, PERMISSION_DENIED,
    RATE_LIMITED, UNAVAILABLE, AccessResult, now_iso,
)
from unified_store import (
    ensure_unified_store,
    finish_run,
    list_access_records,
    record_access,
    start_run,
    upsert_comments,
    upsert_content,
)


def new_collection_run_id(platform: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"col_{platform}_{stamp}_{secrets.token_hex(3)}"


def _safe_evidence(result: AccessResult) -> Dict[str, Any]:
    evidence = {
        key: value for key, value in dict(result.evidence).items()
        if not any(secret in key.lower() for secret in ("token", "cookie", "secret", "password", "authorization", "api_key"))
    }
    endpoint = evidence.get("endpoint")
    if isinstance(endpoint, str):
        try:
            parts = urlsplit(endpoint)
            evidence["endpoint"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            evidence.pop("endpoint", None)
    return evidence


def _safe_message(message: str) -> str:
    return re.sub(
        r"(?i)((?:access_token|refresh_token|xsec_token|token|cookie|secret|password|authorization|api[_-]?key)=)[^&\s]+",
        r"\1[redacted]",
        str(message or ""),
    )


def _record_access_result(
    db_path: str,
    *,
    task_id: str,
    adapter,
    result: AccessResult,
    record_count: int = 0,
) -> None:
    evidence = _safe_evidence(result)
    try:
        access_mode = result.access_mode or adapter.default_access_mode()
    except Exception:
        access_mode = "UNAVAILABLE"
    source = str(evidence.get("endpoint") or f"{result.platform}:{result.capability}")
    record_access(
        db_path,
        task_id=task_id,
        platform=result.platform,
        capability=result.capability,
        access_mode=access_mode,
        status=result.status,
        ok=result.ok,
        source=source,
        record_count=record_count,
        failure_reason="" if result.ok else _safe_message(result.message),
        citation=evidence,
        checked_at=now_iso(),
    )


def _should_stop_platform(status: str) -> bool:
    return status not in {ACCESS_OK, "INVALID_INPUT"}


def _collect_xhs_browser_stream(
    db_path: str,
    *,
    run_id: str,
    adapter,
    keyword: str,
    search_limit: int,
    comment_limit: int,
    summary: Dict[str, Any],
    output_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Reuse the existing Selenium stream; do not invent platform comment IDs."""
    if not keyword:
        summary["status"] = "failed"
        summary["errors"].append("小红书实时浏览器采集需要关键词；不能用 content_id 模式")
        finish_run(db_path, run_id=run_id, status="failed", error_type="INVALID_INPUT")
        return summary

    from crawl_engine import CrawlerEngine
    from db import init_db
    from unified_store import import_xhs_history

    init_db(db_path)
    safe_items = max(1, min(int(search_limit), 3))
    safe_comments = max(1, min(int(comment_limit), 20))
    run_stats = CrawlerEngine(adapter, crawler_config={"concurrency": 1}, db_path=db_path).run_xhs_test_stream(
        keyword=keyword,
        stream_config={
            "success_target": safe_items,
            "max_candidates": min(max(safe_items * 3, 5), 15),
            "max_comments_per_note": safe_comments,
            "sleep_between_notes_seconds": 5,
            "fast_skip_sleep_seconds": 1,
            "write_db": True,
            "generate_summary": False,
        },
        output_config={**dict(output_config or {}), "db_path": db_path},
        ai_config={"enabled": False},
    )
    crawled = int(run_stats.get("crawled") or 0)
    no_comments = int(run_stats.get("no_comments") or 0)
    content_count = int(run_stats.get("detail_successes", crawled + no_comments) or 0)
    detail_attempts = int(run_stats.get("detail_attempts", content_count) or 0)
    comment_attempts = int(run_stats.get("comment_attempts", crawled + no_comments) or 0)
    comments = int(run_stats.get("total_comments") or 0)
    # The legacy XHS scraper has no platform-native comment IDs. Sync only the
    # legacy rows identified by this run; never count old matching-keyword rows.
    storage_stats = {
        "content": {"inserted": 0, "updated": 0},
        "comments": {"inserted": 0, "updated": 0},
    }
    sync_error = ""
    legacy_ids = list(run_stats.get("collected_legacy_ids") or [])
    if content_count and legacy_ids:
        storage_stats = import_xhs_history(
            db_path, keyword=keyword, content_item_ids=legacy_ids, run_id=run_id
        )
    elif content_count:
        sync_error = "本次采集结果缺少本地笔记 ID，无法同步到统一库。"
    stop_status = ""
    stop_reason = _safe_message(str(run_stats.get("platform_stop_reason") or ""))
    if run_stats.get("login_required_count"):
        stop_status = AUTH_REQUIRED
        stop_reason = stop_reason or "小红书实时采集需要登录，请在本机 Chrome 手动登录后重新运行。"
    elif run_stats.get("captcha_required_count"):
        stop_status = "CAPTCHA_REQUIRED"
        stop_reason = stop_reason or "需要用户手动完成安全验证；程序未尝试自动处理。"
    elif run_stats.get("rate_limited_count"):
        stop_status = RATE_LIMITED
        stop_reason = stop_reason or "平台限流，本次已停止小红书采集。"
    elif run_stats.get("blocked_count"):
        stop_status = BLOCKED
        stop_reason = stop_reason or "平台拒绝访问，本次已停止小红书采集。"

    has_item_failures = bool(run_stats.get("failed") or run_stats.get("unavailable") or no_comments or sync_error)
    if stop_status:
        result_status = "partial" if content_count else "failed"
    elif content_count and has_item_failures:
        result_status = "partial"
    elif content_count:
        result_status = "finished"
    elif int(run_stats.get("scanned_candidates") or 0) == 0:
        result_status = "empty"
        stop_reason = "没有取得可采集的搜索结果。"
    else:
        result_status = "failed"
        stop_reason = stop_reason or "没有成功读取笔记详情。"

    scanned = int(run_stats.get("scanned_candidates") or 0)
    search_status = stop_status if stop_status and scanned == 0 else ACCESS_OK
    detail_attempted = detail_attempts > 0
    details_status = ACCESS_OK if content_count else (stop_status or UNAVAILABLE)
    comments_attempted = comment_attempts > 0
    comments_status = ACCESS_OK if comments or no_comments else (stop_status or UNAVAILABLE)
    attempted = [("search_content", scanned, search_status)]
    if detail_attempted:
        attempted.append(("content_detail", content_count, details_status))
    if comments_attempted:
        attempted.append(("comments", comments, comments_status))
    for capability, count, capability_status in attempted:
        ok = capability_status == ACCESS_OK
        failure_reason = "" if ok else stop_reason
        if not ok and not failure_reason:
            failure_reason = (
                "本次未能读取笔记详情" if capability == "content_detail"
                else "已访问评论区，但没有读取到评论" if capability == "comments"
                else "没有取得搜索结果"
            )
        record_access(
            db_path,
            task_id=run_id,
            platform="xiaohongshu",
            capability=capability,
            access_mode=MODE_AUTHORIZED_SESSION,
            status=capability_status,
            ok=ok,
            source="xhs_browser",
            record_count=count,
            failure_reason=_safe_message(failure_reason),
            citation={"source_method": "xhs_browser", "run_id": run_id},
            checked_at=now_iso(),
        )

    summary["status"] = result_status
    summary["contents"] = {
        "seen": content_count,
        "inserted": int(storage_stats["content"].get("inserted", 0)),
        "updated": int(storage_stats["content"].get("updated", 0)),
    }
    summary["comments"] = {
        "seen": comments,
        "inserted": int(storage_stats["comments"].get("inserted", 0)),
        "updated": int(storage_stats["comments"].get("updated", 0)),
    }
    if stop_reason:
        summary["errors"].append(stop_reason)
    if sync_error:
        summary["errors"].append(sync_error)
    if no_comments:
        summary["errors"].append(f"有 {no_comments} 条笔记没有取得评论")
    if run_stats.get("failed") or run_stats.get("unavailable"):
        summary["errors"].append("部分笔记读取失败或不可用")
    finish_run(
        db_path,
        run_id=run_id,
        status=result_status,
        seen=content_count + comments,
        inserted=(summary["contents"]["inserted"] + summary["comments"]["inserted"]),
        updated=(summary["contents"]["updated"] + summary["comments"]["updated"]),
        error_type=stop_status or ("PARTIAL" if result_status == "partial" else ""),
    )
    return summary


def collect_platform_comments(
    db_path: str,
    platform: str,
    *,
    content_id: str = "",
    keyword: str = "",
    search_limit: int = 5,
    comment_limit: int = 30,
    include_replies: bool = True,
    reply_limit: int = 10,
    collect_barrage: bool = False,
    barrage_limit: int = 100,
    topic_label: str = "",
    output_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Collect real platform data and record every access attempt.

    Each platform is isolated. A denied or rate-limited response ends requests for
    that platform, while the caller may continue with the next selected platform.
    """
    run_id = new_collection_run_id(platform)
    adapter = get_adapter(platform, **({"headless": False} if platform == "xiaohongshu" else {}))
    ensure_unified_store(db_path)
    try:
        access_method = adapter.platform_info().access_method
    except Exception:
        access_method = "unknown"
    start_run(db_path, run_id=run_id, platform=platform, task_type="collect_comments", access_method=access_method)

    summary: Dict[str, Any] = {
        "run_id": run_id,
        "platform": platform,
        "status": "running",
        "contents": {"seen": 0, "inserted": 0, "updated": 0},
        "comments": {"seen": 0, "inserted": 0, "updated": 0},
        "barrage_count": 0,
        "targets": [],
        "limited": [],
        "errors": [],
    }
    seen_total = 0

    try:
        if platform == "xiaohongshu":
            return _collect_xhs_browser_stream(
                db_path,
                run_id=run_id,
                adapter=adapter,
                keyword=keyword,
                search_limit=search_limit,
                comment_limit=comment_limit,
                summary=summary,
                output_config=output_config,
            )

        targets: List[Dict[str, Any]] = []
        stop_platform = False
        if content_id:
            detail = adapter.get_content_detail(content_id)
            _record_access_result(db_path, task_id=run_id, adapter=adapter, result=detail, record_count=int(detail.ok))
            if not detail.ok:
                summary["status"] = "failed"
                summary["errors"].append(f"{content_id}: {detail.status} {_safe_message(detail.message)}")
                finish_run(db_path, run_id=run_id, status="failed", error_type=detail.status)
                return summary
            targets.append(detail.detail["content"])
        elif keyword:
            search = adapter.search_content(keyword, limit=search_limit)
            search_items = search.detail.get("items", []) if search.ok else []
            _record_access_result(
                db_path, task_id=run_id, adapter=adapter, result=search, record_count=len(search_items)
            )
            if not search.ok:
                summary["status"] = "failed"
                summary["errors"].append(f"search '{keyword}': {search.status} {_safe_message(search.message)}")
                finish_run(db_path, run_id=run_id, status="failed", error_type=search.status)
                return summary
            for item in search_items:
                content_key = str(item.get("platform_content_id") or "")
                if not content_key:
                    summary["errors"].append("搜索结果缺少内容 ID，已跳过该条")
                    continue
                detail = adapter.get_content_detail(content_key)
                _record_access_result(
                    db_path, task_id=run_id, adapter=adapter, result=detail, record_count=int(detail.ok)
                )
                if detail.ok:
                    targets.append(detail.detail["content"])
                else:
                    summary["errors"].append(f"detail {content_key}: {detail.status} {_safe_message(detail.message)}")
                    if _should_stop_platform(detail.status):
                        stop_platform = True
                        break
        else:
            summary["status"] = "failed"
            summary["errors"].append("需要提供 content_id 或 keyword")
            finish_run(db_path, run_id=run_id, status="failed", error_type="INVALID_INPUT")
            return summary

        if keyword and targets:
            # The keyword is user-provided metadata, not inferred from page content.
            label = topic_label or keyword
            for content in targets:
                metadata = dict(content.get("raw_metadata") or {})
                metadata["keyword"] = label
                content["raw_metadata"] = metadata

        content_stats = upsert_content(db_path, targets, run_id=run_id)
        summary["contents"] = content_stats
        seen_total += content_stats["seen"]
        if stop_platform and not targets:
            summary["status"] = "failed"
            finish_run(db_path, run_id=run_id, status="failed", seen=seen_total, error_type="RESTRICTED")
            return summary

        all_comments: List[Dict[str, Any]] = []
        for content in (targets if not stop_platform else []):
            cid = content["platform_content_id"]
            comments_result = adapter.list_comments(cid, limit=comment_limit)
            batch = comments_result.detail.get("comments", []) if comments_result.ok else []
            _record_access_result(
                db_path, task_id=run_id, adapter=adapter, result=comments_result, record_count=len(batch)
            )
            if not comments_result.ok:
                summary["errors"].append(f"comments {cid}: {comments_result.status} {_safe_message(comments_result.message)}")
                if _should_stop_platform(comments_result.status):
                    break
                continue
            if comments_result.detail.get("partial"):
                summary["limited"].append({
                    "capability": "comments",
                    "content_id": cid,
                    "status": "PARTIAL",
                    "message": _safe_message(comments_result.message),
                    "declared_total": comments_result.detail.get("declared_total", 0),
                    "returned": len(batch),
                })
                stop_platform = True
            all_comments.extend(batch)

            if include_replies and batch and not stop_platform:
                for parent in batch[:5]:
                    replies = adapter.list_replies(cid, parent["platform_comment_id"], limit=reply_limit)
                    reply_items = replies.detail.get("comments", []) if replies.ok else []
                    _record_access_result(
                        db_path, task_id=run_id, adapter=adapter, result=replies, record_count=len(reply_items)
                    )
                    if replies.ok:
                        all_comments.extend(reply_items)
                    else:
                        summary["limited"].append(
                            {"capability": "replies", "content_id": cid, "status": replies.status}
                        )
                        if _should_stop_platform(replies.status):
                            stop_platform = True
                            break

            if stop_platform:
                break
            if collect_barrage:
                barrage = adapter.collect_barrage(cid, limit=barrage_limit)
                barrage_items = barrage.detail.get("barrage", []) if barrage.ok else []
                _record_access_result(
                    db_path, task_id=run_id, adapter=adapter, result=barrage, record_count=len(barrage_items)
                )
                if barrage.ok:
                    summary["barrage_count"] += len(barrage_items)
                else:
                    summary["limited"].append(
                        {"capability": "barrage", "content_id": cid, "status": barrage.status,
                         "message": _safe_message(barrage.message)}
                    )
                    if _should_stop_platform(barrage.status):
                        stop_platform = True
                        break

        comment_stats = upsert_comments(db_path, all_comments)
        summary["comments"] = comment_stats
        seen_total += comment_stats["seen"]
        summary["targets"] = [
            {"content_id": c["platform_content_id"], "title": str(c.get("title") or "")[:60]} for c in targets
        ]
        has_data = bool(content_stats["seen"] or comment_stats["seen"])
        if summary["errors"] or summary["limited"]:
            summary["status"] = "partial" if has_data else "failed"
        elif not targets:
            summary["status"] = "empty"
            summary["errors"].append("搜索没有返回可采集的内容。")
        else:
            summary["status"] = "finished"
        finish_run(
            db_path,
            run_id=run_id,
            status=summary["status"],
            seen=seen_total,
            inserted=content_stats["inserted"] + comment_stats["inserted"],
            updated=content_stats["updated"] + comment_stats["updated"],
            error_type=("PARTIAL" if summary["status"] == "partial" else "EMPTY" if summary["status"] == "empty" else ""),
        )
    except Exception as exc:  # isolate platform errors without claiming success
        summary["status"] = "partial" if seen_total else "failed"
        summary["errors"].append(f"{type(exc).__name__}: {_safe_message(str(exc)[:200])}")
        finish_run(
            db_path, run_id=run_id, status=summary["status"], seen=seen_total,
            error_type=type(exc).__name__,
        )
    finally:
        try:
            adapter.close()
        except Exception:
            pass

    return summary

def check_platform_access(platform: str, capability: str = "", *, live: bool = True) -> Dict[str, Any]:
    """检查平台能力可用性（状态来自**运行时探测**，不做静默降级）。

    第五轮：返回值额外包含 ``access_mode`` / ``runtime_status`` / ``evidence`` /
    ``checked_at``，以便区分"契约通过"与"真实访问成功"。
    """
    try:
        adapter = get_adapter(platform)
    except Exception as exc:
        return {"platform": platform, "capability": capability, "status": "UNAVAILABLE",
                "message": f"适配器不可用: {type(exc).__name__}: {exc}"}
    target = capability or ""
    result: AccessResult = adapter.check_access(target, live=live)
    payload = result.as_dict()
    try:
        payload["verification"] = adapter.platform_info().verification
    except Exception:
        payload["verification"] = "UNKNOWN"
    try:
        probe = adapter.probe_capability(target or "content_detail", live=live)
        payload["runtime_status"] = probe.status
        payload["probed"] = probe.probed
        payload["checked_at"] = probe.checked_at
        payload["capability_reason"] = probe.reason
        payload["capability_evidence"] = dict(probe.evidence)
    except Exception as exc:
        payload["runtime_status"] = "UNKNOWN"
        payload["capability_reason"] = f"探测失败: {type(exc).__name__}: {exc}"
    try:
        payload["access_modes"] = list(adapter.access_modes())
        payload["default_access_mode"] = adapter.default_access_mode()
        payload["login_free_capabilities"] = list(adapter.login_free_capabilities())
    except Exception:
        pass
    return payload


def resolve_platform_access(
    db_path: str,
    platform: str,
    capability: str = "comments",
    *,
    content_id: str = "",
    keyword: str = "",
    limit: int = 50,
    task_id: str = "",
    allow_live: bool = True,
    collect: bool = True,
    mode_order: tuple = (),
) -> Dict[str, Any]:
    """统一调度 + 落库留痕（第五轮主入口）。

    依次尝试：免登录公开 -> 本地已有真实数据 -> 需授权模式 -> 标注缺失，
    并把**每个模式的尝试结果**写入 ``access_records``，失败必须带原因。
    """
    from platform_dispatch import DEFAULT_MODE_ORDER

    outcome = resolve_access(
        platform,
        capability,
        db_path=db_path,
        content_id=content_id,
        keyword=keyword,
        limit=limit,
        task_id=task_id,
        mode_order=tuple(mode_order) or DEFAULT_MODE_ORDER,
        allow_live=allow_live,
        collect=collect,
    )
    if db_path:
        for attempt in outcome.attempts:
            record_access(
                db_path,
                task_id=outcome.task_id,
                platform=platform,
                capability=capability,
                access_mode=attempt.access_mode,
                status=attempt.status,
                ok=attempt.ok,
                source=attempt.source,
                record_count=attempt.record_count,
                failure_reason="" if attempt.ok else attempt.reason,
                citation=attempt.evidence,
                checked_at=outcome.checked_at,
            )
    payload = outcome.as_dict()
    payload["access_records"] = list_access_records(db_path, task_id=outcome.task_id) if db_path else []
    return payload
