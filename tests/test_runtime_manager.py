from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.services.crawler_manager import CrawlerManager
from api.services.runtime_manager import RuntimeManager
from api.services.xhs_verification import XhsVerificationLauncher


@pytest.mark.asyncio
async def test_runtime_start_reports_ready(monkeypatch, tmp_path: Path):
    manager = RuntimeManager()
    manager.project_root = tmp_path
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")

    async def command_ok(_main_file: Path):
        return True, "command ok"

    monkeypatch.setattr(manager, "_check_command", command_ok)
    result = await manager.start()

    assert result["ready"] is True
    assert result["status"] == "ready"
    assert len(result["checks"]) == 5
    assert all(item["ok"] for item in result["checks"])
    assert (tmp_path / "database").is_dir()


@pytest.mark.asyncio
async def test_runtime_start_exposes_command_failure(monkeypatch, tmp_path: Path):
    manager = RuntimeManager()
    manager.project_root = tmp_path
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")

    async def command_fails(_main_file: Path):
        return False, "broken command"

    monkeypatch.setattr(manager, "_check_command", command_fails)
    result = await manager.start(force=True)

    assert result["ready"] is False
    assert result["status"] == "error"
    assert result["checks"][-1] == {
        "name": "命令执行检查",
        "ok": False,
        "detail": "broken command",
    }


def test_crawler_access_diagnostics_detects_explicit_block_signals():
    manager = CrawlerManager()
    manager._create_log_entry("XHS request blocked with HTTP 429", "error")
    manager._create_log_entry("CAPTCHA appeared, request failed", "error")

    result = manager.get_access_diagnostics()

    assert result["suspected"] is True
    assert [item["type"] for item in result["signals"]] == ["HTTP 429", "验证码"]


def test_crawler_access_diagnostics_does_not_guess_without_evidence():
    manager = CrawlerManager()
    manager._create_log_entry("Crawler started", "info")

    result = manager.get_access_diagnostics()

    assert result["suspected"] is False
    assert "未发现明确" in result["summary"]


@pytest.mark.asyncio
async def test_runtime_restart_and_stop_are_scheduled_after_response(monkeypatch):
    monkeypatch.setattr(api_main.server_control, "schedule_restart", lambda: True)
    monkeypatch.setattr(api_main.server_control, "schedule_stop", lambda: True)

    restarted = await api_main.restart_runtime()
    stopped = await api_main.stop_runtime()

    assert restarted["status"] == "restarting"
    assert stopped["status"] == "stopping"


def test_local_file_page_origin_can_reach_api():
    response = TestClient(api_main.app).options(
        "/api/runtime/status",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"


def test_xhs_verification_uses_crawler_profile(monkeypatch, tmp_path: Path):
    launched = {}
    monkeypatch.setattr(XhsVerificationLauncher, "_profile_process_ids", lambda self: [])

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return None

    status_file = tmp_path / ".runtime" / "xhs_verification_status.json"
    def report_ready(_seconds):
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text('{"status":"ready","url":"https://www.xiaohongshu.com/explore"}')

    monkeypatch.setattr("api.services.xhs_verification.time.sleep", report_ready)

    monkeypatch.setattr(
        "api.services.xhs_verification.subprocess.Popen",
        lambda command, **kwargs: launched.update(command=command, **kwargs) or Process(),
    )

    result = XhsVerificationLauncher(tmp_path).open()

    assert result["status"] == "opened"
    assert launched["cwd"] == tmp_path
    assert launched["command"][1:4] == ["-m", "api.services.xhs_verification_browser", str(tmp_path / "browser_data" / "xhs_user_data_dir")]
    assert launched["command"][4] == str(status_file)
    assert launched["start_new_session"] is True


@pytest.mark.asyncio
async def test_manual_verification_endpoint_opens_browser(monkeypatch):
    monkeypatch.setattr(api_main.crawler_manager, "process", None)
    monkeypatch.setattr(
        api_main.xhs_verification,
        "open",
        lambda: {"status": "opened", "message": "ready"},
    )

    result = await api_main.open_xhs_verification()

    assert result == {"status": "opened", "message": "ready"}


@pytest.mark.asyncio
async def test_manual_verification_close_endpoint(monkeypatch):
    monkeypatch.setattr(
        api_main.xhs_verification,
        "close",
        lambda: {"status": "closed", "message": "closed"},
    )

    result = await api_main.close_xhs_verification()

    assert result == {"status": "closed", "message": "closed"}
