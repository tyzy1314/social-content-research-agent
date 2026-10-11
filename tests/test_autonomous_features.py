import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_permissions import build_permission_audit
from crawl_planner import allocate_quota, build_crawl_plan
from db import (
    get_connection,
    init_db,
    list_dedupe_groups,
    save_content_comments,
    save_discovered_keywords,
    upsert_content_item,
)
from dedupe import normalized_text_hash, simhash_distance, simhash_text
from export_utils import export_xlsx


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_permission_allowlist_blocks_forbidden_capabilities():
    audit = build_permission_audit(
        {
            "agent": {
                "autonomy_enabled": True,
                "permission_model": "allowlist",
                "capabilities": ["browser", "api", "captcha_bypass", "proxy_rotation"],
                "allowed_output_dirs": ["outputs/csv"],
            }
        },
        output_config={},
    )

    assert not audit.ok
    assert "browser" in audit.allowed_capabilities
    assert "api" in audit.allowed_capabilities
    assert "captcha_bypass" in audit.blocked_capabilities
    assert "proxy_rotation" in audit.blocked_capabilities


def test_allocate_quota_uses_largest_remainder():
    assert allocate_quota({"xiaohongshu": 0.6, "bilibili": 0.4}, 5) == {
        "xiaohongshu": 3,
        "bilibili": 2,
    }


def test_build_crawl_plan_uses_config_and_discovered_keywords(tmp_path):
    db_path = tmp_path / "crawler.db"
    init_db(db_path)
    save_discovered_keywords(
        [
            {"term": "露营装备", "frequency": 10, "score": 9.0, "platforms": ["xiaohongshu"], "source_count": 2},
            {"term": "户外穿搭", "frequency": 8, "score": 8.0, "platforms": ["bilibili"], "source_count": 2},
        ],
        db_path=db_path,
    )
    config = {
        "keywords": ["露营"],
        "agent": {
            "autonomy_enabled": True,
            "permission_model": "allowlist",
            "allowed_platforms": ["xiaohongshu", "bilibili"],
            "allowed_output_dirs": [str(tmp_path)],
        },
        "output": {"db_path": str(db_path), "csv_dir": str(tmp_path)},
    }

    plan = build_crawl_plan(config, db_path=str(db_path), total_limit=5, platform_filter="all")
    payload = plan.asdict()

    assert payload["permission_audit"]["ok"] is True
    assert payload["keyword_sources"]["core_seed"] == ["露营"]
    assert "露营装备" in payload["keyword_sources"]["discovered"]
    quotas = {item["platform"]: item["quota"] for item in payload["platform_allocations"]}
    assert quotas == {"xiaohongshu": 3, "bilibili": 2}


def test_dedupe_hash_and_simhash_are_stable():
    left_hash = normalized_text_hash("  露营 装备 推荐 ")
    right_hash = normalized_text_hash("露营 装备 推荐")

    assert left_hash == right_hash
    assert simhash_text("露营 装备 推荐") == simhash_text("露营 装备 推荐")
    assert simhash_distance(simhash_text("露营 装备 推荐"), simhash_text("露营 装备 推荐 清单")) < 20


def test_content_and_comments_store_dedupe_fields(tmp_path):
    db_path = tmp_path / "crawler.db"
    init_db(db_path)
    item_id = upsert_content_item(
        platform="xiaohongshu",
        clean_url="https://www.xiaohongshu.com/explore/abc123",
        title="露营装备",
        body="帐篷和天幕推荐",
        status="crawled",
        db_path=db_path,
    )
    save_content_comments(
        content_item_id=item_id,
        platform="xiaohongshu",
        content_id="abc123",
        source_url="https://www.xiaohongshu.com/explore/abc123",
        comments=[{"username": "u", "content": "很好用", "publish_time": "2026-05-15"}],
        db_path=db_path,
    )

    with get_connection(db_path) as connection:
        item = connection.execute("SELECT normalized_text_hash, simhash FROM content_items WHERE id = ?", (item_id,)).fetchone()
        comment = connection.execute("SELECT normalized_text_hash, simhash FROM content_comments LIMIT 1").fetchone()

    assert item["normalized_text_hash"]
    assert item["simhash"]
    assert comment["normalized_text_hash"]
    assert comment["simhash"]


def test_exact_duplicate_content_creates_dedupe_group(tmp_path):
    db_path = tmp_path / "crawler.db"
    init_db(db_path)
    for content_id in ("abc123", "def456"):
        upsert_content_item(
            platform="xiaohongshu",
            clean_url=f"https://www.xiaohongshu.com/explore/{content_id}",
            title="同一篇露营装备",
            body="帐篷和天幕推荐",
            status="crawled",
            db_path=db_path,
        )

    groups = list_dedupe_groups(db_path=db_path)

    assert any(group["group_key"].startswith("exact:") and group["item_count"] >= 2 for group in groups)


def test_export_xlsx_all_tables(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    db_path = tmp_path / "crawler.db"
    init_db(db_path)
    upsert_content_item(
        platform="bilibili",
        clean_url="https://www.bilibili.com/video/BV1234567890",
        content_id="BV1234567890",
        title="露营视频",
        body="户外装备测评",
        status="crawled",
        db_path=db_path,
    )
    output_path = tmp_path / "export.xlsx"

    export_xlsx(table="all", output_path=output_path, limit=100, db_path=db_path)
    workbook = openpyxl.load_workbook(output_path)

    assert {"content", "comments", "keywords", "crawl_runs", "site_profiles", "dedupe_groups"}.issubset(
        set(workbook.sheetnames)
    )


def test_plan_crawl_dry_run_cli_does_not_require_browser(tmp_path):
    db_path = tmp_path / "crawler.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "keywords:",
                "  - 露营",
                "agent:",
                "  autonomy_enabled: true",
                "  permission_model: allowlist",
                "  allowed_platforms:",
                "    - xiaohongshu",
                "    - bilibili",
                "  allowed_output_dirs:",
                f"    - {tmp_path.as_posix()}",
                "output:",
                f"  db_path: {db_path.as_posix()}",
                f"  csv_dir: {tmp_path.as_posix()}",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "main.py", "--config", str(config_path), "plan-crawl", "--dry-run", "--limit", "5"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["platform_allocations"][0]["quota"] > 0
    assert "crawl_run_id" not in result.stdout
