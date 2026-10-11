#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已采集内容中自动发现关键词。"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import jieba.analyse

from db import get_connection, save_discovered_keywords

STOP_WORDS = {
    "一个",
    "一下",
    "不是",
    "什么",
    "真的",
    "可以",
    "这个",
    "那个",
    "已经",
    "因为",
    "所以",
    "没有",
    "还是",
    "就是",
    "感觉",
    "评论",
    "回复",
    "点赞",
    "收藏",
    "分享",
    "展开",
    "登录",
    "搜索",
    "推荐",
}
PLATFORM_NOISE_WORDS = {
    "小红书",
    "笔记",
    "博主",
    "主页",
    "关注",
    "验证码",
    "安全验证",
    "大家都在搜",
    "相关搜索",
}
WORD_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9_\-]{1,}$")


def extract_keywords_from_db(
    *,
    db_path: str | Path | None = None,
    top_n: int = 50,
) -> List[Dict[str, object]]:
    rows = _load_text_rows(db_path=db_path)
    term_counter: Counter[str] = Counter()
    term_platforms: Dict[str, set[str]] = defaultdict(set)
    term_sources: Dict[str, set[int]] = defaultdict(set)
    term_last_seen: Dict[str, str] = {}

    for row in rows:
        text = _normalize_text(row["text"])
        if not text:
            continue
        platform = row["platform"] or "unknown"
        source_id = int(row["source_id"])
        last_seen = row["last_seen"] or ""
        for term in _extract_terms(text):
            term_counter[term] += 1
            term_platforms[term].add(platform)
            term_sources[term].add(source_id)
            if last_seen and last_seen > term_last_seen.get(term, ""):
                term_last_seen[term] = last_seen

    records: List[Dict[str, object]] = []
    for term, frequency in term_counter.items():
        platforms = sorted(term_platforms[term])
        source_count = len(term_sources[term])
        score = _score_term(frequency, len(platforms), source_count, term_last_seen.get(term, ""))
        records.append(
            {
                "term": term,
                "platform": "all",
                "frequency": frequency,
                "score": round(score, 4),
                "platforms": platforms,
                "source_count": source_count,
                "last_seen": term_last_seen.get(term, ""),
            }
        )

    records.sort(key=lambda item: (float(item["score"]), int(item["frequency"])), reverse=True)
    records = records[: max(0, int(top_n or 50))]
    save_discovered_keywords(records, db_path=db_path)
    return records


def write_discovered_keywords_outputs(
    records: Iterable[Dict[str, object]],
    *,
    output_dir: str | Path = "reports",
) -> Dict[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d")
    md_path = output_path / f"discovered_keywords_{timestamp}.md"
    csv_path = output_path / f"discovered_keywords_{timestamp}.csv"
    rows = list(records)

    with md_path.open("a", encoding="utf-8") as file:
        file.write(f"\n\n# 自动发现关键词 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        if not rows:
            file.write("暂无可用关键词。请先采集内容，或提供合法入口数据。\n")
        else:
            file.write("| 排名 | 关键词 | 频次 | 分数 | 平台 | 来源数 | 最近出现 |\n")
            file.write("|---:|---|---:|---:|---|---:|---|\n")
            for index, row in enumerate(rows, start=1):
                platforms = ", ".join(row.get("platforms") or [])
                file.write(
                    f"| {index} | {row.get('term', '')} | {row.get('frequency', 0)} | "
                    f"{row.get('score', 0)} | {platforms} | {row.get('source_count', 0)} | "
                    f"{row.get('last_seen', '')} |\n"
                )

    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        fieldnames = ["term", "frequency", "score", "platforms", "source_count", "last_seen"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "term": row.get("term", ""),
                    "frequency": row.get("frequency", 0),
                    "score": row.get("score", 0),
                    "platforms": ",".join(row.get("platforms") or []),
                    "source_count": row.get("source_count", 0),
                    "last_seen": row.get("last_seen", ""),
                }
            )

    return {"markdown": str(md_path), "csv": str(csv_path)}


def _load_text_rows(*, db_path: str | Path | None = None) -> List[Dict[str, object]]:
    with get_connection(db_path) as connection:
        items = connection.execute(
            """
            SELECT id AS source_id, platform, updated_at AS last_seen,
                   COALESCE(title, note_title, '') || ' ' ||
                   COALESCE(body, note_content, '') || ' ' ||
                   COALESCE(author_name, '') || ' ' ||
                   COALESCE(raw_metadata_json, '') AS text
            FROM content_items
            """
        ).fetchall()
        comments = connection.execute(
            """
            SELECT COALESCE(content_item_id, 0) AS source_id,
                   COALESCE(platform, '') AS platform,
                   COALESCE(crawled_at, created_at, '') AS last_seen,
                   COALESCE(username, '') || ' ' ||
                   COALESCE(content, '') || ' ' ||
                   COALESCE(raw_text, '') AS text
            FROM content_comments
            """
        ).fetchall()
    return [dict(row) for row in [*items, *comments]]


def _extract_terms(text: str) -> List[str]:
    candidates = jieba.analyse.extract_tags(text, topK=30, withWeight=False)
    terms = []
    for candidate in candidates:
        term = candidate.strip()
        if _is_valid_term(term):
            terms.append(term)
    return terms


def _is_valid_term(term: str) -> bool:
    if not term or len(term) < 2:
        return False
    if term in STOP_WORDS or term in PLATFORM_NOISE_WORDS:
        return False
    if term.isdigit():
        return False
    return bool(WORD_PATTERN.match(term))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _score_term(frequency: int, platform_count: int, source_count: int, last_seen: str) -> float:
    score = math.log(frequency + 1) * (1 + math.log(platform_count + 1)) * (1 + math.log(source_count + 1))
    if last_seen and last_seen[:10] == datetime.now().strftime("%Y-%m-%d"):
        score *= 1.15
    return score
