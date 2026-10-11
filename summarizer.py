#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则版总结器，后续可替换为大模型实现。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Sequence

import jieba.analyse

from ai_client import BaseAIClient, build_ai_client

POSITIVE_WORDS = ["好", "棒", "赞", "喜欢", "支持", "优秀", "厉害", "不错", "推荐", "满意"]
NEGATIVE_WORDS = ["差", "烂", "垃圾", "失望", "后悔", "不行", "问题", "踩雷", "难用", "翻车"]

FOCUS_PATTERNS = {
    "价格与性价比": ["价格", "贵", "便宜", "性价比", "值不值", "划算"],
    "效果与质量": ["效果", "质量", "好用", "不好用", "耐用", "做工", "真实"],
    "使用方法": ["怎么", "如何", "请问", "有没有", "可以吗", "步骤", "教程"],
    "外观与款式": ["颜色", "款式", "尺码", "搭配", "外观", "设计"],
    "安全与成分": ["成分", "安全", "过敏", "刺激", "孕妇", "敏感肌"],
    "物流与售后": ["物流", "发货", "到货", "客服", "售后", "退货"],
    "口味与体验": ["味道", "口感", "好吃", "难吃", "推荐", "排队", "环境"],
}

# 点赞数单位换算：平台在过万后只显示 "1.2万" / "3.5w" 这类缩写
LIKE_UNIT_MULTIPLIERS = {"万": 10000, "w": 10000, "亿": 100000000, "千": 1000, "k": 1000}


def parse_like_count(value: str) -> int:
    """解析点赞数，支持 "1.2万" / "3.5w" 等带单位的缩写写法。"""
    text = str(value or "").replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万亿千wWkK]?)", text)
    if not match:
        return 0
    multiplier = LIKE_UNIT_MULTIPLIERS.get(match.group(2).lower(), 1)
    return int(float(match.group(1)) * multiplier)


def normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def truncate_text(text: str, limit: int = 140) -> str:
    value = normalize_text(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


class RuleBasedSummarizer:
    def __init__(self, ai_client: BaseAIClient | None = None, ai_enabled: bool = False):
        self.ai_client = ai_client or build_ai_client({"enabled": False})
        self.ai_enabled = ai_enabled

    def summarize(self, note: Dict[str, object], comments: Sequence[Dict[str, str]]) -> Dict[str, object]:
        return self.summarize_note_and_comments(note, comments, use_ai=self.ai_enabled)

    def summarize_note_and_comments(
        self,
        note: Dict[str, object],
        comments: Sequence[Dict[str, str]],
        use_ai: bool = False,
    ) -> Dict[str, object]:
        normalized_comments = [self._normalize_comment(item) for item in comments]
        comment_texts = [item["评论内容"] for item in normalized_comments if item["评论内容"]]
        note_text = normalize_text(note.get("note_content") or "")
        raw_note_text = normalize_text(note.get("raw_note_text") or "")
        content_source = note_text or raw_note_text
        combined_text = "\n".join([content_source, *comment_texts]).strip()

        content_summary = self._summarize_note_content(note)
        comment_summary = self._summarize_comment_discussion(comment_texts)
        top_keywords = self._extract_top_keywords(combined_text)
        representative_comments = self._pick_representative_comments(normalized_comments)
        focus_points = self._extract_focus_points(comment_texts, content_source, top_keywords)
        sentiment = self._analyze_sentiment(comment_texts, content_source)
        one_line_summary = self._build_one_line_summary(
            note=note,
            comment_count=len(comment_texts),
            content_summary=content_summary,
            comment_summary=comment_summary,
            focus_points=focus_points,
            sentiment=sentiment,
        )
        ai_summary = self._maybe_generate_ai_summary(note, normalized_comments, use_ai=use_ai)
        summary_markdown = self._build_markdown(
            note=note,
            comment_count=len(comment_texts),
            content_summary=content_summary,
            comment_summary=comment_summary,
            top_keywords=top_keywords,
            representative_comments=representative_comments,
            focus_points=focus_points,
            sentiment=sentiment,
            one_line_summary=one_line_summary,
            ai_summary=ai_summary,
        )

        return {
            "comment_count": len(comment_texts),
            "top_keywords": top_keywords,
            "representative_comments": representative_comments,
            "focus_points": focus_points,
            "sentiment": sentiment,
            "one_line_summary": one_line_summary,
            "content_summary": content_summary,
            "comment_summary": comment_summary,
            "ai_summary": ai_summary,
            "ai_provider": getattr(self.ai_client, "provider", "") if ai_summary else "",
            "ai_model": getattr(self.ai_client, "model", "") if ai_summary else "",
            "summary_markdown": summary_markdown,
        }

    def summarize_unavailable(self, note: Dict[str, object], reason: str) -> Dict[str, object]:
        reason = reason or "网页端不可访问或 App-only"
        title = note.get("note_title") or note.get("title") or note.get("search_title") or "未命名笔记"
        summary_markdown = "\n".join(
            [
                f"## {title}",
                f"- 状态：unavailable",
                f"- 关键词：{note.get('keyword') or ''}",
                f"- 作者：{note.get('author_name') or ''}",
                f"- 笔记链接：{note.get('clean_url') or note.get('raw_url') or ''}",
                f"- 原因：{reason}",
                "",
                "### 笔记内容摘要",
                truncate_text(note.get("note_content") or note.get("raw_note_text") or "网页端不可访问，未抓取到笔记正文。"),
                "",
                "### 一句话总结",
                "该笔记网页端不可访问，未采集评论。",
            ]
        ).strip()
        return {
            "comment_count": 0,
            "top_keywords": [],
            "representative_comments": [],
            "focus_points": ["网页端不可访问"],
            "sentiment": "无数据",
            "one_line_summary": "该笔记网页端不可访问，未采集评论。",
            "content_summary": truncate_text(note.get("note_content") or note.get("raw_note_text") or ""),
            "comment_summary": "未获取评论。",
            "ai_summary": "",
            "ai_provider": "",
            "ai_model": "",
            "summary_markdown": summary_markdown,
        }

    def summarize_no_comments(self, note: Dict[str, object], reason: str = "") -> Dict[str, object]:
        final_reason = reason or "未找到评论或解析失败"
        base = self.summarize_note_and_comments(note, [], use_ai=False)
        title = note.get("note_title") or note.get("title") or note.get("search_title") or "未命名笔记"
        base["one_line_summary"] = "该笔记网页端可打开，但本轮未获取到评论。"
        base["comment_summary"] = "当前未抓取到评论，以下总结主要基于笔记正文。"
        base["summary_markdown"] = "\n".join(
            [
                f"## {title}",
                f"- 状态：no_comments",
                f"- 关键词：{note.get('keyword') or ''}",
                f"- 作者：{note.get('author_name') or ''}",
                f"- 笔记链接：{note.get('clean_url') or note.get('raw_url') or ''}",
                f"- 原因：{final_reason}",
                "",
                "### 笔记内容摘要",
                base["content_summary"] or "未提取到明确笔记正文。",
                "",
                "### 评论讨论摘要",
                base["comment_summary"],
                "",
                "### 用户关注点",
                *[f"- {focus}" for focus in base["focus_points"]],
                "",
                "### 一句话总结",
                base["one_line_summary"],
            ]
        ).strip()
        return base

    def _normalize_comment(self, comment: Dict[str, str]) -> Dict[str, str]:
        return {
            "用户名": normalize_text(comment.get("用户名", "") or comment.get("username", "") or comment.get("author_name", "")),
            "评论内容": normalize_text(comment.get("评论内容", "") or comment.get("content", "")),
            "原始文本": comment.get("原始文本", "") or comment.get("raw_text", "") or "",
            "发布时间": normalize_text(comment.get("发布时间", "") or comment.get("publish_time", "")),
            "点赞数": normalize_text(comment.get("点赞数", "") or comment.get("likes", "") or comment.get("like_count", "")),
        }

    def _extract_top_keywords(self, full_text: str, top_k: int = 8) -> List[str]:
        if not full_text.strip():
            return []

        extracted = jieba.analyse.extract_tags(full_text, topK=top_k * 2, withWeight=False)
        keywords = []
        seen = set()

        for word in extracted:
            word = normalize_text(word)
            if len(word) <= 1 or word.isdigit() or word in seen:
                continue
            seen.add(word)
            keywords.append(word)
            if len(keywords) >= top_k:
                break

        return keywords

    def _pick_representative_comments(
        self,
        comments: Sequence[Dict[str, str]],
        top_n: int = 3,
    ) -> List[Dict[str, str]]:
        ranked = sorted(
            comments,
            key=lambda item: (parse_like_count(item.get("点赞数", "")), len(item.get("评论内容", ""))),
            reverse=True,
        )

        picked = []
        seen = set()
        for item in ranked:
            content = item.get("评论内容", "")
            if not content or content in seen:
                continue
            seen.add(content)
            picked.append(
                {
                    "用户名": item.get("用户名", ""),
                    "评论内容": content,
                    "点赞数": item.get("点赞数", ""),
                    "发布时间": item.get("发布时间", ""),
                }
            )
            if len(picked) >= top_n:
                break

        return picked

    def _extract_focus_points(
        self,
        comment_texts: Sequence[str],
        note_text: str,
        top_keywords: Sequence[str],
    ) -> List[str]:
        base_texts = list(comment_texts)
        if note_text:
            base_texts.append(note_text)

        if not base_texts:
            return ["暂无足够文本，暂时无法识别关注点"]

        counts = Counter()
        for text in base_texts:
            for focus_name, patterns in FOCUS_PATTERNS.items():
                if any(pattern in text for pattern in patterns):
                    counts[focus_name] += 1

        focus_points = [name for name, _ in counts.most_common(4)]
        if focus_points:
            return focus_points

        if top_keywords:
            return [f"高频讨论词：{keyword}" for keyword in top_keywords[:3]]

        return ["暂无明显关注点"]

    def _analyze_sentiment(self, comment_texts: Sequence[str], note_text: str) -> str:
        base_texts = list(comment_texts)
        if not base_texts and note_text:
            base_texts = [note_text]

        if not base_texts:
            return "中性"

        positive_hits = 0
        negative_hits = 0
        for text in base_texts:
            positive_hits += sum(1 for word in POSITIVE_WORDS if word in text)
            negative_hits += sum(1 for word in NEGATIVE_WORDS if word in text)

        if positive_hits > negative_hits:
            return "偏正面"
        if negative_hits > positive_hits:
            return "偏负面"
        return "中性"

    def _summarize_note_content(self, note: Dict[str, object]) -> str:
        title = normalize_text(note.get("note_title") or note.get("title") or note.get("search_title") or "")
        content = normalize_text(note.get("note_content") or note.get("raw_note_text") or "")
        author = normalize_text(note.get("author_name") or "")

        pieces = []
        if title:
            pieces.append(f"笔记主题：{title}")
        if author:
            pieces.append(f"作者：{author}")
        if content:
            pieces.append(f"正文摘要：{truncate_text(content, limit=120)}")

        if not pieces:
            return "未提取到明确笔记正文。"
        return " ".join(pieces)

    def _summarize_comment_discussion(self, comment_texts: Sequence[str]) -> str:
        if not comment_texts:
            return "当前未抓取到评论，以下总结主要基于笔记正文。"

        total = len(comment_texts)
        preview = truncate_text("；".join(comment_texts[:3]), limit=120)
        return f"共收集 {total} 条评论，主要讨论集中在：{preview}"

    def _build_one_line_summary(
        self,
        *,
        note: Dict[str, object],
        comment_count: int,
        content_summary: str,
        comment_summary: str,
        focus_points: Sequence[str],
        sentiment: str,
    ) -> str:
        focus_text = "、".join(focus_points[:2]) if focus_points else "暂无明显关注点"
        title = note.get("note_title") or note.get("search_title") or "这篇笔记"
        if comment_count > 0:
            return (
                f"{title} 主要介绍“{truncate_text(note.get('note_content') or title, 26)}”，"
                f"评论围绕 {focus_text} 展开，整体情绪偏向 {sentiment}。"
            )
        return f"{title} 已提取到笔记正文，当前评论较少，用户可能更关注 {focus_text}。"

    def _maybe_generate_ai_summary(
        self,
        note: Dict[str, object],
        comments: Sequence[Dict[str, str]],
        *,
        use_ai: bool,
    ) -> str:
        if not use_ai:
            return ""
        try:
            return normalize_text(self.ai_client.summarize(note, comments))
        except Exception:
            return ""

    def _build_markdown(
        self,
        *,
        note: Dict[str, object],
        comment_count: int,
        content_summary: str,
        comment_summary: str,
        top_keywords: Sequence[str],
        representative_comments: Sequence[Dict[str, str]],
        focus_points: Sequence[str],
        sentiment: str,
        one_line_summary: str,
        ai_summary: str,
    ) -> str:
        title = note.get("note_title") or note.get("title") or note.get("search_title") or "未命名笔记"
        lines = [
            f"## {title}",
            f"- 状态：{note.get('status') or 'crawled'}",
            f"- 关键词：{note.get('keyword') or ''}",
            f"- 作者：{note.get('author_name') or ''}",
            f"- 笔记链接：{note.get('clean_url') or note.get('raw_url') or ''}",
            f"- 评论数量：{comment_count}",
            f"- 简单情绪倾向：{sentiment}",
            "",
            "### 笔记内容摘要",
            content_summary or "未提取到明确笔记正文。",
            "",
            "### 评论讨论摘要",
            comment_summary,
            "",
            "### 用户关注点",
        ]

        if focus_points:
            for focus in focus_points:
                lines.append(f"- {focus}")
        else:
            lines.append("- 暂无明显关注点")

        lines.extend(["", "### 高频词"])
        lines.append("、".join(top_keywords) if top_keywords else "暂无明显高频词")

        lines.extend(["", "### 典型评论"])
        if representative_comments:
            for item in representative_comments:
                lines.append(
                    f"- {item.get('用户名') or '匿名用户'} | 点赞 {item.get('点赞数') or '0'} | {item.get('发布时间') or ''}"
                )
                lines.append(f"  {item.get('评论内容') or ''}")
        else:
            lines.append("- 当前无可展示的典型评论")

        lines.extend(["", "### 一句话总结", one_line_summary])

        if ai_summary:
            lines.extend(["", "### AI 总结", ai_summary])

        return "\n".join(lines).strip()


def build_summarizer(ai_config: Dict[str, object] | None = None) -> RuleBasedSummarizer:
    config = ai_config or {"enabled": False}
    ai_client = build_ai_client(config)
    return RuleBasedSummarizer(ai_client=ai_client, ai_enabled=bool(config.get("enabled", False)))


def summarize_note_and_comments(
    note: Dict[str, object],
    comments: Sequence[Dict[str, str]],
    use_ai: bool = False,
    ai_config: Dict[str, object] | None = None,
) -> Dict[str, object]:
    config = dict(ai_config or {"enabled": False})
    config["enabled"] = bool(use_ai and config.get("enabled", False))
    summarizer = build_summarizer(config)
    return summarizer.summarize_note_and_comments(note, comments, use_ai=config["enabled"])
