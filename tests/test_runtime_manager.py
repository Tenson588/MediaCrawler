from pathlib import Path

import pytest

from api.services.runtime_manager import RuntimeManager


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
