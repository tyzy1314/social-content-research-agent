#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台采集总结输出与通知接口。默认只写 Markdown 文件，不进行远程推送。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence


def ensure_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _summary_output_path(summary_dir: str | Path = "outputs/summaries") -> Path:
    directory = ensure_directory(summary_dir)
    output_path = directory / f"summary_{datetime.now().strftime('%Y%m%d')}.md"
    if not output_path.exists():
        output_path.write_text("# 多平台内容采集日报\n\n", encoding="utf-8")
    return output_path


def write_summary_markdown(
    note: Dict[str, object],
    summary: Dict[str, object],
    summary_dir: str | Path = "outputs/summaries",
) -> Path:
    output_path = _summary_output_path(summary_dir)
    title = (
        note.get("title")
        or note.get("note_title")
        or note.get("search_title")
        or "未命名内容"
    )
    platform = (note.get("platform") or "").strip()
    header_meta = f" | {platform}" if platform else ""
    section_lines = [
        f"### {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{header_meta} | {title}",
        summary.get("summary_markdown") or "",
        "",
        "---",
        "",
    ]

    with open(output_path, "a", encoding="utf-8") as file:
        file.write("\n".join(section_lines))

    return output_path


def _render_note_card(note_report: Dict[str, object]) -> List[str]:
    note = note_report.get("note") or {}
    summary = note_report.get("summary") or {}
    status = str(note_report.get("status") or note.get("status") or "unknown")
    reason = str(note_report.get("reason") or note.get("fail_reason") or "").strip()
    focus_points = summary.get("focus_points") or []
    top_keywords = summary.get("top_keywords") or []
    representative_comments = summary.get("representative_comments") or []
    comment_count = int(note_report.get("comment_count") or summary.get("comment_count") or 0)
    platform = (note.get("platform") or "").strip()
    if not platform and "xiaohongshu.com" in str(note.get("clean_url") or note.get("raw_url") or ""):
        platform = "xiaohongshu"
    title = (
        note.get("title")
        or note.get("note_title")
        or note.get("search_title")
        or "未命名内容"
    )
    body_text = (
        summary.get("content_summary")
        or note.get("body")
        or note.get("note_content")
        or "未提取到明确正文。"
    )

    lines = [
        f"### {title}",
        f"- 平台：{platform or '?'}",
        f"- 状态：{status}",
        f"- 作者：{note.get('author_name') or ''}",
        f"- 内容链接：{note.get('clean_url') or note.get('raw_url') or ''}",
        f"- 评论数：{comment_count}",
        f"- 正文摘要：{body_text}",
        f"- 评论讨论摘要：{summary.get('comment_summary') or '暂无评论摘要'}",
        f"- 一句话总结：{summary.get('one_line_summary') or '暂无'}",
        f"- 用户关注点：{'、'.join(focus_points) if focus_points else '暂无明显关注点'}",
        f"- 高频词：{'、'.join(top_keywords) if top_keywords else '暂无明显高频词'}",
    ]

    if reason:
        lines.append(f"- 失败原因：{reason}")

    lines.extend(["", "#### 典型评论"])
    if representative_comments:
        for item in representative_comments:
            # 兼容 v3 中文键与以后的英文键
            username = item.get("username") or item.get("用户名") or "匿名用户"
            content = item.get("content") or item.get("评论内容") or ""
            lines.append(f"- {username}：{content}")
    else:
        lines.append("- 当前无可展示的典型评论")

    lines.extend(["", "---", ""])
    return lines


def write_stream_run_report(
    *,
    keyword: str,
    run_stats: Dict[str, object],
    note_reports: Sequence[Dict[str, object]],
    summary_dir: str | Path = "outputs/summaries",
) -> Path:
    output_path = _summary_output_path(summary_dir)
    unavailable_records = run_stats.get("unavailable_records") or []
    failed_records = run_stats.get("failed_records") or []
    top_block_reasons = run_stats.get("top_block_reasons") or []

    platform = str(run_stats.get("platform") or "").strip()
    header = f"## 本次采集概览 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if platform:
        header += f" | {platform}"

    lines = [
        header,
        f"- 关键词：{keyword}",
        f"- 扫描候选数：{run_stats.get('scanned_candidates', 0)}",
        f"- 成功采集条目数：{run_stats.get('crawled', 0)}",
        f"- skipped_non_note：{run_stats.get('skipped_non_note', 0)}",
        f"- skipped_author_profile：{run_stats.get('skipped_author_profile', 0)}",
        f"- unavailable：{run_stats.get('unavailable', 0)}",
        f"- no_comments：{run_stats.get('no_comments', 0)}",
        f"- failed：{run_stats.get('failed', 0)}",
        f"- 评论总数：{run_stats.get('total_comments', 0)}",
        f"- clicked_profile：{run_stats.get('clicked_profile', 0)}",
        f"- fast_skip_count：{run_stats.get('fast_skip_count', 0)}",
        f"- blocked_count：{run_stats.get('blocked_count', 0)}",
        f"- login_required_count：{run_stats.get('login_required_count', 0)}",
        f"- captcha_required_count：{run_stats.get('captcha_required_count', 0)}",
        f"- rate_limited_count：{run_stats.get('rate_limited_count', 0)}",
        f"- skipped_robots_count：{run_stats.get('skipped_robots_count', 0)}",
        f"- circuit_breaker_triggered：{run_stats.get('circuit_breaker_triggered', False)}",
        "",
    ]

    if note_reports:
        lines.append("## 内容卡片")
        lines.append("")
        for note_report in note_reports:
            lines.extend(_render_note_card(note_report))

    lines.extend(
        [
            "## 失败和跳过统计",
            f"- skipped_non_note 数量：{run_stats.get('skipped_non_note', 0)}",
            f"- skipped_author_profile 数量：{run_stats.get('skipped_author_profile', 0)}",
            f"- fast_skip_count：{run_stats.get('fast_skip_count', 0)}",
            f"- blocked_count：{run_stats.get('blocked_count', 0)}",
            f"- cooldown_platforms：{', '.join(run_stats.get('cooldown_platforms') or []) or '无'}",
            "",
        ]
    )

    lines.append("### top_block_reasons")
    if top_block_reasons:
        for item in top_block_reasons:
            lines.append(f"- {item.get('reason') or ''}：{item.get('count') or 0}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("### unavailable 列表")
    if unavailable_records:
        for item in unavailable_records:
            lines.append(f"- {item.get('url') or ''} | {item.get('reason') or '网页端不可访问或 App-only'}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("### failed 列表")
    if failed_records:
        for item in failed_records:
            lines.append(f"- {item.get('url') or ''} | {item.get('status') or 'failed'} | {item.get('reason') or ''}")
    else:
        lines.append("- 无")
    lines.extend(["", "---", ""])

    with open(output_path, "a", encoding="utf-8") as file:
        file.write("\n".join(lines))

    return output_path


def send_email(note: Dict[str, object], summary: Dict[str, object], config: Dict[str, object]) -> bool:
    return False


def send_wecom(note: Dict[str, object], summary: Dict[str, object], config: Dict[str, object]) -> bool:
    return False


def send_feishu(note: Dict[str, object], summary: Dict[str, object], config: Dict[str, object]) -> bool:
    return False


def send_dingtalk(note: Dict[str, object], summary: Dict[str, object], config: Dict[str, object]) -> bool:
    return False


def notify_summary(
    note: Dict[str, object],
    summary: Dict[str, object],
    notify_config: Dict[str, object] | None = None,
    dashboard_stats: Dict[str, object] | None = None,
    summary_dir: str | Path = "outputs/summaries",
) -> Path:
    output_path = write_summary_markdown(note, summary, summary_dir=summary_dir)
    notify_config = notify_config or {}

    if not notify_config.get("enabled", False):
        return output_path

    channels = notify_config.get("channels") or []
    for channel in channels:
        channel = str(channel).strip().lower()
        if channel == "email":
            send_email(note, summary, notify_config.get("email") or {})
        elif channel == "wecom":
            send_wecom(note, summary, notify_config.get("wecom") or {})
        elif channel == "feishu":
            send_feishu(note, summary, notify_config.get("feishu") or {})
        elif channel == "dingtalk":
            send_dingtalk(note, summary, notify_config.get("dingtalk") or {})

    return output_path
