"""Offline regression checks: no browsers, real accounts or network requests."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from api.services.crawler_manager import CrawlerManager
from api.services.dealer_monitor import DealerMonitorManager
from api.services.brand_monitor import BrandMonitorManager
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.exception import PlatformAccessError
from media_platform.xhs.login import XiaoHongShuLogin
from tools.xhs_profile import xhs_profile_dir


@pytest.fixture
def search_crawler(monkeypatch):
    crawler = XiaoHongShuCrawler()
    crawler.context_page = SimpleNamespace(url="https://www.xiaohongshu.com/explore")
    crawler.xhs_client = AsyncMock()
    crawler._sleep_between_requests = AsyncMock()
    crawler.download_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr(crawler, "_recent_note_ids", lambda: set())
    monkeypatch.setattr(config, "KEYWORDS", "冷山,傲天,冷山,ace")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 5)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "XHS_MAX_NOTES_PER_RUN", 3)
    monkeypatch.setattr(config, "XHS_MAX_SEARCH_REQUESTS_PER_RUN", 12)
    monkeypatch.setattr("media_platform.xhs.core.xhs_store.update_xhs_note", AsyncMock())
    return crawler


def page(*ids, more=False):
    return {"has_more": more, "items": [{"id": i, "model_type": "note"} for i in ids]}


@pytest.mark.asyncio
async def test_budget_and_duplicates_across_keywords(search_crawler):
    crawler = search_crawler
    crawler.xhs_client.get_note_by_keyword.side_effect = [page("a", "a", "b"), page("a", "c", "d")]
    crawler.xhs_client.get_note_by_id.side_effect = lambda note_id, *_: {"note_id": note_id}
    await crawler.search()
    assert [call.args[0] for call in crawler.xhs_client.get_note_by_id.await_args_list] == ["a", "b", "c"]
    assert crawler.xhs_client.get_note_by_keyword.await_count == 2


@pytest.mark.asyncio
async def test_search_budget_also_bounds_empty_results(search_crawler, monkeypatch):
    monkeypatch.setattr(config, "XHS_MAX_SEARCH_REQUESTS_PER_RUN", 1)
    search_crawler.xhs_client.get_note_by_keyword.return_value = page()
    await search_crawler.search()
    assert search_crawler.xhs_client.get_note_by_keyword.await_count == 1


@pytest.mark.asyncio
async def test_recent_notes_do_not_trigger_detail_requests(search_crawler, monkeypatch):
    monkeypatch.setattr(search_crawler, "_recent_note_ids", lambda: {"recent"})
    search_crawler.xhs_client.get_note_by_keyword.return_value = page("recent")
    await search_crawler.search()
    search_crawler.xhs_client.get_note_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_notes_survive_later_restriction(search_crawler):
    from media_platform.xhs.core import xhs_store
    crawler = search_crawler
    crawler.xhs_client.get_note_by_keyword.return_value = page("ok", "blocked", "never")
    crawler.xhs_client.get_note_by_id.side_effect = [
        {"note_id": "ok"}, PlatformAccessError("HTTP 403")
    ]
    with pytest.raises(PlatformAccessError):
        await crawler.search()
    xhs_store.update_xhs_note.assert_awaited_once()
    assert crawler.xhs_client.get_note_by_id.await_count == 2
    crawler.batch_get_note_comments.assert_not_awaited()
    crawler.xhs_client.get_note_by_id_from_html.assert_not_awaited()


@pytest.mark.asyncio
async def test_captcha_stops_before_search(search_crawler):
    search_crawler.context_page.url = "https://www.xiaohongshu.com/website-login/captcha?verifyType=217"
    with pytest.raises(PlatformAccessError):
        await search_crawler.search()
    search_crawler.xhs_client.get_note_by_keyword.assert_not_awaited()


def test_recent_sqlite_ids_only(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE xhs_note (note_id TEXT, last_modify_ts INTEGER)")
        db.executemany("INSERT INTO xhs_note VALUES (?, ?)", [
            ("recent", int(time.time() * 1000)), ("old", 0)
        ])
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "sqlite")
    monkeypatch.setattr(config, "sqlite_db_config", {"db_path": str(path)})
    monkeypatch.setattr(config, "XHS_SKIP_RECENT_HOURS", 24)
    assert XiaoHongShuCrawler._recent_note_ids() == {"recent"}


def test_private_rotating_logs_redact_secrets_and_raw_payloads(tmp_path):
    manager = CrawlerManager()
    manager._project_root = tmp_path
    manager._open_disk_log()
    manager._create_log_entry("CAPTCHA verifyUuid=private-id xsec_token=secret-token password='secret-pass'", "error")
    manager._create_log_entry("Cookie: a1=private-cookie; web_session=private-session")
    manager._create_log_entry("[store.xhs.update_xhs_note] xhs note: {'nickname': 'private-name'}")
    initial_log = (tmp_path / ".runtime" / "crawler.log").read_text()
    assert "private-" not in initial_log and "secret-" not in initial_log
    assert "[REDACTED]" in initial_log
    # Force rotation and ensure new files retain private permissions.
    manager._disk_log.maxBytes = 200
    for _ in range(10):
        manager._create_log_entry("ordinary log message " * 4)
    for path in (tmp_path / ".runtime").glob("crawler.log*"):
        text = path.read_text()
        assert "private-" not in text and "secret-" not in text
        assert path.stat().st_mode & 0o777 == 0o600
    assert manager.failure_reason(0) is not None
    manager._disk_log.close()


def test_access_signal_survives_log_eviction():
    manager = CrawlerManager()
    manager._create_log_entry("XHS request blocked with HTTP 429", "error")
    for _ in range(510):
        manager._create_log_entry("traceback context")
    assert manager.get_access_diagnostics()["suspected"]
    assert "平台访问限制" in manager.failure_reason(1)


@pytest.mark.asyncio
async def test_dealer_does_not_report_success_for_blocked_zero_exit():
    crawler = CrawlerManager()
    crawler.process = SimpleNamespace(poll=lambda: 0, returncode=0)
    crawler._create_log_entry("PlatformAccessError: HTTP 403", "error")
    manager = DealerMonitorManager(crawler)
    await manager._watch()
    assert manager.status == "error"
    assert "平台访问限制" in manager.message


def test_budget_limited_progress_does_not_claim_all_keywords_completed():
    crawler = CrawlerManager()
    crawler._create_log_entry("Current search keyword: 冷山")
    crawler._create_log_entry("[XHS_BUDGET_REACHED] 本轮达到请求限额，剩余关键词未采集")
    manager = DealerMonitorManager(crawler)
    manager.keywords = ["冷山", "傲天", "ace"]
    manager.status = "completed"
    state = manager.snapshot()
    assert state["budget_limited"]
    assert state["completed_keywords"] == 1
    assert state["progress"] == 33


@pytest.mark.asyncio
async def test_brand_does_not_start_next_phase_after_restriction():
    crawler = CrawlerManager()
    async def start(_config):
        crawler.process = SimpleNamespace(poll=lambda: 0, returncode=0)
        crawler._create_log_entry("CAPTCHA appeared", "error")
        return True
    crawler.start = AsyncMock(side_effect=start)
    manager = BrandMonitorManager(crawler)
    await manager._run()
    assert manager.status == "error"
    assert crawler.start.await_count == 1


@pytest.mark.asyncio
async def test_login_restriction_does_not_turn_into_another_qrcode_attempt():
    client = XiaoHongShuClient.__new__(XiaoHongShuClient)
    client.query_self = AsyncMock(side_effect=PlatformAccessError("HTTP 403"))
    with pytest.raises(PlatformAccessError):
        await client.pong()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,html", [
    ("https://www.xiaohongshu.com/website-login/captcha", ""),
    ("https://www.xiaohongshu.com/explore", "请通过验证"),
])
async def test_login_verification_is_not_mistaken_for_success(url, html):
    page_mock = AsyncMock()
    page_mock.url = url
    page_mock.content.return_value = html
    page_mock.is_visible.return_value = True
    context = AsyncMock()
    login = XiaoHongShuLogin.__new__(XiaoHongShuLogin)
    login.context_page = page_mock
    login.browser_context = context
    with pytest.raises(PlatformAccessError):
        await login.check_login_state("old-session")
    context.cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_restriction_stops_other_waiting_comments(search_crawler):
    crawler = search_crawler
    crawler.xhs_client.get_note_all_comments.side_effect = PlatformAccessError("HTTP 429")
    semaphore = asyncio.Semaphore(1)
    outcomes = await asyncio.gather(
        crawler.get_comments("a", "token", semaphore),
        crawler.get_comments("b", "token", semaphore),
        return_exceptions=True,
    )
    assert isinstance(outcomes[0], PlatformAccessError)
    assert crawler.xhs_client.get_note_all_comments.await_count == 1


def test_named_account_profile_matches_verification_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    monkeypatch.setenv("MEDIACRAWLER_XHS_PROFILE", "account_a")
    assert xhs_profile_dir(tmp_path).name == "cdp_xhs_user_data_dir_account_a"
    assert xhs_profile_dir(tmp_path, cdp=False).name == "xhs_user_data_dir_account_a"
    monkeypatch.setenv("MEDIACRAWLER_XHS_PROFILE", "../other")
    with pytest.raises(ValueError):
        xhs_profile_dir(tmp_path)
