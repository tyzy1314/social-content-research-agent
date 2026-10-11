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

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List

from platform_dispatch import resolve_access
from platforms import get_adapter
from platforms.capabilities import AccessResult
from unified_store import (
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
) -> Dict[str, Any]:
    """采集一个平台的内容与评论并写入统一库。

    Args:
        db_path: 统一库路径。
        platform: 平台名。
        content_id: 指定内容 ID（优先）。
        keyword: 关键词（``content_id`` 为空时用搜索取前 ``search_limit`` 条）。
        topic_label: 主题标签；写入 ``raw_metadata.keyword``，使后续跨平台比较能按同主题筛选。
        comment_limit: 每条内容的评论上限。
        include_replies: 是否抓取二级评论。
        collect_barrage: 是否抓取弹幕（仅支持平台）。

    Returns:
        汇总字典：run_id / status / 各部分计数 / 受限能力说明。
    """
    run_id = new_collection_run_id(platform)
    adapter = get_adapter(platform)
    access_method = ""
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
        targets: List[Dict[str, Any]] = []

        if content_id:
            detail = adapter.get_content_detail(content_id)
            if not detail.ok:
                summary["status"] = "failed"
                summary["errors"].append(f"{content_id}: {detail.status} {detail.message}")
                finish_run(db_path, run_id=run_id, status="failed", error_type=detail.status)
                return summary
            targets.append(detail.detail["content"])
        elif keyword:
            search = adapter.search_content(keyword, limit=search_limit)
            if not search.ok:
                summary["status"] = "failed"
                summary["errors"].append(f"search '{keyword}': {search.status} {search.message}")
                finish_run(db_path, run_id=run_id, status="failed", error_type=search.status)
                return summary
            for item in search.detail.get("items", []):
                # 搜索结果字段不完整，逐个取详情以补全统计与正文
                detail = adapter.get_content_detail(item["platform_content_id"])
                if detail.ok:
                    targets.append(detail.detail["content"])
                else:
                    summary["errors"].append(f"detail {item['platform_content_id']}: {detail.status}")
        else:
            summary["status"] = "failed"
            summary["errors"].append("需要提供 content_id 或 keyword")
            finish_run(db_path, run_id=run_id, status="failed", error_type="INVALID_INPUT")
            return summary

        # 记录主题标签，保证后续跨平台比较可以按同主题筛选（可复现，非事后编造）。
        label = topic_label or keyword
        if label:
            for content in targets:
                metadata = dict(content.get("raw_metadata") or {})
                metadata["keyword"] = label
                content["raw_metadata"] = metadata

        content_stats = upsert_content(db_path, targets, run_id=run_id)
        summary["contents"] = content_stats
        seen_total += content_stats["seen"]

        all_comments: List[Dict[str, Any]] = []
        for content in targets:
            cid = content["platform_content_id"]
            comments_result = adapter.list_comments(cid, limit=comment_limit)
            if not comments_result.ok:
                summary["errors"].append(f"comments {cid}: {comments_result.status} {comments_result.message}")
                continue
            batch = comments_result.detail.get("comments", [])
            all_comments.extend(batch)

            if include_replies and batch:
                for parent in batch[:5]:  # 仅对前若干条取二级评论，控制请求量
                    replies = adapter.list_replies(cid, parent["platform_comment_id"], limit=reply_limit)
                    if replies.ok:
                        all_comments.extend(replies.detail.get("comments", []))
                    else:
                        summary["limited"].append(
                            {"capability": "replies", "content_id": cid, "status": replies.status}
                        )

            if collect_barrage:
                barrage = adapter.collect_barrage(cid, limit=barrage_limit)
                if barrage.ok:
                    summary["barrage_count"] += len(barrage.detail.get("barrage", []))
                else:
                    summary["limited"].append(
                        {"capability": "barrage", "content_id": cid, "status": barrage.status,
                         "message": barrage.message}
                    )

        comment_stats = upsert_comments(db_path, all_comments)
        summary["comments"] = comment_stats
        seen_total += comment_stats["seen"]
        summary["targets"] = [
            {"content_id": c["platform_content_id"], "title": c["title"][:60]} for c in targets
        ]
        summary["status"] = "finished"
        finish_run(
            db_path, run_id=run_id, status="finished", seen=seen_total,
            inserted=content_stats["inserted"] + comment_stats["inserted"],
            updated=content_stats["updated"] + comment_stats["updated"],
        )
    except Exception as exc:  # 错误隔离：记录后返回，不污染其他平台
        summary["status"] = "failed"
        summary["errors"].append(f"{type(exc).__name__}: {exc}")
        finish_run(db_path, run_id=run_id, status="failed", seen=seen_total, error_type=type(exc).__name__)

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
