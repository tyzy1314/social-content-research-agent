import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_safe_core_modules_import_without_browser_or_network():
    modules = [
        "ai_client",
        "agent_permissions",
        "crawl_engine",
        "crawl_guard",
        "crawl_planner",
        "db",
        "dedupe",
        "export_utils",
        "models",
        "notifier",
        "summarizer",
        "platforms.base",
        "platforms.bilibili",
        "main",
    ]

    for module_name in modules:
        importlib.import_module(module_name)


def test_crawl_help_is_available_without_opening_browser():
    result = subprocess.run(
        [sys.executable, "main.py", "crawl", "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--engine" in result.stdout
    assert "--platform" in result.stdout
    assert "--url" in result.stdout


def test_bilibili_url_helpers_are_local_and_deterministic():
    from platforms.bilibili import canonicalize_bilibili_video_url, extract_bilibili_content_id

    url = "https://www.bilibili.com/video/BV1WCqZBfEug/?spm_id_from=333"

    assert extract_bilibili_content_id(url) == "BV1WCqZBfEug"
    assert canonicalize_bilibili_video_url(url) == "https://www.bilibili.com/video/BV1WCqZBfEug"
