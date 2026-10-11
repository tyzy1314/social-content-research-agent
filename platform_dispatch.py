#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一接入调度与自动降级（第五轮核心）。

解决的问题：让用户**不必为分析公开内容而配置多个平台账号**。

调度链（每一步都有真实依据，不臆断）：

    1. 识别平台与能力（适配器声明）
    2. 盘点本地已有真实数据（LOCAL_DATABASE，不发网络请求）
    3. 尝试免登录公开只读（PUBLIC_READONLY）真实探测/采集
    4. 公开路径被限流/拒绝 -> 回退本地已有数据（若足够）
    5. 仍不足 -> 判定是否需要授权（OFFICIAL_API / AUTHORIZED_SESSION），
       返回准确的错误与授权指引（**不伪造**任何数据）
    6. 数据不足 -> 明确标注"缺失"，不静默产出空分析

硬约束（与安全合规相关，任何修改都不得违反）：
* 遇到 401/403/412/429 等限制信号立即停止该能力请求，记录原因；
* **绝不**更换 IP / UA / 账号重试，**绝不**伪造请求头或破解签名；
* **绝不**用模拟数据冒充真实采集；
* 契约测试通过 ≠ 真实访问成功，两者分别记录。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from platforms import get_adapter
from platforms.capabilities import (
    CAP_AVAILABLE,
    CAP_COMMENTS,
    CAP_RATE_LIMITED,
    DEFAULT_MODE_ORDER,
    MODE_AUTHORIZED_SESSION,
    MODE_LOCAL_DATABASE,
    MODE_OFFICIAL_API,
    MODE_PUBLIC_READONLY,
    MODE_UNAVAILABLE,
    MODE_USER_IMPORT,
    AccessResult,
    now_iso,
)

# 判定"本地数据足够"的最小样本量（可按需调整，但阈值本身要写进报告）
MIN_SUFFICIENT_COMMENTS = 20


def _now() -> str:
    return now_iso()


@dataclass
class ModeAttempt:
    """一次模式尝试的结果（成功与否都记录，失败必须带原因）。"""

    access_mode: str
    status: str
    ok: bool
    reason: str = ""
    source: str = ""            # 来源：接口 URL / 数据表 / 用户文件
    record_count: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "access_mode": self.access_mode,
            "status": self.status,
            "ok": self.ok,
            "reason": self.reason,
            "source": self.source,
            "record_count": self.record_count,
            "evidence": self.evidence,
        }


@dataclass
class DispatchOutcome:
    """统一调度结果：可直接写入报告与统一记录表。"""

    task_id: str
    platform: str
    capability: str
    chosen_access_mode: str
    status: str                      # OK / AUTH_REQUIRED / RATE_LIMITED / UNAVAILABLE / NOT_SUPPORTED / INSUFFICIENT
    ok: bool
    reason: str = ""
    source: str = ""
    record_count: int = 0
    sufficient: bool = False
    attempts: List[ModeAttempt] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)   # 原始引用（可回溯）
    missing_note: str = ""           # 数据缺失时的明确标注
    auth_guidance: str = ""          # 需要授权时的操作指引
    checked_at: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "capability": self.capability,
            "access_mode": self.chosen_access_mode,
            "status": self.status,
            "ok": self.ok,
            "reason": self.reason,
            "source": self.source,
            "record_count": self.record_count,
            "sufficient": self.sufficient,
            "attempts": [a.as_dict() for a in self.attempts],
            "citations": self.citations,
            "missing_note": self.missing_note,
            "auth_guidance": self.auth_guidance,
            "checked_at": self.checked_at,
            "data": self.data,
        }


def local_inventory(db_path: str, platform: str) -> Dict[str, int]:
    """盘点本地统一库中该平台的真实数据量（只读，不发网络请求）。"""
    empty = {"content": 0, "comments": 0}
    if not db_path:
        return empty
    try:
        connection = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "platform_content" not in tables or "platform_comments" not in tables:
                return empty
            content = connection.execute(
                "SELECT COUNT(*) FROM platform_content WHERE platform=?", (platform,)).fetchone()[0]
            comments = connection.execute(
                "SELECT COUNT(*) FROM platform_comments WHERE platform=?", (platform,)).fetchone()[0]
            return {"content": int(content), "comments": int(comments)}
        finally:
            connection.close()
    except sqlite3.Error:
        return empty


def _fetch_local_rows(db_path: str, platform: str, capability: str, limit: int) -> List[Dict[str, Any]]:
    """按能力读取本地已有真实数据（带原始引用，可回溯）。

    能力不匹配时返回空列表：例如本地库没有弹幕表，就不该用"评论条数"冒充弹幕可用。
    """
    if not db_path:
        return []
    if capability not in (CAP_COMMENTS, "replies", "content_detail"):
        return []
    try:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            if capability == "content_detail":
                rows = connection.execute(
                    "SELECT platform_content_id, title, text, source_url, published_at "
                    "FROM platform_content WHERE platform=? ORDER BY id LIMIT ?",
                    (platform, max(1, limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT platform_comment_id, platform_content_id, content, parent_comment_id, "
                    "author_ref, published_at FROM platform_comments WHERE platform=? "
                    "ORDER BY platform_comment_id LIMIT ?", (platform, max(1, limit)),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def resolve_access(
    platform: str,
    capability: str = CAP_COMMENTS,
    *,
    db_path: str = "",
    content_id: str = "",
    keyword: str = "",
    limit: int = 50,
    task_id: str = "",
    mode_order: tuple[str, ...] = DEFAULT_MODE_ORDER,
    allow_live: bool = True,
    min_sufficient: int = MIN_SUFFICIENT_COMMENTS,
    collect: bool = True,
) -> DispatchOutcome:
    """统一调度：按模式链尝试，返回**真实**可用的数据来源与完整降级依据。

    Args:
        platform: 平台名。
        capability: 能力名，默认取评论。
        db_path: 统一库路径（用于 LOCAL_DATABASE 模式）。
        content_id: 明确的内容 ID/链接（免登录公开采集优先使用）。
        keyword: 关键词（仅对支持搜索且未被限流的能力生效）。
        limit: 需要的记录数。
        mode_order: 模式尝试顺序，默认 ``DEFAULT_MODE_ORDER``。
        allow_live: 是否允许真实网络请求（False 时只做本地与凭据判定）。
        collect: 公开模式可用时是否真的拉取数据（False 仅做能力判定）。

    Returns:
        DispatchOutcome：含选定模式、逐模式尝试记录、原始引用与缺失标注。
    """
    task_id = task_id or f"task_{platform}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    attempts: List[ModeAttempt] = []
    citations: List[Dict[str, Any]] = []

    try:
        adapter = get_adapter(platform)
    except Exception as exc:
        return DispatchOutcome(
            task_id=task_id, platform=platform, capability=capability,
            chosen_access_mode=MODE_UNAVAILABLE, status="UNAVAILABLE", ok=False,
            reason=f"适配器不可用：{type(exc).__name__}: {exc}", checked_at=_now(),
        )

    declared = set(adapter.capabilities())
    if capability not in declared:
        return DispatchOutcome(
            task_id=task_id, platform=platform, capability=capability,
            chosen_access_mode=MODE_UNAVAILABLE, status="NOT_SUPPORTED", ok=False,
            reason=f"{platform} 未实现 {capability} 能力（适配器声明：{sorted(declared)}）",
            checked_at=_now(),
            missing_note=f"{platform} 的 {capability} 能力不存在，报告中标注为『平台不支持』",
        )

    target_id = content_id
    probe = adapter.probe_capability(capability, live=allow_live)

    for mode in mode_order:
        if mode == MODE_PUBLIC_READONLY:
            if not allow_live:
                attempts.append(ModeAttempt(mode, "SKIPPED", False, "已禁止真实网络请求（allow_live=False）"))
                continue
            if probe.status != CAP_AVAILABLE:
                attempts.append(ModeAttempt(
                    mode, probe.status, False, probe.reason,
                    source=str(probe.evidence.get("endpoint", "")), evidence=dict(probe.evidence),
                ))
                continue
            if not target_id and capability != "search_content":
                # 未给明确 ID：若该平台搜索可用，先用搜索定位内容（1 次请求）再采集。
                # 搜索被限流时如实记录并继续降级，不重试、不绕过。
                resolved_id, search_attempt = _resolve_id_by_search(
                    adapter, keyword, search_limit=max(1, min(limit, 5))
                )
                if search_attempt is not None:
                    attempts.append(search_attempt)
                if resolved_id:
                    target_id = resolved_id
                else:
                    attempts.append(ModeAttempt(
                        mode, "INVALID_INPUT", False,
                        "免登录公开采集需要明确内容 ID/链接；关键词搜索不可用（可能被限流），无法凭关键词定位内容",
                    ))
                    continue
            if not collect:
                attempts.append(ModeAttempt(mode, CAP_AVAILABLE, True, probe.reason,
                                            source=str(probe.evidence.get("endpoint", "")),
                                            evidence=dict(probe.evidence)))
                return DispatchOutcome(
                    task_id=task_id, platform=platform, capability=capability,
                    chosen_access_mode=mode, status="OK", ok=True, reason=probe.reason,
                    source=str(probe.evidence.get("endpoint", "")), sufficient=False,
                    attempts=attempts, citations=[dict(probe.evidence)], checked_at=_now(),
                )
            result: AccessResult = _invoke(adapter, capability, target_id, keyword, limit)
            attempts.append(ModeAttempt(
                mode, result.status, result.ok, result.message,
                source=str(result.evidence.get("endpoint", "")) or f"{platform}:{target_id}",
                record_count=_count(result), evidence=dict(result.evidence),
            ))
            if result.ok:
                for item in _citations_from(result, platform):
                    citations.append(item)
                records = _count(result)
                return DispatchOutcome(
                    task_id=task_id, platform=platform, capability=capability,
                    chosen_access_mode=mode, status="OK", ok=True, reason=result.message,
                    source=str(result.evidence.get("endpoint", "")) or f"{platform}:{target_id}",
                    record_count=records, sufficient=records >= min_sufficient,
                    attempts=attempts, citations=citations, checked_at=_now(),
                    missing_note=("" if records >= min_sufficient else
                                  f"公开数据仅 {records} 条 < 充分阈值 {min_sufficient}，报告中标注为『样本不足』"),
                    data={"items": _items(result)},
                )

        elif mode == MODE_LOCAL_DATABASE:
            inventory = local_inventory(db_path, platform)
            rows = _fetch_local_rows(db_path, platform, capability, limit)
            # 本地库只存内容与评论；弹幕等能力本地无对应数据，不能拿评论条数冒充
            storable = capability in (CAP_COMMENTS, "replies", "content_detail")
            attempts.append(ModeAttempt(
                mode, "OK" if rows else ("EMPTY" if storable else "NOT_STORED"),
                bool(rows),
                (f"本地统一库：{inventory['content']} 条内容 / {inventory['comments']} 条评论"
                 + ("" if storable else "（该能力本地不落库，无可用本地数据）")),
                source=f"sqlite:{db_path}#{'platform_comments' if capability != 'content_detail' else 'platform_content'}",
                record_count=len(rows),
                evidence={"inventory": inventory, "capability": capability},
            ))
            if rows:
                id_key = "platform_comment_id" if capability != "content_detail" else "platform_content_id"
                text_key = "content" if capability != "content_detail" else "title"
                citations = [{
                    "platform": platform, "access_mode": MODE_LOCAL_DATABASE,
                    "source": f"sqlite:{db_path}#{'platform_comments' if capability != 'content_detail' else 'platform_content'}",
                    "sample_ids": [str(r.get(id_key) or "") for r in rows[:5]],
                    "sample_excerpt": [str(r.get(text_key) or "")[:60] for r in rows[:3]],
                    "retrieved_at": _now(),
                }]
                return DispatchOutcome(
                    task_id=task_id, platform=platform, capability=capability,
                    chosen_access_mode=mode, status="OK", ok=True,
                    reason=f"使用本地已有真实数据（{len(rows)} 条{'评论' if capability != 'content_detail' else '内容'}）",
                    source=f"sqlite:{db_path}#{'platform_comments' if capability != 'content_detail' else 'platform_content'}",
                    record_count=len(rows), sufficient=len(rows) >= min_sufficient,
                    attempts=attempts, citations=citations, checked_at=_now(),
                    missing_note=("" if len(rows) >= min_sufficient else
                                  f"本地数据仅 {len(rows)} 条 < 充分阈值 {min_sufficient}，报告中标注为『样本不足』"),
                    data={"items": rows, "inventory": inventory},
                )

        elif mode in (MODE_OFFICIAL_API, MODE_AUTHORIZED_SESSION):
            modes = set(adapter.access_modes())
            if mode not in modes:
                attempts.append(ModeAttempt(mode, "NOT_SUPPORTED", False, f"{platform} 不支持 {mode} 接入模式"))
                continue
            status = probe.status
            ok = status == CAP_AVAILABLE
            attempts.append(ModeAttempt(
                mode, status, ok, probe.reason,
                source=str(probe.evidence.get("oauth_url", "")) if not ok else "",
                evidence=dict(probe.evidence),
            ))
            if ok:
                return DispatchOutcome(
                    task_id=task_id, platform=platform, capability=capability,
                    chosen_access_mode=mode, status="OK", ok=True, reason=probe.reason,
                    source=f"{platform}_{mode}", attempts=attempts, checked_at=_now(),
                )
            guidance = _auth_guidance(platform, mode, adapter)
            return DispatchOutcome(
                task_id=task_id, platform=platform, capability=capability,
                chosen_access_mode=mode, status=status if status != CAP_AVAILABLE else "AUTH_REQUIRED",
                ok=False, reason=probe.reason, attempts=attempts,
                auth_guidance=guidance, checked_at=_now(),
                missing_note=f"{platform} 的 {capability} 需授权才能取得，报告中标注为『需授权，未采集』",
            )

        elif mode == MODE_USER_IMPORT:
            attempts.append(ModeAttempt(
                mode, "AUTH_REQUIRED", False,
                "需用户提供内容链接/数据文件后导入（用户主动提供，系统不代为抓取）",
            ))

        elif mode == MODE_UNAVAILABLE:
            attempts.append(ModeAttempt(mode, "UNAVAILABLE", False, "无可用接入路径"))

    # 全部模式都不可用：原因取自**真实尝试**，不使用缓存探测结论
    failed = [a for a in attempts if not a.ok]
    final_status = "UNAVAILABLE"
    for attempt in failed:
        if attempt.status == CAP_RATE_LIMITED:
            final_status = "RATE_LIMITED"
            break
        if attempt.status == "AUTH_REQUIRED":
            final_status = "AUTH_REQUIRED"
        elif attempt.status == "BLOCKED" and final_status == "UNAVAILABLE":
            final_status = "BLOCKED"
    reason = "；".join(f"{a.access_mode}:{a.reason}" for a in failed[:3]) or "所有接入模式均不可用"
    return DispatchOutcome(
        task_id=task_id, platform=platform, capability=capability,
        chosen_access_mode=MODE_UNAVAILABLE, status=final_status, ok=False,
        reason=reason,
        attempts=attempts, checked_at=_now(),
        auth_guidance=_auth_guidance(platform, "", adapter),
        missing_note=f"{platform} 的 {capability} 无合法可用数据路径，报告中标注为『数据缺失』",
    )


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _invoke(adapter, capability: str, content_id: str, keyword: str, limit: int) -> AccessResult:
    if capability == CAP_COMMENTS:
        return adapter.list_comments(content_id, limit=limit)
    if capability == "replies":
        return adapter.list_replies(content_id, "", limit=limit)
    if capability == "barrage":
        return adapter.collect_barrage(content_id, limit=limit)
    if capability == "content_detail":
        return adapter.get_content_detail(content_id)
    if capability == "search_content":
        return adapter.search_content(keyword, limit=limit)
    if capability == "creator_content":
        return adapter.list_creator_content(content_id, limit=limit)
    return AccessResult(adapter.platform_name, capability, "NOT_SUPPORTED", f"未知能力 {capability}")


def _resolve_id_by_search(adapter, keyword: str, *, search_limit: int = 3):
    """用关键词搜索定位内容 ID（仅发 1 次请求）。

    Returns:
        ``(content_id, attempt)``；搜索不可用或限流时 ``content_id`` 为空，
        并把失败原因放进 ``attempt``，绝不重试、绝不绕过。
    """
    if not keyword or "search_content" not in set(adapter.capabilities()):
        return "", None
    try:
        result = adapter.search_content(keyword, limit=search_limit)
    except Exception as exc:
        return "", ModeAttempt(
            MODE_PUBLIC_READONLY, "UNAVAILABLE", False,
            f"搜索定位内容失败：{type(exc).__name__}: {exc}",
            source="search_content",
        )
    attempt = ModeAttempt(
        MODE_PUBLIC_READONLY, result.status, result.ok,
        f"用搜索定位内容：{result.message}",
        source=str(result.evidence.get("endpoint", "")) or "search_content",
        record_count=len(result.detail.get("items") or []),
        evidence=dict(result.evidence),
    )
    if not result.ok:
        return "", attempt
    items = result.detail.get("items") or []
    for item in items:
        cid = str(item.get("platform_content_id") or "")
        if cid:
            return cid, attempt
    return "", attempt


def _count(result: AccessResult) -> int:
    detail = result.detail or {}
    for key in ("comments", "items", "barrage"):
        if isinstance(detail.get(key), list):
            return len(detail[key])
    if "content" in detail:
        return 1
    return 0


def _items(result: AccessResult) -> List[Dict[str, Any]]:
    detail = result.detail or {}
    for key in ("comments", "items", "barrage"):
        if isinstance(detail.get(key), list):
            return list(detail[key])
    if "content" in detail:
        return [detail["content"]]
    return []


def _citations_from(result: AccessResult, platform: str) -> List[Dict[str, Any]]:
    """生成可回溯的原始引用（评论 ID + 文本片段 + 来源端点）。"""
    items = _items(result)
    if not items:
        return []
    citation: Dict[str, Any] = {
        "platform": platform,
        "access_mode": MODE_PUBLIC_READONLY,
        "source": str(result.evidence.get("endpoint", "")),
        "http_status": result.evidence.get("http_status"),
        "sample_ids": [str(i.get("platform_comment_id") or i.get("platform_content_id") or "") for i in items[:5]],
        "sample_excerpt": [str(i.get("content") or i.get("title") or "")[:60] for i in items[:3]],
        "retrieved_at": _now(),
    }
    return [citation]


def _auth_guidance(platform: str, mode: str, adapter) -> str:
    """需要授权时给出**具体、可执行**的指引（不虚构"已授权"）。"""
    if platform == "douyin":
        return ("配置抖音开放平台凭据后重试：设置环境变量 DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET，"
                "完成 OAuth 授权得到 DOUYIN_ACCESS_TOKEN。注意：授权后仅可取得授权账号自己的视频与评论，"
                "不含全站任意视频评论。")
    if platform == "xiaohongshu":
        return ("小红书线上数据需登录态：在有可用浏览器会话（已登录）的环境下运行抓取流程，"
                "或改用本地已有真实数据（LOCAL_DATABASE）进行分析。系统不绕过登录墙。")
    if platform in ("weibo", "kuaishou"):
        return (f"{platform} 需通过官方开放平台创建应用并通过审核，取得 App Key/Secret 后授权；"
                "未授权前不做未授权抓取。")
    if platform in ("zhihu", "tieba"):
        return f"{platform} 无官方开放接口且需登录态，建议改用用户主动导入（USER_IMPORT）或本地已有数据。"
    return "该平台需授权后才能取得数据；可改用本地已有数据或用户导入。"
