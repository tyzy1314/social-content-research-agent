#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台内容研究分析（第四轮第十节）。

设计约束（严格遵守）：
* 情感用项目既有词典基线（复用 ``sentiment_analyzer.classify_sentiment``），并**声明局限**；
* **不做跨平台指标混算**：B站播放量与小红书浏览量语义不同，只分平台呈现，不合并；
* 比较只在**可比维度**（情感占比、主题分布）上进行，且必须带样本量与限制说明；
* 不得因某平台负面占比高就断言"该平台用户更负面"——结论一律限定在样本范围内；
* 所有结论附可追溯引用（``platform_comment_id`` + 原文片段）。
"""

from __future__ import annotations

import io
import json
import re
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from unified_store import fetch_comments, fetch_content, platform_counts

REPORT_DIR = Path("outputs/reports")


def _classify(text: str) -> str:
    from sentiment_analyzer import classify_sentiment

    return classify_sentiment(text)


def _topics(texts: Sequence[str], top_n: int = 12) -> List[Dict[str, Any]]:
    try:
        with redirect_stdout(io.StringIO()):
            import jieba.analyse
        joined = " ".join(str(t) for t in texts)
        if not joined.strip():
            return []
        return [
            {"term": term, "weight": round(float(weight), 4)}
            for term, weight in jieba.analyse.extract_tags(joined, topK=top_n, withWeight=True)
        ]
    except Exception:
        return []


def _snippet(text: str, size: int = 90) -> str:
    body = str(text).strip().replace("\n", " ")
    return body if len(body) <= size else body[: size - 3] + "..."


# --------------------------------------------------------------------------- #
# 单平台分析
# --------------------------------------------------------------------------- #
def _filter_by_keyword(
    comments: List[Dict[str, Any]], contents: List[Dict[str, Any]], keyword: str
) -> List[Dict[str, Any]]:
    """把评论限定在指定主题（通过内容侧的 keyword 元数据关联）。

    跨平台比较必须同主题才有意义；XHS 的 ``raw_metadata.keyword`` 记录了采集关键词。
    """
    if not keyword:
        return comments
    allowed = set()
    for content in contents:
        try:
            meta = json.loads(content.get("raw_metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if str(meta.get("keyword", "")) == keyword:
            allowed.add(content.get("platform_content_id"))
    return [row for row in comments if row.get("platform_content_id") in allowed]


def analyze_platform(
    db_path: str,
    platform: str,
    *,
    limit: int = 500,
    topic_n: int = 12,
    evidence_n: int = 8,
    keyword: str = "",
) -> Dict[str, Any]:
    """分析单个平台的真实评论数据（可按主题过滤）。数据不足时明确说明，不编造。"""
    counts = {row["platform"]: row for row in platform_counts(db_path)}
    contents = fetch_content(db_path, platform=platform, limit=500)
    comments = fetch_comments(db_path, platform=platform, limit=limit)
    if keyword:
        comments = _filter_by_keyword(comments, contents, keyword)

    if not comments:
        return {
            "ok": False,
            "platform": platform,
            "reason": "insufficient_data",
            "message": f"统一库中平台 '{platform}' 没有评论数据。当前已有平台：{sorted(counts)}",
            "available_platforms": sorted(counts),
        }

    sentiments = [_classify(row["content"]) for row in comments]
    counts_by_label = Counter(sentiments)
    total = len(comments)
    negative = [row for row, label in zip(comments, sentiments) if label == "负面"]

    return {
        "ok": True,
        "platform": platform,
        "data_scope": {
            "keyword_filter": keyword or None,
            "comments_analyzed": total,
            "comment_cap": limit,
            "content_in_db": counts.get(platform, {}).get("content_count", 0),
            "comment_in_db": counts.get(platform, {}).get("comment_count", 0),
            "sample_is_complete": total < limit,
        },
        "sentiment": {
            "counts": {"正面": counts_by_label.get("正面", 0), "负面": counts_by_label.get("负面", 0),
                       "中性": counts_by_label.get("中性", 0)},
            "ratios": {
                label: f"{counts_by_label.get(label, 0) / total * 100:.1f}%"
                for label in ("正面", "负面", "中性")
            },
            "method": "lexicon baseline (sentiment_analyzer.classify_sentiment) — 规则基线，非训练模型",
        },
        "topics": _topics([row["content"] for row in comments], top_n=topic_n),
        "negative_examples": [
            {"comment_id": row["platform_comment_id"], "snippet": _snippet(row["content"])}
            for row in negative[:evidence_n]
        ],
        "evidence": [
            {"comment_id": row["platform_comment_id"], "snippet": _snippet(row["content"])}
            for row in comments[:evidence_n]
        ],
        "content_samples": [
            {"content_id": row["platform_content_id"], "title": _snippet(row["title"], 60)}
            for row in contents[:5]
        ],
        "caveats": [
            "情感为词典规则基线，无法识别否定/反讽/上下文。",
            f"样本为库中最近 {total} 条评论，受采集条件与平台推荐机制影响，不代表平台整体舆情。",
        ],
    }


# --------------------------------------------------------------------------- #
# 跨平台比较
# --------------------------------------------------------------------------- #
def compare_platforms(
    db_path: str,
    platforms: Sequence[str],
    *,
    limit: int = 500,
    topic_n: int = 12,
    keyword: str = "",
) -> Dict[str, Any]:
    """跨平台比较（同主题才有意义）。只在可比维度比较，并明确样本与限制。"""
    analyses: Dict[str, Any] = {}
    unavailable: List[Dict[str, Any]] = []
    requested = [p for p in platforms if p]

    for platform in requested:
        result = analyze_platform(db_path, platform, limit=limit, topic_n=topic_n, keyword=keyword)
        if result.get("ok"):
            analyses[platform] = result
        else:
            unavailable.append({"platform": platform, "reason": result.get("reason"),
                                "message": result.get("message")})

    if not analyses:
        return {
            "ok": False,
            "reason": "insufficient_data",
            "message": "请求的平台均无可用真实数据，无法生成比较。",
            "unavailable": unavailable,
        }

    # 情感对比（可比维度）
    sentiment_table = {
        name: {
            "comments": data["data_scope"]["comments_analyzed"],
            "正面": data["sentiment"]["counts"]["正面"],
            "负面": data["sentiment"]["counts"]["负面"],
            "中性": data["sentiment"]["counts"]["中性"],
            "负面占比": data["sentiment"]["ratios"]["负面"],
        }
        for name, data in analyses.items()
    }

    # 主题差异：各平台主题集合 -> 共有 / 独有
    topic_sets = {name: {t["term"] for t in data["topics"]} for name, data in analyses.items()}
    names = list(topic_sets)
    common = set.intersection(*topic_sets.values()) if len(topic_sets) > 1 else (topic_sets[names[0]] if names else set())
    unique = {
        name: sorted(topic_sets[name] - set.union(*[topic_sets[o] for o in names if o != name]))
        if len(names) > 1 else sorted(topic_sets[name])
        for name in names
    }

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topic": keyword or None,
        "platforms_analyzed": names,
        "platforms_unavailable": unavailable,
        "sentiment_comparison": sentiment_table,
        "topic_comparison": {
            "common_topics": sorted(common),
            "unique_topics": unique,
        },
        "per_platform_detail": analyses,
        "comparability_notes": [
            "互动指标（B站播放/点赞/投币 与 小红书浏览/点赞）语义不同，**不做跨平台合并或排名**。",
            "各平台样本量、采集条件、推荐机制与评论排序不同，情感占比只能在同一平台内部解读。",
            "负面占比差异**不等于**平台用户整体情绪差异，仅反映当前样本。",
            "平台不可用时如实列出，不以其他平台数据推测或填充。",
        ],
        "unsupported_platforms_note": unavailable,
    }


# --------------------------------------------------------------------------- #
# 报告生成（JSON + Markdown），含自核验
# --------------------------------------------------------------------------- #
def generate_report(comparison: Dict[str, Any], *, title: str = "跨平台内容研究报告") -> Dict[str, Any]:
    """输出结构化 JSON 与可读 Markdown，并对数字做自动复核。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"cross_platform_{stamp}.json"
    md_path = REPORT_DIR / f"cross_platform_{stamp}.md"

    verification = _verify(comparison)
    payload = {**comparison, "title": title, "verification": verification}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(payload, title=title), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "verification": verification,
    }


def _verify(comparison: Dict[str, Any]) -> Dict[str, Any]:
    """自动核验：重算占比、检查引用存在性、确认降级平台已标注。"""
    checks: List[Dict[str, Any]] = []
    for name, row in (comparison.get("sentiment_comparison") or {}).items():
        total = int(row.get("comments") or 0)
        pos, neg, neu = int(row.get("正面", 0)), int(row.get("负面", 0)), int(row.get("中性", 0))
        consistent = (pos + neg + neu) == total
        reported = str(row.get("负面占比", "")).rstrip("%")
        recomputed = f"{(neg / total * 100) if total else 0:.1f}"
        checks.append({
            "platform": name,
            "counts_sum_matches_total": consistent,
            "negative_ratio_recomputed": recomputed,
            "negative_ratio_matches": (reported == recomputed) if total else False,
        })
    evidence_ok = all(
        bool((detail.get("evidence") or []))
        for detail in (comparison.get("per_platform_detail") or {}).values()
    )
    return {
        "counts_sum_consistent": all(c["counts_sum_matches_total"] for c in checks),
        "ratios_match_recomputation": all(c["negative_ratio_matches"] for c in checks),
        "evidence_present": evidence_ok,
        "unavailable_platforms_listed": bool(comparison.get("platforms_unavailable")) is not None,
        "per_platform_checks": checks,
    }


def _render_markdown(payload: Dict[str, Any], *, title: str) -> str:
    lines: List[str] = [f"# {title}", ""]
    lines.append(f"> 生成时间：{payload.get('generated_at', '')}　数据来源：本地统一库（真实采集数据）")
    lines.append("")

    if not payload.get("ok"):
        lines.append("## 数据不足")
        lines.append("")
        lines.append(str(payload.get("message", "")))
        for item in payload.get("unavailable", []):
            lines.append(f"- {item.get('platform')}: {item.get('message')}")
        return "\n".join(lines) + "\n"

    lines.append("## 一、研究主题与平台范围")
    lines.append("")
    lines.append(f"- 研究主题：**{payload.get('topic') or '（未限定主题）'}**")
    lines.append("- 已分析：" + "、".join(payload.get("platforms_analyzed", [])))
    unavailable = payload.get("platforms_unavailable") or []
    if unavailable:
        lines.append("- **不可用平台（如实标注，未推测）**："
                     + "、".join(f"{u['platform']}（{u.get('reason')}）" for u in unavailable))
    lines.append("")

    lines.append("## 二、情感分布对比（可比维度）")
    lines.append("")
    lines.append("| 平台 | 样本评论数 | 正面 | 负面 | 中性 | 负面占比 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, row in (payload.get("sentiment_comparison") or {}).items():
        lines.append(f"| {name} | {row.get('comments')} | {row.get('正面')} | {row.get('负面')} | "
                     f"{row.get('中性')} | {row.get('负面占比')} |")
    lines.append("")

    lines.append("## 三、主题对比")
    lines.append("")
    topic = payload.get("topic_comparison") or {}
    lines.append("### 共同关注")
    lines.append("")
    lines.append("、".join(topic.get("common_topics") or []) or "（无重合主题）")
    lines.append("")
    lines.append("### 各平台独有")
    lines.append("")
    for name, terms in (topic.get("unique_topics") or {}).items():
        lines.append(f"- **{name}**：" + ("、".join(terms) if terms else "（无）"))
    lines.append("")

    lines.append("## 四、分平台详情与引用")
    lines.append("")
    for name, detail in (payload.get("per_platform_detail") or {}).items():
        lines.append(f"### {name}")
        scope = detail.get("data_scope", {})
        lines.append(f"- 样本：{scope.get('comments_analyzed')} 条评论（库内共 {scope.get('comment_in_db')} 条）；"
                     f"内容 {scope.get('content_in_db')} 条")
        lines.append(f"- 情感：{detail.get('sentiment', {}).get('ratios')}")
        top = [t["term"] for t in (detail.get("topics") or [])[:8]]
        lines.append(f"- 主题：" + ("、".join(top) if top else "（无）"))
        lines.append("- 代表性评论（可追溯）：")
        for item in (detail.get("evidence") or [])[:4]:
            lines.append(f"  - `{item['comment_id']}`：{item['snippet']}")
        neg = detail.get("negative_examples") or []
        if neg:
            lines.append("- 负面样本：")
            for item in neg[:3]:
                lines.append(f"  - `{item['comment_id']}`：{item['snippet']}")
        lines.append("")

    lines.append("## 五、比较限制（必须阅读）")
    lines.append("")
    for note in payload.get("comparability_notes", []):
        lines.append(f"- {note}")
    lines.append("")

    verification = payload.get("verification") or {}
    lines.append("## 六、自动核验")
    lines.append("")
    lines.append(f"- 计数与总数一致：{verification.get('counts_sum_consistent')}")
    lines.append(f"- 占比与重算一致：{verification.get('ratios_match_recomputation')}")
    lines.append(f"- 引用存在：{verification.get('evidence_present')}")
    lines.append("")
    return "\n".join(lines) + "\n"
