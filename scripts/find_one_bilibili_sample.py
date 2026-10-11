#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Find and classify one public Bilibili video sample for manual crawler validation.

This script is intentionally outside pytest and engineering_review.ps1 because it
opens a real public website and may launch Chrome. It does not retry, run in
parallel, or call hidden APIs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from notifier import ensure_directory  # noqa: E402
from platforms.bilibili import BILIBILI_SEARCH_URL, BilibiliAdapter, extract_bilibili_content_id  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find one public Bilibili video sample and classify its page status.")
    parser.add_argument("--seed", default="Python教程", help="Search keyword used to find one public video candidate.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless. Default is visible Chrome.")
    return parser.parse_args()


def write_report(*, keyword: str, sample_url: str, content_id: str, classification: Dict[str, object]) -> Path:
    log_dir = ensure_directory("outputs/logs")
    output_path = log_dir / f"bilibili_sample_public_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    command = ""
    if sample_url:
        command = f'python main.py crawl --platform bilibili --mode test --engine crawler --url "{sample_url}"'
    lines = [
        f"keyword: {keyword}",
        f"sample_url: {sample_url or 'NOT_FOUND'}",
        f"valid_content_id: {bool(content_id)}",
        f"content_id: {content_id}",
        f"page_status: {classification.get('status', '')}",
        f"reason: {classification.get('reason', '')}",
        f"has_bilibili_video_content: {classification.get('has_bilibili_video_content', '')}",
        f"debug_page: {PROJECT_ROOT / 'debug_bilibili_page.html'}",
        f"manual_crawl_command: {command}",
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    args = parse_args()
    adapter = BilibiliAdapter(headless=bool(args.headless))
    keyword = str(args.seed or "").strip() or "Python教程"
    search_url = BILIBILI_SEARCH_URL.format(keyword=keyword)
    classification: Dict[str, object] = {"status": "not_found", "reason": "no_candidate"}
    sample_url = ""
    content_id = ""

    print(f"样本来源：Bilibili 公开搜索页，提取第一个 /video/BV... 候选")
    print(f"搜索关键词：{keyword}")
    print(f"搜索页：{search_url}")

    try:
        adapter.open_search_page(keyword)
        candidates = adapter.iter_search_candidates(limit=1)
        if not candidates:
            output_path = write_report(
                keyword=keyword,
                sample_url=sample_url,
                content_id=content_id,
                classification=classification,
            )
            print("找到的样本 URL：未找到")
            print("是否能提取有效 BV/av ID：False")
            print("页面状态分类：not_found")
            print(f"输出文件路径：{output_path}")
            print('未找到公开样本，请手动提供 --url，例如：python main.py crawl --platform bilibili --mode test --engine crawler --url "视频URL"')
            return 1

        sample_url = str(candidates[0].get("href") or "").strip()
        content_id = extract_bilibili_content_id(sample_url)
        if not sample_url:
            classification = {"status": "not_found", "reason": "empty_candidate_url"}
        else:
            adapter.open_url(sample_url)
            classification = adapter.classify_current_page()
            sample_url = adapter.current_url() or sample_url
            content_id = extract_bilibili_content_id(sample_url)

        output_path = write_report(
            keyword=keyword,
            sample_url=sample_url,
            content_id=content_id,
            classification=classification,
        )
    finally:
        adapter.close()

    print(f"找到的样本 URL：{sample_url or '未找到'}")
    print(f"是否能提取有效 BV/av ID：{bool(content_id)}")
    print(f"页面状态分类：{classification.get('status', '')}")
    print(f"输出文件路径：{output_path}")
    if sample_url:
        print(f'手动验证命令：python main.py crawl --platform bilibili --mode test --engine crawler --url "{sample_url}"')
    else:
        print('未找到公开样本，请手动提供 --url，例如：python main.py crawl --platform bilibili --mode test --engine crawler --url "视频URL"')
    return 0 if sample_url and content_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
