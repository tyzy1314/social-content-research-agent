#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台内容采集与分析框架 CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

import yaml

from agent.cli import add_agent_parser, run_agent
from crawl_planner import EXECUTABLE_PLATFORMS, build_crawl_plan
from db import (
    create_crawl_run,
    get_daily_dashboard_stats,
    get_platform_status_counts,
    init_db,
    update_crawl_run,
    upsert_site_profile,
)
from discovery.keyword_extractor import extract_keywords_from_db, write_discovered_keywords_outputs
from export_utils import EXPORT_TABLES, export_dataset
from platforms import ADAPTERS, get_adapter_class

DEFAULT_CONFIG_PATH = Path("config.yaml")
CRAWLER_TEST_STREAM_LIMITS = {
    "success_target": ("max", 3),
    "max_candidates": ("max", 15),
    "max_comments_per_note": ("max", 20),
    "sleep_between_notes_seconds": ("min", 5),
}
BILIBILI_TEST_STREAM_LIMITS = {
    "success_target": ("max", 3),
    "max_candidates": ("max", 10),
    "max_comments_per_note": ("max", 10),
    "sleep_between_notes_seconds": ("min", 5),
}
CRAWLER_TEST_CRAWLER_LIMITS = {
    "concurrency": ("fixed", 1),
    "max_retries": ("max", 1),
}
CRAWLER_TEST_BUDGET_LIMITS = {
    "max_requests_per_run": ("max", 30),
    "max_blocked_per_run": ("max", 1),
    "max_failures_per_run": ("max", 3),
}
BILIBILI_TEST_BUDGET_LIMITS = {
    "max_requests_per_run": ("max", 20),
    "max_blocked_per_run": ("max", 1),
    "max_failures_per_run": ("max", 3),
}
TEST_STREAM_DEFAULTS = {
    "success_target": 3,
    "max_candidates": 15,
    "max_comments_per_note": 10,
    "sleep_between_notes_seconds": 5,
    "fast_skip_sleep_seconds": 1,
    "write_db": False,
    "generate_summary": False,
}
PROD_STREAM_DEFAULTS = {
    "success_target": 20,
    "max_candidates": 80,
    "max_comments_per_note": 50,
    "sleep_between_notes_seconds": 60,
    "fast_skip_sleep_seconds": 2,
    "write_db": True,
    "generate_summary": True,
}


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Dict[str, object]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多平台内容采集与分析框架")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl_parser = subparsers.add_parser("crawl", help="采集公开内容")
    crawl_parser.add_argument("--platform", default="xiaohongshu", choices=[*ADAPTERS.keys(), "all"])
    crawl_parser.add_argument("--mode", default="auto", choices=["auto", "test", "prod"])
    crawl_parser.add_argument("--engine", default="legacy", choices=["legacy", "crawler", "autonomous"])
    crawl_parser.add_argument("--dry-run", action="store_true", help="只打印调度计划，不打开浏览器、不采集、不写入")
    crawl_parser.add_argument("--seed", default="", help="可选种子关键词或入口")
    crawl_parser.add_argument("--url", default="", help="可选单条内容 URL，仅 crawler test 支持")
    crawl_parser.add_argument("--limit", type=int, default=10)
    crawl_parser.add_argument("--concurrency", type=int, default=None)

    plan_parser = subparsers.add_parser("plan-crawl", help="生成自主采集计划")
    plan_parser.add_argument("--platform", default="all", choices=[*ADAPTERS.keys(), "all"])
    plan_parser.add_argument("--mode", default="test", choices=["auto", "test", "prod"])
    plan_parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入数据库、不采集")
    plan_parser.add_argument("--seed", default="", help="可选临时种子关键词")
    plan_parser.add_argument("--limit", type=int, default=10)

    profile_parser = subparsers.add_parser("profile-site", help="更新站点专精工作区画像")
    profile_parser.add_argument("--platform", required=True, choices=list(ADAPTERS.keys()))

    discover_parser = subparsers.add_parser("discover-keywords", help="从已采集内容发现关键词")
    discover_parser.add_argument("--from-db", action="store_true", help="从 SQLite 读取内容")
    discover_parser.add_argument("--top", type=int, default=50)
    discover_parser.add_argument("--output-format", choices=["md", "csv", "both"], default="both")

    analyze_parser = subparsers.add_parser("analyze", help="从数据库读取统计和分析结果")
    analyze_parser.add_argument("--from-db", action="store_true")

    export_parser = subparsers.add_parser("export", help="导出通用内容表")
    export_parser.add_argument("--format", choices=["csv", "xlsx"], default="csv")
    export_parser.add_argument("--table", choices=[*EXPORT_TABLES, "all"], default="content")
    export_parser.add_argument("--output", default="")
    export_parser.add_argument("--limit", type=int, default=1000)

    add_agent_parser(subparsers)

    return parser.parse_args()


def run_crawl(args: argparse.Namespace, config: Dict[str, object]) -> int:
    crawler_config = dict(config.get("crawler") or {})
    if args.concurrency is not None:
        crawler_config["concurrency"] = args.concurrency

    if args.platform == "all":
        platforms = list(ADAPTERS.keys())
    else:
        platforms = [args.platform]

    stream_config = config.get("stream") or {}
    effective_stream = resolve_effective_stream_config(stream_config, args.mode)
    clamp_messages: list[str] = []
    if args.engine == "crawler" and args.mode == "test":
        effective_stream, crawler_config, clamp_messages = apply_crawler_test_safety_limits(
            args.platform,
            effective_stream,
            crawler_config,
        )

    if args.dry_run:
        print_crawl_dry_run(args, config, crawler_config, platforms, effective_stream, clamp_messages)
        return 0

    if args.engine == "autonomous":
        return run_autonomous_crawl_entry(args, config, crawler_config, effective_stream, platforms)

    if args.engine == "crawler":
        return run_crawler_test_entry(args, config, crawler_config, effective_stream, clamp_messages, platforms)

    if platforms == ["xiaohongshu"]:
        return run_xhs_compatible_entry(args.mode)

    print("当前只有 xiaohongshu 适配器真实可用；其他平台仍是 placeholder。")
    for platform in platforms:
        if platform != "xiaohongshu":
            print(f"{platform}: placeholder adapter, not implemented yet")
            continue
        adapter_class = get_adapter_class(platform)
        try:
            adapter = adapter_class(crawler_config=crawler_config)
            print(f"{platform}: {adapter.__class__.__name__} ready")
        except NotImplementedError as exc:
            print(f"{platform}: {exc}")
    return 0


def print_crawl_dry_run(
    args: argparse.Namespace,
    config: Dict[str, object],
    crawler_config: Dict[str, object],
    platforms: list[str],
    effective_stream: Dict[str, object],
    clamp_messages: list[str] | None = None,
):
    output_config = config.get("output") or {}
    discovery_config = config.get("discovery") or {}
    platform = args.platform
    adapter_class_name = None
    if platform != "all" and platform in ADAPTERS:
        adapter_class_name = ADAPTERS[platform].split(":", 1)[1]

    platform_budget = None
    if platform != "all":
        budgets = crawler_config.get("platform_budgets") or {}
        platform_budget = budgets.get(platform)

    print("crawl dry-run plan")
    print(f"- platform: {platform}")
    print(f"- mode: {args.mode}")
    print(f"- engine: {args.engine}")
    print("- dry_run: true")
    print(f"- config path: {args.config}")
    print(f"- keywords: {config.get('keywords') or []}")
    print(f"- seed keywords: {discovery_config.get('seed_keywords') or []}")
    print(f"- url: {getattr(args, 'url', '') or ''}")
    print(f"- selected platforms: {platforms}")
    print(f"- success_target: {effective_stream.get('success_target')}")
    print(f"- max_candidates: {effective_stream.get('max_candidates')}")
    print(f"- max_comments_per_note: {effective_stream.get('max_comments_per_note')}")
    if platform == "bilibili":
        print(f"- max_comments_per_item: {effective_stream.get('max_comments_per_item')}")
    print(f"- sleep_between_notes_seconds: {effective_stream.get('sleep_between_notes_seconds')}")
    if platform == "bilibili":
        print(f"- sleep_between_items_seconds: {effective_stream.get('sleep_between_items_seconds')}")
    print(f"- fast_skip_sleep_seconds: {effective_stream.get('fast_skip_sleep_seconds')}")
    print(f"- write_db: {effective_stream.get('write_db')}")
    print(f"- generate_summary: {effective_stream.get('generate_summary')}")
    print(f"- database path: {output_config.get('db_path')}")
    print(f"- output csv dir: {output_config.get('csv_dir')}")
    print(f"- output log dir: {output_config.get('log_dir')}")
    print(f"- output summary dir: {output_config.get('summary_dir')}")
    print(f"- crawler concurrency: {crawler_config.get('concurrency')}")
    print(f"- request_timeout_seconds: {crawler_config.get('request_timeout_seconds')}")
    print(f"- max_retries: {crawler_config.get('max_retries')}")
    print(f"- min_delay_seconds: {crawler_config.get('min_delay_seconds')}")
    print(f"- max_delay_seconds: {crawler_config.get('max_delay_seconds')}")
    print(f"- platform budget: {platform_budget}")
    print(f"- adapter class: {adapter_class_name}")
    for message in clamp_messages or []:
        print(f"- clamp: {message}")
    print("- will open browser: false")
    print("- will collect: false")
    print("- will write database: false")
    print("- will write csv: false")
    print("- will write markdown: false")


def resolve_effective_stream_config(stream_config: Dict[str, object], mode: str) -> Dict[str, object]:
    effective = {
        "success_target": stream_config.get("success_target"),
        "max_candidates": stream_config.get("max_candidates"),
        "max_comments_per_note": stream_config.get("max_comments_per_note"),
        "sleep_between_notes_seconds": stream_config.get("sleep_between_notes_seconds"),
        "fast_skip_sleep_seconds": stream_config.get("fast_skip_sleep_seconds"),
        "write_db": stream_config.get("write_db"),
        "generate_summary": stream_config.get("generate_summary"),
    }
    if mode == "test":
        effective.update(TEST_STREAM_DEFAULTS)
    elif mode == "prod":
        effective.update(PROD_STREAM_DEFAULTS)
    return effective


def apply_crawler_test_safety_limits(
    platform: str,
    stream_config: Dict[str, object],
    crawler_config: Dict[str, object],
) -> tuple[Dict[str, object], Dict[str, object], list[str]]:
    effective_stream = dict(stream_config)
    effective_crawler = dict(crawler_config)
    messages: list[str] = []
    stream_limits = BILIBILI_TEST_STREAM_LIMITS if platform == "bilibili" else CRAWLER_TEST_STREAM_LIMITS
    budget_limits = BILIBILI_TEST_BUDGET_LIMITS if platform == "bilibili" else CRAWLER_TEST_BUDGET_LIMITS

    effective_stream["write_db"] = True
    effective_stream["generate_summary"] = True

    for key, (mode, limit) in stream_limits.items():
        original = effective_stream.get(key)
        value = int(original if original is not None else limit)
        if mode == "max" and value > limit:
            effective_stream[key] = limit
            messages.append(f"{key}: {value} -> {limit}")
        elif mode == "min" and value < limit:
            effective_stream[key] = limit
            messages.append(f"{key}: {value} -> {limit}")
        else:
            effective_stream[key] = value
    if platform == "bilibili":
        effective_stream["max_comments_per_item"] = effective_stream["max_comments_per_note"]
        effective_stream["sleep_between_items_seconds"] = effective_stream["sleep_between_notes_seconds"]

    for key, (mode, limit) in CRAWLER_TEST_CRAWLER_LIMITS.items():
        original = effective_crawler.get(key)
        value = int(original if original is not None else limit)
        if mode == "fixed" and value != limit:
            effective_crawler[key] = limit
            messages.append(f"{key}: {value} -> {limit}")
        elif mode == "max" and value > limit:
            effective_crawler[key] = limit
            messages.append(f"{key}: {value} -> {limit}")
        else:
            effective_crawler[key] = limit if mode == "fixed" else value

    budgets = dict(effective_crawler.get("platform_budgets") or {})
    platform_budget = dict(budgets.get(platform) or {})
    for key, (mode, limit) in budget_limits.items():
        original = platform_budget.get(key)
        value = int(original if original is not None else limit)
        if mode == "max" and value > limit:
            platform_budget[key] = limit
            messages.append(f"platform_budgets.{platform}.{key}: {value} -> {limit}")
        else:
            platform_budget[key] = value
    budgets[platform] = platform_budget
    effective_crawler["platform_budgets"] = budgets
    return effective_stream, effective_crawler, messages


def choose_crawler_keyword(args: argparse.Namespace, config: Dict[str, object]) -> str:
    if args.seed.strip():
        return args.seed.strip()

    keywords = config.get("keywords") or []
    discovery = config.get("discovery") or {}
    seed_keywords = discovery.get("seed_keywords") or []
    default_keyword = ""
    if keywords:
        first = keywords[0]
        if isinstance(first, str):
            default_keyword = first.strip()
        elif isinstance(first, dict):
            default_keyword = str(first.get("keyword") or first.get("name") or "").strip()
    if not default_keyword and seed_keywords:
        first_seed = seed_keywords[0]
        if isinstance(first_seed, str):
            default_keyword = first_seed.strip()

    prompt = "请输入关键词"
    if default_keyword:
        prompt += f"（默认 {default_keyword}）"
    prompt += "："
    value = input(prompt).strip()
    return value or default_keyword


def run_crawler_test_entry(
    args: argparse.Namespace,
    config: Dict[str, object],
    crawler_config: Dict[str, object],
    effective_stream: Dict[str, object],
    clamp_messages: list[str],
    platforms: list[str],
) -> int:
    if args.mode != "test":
        print("crawler engine prod/auto 真实采集尚未验证；请先使用 --mode test，或回退 --engine legacy。")
        return 0
    if len(platforms) != 1 or platforms[0] not in {"xiaohongshu", "bilibili"}:
        print("crawler engine 真实采集当前仅开放 xiaohongshu/bilibili test 模式。")
        return 2

    platform = platforms[0]
    keyword = "" if args.url.strip() else choose_crawler_keyword(args, config)
    if not keyword:
        if platform == "bilibili" and args.url.strip():
            keyword = "direct_url"
        else:
            print("错误：关键词不能为空。")
            return 2
    if args.url.strip() and platform != "bilibili":
        print("当前 --url 真实采集仅开放 bilibili crawler test。")
        return 2

    for message in clamp_messages:
        print(f"crawler test safety clamp: {message}")
    print(f"{platform} crawler engine test 模式已启用真实小规模采集。")

    from crawl_engine import CrawlerEngine
    from db import init_db, sync_keywords

    output_config = config.get("output") or {}
    db_path = str(output_config.get("db_path") or "data/xhs_crawler.db")
    init_db(db_path)
    if keyword != "direct_url":
        sync_keywords([keyword], db_path)

    headless = bool((config.get("stream") or {}).get("headless", False))
    if platform == "xiaohongshu":
        from platforms.xiaohongshu import XhsAdapter

        adapter = XhsAdapter(headless=headless, crawler_config=crawler_config)
    else:
        from platforms.bilibili import BilibiliAdapter

        adapter = BilibiliAdapter(headless=headless, crawler_config=crawler_config)
    engine = CrawlerEngine(adapter, crawler_config=crawler_config, db_path=db_path)
    if platform == "xiaohongshu":
        engine.run_xhs_test_stream(
            keyword=keyword,
            stream_config=effective_stream,
            output_config=output_config,
            ai_config=config.get("ai") or {"enabled": False},
        )
    else:
        engine.run_bilibili_test_stream(
            keyword="" if args.url.strip() else keyword,
            direct_url=args.url.strip(),
            stream_config=effective_stream,
            output_config=output_config,
            ai_config=config.get("ai") or {"enabled": False},
        )
    return 0


def run_autonomous_crawl_entry(
    args: argparse.Namespace,
    config: Dict[str, object],
    crawler_config: Dict[str, object],
    effective_stream: Dict[str, object],
    platforms: list[str],
) -> int:
    if args.mode != "test":
        print("autonomous engine 当前仅开放 test 模式；请先 dry-run 验证计划。")
        return 2

    output_config = config.get("output") or {}
    db_path = str(output_config.get("db_path") or "data/xhs_crawler.db")
    init_db(db_path)
    plan = build_crawl_plan(
        config,
        db_path=db_path,
        mode=args.mode,
        total_limit=args.limit,
        platform_filter=args.platform,
        seed=args.seed,
    )
    plan_payload = plan.asdict()
    print(json.dumps(plan_payload, ensure_ascii=False, indent=2))
    if not plan.permission_audit.ok:
        print("autonomous permission audit failed; collection stopped.")
        return 2
    if not plan.permission_audit.autonomy_enabled:
        print("agent.autonomy_enabled is false; autonomous collection stopped.")
        return 2

    executable_allocations = [
        allocation
        for allocation in plan.platform_allocations
        if allocation.executable and allocation.quota > 0 and allocation.keywords
    ]
    if not executable_allocations:
        print("没有可执行的平台或关键词；autonomous crawl 未启动。")
        return 2

    run_id = create_crawl_run(
        run_type="autonomous",
        mode=args.mode,
        status="running",
        plan=plan_payload,
        audit=plan.permission_audit.asdict(),
        db_path=db_path,
    )
    stats: Dict[str, object] = {"platforms": [], "requested_platforms": platforms}
    status = "completed"
    for allocation in executable_allocations:
        if allocation.platform not in EXECUTABLE_PLATFORMS:
            continue
        platform_stream = dict(effective_stream)
        platform_stream, platform_crawler_config, clamp_messages = apply_crawler_test_safety_limits(
            allocation.platform,
            platform_stream,
            crawler_config,
        )
        platform_stream["success_target"] = max(1, min(int(platform_stream.get("success_target") or 1), allocation.quota))
        platform_stream["max_candidates"] = max(
            int(platform_stream["success_target"]),
            min(int(platform_stream.get("max_candidates") or allocation.quota), max(1, allocation.quota * 5)),
        )
        platform_args = argparse.Namespace(
            platform=allocation.platform,
            mode="test",
            engine="crawler",
            dry_run=False,
            seed=allocation.keywords[0],
            url="",
            limit=allocation.quota,
            concurrency=args.concurrency,
            config=args.config,
        )
        result = run_crawler_test_entry(
            platform_args,
            config,
            platform_crawler_config,
            platform_stream,
            clamp_messages,
            [allocation.platform],
        )
        stats["platforms"].append(
            {
                "platform": allocation.platform,
                "quota": allocation.quota,
                "keyword": allocation.keywords[0],
                "returncode": result,
            }
        )
        if result != 0:
            status = "failed"
            break

    update_crawl_run(run_id, status=status, stats=stats, db_path=db_path)
    print(f"crawl_run_id: {run_id}")
    return 0 if status == "completed" else 2


def run_xhs_compatible_entry(mode: str) -> int:
    import search_stream_scrape

    original_argv = sys.argv[:]
    try:
        sys.argv = ["search_stream_scrape.py"]
        if mode == "test":
            sys.argv.append("--test")
        elif mode == "prod":
            sys.argv.append("--prod")
        search_stream_scrape.main()
    finally:
        sys.argv = original_argv
    return 0


def run_discover_keywords(args: argparse.Namespace, config: Dict[str, object]) -> int:
    if not args.from_db:
        print("请使用 --from-db 从 SQLite 读取已采集内容。")
        return 2
    db_path = ((config.get("output") or {}).get("db_path")) or None
    init_db(db_path)
    records = extract_keywords_from_db(db_path=db_path, top_n=args.top)
    output_dir = "reports"
    paths = write_discovered_keywords_outputs(records, output_dir=output_dir)
    if args.output_format == "md":
        print(paths["markdown"])
    elif args.output_format == "csv":
        print(paths["csv"])
    else:
        print(f"Markdown: {paths['markdown']}")
        print(f"CSV: {paths['csv']}")
    print(f"发现关键词数量: {len(records)}")
    return 0


def run_plan_crawl(args: argparse.Namespace, config: Dict[str, object]) -> int:
    output_config = config.get("output") or {}
    db_path = str(output_config.get("db_path") or "data/xhs_crawler.db")
    init_db(db_path)
    plan = build_crawl_plan(
        config,
        db_path=db_path,
        mode=args.mode,
        total_limit=args.limit,
        platform_filter=args.platform,
        seed=args.seed,
    )
    print(json.dumps(plan.asdict(), ensure_ascii=False, indent=2))
    if not args.dry_run:
        run_id = create_crawl_run(
            run_type="autonomous_plan",
            mode=args.mode,
            status="planned",
            plan=plan.asdict(),
            audit=plan.permission_audit.asdict(),
            db_path=db_path,
        )
        print(f"crawl_run_id: {run_id}")
    return 0 if plan.permission_audit.ok else 2


def run_profile_site(args: argparse.Namespace, config: Dict[str, object]) -> int:
    output_config = config.get("output") or {}
    db_path = str(output_config.get("db_path") or "data/xhs_crawler.db")
    crawler_config = config.get("crawler") or {}
    init_db(db_path)
    adapter_class = get_adapter_class(args.platform)
    supported_channels = list(getattr(adapter_class, "supported_channels", ["browser"]))
    budgets = crawler_config.get("platform_budgets") or {}
    platform_budget = budgets.get(args.platform) or {}
    max_blocked = max(1, int(platform_budget.get("max_blocked_per_run", 3)))
    health_score = 1.0
    status = "active"
    last_failure_reason = ""

    stats = get_platform_status_counts(args.platform, db_path=db_path)
    blocked_total = int(stats.get("blocked") or 0) + int(stats.get("login_required") or 0)
    blocked_total += int(stats.get("captcha_required") or 0) + int(stats.get("rate_limited") or 0)
    if blocked_total:
        health_score = max(0.0, 1.0 - (blocked_total / max_blocked))
        last_failure_reason = "recent_block_or_login_signal"
    if health_score <= 0:
        status = "blocked"

    selector_observations = {
        "source": "static_adapter_profile",
        "channels": supported_channels,
        "real_collection_enabled": args.platform in EXECUTABLE_PLATFORMS,
    }
    profile_id = upsert_site_profile(
        platform=args.platform,
        supported_channels=supported_channels,
        selector_observations=selector_observations,
        last_failure_reason=last_failure_reason,
        robots_status="respect_configured",
        recommended_min_delay_seconds=float(crawler_config.get("min_delay_seconds", 1.5)),
        recommended_max_delay_seconds=float(crawler_config.get("max_delay_seconds", 5.0)),
        health_score=health_score,
        status=status,
        tuning_notes=[
            "use official API or authorized export when available",
            "stop on login, captcha, rate limit, robots or access restriction signals",
        ],
        db_path=db_path,
    )
    print(
        json.dumps(
            {
                "profile_id": profile_id,
                "platform": args.platform,
                "supported_channels": supported_channels,
                "health_score": health_score,
                "status": status,
                "last_failure_reason": last_failure_reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_analyze(args: argparse.Namespace, config: Dict[str, object]) -> int:
    if not args.from_db:
        print("请使用 --from-db 从 SQLite 读取分析对象。")
        return 2
    db_path = ((config.get("output") or {}).get("db_path")) or None
    init_db(db_path)
    stats = get_daily_dashboard_stats(db_path=db_path)
    print("今日采集概览:")
    for key, value in stats.items():
        if isinstance(value, list):
            continue
        print(f"- {key}: {value}")
    return 0


def run_export(args: argparse.Namespace, config: Dict[str, object]) -> int:
    db_path = ((config.get("output") or {}).get("db_path")) or None
    init_db(db_path)
    result = export_dataset(
        table=args.table,
        output_format=args.format,
        output_path=args.output or None,
        limit=args.limit,
        db_path=db_path,
    )
    for path in result["paths"]:
        print(path)
    return 0


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.command == "crawl":
        return run_crawl(args, config)
    if args.command == "plan-crawl":
        return run_plan_crawl(args, config)
    if args.command == "discover-keywords":
        return run_discover_keywords(args, config)
    if args.command == "profile-site":
        return run_profile_site(args, config)
    if args.command == "analyze":
        return run_analyze(args, config)
    if args.command == "export":
        return run_export(args, config)
    if args.command == "agent":
        return run_agent(args, config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
