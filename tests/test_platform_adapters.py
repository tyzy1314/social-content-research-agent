#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台适配层测试（第四轮）。

分层说明：
* Tier 1 契约测试：能力声明、错误处理、不支持能力的显式状态、归一化字段映射 —— **不联网**。
* Tier 2 集成测试：统一数据层 + 分析链路，使用**真实格式**的样本数据（非 mock 平台响应）。

真实验证（Tier 3 真实平台访问）不在本文件内，见
``docs/agent/acceptance-report-round4.md`` 的真实运行记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platforms import ADAPTERS, list_platforms
from platforms.capabilities import (
    AUTH_REQUIRED,
    BLOCKED,
    LIVE_VERIFIED,
    NOT_IMPLEMENTED,
    NOT_SUPPORTED,
    UNAVAILABLE,
)
from unified_store import (
    fetch_comments,
    migrate,
    platform_counts,
    upsert_comments,
    upsert_content,
)

VALID_GRADES = {LIVE_VERIFIED, "DATA_VERIFIED", "CONTRACT_VERIFIED", BLOCKED, NOT_IMPLEMENTED}

# --------------------------------------------------------------------------- #
# Tier 1 — 契约
# --------------------------------------------------------------------------- #


def test_every_platform_declares_honest_verification_grade():
    infos = list_platforms()
    assert infos, "注册表不能为空"
    for info in infos:
        assert info["verification"] in VALID_GRADES, f"{info['name']} 验收等级非法: {info['verification']}"
        # 不得用空能力 + BLOCKED 之外的方式冒充支持
        if not info["capabilities"]:
            assert info["verification"] in (BLOCKED, NOT_IMPLEMENTED), \
                f"{info['name']} 无能力却标为 {info['verification']}"


def test_bilibili_declares_real_capabilities():
    from platforms.bilibili_api import BilibiliApiAdapter

    adapter = BilibiliApiAdapter()
    caps = adapter.capabilities()
    for expected in ("search_content", "content_detail", "comments", "replies", "barrage"):
        assert expected in caps
    assert adapter.platform_info().verification == LIVE_VERIFIED


def test_bilibili_unsupported_capability_is_explicit():
    """不支持的能力必须返回 NOT_SUPPORTED，不得返回空数据伪装成功。"""
    from platforms.bilibili_api import BilibiliApiAdapter

    result = BilibiliApiAdapter().list_creator_content("123")
    assert result.status == NOT_SUPPORTED
    assert result.ok is False
    assert not (result.detail.get("items") or result.detail.get("comments"))


def test_douyin_requires_auth_and_never_fakes_success():
    from platforms.douyin import DouyinAdapter

    adapter = DouyinAdapter()  # 无凭据
    assert adapter.has_credentials is False
    info = adapter.platform_info()
    assert info.verification == BLOCKED and info.auth_required is True

    for capability in adapter.capabilities():
        result = adapter.check_access(capability)
        assert result.status == AUTH_REQUIRED, f"{capability} 在无凭据时不得为 {result.status}"
        assert result.ok is False

    # 无 token 时不得返回空数据伪装成功
    comments = adapter.list_comments("any", limit=5)
    assert comments.ok is False and comments.status == AUTH_REQUIRED


def test_douyin_oauth_path_is_implemented_not_stubbed():
    from platforms.douyin import DouyinAdapter

    assert DouyinAdapter().build_oauth_url() == ""  # 无 client_key 时不得编造 URL
    with_key = DouyinAdapter(client_key="test_key")
    url = with_key.build_oauth_url()
    assert url.startswith("https://open.douyin.com/platform/oauth/connect/")
    assert "client_key=test_key" in url
    # 换取 token 需要 key + secret 两者齐全
    with_both = DouyinAdapter(client_key="test_key", client_secret="test_secret")
    payload = with_both.exchange_code_for_token("code")
    assert "oauth/access_token" in payload["endpoint"], payload
    assert "refresh_token" in with_both.refresh_token("rt")["endpoint"]


def test_douyin_does_not_claim_global_search():
    """不得把授权账号接口冒充为全站搜索/任意视频评论接口。"""
    from platforms.douyin import DouyinAdapter

    result = DouyinAdapter(client_key="k", client_secret="s", access_token="t").search_content("x")
    assert result.status == NOT_SUPPORTED


@pytest.mark.parametrize("platform", ["kuaishou", "weibo", "zhihu", "tieba"])
def test_pending_platforms_are_blocked_not_faked(platform):
    from platforms import get_adapter

    adapter = get_adapter(platform)
    info = adapter.platform_info()
    assert info.verification in (BLOCKED, NOT_IMPLEMENTED)
    # 数据方法必须返回明确阻塞状态，而不是空结果
    result = adapter.list_comments("x")
    assert result.ok is False and result.status in (AUTH_REQUIRED, NOT_SUPPORTED)


def test_normalization_maps_real_bilibili_fields():
    """用真实响应格式的样本校验字段映射（非 mock 平台访问）。"""
    from platforms.bilibili_api import BilibiliApiAdapter

    adapter = BilibiliApiAdapter()
    raw_video = {
        "bvid": "BV1GJ411x7h7", "aid": 80433022, "cid": 137649199,
        "title": "标题", "desc": "简介", "pubdate": 1577835803,
        "owner": {"name": "UP主", "mid": 1},
        "stat": {"view": 107108584, "reply": 237242, "like": 2951003},
    }
    content = adapter.normalize_content(raw_video)
    assert content.platform == "bilibili"
    assert content.platform_content_id == "BV1GJ411x7h7"
    assert content.author_ref == "UP主"
    assert content.engagement_metrics["view"] == 107108584  # 取自 data.stat
    assert content.published_at.startswith("2019-")

    raw_comment = {
        "rpid": 3760801399, "root": 0, "parent": 0,
        "content": {"message": "一级评论"},
        "member": {"uname": "用户"}, "like": 10, "ctime": 1577835803,
    }
    comment = adapter.normalize_comment(raw_comment, platform_content_id="BV1GJ411x7h7")
    assert comment.platform_comment_id == "3760801399"
    assert comment.parent_comment_id == ""        # 一级评论
    assert comment.is_reply is False

    raw_reply = {"rpid": 2, "root": 3760801399, "parent": 3760801399,
                 "content": {"message": "二级"}, "member": {"uname": "u"}}
    reply = adapter.normalize_comment(raw_reply, platform_content_id="BV1GJ411x7h7")
    assert reply.parent_comment_id == "3760801399"  # 二级评论关系明确
    assert reply.is_reply is True


# --------------------------------------------------------------------------- #
# Tier 2 — 统一数据层（真实格式样本，不联网）
# --------------------------------------------------------------------------- #


def _seed(db_path: Path) -> None:
    migrate(db_path, with_backup=False)
    upsert_content(db_path, [
        {"platform": "bilibili", "platform_content_id": "BV1", "title": "b站视频",
         "raw_metadata": {"keyword": "南京美食"}, "source_method": "bilibili_public_api"},
        {"platform": "xiaohongshu", "platform_content_id": "xhs_1", "title": "小红书笔记",
         "raw_metadata": {"keyword": "南京美食"}, "source_method": "xhs_legacy_import"},
    ], run_id="seed")
    upsert_comments(db_path, [
        {"platform": "bilibili", "platform_comment_id": "b1", "platform_content_id": "BV1",
         "content": "这个太好吃了，推荐", "parent_comment_id": ""},
        {"platform": "bilibili", "platform_comment_id": "b2", "platform_content_id": "BV1",
         "content": "体验很差，失望", "parent_comment_id": "b1"},
        {"platform": "xiaohongshu", "platform_comment_id": "x1", "platform_content_id": "xhs_1",
         "content": "不错，感谢分享", "parent_comment_id": ""},
    ])


def test_migration_creates_unified_tables(tmp_path):
    db = tmp_path / "u.db"
    report = migrate(db, with_backup=False)
    # 第四轮三张表 + 第五轮统一接入记录表
    assert report["schema_version"] >= 3
    assert {"platform_content", "platform_comments", "platform_runs", "access_records"} <= set(report["tables"])


def test_same_raw_id_on_different_platforms_does_not_collide(tmp_path):
    db = tmp_path / "u.db"
    migrate(db, with_backup=False)
    upsert_content(db, [
        {"platform": "bilibili", "platform_content_id": "SAME_ID", "title": "b"},
        {"platform": "xiaohongshu", "platform_content_id": "SAME_ID", "title": "x"},
    ])
    counts = {row["platform"]: row["content_count"] for row in platform_counts(db)}
    assert counts == {"bilibili": 1, "xiaohongshu": 1}, "不同平台相同原始 ID 必须共存"


def test_upsert_is_idempotent(tmp_path):
    db = tmp_path / "u.db"
    migrate(db, with_backup=False)
    record = {"platform": "bilibili", "platform_content_id": "BV1", "title": "t"}
    first = upsert_content(db, [record])
    second = upsert_content(db, [record])
    assert first["inserted"] == 1 and second["inserted"] == 0 and second["updated"] == 1
    assert platform_counts(db)[0]["content_count"] == 1, "重复执行不得无限插入"


def test_comment_parent_relation_is_persisted(tmp_path):
    db = tmp_path / "u.db"
    _seed(db)
    rows = {row["platform_comment_id"]: row for row in fetch_comments(db, platform="bilibili")}
    assert rows["b1"]["parent_comment_id"] == ""
    assert rows["b2"]["parent_comment_id"] == "b1", "二级评论必须指向父评论"


def test_platform_filter_and_counts(tmp_path):
    db = tmp_path / "u.db"
    _seed(db)
    counts = {row["platform"]: row for row in platform_counts(db)}
    assert counts["bilibili"]["comment_count"] == 2
    assert counts["xiaohongshu"]["comment_count"] == 1


def test_analysis_reports_insufficient_data_instead_of_guessing(tmp_path):
    from cross_platform import analyze_platform

    db = tmp_path / "u.db"
    _seed(db)
    result = analyze_platform(str(db), "douyin")
    assert result["ok"] is False and result["reason"] == "insufficient_data"
    assert "sentiment" not in result


def test_analysis_on_seeded_real_format_data(tmp_path):
    from cross_platform import analyze_platform

    db = tmp_path / "u.db"
    _seed(db)
    result = analyze_platform(str(db), "bilibili")
    assert result["ok"] is True
    assert result["data_scope"]["comments_analyzed"] == 2
    assert result["sentiment"]["counts"]["正面"] == 1
    assert result["sentiment"]["counts"]["负面"] == 1
    assert result["evidence"], "结论必须附可追溯引用"
    assert "非训练模型" in result["sentiment"]["method"], "必须声明情感为规则基线而非训练模型"


def test_cross_platform_comparison_lists_unavailable_platforms(tmp_path):
    from cross_platform import compare_platforms

    db = tmp_path / "u.db"
    _seed(db)
    result = compare_platforms(str(db), ["bilibili", "douyin"], keyword="南京美食")
    assert result["platforms_analyzed"] == ["bilibili"]
    assert result["platforms_unavailable"][0]["platform"] == "douyin", "不可用平台必须如实列出"
    # 不得用可用平台数据推测不可用平台
    assert "douyin" not in result["sentiment_comparison"]


def test_report_verification_recomputes_numbers(tmp_path):
    from cross_platform import compare_platforms, generate_report

    db = tmp_path / "u.db"
    _seed(db)
    comparison = compare_platforms(str(db), ["bilibili", "xiaohongshu"], keyword="南京美食")
    report = generate_report(comparison, title="测试报告")
    assert Path(report["json_path"]).exists() and Path(report["markdown_path"]).exists()
    verification = report["verification"]
    assert verification["counts_sum_consistent"] is True
    assert verification["ratios_match_recomputation"] is True, "报告数字必须可复算"
    assert verification["evidence_present"] is True
