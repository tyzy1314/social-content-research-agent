#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终封箱 Smoke Test：验证四条核心业务链路仍然真实可用。

1. Agent 工具注册（18 个）
2. B站真实公开分析（详情/评论）
3. 跨平台报告生成（含自动数字复核）
4. 免登录降级链（小红书→本地数据 / 抖音→需授权）

不做任何架构改动，只读 + 受控写入报告目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    print("=" * 70)
    print("最终封箱 Smoke Test")
    print("=" * 70)

    # ---- 1. Agent 工具注册 ----
    print("\n[1] Agent 工具注册")
    from agent.state import AgentStore
    from agent_tools import build_default_registry

    # 用 outputs/ 下的一次性库（该目录已 gitignore）；不临时目录，避免 Windows 文件占用导致清理失败
    smoke_db = ROOT / "outputs" / "smoke_agent_runs.db"
    store = AgentStore(str(smoke_db))
    registry = build_default_registry(store)
    names = sorted(registry.names())
    check("工具数量 = 18", len(names) == 18, f"实际 {len(names)}")
    for expected in ("probe_platform_capabilities", "resolve_platform_access",
                     "analyze_platform_content", "compare_platforms",
                     "generate_cross_platform_report", "check_platform_access"):
        check(f"工具存在: {expected}", expected in names)

    # ---- 2. B站真实公开分析 ----
    print("\n[2] B站真实公开分析（免登录，真实网络请求）")
    from platforms import get_adapter

    adapter = get_adapter("bilibili")
    detail = adapter.get_content_detail("BV1YtAoz3Er3")
    check("视频详情获取成功", detail.ok, f"{detail.status} {detail.message[:60]}")
    if detail.ok:
        content = detail.detail["content"]
        check("详情含真实标题", bool(content["title"]), content["title"][:40])
        check("详情含真实播放量", bool(content["engagement_metrics"].get("view")),
              f"view={content['engagement_metrics'].get('view')}")

    comments = adapter.list_comments("BV1YtAoz3Er3", limit=20)
    check("评论获取成功", comments.ok, f"{comments.status} {comments.message[:80]}")
    if comments.ok:
        items = comments.detail["comments"]
        check("评论非空（不是空列表冒充成功）", len(items) > 0, f"{len(items)} 条")
        check("评论可回溯（有平台 ID）", all(i["platform_comment_id"] for i in items))
        check("记录了来源端点", bool(comments.evidence.get("endpoint")))

    # ---- 3. 跨平台报告 ----
    print("\n[3] 跨平台报告（含自动数字复核）")
    from cross_platform import analyze_platform, compare_platforms

    db = str(ROOT / "data" / "xhs_crawler.db")
    xhs = analyze_platform(db, "xiaohongshu", keyword="南京美食", limit=300)
    bili = analyze_platform(db, "bilibili", keyword="南京美食", limit=100)
    check("小红书分析有真实样本", xhs.get("data_scope", {}).get("comments_analyzed", 0) > 0,
          f"{xhs.get('data_scope', {}).get('comments_analyzed')} 条")
    check("B站分析有真实样本", bili.get("data_scope", {}).get("comments_analyzed", 0) > 0,
          f"{bili.get('data_scope', {}).get('comments_analyzed')} 条")
    check("分析报告声明方法为词典基线", "lexicon" in str(bili.get("sentiment", {}).get("method", "")).lower()
          or "规则" in str(bili.get("sentiment", {}).get("method", "")))

    comparison = compare_platforms(db, ["xiaohongshu", "bilibili"], keyword="南京美食")
    analyzed = comparison.get("platforms_analyzed", [])
    check("跨平台比较返回两平台", len(analyzed) == 2, f"{analyzed}")
    check("不混算互动指标（有可比性说明）", bool(comparison.get("comparability_notes")))
    check("情感对比表含两平台",
          len(comparison.get("sentiment_comparison", {})) == 2,
          str(comparison.get("sentiment_comparison", {}).keys()))

    # ---- 4. 免登录降级链 ----
    print("\n[4] 免登录降级链")
    from platform_service import resolve_platform_access

    xhs_out = resolve_platform_access(db, "xiaohongshu", "comments", keyword="南京美食",
                                      limit=50, allow_live=False)
    check("小红书 → 本地数据模式", xhs_out["access_mode"] == "LOCAL_DATABASE",
          f"{xhs_out['access_mode']} / {xhs_out['status']}")
    check("小红书免登录成功", xhs_out["ok"] is True, f"{xhs_out['record_count']} 条")

    douyin_out = resolve_platform_access(db, "douyin", "comments", allow_live=False)
    check("抖音 → 需授权", douyin_out["status"] == "AUTH_REQUIRED", douyin_out["status"])
    check("抖音未产出任何数据（不伪造）", douyin_out["record_count"] == 0 and not douyin_out["data"].get("items"))
    check("抖音给出授权指引", bool(douyin_out["auth_guidance"]))

    # 微博走真实探测（1 次请求）：预期被访客系统拦截，绝不是 AVAILABLE
    weibo_out = resolve_platform_access(db, "weibo", "search_content", keyword="南京美食",
                                        allow_live=True)
    check("微博未误报可用", weibo_out["ok"] is False and weibo_out["status"] != "AVAILABLE",
          weibo_out["status"])
    check("微博给出具体阻塞原因", bool(weibo_out["reason"]), weibo_out["reason"][:70])

    # ---- 5. 统一接入记录 ----
    print("\n[5] 统一接入记录留痕")
    from unified_store import list_access_records

    records = list_access_records(db, limit=5)
    check("接入记录非空", len(records) > 0, f"{len(records)} 条（最近）")
    if records:
        row = records[0]
        check("记录含九要素", all(k in row for k in
              ("task_id", "platform", "capability", "access_mode", "status",
               "source", "record_count", "failure_reason", "checked_at", "citation")))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"Smoke Test 失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("Smoke Test 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
