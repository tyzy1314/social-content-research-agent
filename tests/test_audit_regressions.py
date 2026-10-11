from crawl_engine import CrawlerEngine
from crawl_guard import CrawlGuard, RiskDetectionResult
from db import get_connection, init_db, upsert_content_item
from dedupe import simhash_distance, simhash_text
from sentiment_analyzer import SentimentAnalyzer


def test_numeric_page_content_does_not_trigger_rate_limit():
    guard = CrawlGuard("xiaohongshu")

    normal = guard.detect_text(text="这篇笔记有 4290 次浏览，正文内容正常。")
    blocked = guard.detect_text(text="HTTP 429: Too Many Requests")

    assert normal.blocked is False
    assert blocked.status == "rate_limited"


def test_continuous_chinese_text_uses_near_duplicate_shingles():
    left = simhash_text("露营装备清单帐篷天幕睡袋")
    right = simhash_text("露营装备清单帐篷天幕防潮垫")

    assert left != right
    assert simhash_distance(left, right) < 20


def test_like_count_units_are_preserved():
    analyzer = SentimentAnalyzer("unused.csv")

    assert analyzer._parse_likes("1.2万") == 12_000
    assert analyzer._parse_likes("3.5w") == 35_000
    assert analyzer._parse_likes("2亿") == 200_000_000


def test_content_id_collision_reuses_existing_row(tmp_path):
    db_path = tmp_path / "crawler.db"
    init_db(db_path)

    first_id = upsert_content_item(
        platform="xiaohongshu",
        content_id="same-note",
        clean_url="https://www.xiaohongshu.com/explore/same-note",
        title="第一次采集",
        db_path=db_path,
    )
    second_id = upsert_content_item(
        platform="xiaohongshu",
        content_id="same-note",
        clean_url="https://www.xiaohongshu.com/discovery/item/same-note",
        title="第二次采集",
        db_path=db_path,
    )

    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT id, title FROM content_items WHERE platform = ? AND content_id = ?",
            ("xiaohongshu", "same-note"),
        ).fetchall()

    assert second_id == first_id
    assert [(row["id"], row["title"]) for row in rows] == [(first_id, "第二次采集")]


def test_blocked_search_page_always_closes_adapter(tmp_path):
    class BlockedAdapter:
        def __init__(self):
            self.guard = CrawlGuard("xiaohongshu")
            self.closed = False

        def open_search_page(self, keyword):
            return f"https://example.test/search?q={keyword}"

        def detect_current_risk(self):
            return RiskDetectionResult(
                blocked=True,
                reason="login_required",
                severity="high",
                action="require_user_action",
                status="login_required",
            )

        def close(self):
            self.closed = True

    adapter = BlockedAdapter()
    engine = CrawlerEngine(adapter, db_path=str(tmp_path / "crawler.db"))
    engine.run_xhs_test_stream(
        keyword="露营",
        stream_config={
            "success_target": 1,
            "max_candidates": 1,
            "max_comments_per_note": 1,
            "sleep_between_notes_seconds": 0,
            "fast_skip_sleep_seconds": 0,
            "write_db": False,
            "generate_summary": False,
        },
        output_config={
            "db_path": str(tmp_path / "crawler.db"),
            "csv_dir": str(tmp_path),
            "log_dir": str(tmp_path),
            "summary_dir": str(tmp_path),
        },
    )

    assert adapter.closed is True
