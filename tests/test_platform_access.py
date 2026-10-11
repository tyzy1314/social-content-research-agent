#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第五轮：免登录接入与智能体自动降级测试。

覆盖要求的 10 个测试类别：
  1. 无凭据启动（默认路径必须能工作）
  2. 本地已有数据可分析（无登录）
  3. 真实公开免登录访问（联网，B站）
  4. 缺 OAuth 凭据时优雅降级（不崩溃、准确报错）
  5. 401/403/412/429 等限制信号处理（停止并记录，不绕过）
  6. 多平台错误隔离（一个平台失败不影响其他）
  7. 去重与幂等（重复调度不产生重复记录）
  8. 智能体模式选择（调度链按预期选中模式）
  9. 真实评论与引用可回溯验证
 10. 完整的多平台报告（缺失项明确标注）

原则：任何测试都不得用 mock 数据冒充真实采集；契约通过 ≠ 真实访问成功。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from platform_dispatch import local_inventory, resolve_access
from platforms import get_adapter, list_platforms
from platforms.capabilities import (
    ALL_ACCESS_MODES,
    CAP_AVAILABLE,
    CAP_NOT_SUPPORTED,
    MODE_LOCAL_DATABASE,
    MODE_OFFICIAL_API,
    MODE_PUBLIC_READONLY,
    MODE_UNAVAILABLE,
    MODE_USER_IMPORT,
)
from unified_store import list_access_records, migrate, record_access, upsert_comments, upsert_content

LIVE = os.environ.get("PLATFORM_LIVE_TESTS") == "1"


def _fresh_db(tmp_path: Path) -> str:
    path = tmp_path / "unified.db"
    migrate(path, with_backup=False)
    return str(path)


def _seed_bilibili(db_path: str, n: int = 25) -> None:
    upsert_content(db_path, [{
        "platform": "bilibili",
        "platform_content_id": "BV_SEED_0001",
        "content_type": "video",
        "title": "真实样本视频",
        "text": "desc",
        "source_url": "https://www.bilibili.com/video/BV_SEED_0001",
        "engagement_metrics": {"view": 100},
        "raw_metadata": {"keyword": "南京美食"},
        "source_method": "seed",
    }], run_id="seed_run")
    upsert_comments(db_path, [{
        "platform": "bilibili",
        "platform_comment_id": f"seed_{i}",
        "platform_content_id": "BV_SEED_0001",
        "content": f"真实样本评论 {i}：这家店真的好吃" if i % 3 else f"真实样本评论 {i}：排队太久不好",
        "parent_comment_id": "",
        "author_ref": f"user{i}",
        "published_at": "2026-05-01T00:00:00+00:00",
        "provenance": {"source_method": "seed"},
    } for i in range(n)])


# --------------------------------------------------------------------------- #
# 类别 1：无凭据启动
# --------------------------------------------------------------------------- #
def test_01_no_credentials_startup_works():
    """不配置任何平台凭据时，系统必须能启动并给出可用路径。"""
    infos = list_platforms()
    assert infos, "无凭据状态下仍应能列出平台"

    # 未配置凭据时，抖音不得声称可用
    douyin = get_adapter("douyin")
    status = douyin.probe_capability("comments", live=False)
    assert status.status != CAP_AVAILABLE, "无凭据时抖音不得标记为 AVAILABLE"
    assert status.reason, "必须给出不可用的原因"

    # 至少存在一个无需凭据即可用的平台路径
    usable = []
    for info in infos:
        if info["name"] in ("bilibili", "xiaohongshu"):
            usable.append(info["name"])
    assert usable, "应至少存在免登录/本地数据可用的平台"


def test_01_every_platform_declares_valid_access_modes():
    """每个平台必须声明接入模式，且模式必须在六统一模式集合内。"""
    for info in list_platforms():
        modes = info.get("access_modes") or []
        assert modes, f"{info['name']} 未声明接入模式"
        for mode in modes:
            assert mode in ALL_ACCESS_MODES, f"{info['name']} 声明了非法接入模式 {mode}"
        assert info.get("default_access_mode") in ALL_ACCESS_MODES


# --------------------------------------------------------------------------- #
# 类别 2：本地已有数据可分析（免登录）
# --------------------------------------------------------------------------- #
def test_02_local_database_analysis_without_login(tmp_path: Path):
    """无需任何凭据，本地真实数据即可被选中用于分析。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)

    outcome = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    assert outcome.ok is True
    assert outcome.chosen_access_mode == MODE_LOCAL_DATABASE
    assert outcome.record_count == 25
    assert outcome.sufficient is True, "25 条 >= 阈值 20，应判定为充分"
    assert outcome.citations, "必须带可回溯引用"
    assert outcome.citations[0]["sample_ids"], "引用必须含评论 ID"


def test_02_xiaohongshu_login_free_uses_local_data(tmp_path: Path):
    """小红书线上需登录，但免登录可用本地已有真实数据。"""
    db = _fresh_db(tmp_path)
    upsert_comments(db, [{
        "platform": "xiaohongshu",
        "platform_comment_id": f"xhs_{i}",
        "platform_content_id": "xhs_note_1",
        "content": "南京美食真实评论",
        "provenance": {"source_method": "local_import"},
    } for i in range(30)])

    outcome = resolve_access("xiaohongshu", "comments", db_path=db, limit=50, allow_live=False)
    assert outcome.ok is True
    assert outcome.chosen_access_mode == MODE_LOCAL_DATABASE
    # 且线上能力不得被误报为免登录可用
    adapter = get_adapter("xiaohongshu")
    assert adapter.login_free_capabilities() == (), "小红书线上能力不得声明为免登录"


def test_02_local_data_insufficient_is_marked(tmp_path: Path):
    """本地数据不足时，必须标注『样本不足』而不是静默产出。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=3)
    outcome = resolve_access("bilibili", "comments", db_path=db, limit=50,
                             allow_live=False, min_sufficient=20)
    assert outcome.ok is True
    assert outcome.sufficient is False
    assert "样本不足" in outcome.missing_note


# --------------------------------------------------------------------------- #
# 类别 3：真实公开免登录访问（联网）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="需要联网，设置 PLATFORM_LIVE_TESTS=1 启用")
def test_03_real_public_readonly_access():
    """B站公开接口必须能真实取到数据（非 mock）。"""
    outcome = resolve_access("bilibili", "content_detail", content_id="BV1YtAoz3Er3",
                             db_path="", allow_live=True)
    assert outcome.ok is True, f"真实公开访问失败: {outcome.status} {outcome.reason}"
    assert outcome.chosen_access_mode == MODE_PUBLIC_READONLY
    assert outcome.record_count >= 1
    assert outcome.citations[0]["sample_ids"], "必须带真实内容 ID 作为引用"


@pytest.mark.skipif(not LIVE, reason="需要联网，设置 PLATFORM_LIVE_TESTS=1 启用")
def test_03_real_comments_are_verifiable():
    """真实取到的评论必须可回溯：有 ID、有文本、有来源端点。"""
    adapter = get_adapter("bilibili")
    result = adapter.list_comments("BV1YtAoz3Er3", limit=20)
    assert result.ok, f"评论获取失败: {result.status} {result.message}"
    comments = result.detail["comments"]
    assert comments, "不得返回空列表冒充成功"
    first = comments[0]
    assert first["platform_comment_id"], "评论必须有平台原始 ID"
    assert first["content"], "评论必须有正文"
    assert result.evidence.get("endpoint"), "必须记录来源端点"


def test_03_status_is_runtime_not_hardcoded():
    """运行时状态必须来自探测：live=False 时不得声称 AVAILABLE（除已有凭据/会话外）。"""
    adapter = get_adapter("bilibili")
    offline = adapter.probe_capability("comments", live=False)
    # 无缓存时离线探测必须标记为未探测且非 AVAILABLE
    if not offline.probed:
        assert offline.status != CAP_AVAILABLE
        assert offline.evidence.get("not_probed_reason") or offline.reason


# --------------------------------------------------------------------------- #
# 类别 4：缺 OAuth 凭据时优雅降级
# --------------------------------------------------------------------------- #
def test_04_missing_oauth_degrades_gracefully(tmp_path: Path):
    """缺抖音凭据：不崩溃，返回 AUTH_REQUIRED + 可操作指引。"""
    db = _fresh_db(tmp_path)
    outcome = resolve_access("douyin", "comments", db_path=db, allow_live=False)
    assert outcome.ok is False
    assert outcome.status == "AUTH_REQUIRED"
    assert "DOUYIN_CLIENT_KEY" in (outcome.auth_guidance + outcome.reason)
    assert outcome.auth_guidance, "必须给出授权指引"
    assert "需授权" in outcome.missing_note or "缺失" in outcome.missing_note
    # 关键：绝不能产出任何数据
    assert outcome.record_count == 0
    assert not outcome.data.get("items")


def test_04_douyin_never_claims_login_free():
    """抖音不得声明任何免登录能力（无合法免登录只读路径）。"""
    adapter = get_adapter("douyin")
    assert adapter.login_free_capabilities() == ()
    assert MODE_PUBLIC_READONLY not in adapter.access_modes()


def test_04_weibo_reports_real_block_reason():
    """微博：无凭据时必须给出具体阻塞原因，不得空泛报错。"""
    adapter = get_adapter("weibo")
    status = adapter.probe_capability("search_content", live=False)
    assert status.status != CAP_AVAILABLE
    # 未探测时必须写明"未发起请求"的原因
    if not status.probed:
        assert status.evidence.get("not_probed_reason")


# --------------------------------------------------------------------------- #
# 类别 5：限制信号处理（401/403/412/429）
# --------------------------------------------------------------------------- #
def test_05_rate_limit_maps_to_rate_limited_status():
    """HTTP 412 必须映射为 RATE_LIMITED，而不是成功或空数据。"""
    from platforms import bilibili_api
    from platforms.capabilities import CAP_RATE_LIMITED

    adapter = bilibili_api.BilibiliApiAdapter()

    class _FakeResponse:
        status = 412

        def read(self):
            return b"<html>risk control</html>"

    def fake_request(url, *, accept="application/json"):
        return 412, b"<html>risk control</html>"

    adapter._request = fake_request  # type: ignore[assignment]
    status = adapter.probe_capability("search_content", live=True)
    assert status.status == CAP_RATE_LIMITED
    assert "412" in status.reason
    assert "不换 IP" in status.reason or "不绕过" in status.reason, "必须显式声明不做绕过"


def test_05_403_maps_to_blocked():
    """HTTP 403 必须映射为 BLOCKED（平台拒绝访客访问）。"""
    from platforms import bilibili_api
    from platforms.capabilities import CAP_BLOCKED

    adapter = bilibili_api.BilibiliApiAdapter()
    adapter._request = lambda url, *, accept="application/json": (403, b"forbidden")  # type: ignore[assignment]
    status = adapter.probe_capability("content_detail", live=True)
    assert status.status == CAP_BLOCKED


def test_05_search_content_reports_rate_limited_not_empty_success():
    """搜索被限流时，返回状态为 RATE_LIMITED 且不带 items（绝不空列表冒充成功）。"""
    from platforms import bilibili_api

    adapter = bilibili_api.BilibiliApiAdapter()
    adapter._request = lambda url, *, accept="application/json": (412, b"risk")  # type: ignore[assignment]
    result = adapter.search_content("南京美食", limit=5)
    assert result.status == "RATE_LIMITED"
    assert result.ok is False
    assert not result.detail.get("items")
    assert "BV" in result.message, "必须给出降级建议（改用明确 BV 号）"


# --------------------------------------------------------------------------- #
# 类别 6：多平台错误隔离
# --------------------------------------------------------------------------- #
def test_06_platform_failure_is_isolated(tmp_path: Path):
    """某平台失败不得影响其他平台的调度结果。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)

    failed = resolve_access("douyin", "comments", db_path=db, allow_live=False)
    assert failed.ok is False

    ok = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    assert ok.ok is True, "抖音失败不得影响 B站/本地路径"
    assert ok.record_count == 25


def test_06_each_platform_has_independent_status():
    """各平台能力状态互相独立，不共享硬编码结论。"""
    statuses = {}
    for name in ("bilibili", "douyin", "xiaohongshu", "weibo", "kuaishou", "zhihu", "tieba"):
        try:
            adapter = get_adapter(name)
        except Exception:
            continue
        statuses[name] = adapter.probe_capability("comments", live=False).status
    assert len(statuses) >= 5
    # 至少存在两种不同的状态（证明不是统一硬编码）
    assert len(set(statuses.values())) >= 2, f"状态过于一致，疑似硬编码: {statuses}"


# --------------------------------------------------------------------------- #
# 类别 7：去重与幂等
# --------------------------------------------------------------------------- #
def test_07_repeated_resolve_is_idempotent(tmp_path: Path):
    """重复调度不重复写入数据；接入记录按次追加（审计留痕）。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    _seed_bilibili(db, n=25)  # 重复写入

    with sqlite3.connect(db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM platform_comments WHERE platform='bilibili'").fetchone()[0]
    assert count == 25, f"重复写入产生重复记录: {count}"

    first = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    second = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    assert first.record_count == second.record_count == 25


def test_07_access_records_are_append_only(tmp_path: Path):
    """接入记录持续追加，且字段完整（九要素）。"""
    db = _fresh_db(tmp_path)
    for i in range(3):
        record_access(
            db, task_id="T1", platform="bilibili", capability="comments",
            access_mode=MODE_PUBLIC_READONLY, status="OK", ok=True,
            source="https://api.bilibili.com/x/v2/reply", record_count=10,
            citation={"sample_ids": ["1", "2"]},
        )
    rows = list_access_records(db, task_id="T1")
    assert len(rows) == 3, "接入记录必须逐条留痕"
    row = rows[0]
    for key in ("task_id", "platform", "capability", "access_mode", "status",
                "source", "failure_reason", "checked_at", "citation"):
        assert key in row, f"接入记录缺少字段 {key}"
    assert row["citation"]["sample_ids"] == ["1", "2"]


def test_07_failure_records_carry_reason(tmp_path: Path):
    """失败记录必须带失败原因，不能只有状态。"""
    db = _fresh_db(tmp_path)
    record_access(db, task_id="T2", platform="douyin", capability="comments",
                  access_mode=MODE_OFFICIAL_API, status="AUTH_REQUIRED", ok=False,
                  failure_reason="缺少 DOUYIN_CLIENT_KEY")
    rows = list_access_records(db, task_id="T2")
    assert rows[0]["failure_reason"] == "缺少 DOUYIN_CLIENT_KEY"
    assert rows[0]["ok"] is False


# --------------------------------------------------------------------------- #
# 类别 8：智能体模式选择
# --------------------------------------------------------------------------- #
def test_08_dispatch_prefers_public_readonly_when_available(tmp_path: Path):
    """公开免登录可用且给了明确 ID 时，应优先 PUBLIC_READONLY。"""
    from platforms import bilibili_api

    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)  # 本地也有数据，但公开优先

    adapter = bilibili_api.BilibiliApiAdapter()
    adapter._request = lambda url, *, accept="application/json": (  # type: ignore[assignment]
        200, json.dumps({
            "code": 0, "message": "OK",
            "data": {"page": {"count": 2}, "replies": [
                {"rpid": 1, "root": 0, "parent": 0, "content": {"message": "真实评论一"},
                 "member": {"uname": "u1"}, "ctime": 1700000000, "like": 1, "rcount": 0},
                {"rpid": 2, "root": 0, "parent": 0, "content": {"message": "真实评论二"},
                 "member": {"uname": "u2"}, "ctime": 1700000001, "like": 2, "rcount": 0},
            ]},
        }).encode(),
    )
    import platform_dispatch

    original = platform_dispatch.get_adapter

    def patched(name):
        if name == "bilibili":
            return adapter
        return original(name)

    platform_dispatch.get_adapter = patched
    try:
        # 注意：必须是符合 B站 BV 号格式的 ID（BV + 10 位字母数字），否则适配器会判为 INVALID_INPUT
        outcome = resolve_access("bilibili", "comments", db_path=db, content_id="BV1YtAoz3Er3",
                                 limit=2, allow_live=True, min_sufficient=1)
        assert outcome.chosen_access_mode == MODE_PUBLIC_READONLY
        assert outcome.record_count == 2
    finally:
        platform_dispatch.get_adapter = original


def test_08_dispatch_falls_back_to_local_when_public_unavailable(tmp_path: Path):
    """公开路径不可用时，应回退到本地已有真实数据。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    # allow_live=False 模拟公开路径不可用
    outcome = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    assert outcome.chosen_access_mode == MODE_LOCAL_DATABASE
    assert outcome.ok is True
    # 尝试记录中必须能看到 PUBLIC_READONLY 被跳过及其原因
    skipped = [a for a in outcome.attempts if a.access_mode == MODE_PUBLIC_READONLY]
    assert skipped and skipped[0].ok is False


def test_08_unsupported_capability_is_not_silently_skipped():
    """平台不具备的能力必须明确 NOT_SUPPORTED，不得静默降级成别的东西。"""
    outcome = resolve_access("zhihu", "comments", db_path="", allow_live=False)
    assert outcome.status == CAP_NOT_SUPPORTED
    assert "平台不支持" in outcome.missing_note


def test_08_local_mode_not_abused_for_unstorable_capability(tmp_path: Path):
    """弹幕本地不落库，不得用评论条数冒充弹幕可用。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    outcome = resolve_access("bilibili", "barrage", db_path=db, allow_live=False)
    assert outcome.ok is False, "弹幕无本地数据，不得误判为可用"
    local_attempt = [a for a in outcome.attempts if a.access_mode == MODE_LOCAL_DATABASE]
    assert local_attempt and local_attempt[0].record_count == 0


# --------------------------------------------------------------------------- #
# 类别 9：真实评论与引用可回溯验证
# --------------------------------------------------------------------------- #
def test_09_citations_are_traceable(tmp_path: Path):
    """引用必须包含：平台、接入模式、来源、样本 ID、文本片段、时间。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    outcome = resolve_access("bilibili", "comments", db_path=db, limit=50, allow_live=False)
    citation = outcome.citations[0]
    assert citation["platform"] == "bilibili"
    assert citation["access_mode"] == MODE_LOCAL_DATABASE
    assert citation["source"]
    assert citation["sample_ids"]
    assert citation["sample_excerpt"]
    # 引用的 ID 必须真的能在库里查到
    with sqlite3.connect(db) as connection:
        for cid in citation["sample_ids"]:
            found = connection.execute(
                "SELECT 1 FROM platform_comments WHERE platform=? AND platform_comment_id=?",
                ("bilibili", cid)).fetchone()
            assert found, f"引用 ID {cid} 在库中不存在，引用不可回溯"


def test_09_no_fabricated_data_when_all_paths_fail(tmp_path: Path):
    """所有路径失败时不得产出任何数据或伪造引用。"""
    db = _fresh_db(tmp_path)
    outcome = resolve_access("douyin", "comments", db_path=db, allow_live=False)
    assert outcome.ok is False
    assert outcome.citations == []
    assert outcome.record_count == 0
    assert outcome.reason, "必须给出失败原因"


# --------------------------------------------------------------------------- #
# 类别 10：完整的多平台报告（缺失项明确标注）
# --------------------------------------------------------------------------- #
def test_10_multi_platform_dispatch_report(tmp_path: Path):
    """一次调度多个平台：可用与不可用都必须被记录，缺失项明确标注。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    upsert_comments(db, [{
        "platform": "xiaohongshu",
        "platform_comment_id": f"xhs_{i}",
        "platform_content_id": "note_1",
        "content": "南京美食本地真实评论",
        "provenance": {"source_method": "local"},
    } for i in range(30)])

    report = {}
    for platform in ("bilibili", "xiaohongshu", "douyin", "weibo", "zhihu"):
        outcome = resolve_access(platform, "comments", db_path=db, limit=50, allow_live=False)
        report[platform] = {
            "access_mode": outcome.chosen_access_mode,
            "status": outcome.status,
            "ok": outcome.ok,
            "record_count": outcome.record_count,
            "missing_note": outcome.missing_note,
        }

    assert report["bilibili"]["ok"] is True
    assert report["xiaohongshu"]["ok"] is True
    assert report["douyin"]["ok"] is False
    assert report["weibo"]["ok"] is False
    assert report["zhihu"]["ok"] is False
    # 每个失败平台都必须有明确标注或原因
    for platform in ("douyin", "weibo", "zhihu"):
        assert report[platform]["status"] != "OK"
    # 报告不得把不可用平台写成 0 条"成功"
    for platform, item in report.items():
        if not item["ok"]:
            assert item["record_count"] == 0


def test_10_inventory_reports_real_counts(tmp_path: Path):
    """本地库存盘点必须反映真实行数。"""
    db = _fresh_db(tmp_path)
    _seed_bilibili(db, n=25)
    inventory = local_inventory(db, "bilibili")
    assert inventory == {"content": 1, "comments": 25}
    assert local_inventory(db, "douyin") == {"content": 0, "comments": 0}
