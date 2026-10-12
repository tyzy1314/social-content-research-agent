from __future__ import annotations

import argparse
import json
import sys

import pytest

import main
import platform_service
import unified_store
from platforms import PLATFORM_ORDER


def test_collect_live_parser_accepts_all_platform_keyword(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "collect-live", "--platform", "all", "--keyword", "露营", "--limit", "10"],
    )

    args = main.parse_args()

    assert args.command == "collect-live"
    assert args.platform == "all"
    assert args.keyword == "露营"
    assert args.content_id == ""
    assert args.limit == 10


def test_collect_live_rejects_missing_input_before_database(monkeypatch, capsys):
    migrated = []
    called = []
    monkeypatch.setattr(unified_store, "migrate", lambda *args, **kwargs: migrated.append(True))
    monkeypatch.setattr(
        platform_service,
        "collect_platform_comments",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    args = argparse.Namespace(platform="all", keyword="", content_id="", limit=10)

    exit_code = main.run_collect_live(args, {})

    assert exit_code == 2
    assert migrated == []
    assert called == []
    assert "需要且只能提供一个" in capsys.readouterr().out


def test_collect_live_rejects_content_id_for_all_platforms(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(unified_store, "migrate", lambda *args, **kwargs: called.append("migrate"))
    args = argparse.Namespace(platform="all", keyword="", content_id="BV123", limit=10)

    exit_code = main.run_collect_live(args, {})

    assert exit_code == 2
    assert called == []
    assert "content-id 不能用于 all" in capsys.readouterr().out


def test_collect_live_all_isolates_platform_failures(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_collect(db_path, platform, **kwargs):
        calls.append((platform, kwargs))
        if platform == PLATFORM_ORDER[0]:
            return {
                "run_id": "run-first",
                "platform": platform,
                "status": "failed",
                "contents": {"seen": 0, "inserted": 0, "updated": 0},
                "comments": {"seen": 0, "inserted": 0, "updated": 0},
                "errors": ["AUTH_REQUIRED: 需要登录"],
                "limited": [],
            }
        return {
            "run_id": f"run-{platform}",
            "platform": platform,
            "status": "finished",
            "contents": {"seen": 1, "inserted": 1, "updated": 0},
            "comments": {"seen": 2, "inserted": 2, "updated": 0},
            "errors": [],
            "limited": [],
        }

    monkeypatch.setattr(unified_store, "migrate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform_service, "collect_platform_comments", fake_collect)
    args = argparse.Namespace(platform="all", keyword="露营", content_id="", limit=10)

    exit_code = main.run_collect_live(args, {"output": {"db_path": str(tmp_path / "db.sqlite")}})

    output = capsys.readouterr().out
    assert exit_code == 0
    assert [platform for platform, _ in calls] == PLATFORM_ORDER
    assert all(kwargs["keyword"] == "露营" for _, kwargs in calls)
    assert all(kwargs["search_limit"] == 10 for _, kwargs in calls)
    assert f"{PLATFORM_ORDER[0]}: failed" in output
    assert f"{PLATFORM_ORDER[1]}: finished" in output
    assert "未能完整采集 1" in output
    assert "已采集 18 条" in output


@pytest.mark.parametrize("risk_status", ["login_required", "captcha_required"])
def test_xhs_login_wall_waits_for_manual_action_once(risk_status):
    import crawl_engine
    from crawl_guard import RiskDetectionResult

    class Adapter:
        headless = False

        def __init__(self):
            self.wait_count = 0
            self.check_count = 0

        def wait_user_login(self):
            self.wait_count += 1

        def detect_current_risk(self):
            self.check_count += 1
            return RiskDetectionResult(blocked=False, status="ok")

    adapter = Adapter()
    risk = RiskDetectionResult(blocked=True, status=risk_status, action="require_user_action")

    result = getattr(crawl_engine, "_resume_after_xhs_user_action")(adapter, risk)

    assert adapter.wait_count == 1
    assert adapter.check_count == 1
    assert result.status == "ok"


def test_xhs_rate_limit_is_not_retried_or_paused_for_manual_login():
    import crawl_engine
    from crawl_guard import RiskDetectionResult

    class Adapter:
        headless = False

        def wait_user_login(self):
            raise AssertionError("rate limit must not prompt for login")

    risk = RiskDetectionResult(blocked=True, status="rate_limited", action="pause")

    result = getattr(crawl_engine, "_resume_after_xhs_user_action")(Adapter(), risk)

    assert result is risk


def test_xhs_headless_login_wall_stops_without_prompt():
    import crawl_engine
    from crawl_guard import RiskDetectionResult

    class Adapter:
        headless = True

        def wait_user_login(self):
            raise AssertionError("headless browser cannot request manual login")

    risk = RiskDetectionResult(blocked=True, status="login_required", action="require_user_action")

    result = getattr(crawl_engine, "_resume_after_xhs_user_action")(Adapter(), risk)

    assert result is risk


def test_collection_service_records_access_and_stops_after_rate_limit(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import AccessResult, MODE_PUBLIC_READONLY, RATE_LIMITED
    from unified_store import list_access_records, migrate

    db_path = str(tmp_path / "platform.db")
    migrate(db_path, with_backup=False)

    class Adapter:
        platform_name = "bilibili"

        def __init__(self):
            self.reply_calls = 0
            self.closed = False

        def platform_info(self):
            return SimpleNamespace(access_method="public API")

        def default_access_mode(self):
            return MODE_PUBLIC_READONLY

        def search_content(self, keyword, limit=10):
            return AccessResult(
                "bilibili", "search_content", "OK", "found one item",
                {"items": [{"platform_content_id": "BV-test", "title": "sample"}]},
                access_mode=MODE_PUBLIC_READONLY, evidence={"endpoint": "search"},
            )

        def get_content_detail(self, content_id):
            return AccessResult(
                "bilibili", "content_detail", "OK", "detail loaded",
                {"content": {"platform": "bilibili", "platform_content_id": content_id,
                             "title": "sample", "source_method": "test-adapter"}},
                access_mode=MODE_PUBLIC_READONLY, evidence={"endpoint": "detail"},
            )

        def list_comments(self, content_id, limit=30):
            secret_key = "access_" + "token"
            fake_value = "unit" + "-redaction-value"
            message = f"HTTP 412 from https://example.test/comments?{secret_key}={fake_value}&cursor=1"
            return AccessResult(
                "bilibili", "comments", RATE_LIMITED, message,
                access_mode=MODE_PUBLIC_READONLY, evidence={"http_status": 412},
            )

        def list_replies(self, *args, **kwargs):
            self.reply_calls += 1
            raise AssertionError("replies must not be requested after HTTP 412")

        def close(self):
            self.closed = True

    adapter = Adapter()
    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: adapter)

    summary = platform_service.collect_platform_comments(
        db_path, "bilibili", keyword="camping", search_limit=1, comment_limit=5
    )

    records = list_access_records(db_path, task_id=summary["run_id"])
    assert summary["status"] == "partial"
    assert summary["contents"]["seen"] == 1
    assert adapter.reply_calls == 0
    assert adapter.closed is True
    assert [row["capability"] for row in reversed(records)] == ["search_content", "content_detail", "comments"]
    assert records[0]["status"] == RATE_LIMITED
    assert "HTTP 412" in records[0]["failure_reason"]
    assert "unit-redaction-value" not in records[0]["failure_reason"]


def test_bilibili_http_412_maps_to_rate_limited_without_retry(monkeypatch):
    from platforms.bilibili_api import BilibiliApiAdapter
    from platforms.capabilities import RATE_LIMITED

    adapter = BilibiliApiAdapter()
    calls = []

    def blocked_request(url, **kwargs):
        calls.append(url)
        raise RuntimeError(f"HTTP 412 from {url}")

    monkeypatch.setattr(adapter, "_get_json", blocked_request)

    result = adapter.get_content_detail("BV1234567890")

    assert result.status == RATE_LIMITED
    assert len(calls) == 1




def test_xhs_service_reuses_stream_and_audits_manual_session(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import MODE_AUTHORIZED_SESSION
    from unified_store import list_access_records, migrate

    db_path = str(tmp_path / "xhs.db")
    migrate(db_path, with_backup=False)

    class Adapter:
        platform_name = "xiaohongshu"
        headless = False
        closed = False

        def platform_info(self):
            return SimpleNamespace(access_method="visible Chrome with manual login")

        def close(self):
            self.closed = True

    class FakeEngine:
        def __init__(self, adapter, **kwargs):
            self.adapter = adapter

        def run_xhs_test_stream(self, **kwargs):
            return {
                "scanned_candidates": 4,
                "detail_attempts": 2,
                "detail_successes": 2,
                "comment_attempts": 2,
                "crawled": 2,
                "total_comments": 8,
                "no_comments": 0,
                "collected_legacy_ids": [101, 102],
                "failed": 0,
                "unavailable": 0,
                "blocked_count": 0,
                "login_required_count": 0,
                "captcha_required_count": 0,
                "rate_limited_count": 0,
                "platform_stop_reason": "",
            }

    adapter = Adapter()
    import_calls = []

    def fake_import(db_path, **kwargs):
        import_calls.append((db_path, kwargs))
        return {
            "content": {"seen": 2, "inserted": 2, "updated": 0},
            "comments": {"seen": 8, "inserted": 8, "updated": 0},
        }

    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr("crawl_engine.CrawlerEngine", FakeEngine)
    monkeypatch.setattr("unified_store.import_xhs_history", fake_import)

    summary = platform_service.collect_platform_comments(
        db_path, "xiaohongshu", keyword="露营", search_limit=10, comment_limit=30
    )

    records = list_access_records(db_path, task_id=summary["run_id"])
    assert summary["status"] == "finished"
    assert summary["contents"]["seen"] == 2
    assert summary["contents"]["inserted"] == 2
    assert summary["comments"]["seen"] == 8
    assert summary["comments"]["inserted"] == 8
    assert import_calls == [(db_path, {"keyword": "露营", "content_item_ids": [101, 102], "run_id": summary["run_id"]})]
    assert adapter.closed is True
    assert [row["capability"] for row in reversed(records)] == ["search_content", "content_detail", "comments"]
    assert all(row["access_mode"] == MODE_AUTHORIZED_SESSION for row in records)


def test_collect_live_rejects_xhs_content_id_with_login_guidance(monkeypatch, capsys):
    from types import SimpleNamespace

    called = []
    monkeypatch.setattr(unified_store, "migrate", lambda *args, **kwargs: called.append("migrate"))
    args = SimpleNamespace(platform="xiaohongshu", keyword="", content_id="note-id", limit=10)

    exit_code = main.run_collect_live(args, {})

    assert exit_code == 2
    assert called == []
    assert "需要关键词" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("http_status", "expected_status"),
    [(412, "RATE_LIMITED"), (429, "RATE_LIMITED"), (403, "PERMISSION_DENIED")],
)
def test_bilibili_barrage_maps_access_limits(monkeypatch, http_status, expected_status):
    from platforms.bilibili_api import BilibiliApiAdapter

    adapter = BilibiliApiAdapter()
    monkeypatch.setattr(adapter, "_resolve", lambda _content_id: {"bvid": "BV123", "aid": "1", "cid": "2"})
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: (http_status, b""))

    result = adapter.collect_barrage("BV123")

    assert result.status == expected_status
    assert result.evidence["http_status"] == http_status



def test_unified_store_ensure_does_not_backup_an_already_ready_database(tmp_path):
    from unified_store import ensure_unified_store, migrate

    db_path = tmp_path / "ready.db"
    migrate(db_path, with_backup=False)

    report = ensure_unified_store(db_path)

    assert report["backup"] == ""
    assert sorted(report["tables"]) == ["access_records", "platform_comments", "platform_content", "platform_runs"]


def test_unified_store_ensure_backups_before_adding_tables_to_legacy_db(tmp_path):
    import sqlite3
    from pathlib import Path

    from unified_store import ensure_unified_store

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE legacy_notes(id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO legacy_notes(body) VALUES('keep')")

    report = ensure_unified_store(db_path)

    assert report["backup"]
    assert Path(report["backup"]).is_file()
    with sqlite3.connect(report["backup"]) as connection:
        assert connection.execute("SELECT body FROM legacy_notes").fetchone() == ("keep",)


def test_bilibili_comment_resolution_http_412_is_not_invalid_input(monkeypatch):
    from platforms.bilibili_api import BilibiliApiAdapter
    from platforms.capabilities import RATE_LIMITED

    adapter = BilibiliApiAdapter()
    calls = []

    def blocked_get_json(url):
        calls.append(url)
        raise RuntimeError(f"HTTP 412 from {url}")

    monkeypatch.setattr(adapter, "_get_json", blocked_get_json)

    result = adapter.list_comments("BV1234567890", limit=5)

    assert result.status == RATE_LIMITED
    assert len(calls) == 1


def test_collection_service_marks_partial_comment_response(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import AccessResult, MODE_PUBLIC_READONLY
    from unified_store import migrate

    db_path = str(tmp_path / "partial.db")
    migrate(db_path, with_backup=False)

    class Adapter:
        platform_name = "bilibili"

        def platform_info(self):
            return SimpleNamespace(access_method="public API")

        def default_access_mode(self):
            return MODE_PUBLIC_READONLY

        def search_content(self, keyword, limit=10):
            return AccessResult("bilibili", "search_content", "OK", "found",
                                {"items": [{"platform_content_id": "BV1"}]},
                                access_mode=MODE_PUBLIC_READONLY)

        def get_content_detail(self, content_id):
            return AccessResult("bilibili", "content_detail", "OK", "found",
                                {"content": {"platform": "bilibili", "platform_content_id": content_id}},
                                access_mode=MODE_PUBLIC_READONLY)

        def list_comments(self, content_id, limit=30):
            return AccessResult("bilibili", "comments", "OK", "partial",
                                {"comments": [], "partial": True, "declared_total": 768},
                                access_mode=MODE_PUBLIC_READONLY)

        def list_replies(self, *args, **kwargs):
            raise AssertionError("no replies without comments")

        def close(self):
            pass

    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: Adapter())
    summary = platform_service.collect_platform_comments(db_path, "bilibili", keyword="camping")

    assert summary["status"] == "partial"
    assert summary["limited"]
    assert summary["limited"][0]["status"] == "PARTIAL"


def test_xhs_login_required_records_only_the_attempted_search(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import AUTH_REQUIRED
    from unified_store import list_access_records, migrate

    db_path = str(tmp_path / "xhs-login.db")
    migrate(db_path, with_backup=False)

    class Adapter:
        platform_name = "xiaohongshu"

        def platform_info(self):
            return SimpleNamespace(access_method="visible Chrome with manual login")

        def close(self):
            pass

    class FakeEngine:
        def __init__(self, adapter, **kwargs):
            pass

        def run_xhs_test_stream(self, **kwargs):
            return {
                "scanned_candidates": 0,
                "detail_attempts": 0,
                "detail_successes": 0,
                "comment_attempts": 0,
                "crawled": 0,
                "total_comments": 0,
                "no_comments": 0,
                "failed": 0,
                "unavailable": 0,
                "blocked_count": 1,
                "login_required_count": 1,
                "captcha_required_count": 0,
                "rate_limited_count": 0,
                "platform_stop_reason": "login wall remains",
            }

    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: Adapter())
    monkeypatch.setattr("crawl_engine.CrawlerEngine", FakeEngine)
    monkeypatch.setattr(
        "unified_store.import_xhs_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no local history import on failed login")),
    )

    summary = platform_service.collect_platform_comments(db_path, "xiaohongshu", keyword="露营")
    records = list_access_records(db_path, task_id=summary["run_id"])

    assert summary["status"] == "failed"
    assert [row["capability"] for row in records] == ["search_content"]
    assert records[0]["status"] == AUTH_REQUIRED
    assert "login" in records[0]["failure_reason"]



def test_collection_service_stops_before_comments_after_detail_rate_limit(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import AccessResult, MODE_PUBLIC_READONLY, RATE_LIMITED
    from unified_store import list_access_records, migrate

    db_path = str(tmp_path / "detail-limited.db")
    migrate(db_path, with_backup=False)

    class Adapter:
        platform_name = "bilibili"
        comment_calls = 0

        def platform_info(self):
            return SimpleNamespace(access_method="public API")

        def default_access_mode(self):
            return MODE_PUBLIC_READONLY

        def search_content(self, keyword, limit=10):
            return AccessResult("bilibili", "search_content", "OK", "found",
                                {"items": [{"platform_content_id": "BV1"}, {"platform_content_id": "BV2"}]},
                                access_mode=MODE_PUBLIC_READONLY)

        def get_content_detail(self, content_id):
            if content_id == "BV2":
                return AccessResult("bilibili", "content_detail", RATE_LIMITED, "HTTP 412",
                                    access_mode=MODE_PUBLIC_READONLY)
            return AccessResult("bilibili", "content_detail", "OK", "found",
                                {"content": {"platform": "bilibili", "platform_content_id": content_id}},
                                access_mode=MODE_PUBLIC_READONLY)

        def list_comments(self, *args, **kwargs):
            self.comment_calls += 1
            raise AssertionError("must stop before comments after a restricted detail request")

        def close(self):
            pass

    adapter = Adapter()
    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: adapter)
    summary = platform_service.collect_platform_comments(db_path, "bilibili", keyword="camping")
    records = list_access_records(db_path, task_id=summary["run_id"])

    assert summary["status"] == "partial"
    assert summary["contents"]["seen"] == 1
    assert adapter.comment_calls == 0
    assert [row["capability"] for row in reversed(records)] == [
        "search_content", "content_detail", "content_detail"
    ]


def test_unified_store_ensure_creates_missing_database_directory(tmp_path):
    from unified_store import ensure_unified_store

    db_path = tmp_path / "new-data" / "nested" / "research.db"

    ensure_unified_store(db_path)

    assert db_path.is_file()



def test_bilibili_search_uses_one_network_request(monkeypatch):
    import json

    from platforms.bilibili_api import BilibiliApiAdapter

    adapter = BilibiliApiAdapter()
    calls = []
    body = json.dumps({"code": 0, "message": "0", "data": {"result": []}}).encode()
    monkeypatch.setattr(adapter, "_request", lambda url, **kwargs: (calls.append(url) or 200, body))

    result = adapter.search_content("camping", limit=5)

    assert result.ok
    assert len(calls) == 1


def test_import_xhs_history_can_limit_sync_to_current_run_ids(tmp_path):
    from db import init_db, save_comments, upsert_note
    from unified_store import fetch_comments, fetch_content, import_xhs_history, migrate

    db_path = str(tmp_path / "xhs-legacy.db")
    init_db(db_path)
    migrate(db_path, with_backup=False)
    ids = []
    for number in (1, 2):
        url = f"https://www.xiaohongshu.com/explore/note-{number}"
        ids.append(upsert_note(
            keyword="露营", clean_url=url, raw_url=url, note_title=f"笔记{number}",
            note_content=f"正文{number}", author_name="作者", db_path=db_path,
        ))
    save_comments(ids[0], "https://www.xiaohongshu.com/explore/note-1", [
        {"用户名": "甲", "评论内容": "真实评论"},
    ], db_path=db_path)
    save_comments(ids[1], "https://www.xiaohongshu.com/explore/note-2", [
        {"用户名": "乙", "评论内容": "旧评论"},
    ], db_path=db_path)

    result = import_xhs_history(db_path, keyword="露营", content_item_ids=[ids[0]])

    assert result["content"]["seen"] == 1
    assert result["comments"]["seen"] == 1
    assert {row["title"] for row in fetch_content(db_path, platform="xiaohongshu")} == {"笔记1"}
    assert [row["content"] for row in fetch_comments(db_path, platform="xiaohongshu")] == ["真实评论"]


def test_xhs_noninteractive_login_prompt_stops_cleanly(capsys):
    import crawl_engine
    from crawl_guard import RiskDetectionResult

    class Adapter:
        headless = False
        detected_after_prompt = False

        def wait_user_login(self):
            raise EOFError

        def detect_current_risk(self):
            self.detected_after_prompt = True
            return RiskDetectionResult(blocked=False, status="ok")

    adapter = Adapter()
    risk = RiskDetectionResult(blocked=True, status="login_required", action="require_user_action")

    result = crawl_engine._resume_after_xhs_user_action(adapter, risk)

    assert result is risk
    assert adapter.detected_after_prompt is False
    assert "交互式终端" in capsys.readouterr().out


def test_collect_live_help_explains_manual_login(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["main.py", "collect-live", "--help"])

    try:
        main.parse_args()
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("--help must exit")

    assert "手动登录" in capsys.readouterr().out


def test_xhs_stream_exposes_only_usable_legacy_note_ids(tmp_path):
    from crawl_engine import CrawlerEngine
    from platforms.xiaohongshu import XhsAdapter

    engine = CrawlerEngine(XhsAdapter(headless=True), db_path=str(tmp_path / "xhs.db"))
    summary = engine._finalize_xhs_stream(
        keyword="露营",
        note_reports=[
            {"note": {"id": 11}, "status": "crawled", "detail_read": True},
            {"note": {"id": "12"}, "status": "no_comments", "detail_read": True},
            {"note": {"id": 13}, "status": "unavailable", "detail_read": False},
            {"note": {"id": "bad"}, "status": "crawled", "detail_read": True},
        ],
        failed_records=[],
        unavailable_records=[],
        summary_dir=tmp_path,
        generate_summary=False,
        notes_csv_path=tmp_path / "notes.csv",
        comments_csv_path=tmp_path / "comments.csv",
        failed_log_path=tmp_path / "failed.txt",
    )

    assert summary["collected_legacy_ids"] == [11, 12]
    assert summary["detail_attempts"] == 0
    assert summary["detail_successes"] == 0
    assert summary["comment_attempts"] == 0


def test_bilibili_search_rejects_malformed_result_rows(monkeypatch):
    import json

    from platforms.bilibili_api import BilibiliApiAdapter
    from platforms.capabilities import UNAVAILABLE

    adapter = BilibiliApiAdapter()
    body = json.dumps({"code": 0, "data": {"result": ["unexpected"]}}).encode()
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: (200, body))

    result = adapter.search_content("camping")

    assert result.status == UNAVAILABLE
    assert result.ok is False


def test_xhs_login_wall_on_comments_records_only_reached_capabilities(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import ACCESS_OK, AUTH_REQUIRED
    from unified_store import list_access_records, migrate

    db_path = str(tmp_path / "xhs-comment-login.db")
    migrate(db_path, with_backup=False)
    import_calls = []

    class Adapter:
        platform_name = "xiaohongshu"

        def platform_info(self):
            return SimpleNamespace(access_method="visible Chrome with manual login")

        def close(self):
            pass

    class FakeEngine:
        def __init__(self, adapter, **kwargs):
            pass

        def run_xhs_test_stream(self, **kwargs):
            return {
                "scanned_candidates": 1,
                "detail_attempts": 1,
                "detail_successes": 1,
                "comment_attempts": 1,
                "crawled": 0,
                "total_comments": 0,
                "no_comments": 0,
                "collected_legacy_ids": [77],
                "failed": 0,
                "unavailable": 0,
                "blocked_count": 1,
                "login_required_count": 1,
                "captcha_required_count": 0,
                "rate_limited_count": 0,
                "platform_stop_reason": "login wall while loading comments",
            }

    def fake_import(db_path, **kwargs):
        import_calls.append(kwargs)
        return {
            "content": {"seen": 1, "inserted": 1, "updated": 0},
            "comments": {"seen": 0, "inserted": 0, "updated": 0},
        }

    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: Adapter())
    monkeypatch.setattr("crawl_engine.CrawlerEngine", FakeEngine)
    monkeypatch.setattr("unified_store.import_xhs_history", fake_import)

    summary = platform_service.collect_platform_comments(db_path, "xiaohongshu", keyword="露营")
    records = list_access_records(db_path, task_id=summary["run_id"])
    by_capability = {row["capability"]: row for row in records}

    assert summary["status"] == "partial"
    assert summary["contents"]["seen"] == 1
    assert import_calls == [{"keyword": "露营", "content_item_ids": [77], "run_id": summary["run_id"]}]
    assert by_capability["search_content"]["status"] == ACCESS_OK
    assert by_capability["content_detail"]["status"] == ACCESS_OK
    assert by_capability["comments"]["status"] == AUTH_REQUIRED


def test_collection_service_explains_empty_search_results(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from platforms.capabilities import AccessResult, MODE_PUBLIC_READONLY

    class Adapter:
        platform_name = "bilibili"

        def platform_info(self):
            return SimpleNamespace(access_method="public API")

        def default_access_mode(self):
            return MODE_PUBLIC_READONLY

        def search_content(self, keyword, limit=10):
            return AccessResult("bilibili", "search_content", "OK", "返回 0 条",
                                {"items": []}, access_mode=MODE_PUBLIC_READONLY)

        def close(self):
            pass

    monkeypatch.setattr(platform_service, "get_adapter", lambda *_args, **_kwargs: Adapter())
    summary = platform_service.collect_platform_comments(
        str(tmp_path / "empty.db"), "bilibili", keyword="no-results"
    )

    assert summary["status"] == "empty"
    assert "搜索没有返回可采集的内容。" in summary["errors"]



def test_collect_live_parser_accepts_single_platform_content_id(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "collect-live", "--platform", "bilibili", "--content-id", "BV1234567890"],
    )

    args = main.parse_args()

    assert args.platform == "bilibili"
    assert args.content_id == "BV1234567890"
    assert args.keyword == ""


def test_collect_live_rejects_keyword_and_content_id_together(monkeypatch, capsys):
    migrated = []
    monkeypatch.setattr(unified_store, "ensure_unified_store", lambda *_args, **_kwargs: migrated.append(True))
    args = argparse.Namespace(platform="bilibili", keyword="camping", content_id="BV123", limit=10)

    exit_code = main.run_collect_live(args, {})

    assert exit_code == 2
    assert migrated == []
    assert "需要且只能提供一个" in capsys.readouterr().out
