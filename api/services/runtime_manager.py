"""Prepare and inspect the local crawler execution environment."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


class RuntimeManager:
    """Idempotent bootstrap for the process used by the crawler API."""

    REQUIRED_MODULES = ("fastapi", "pydantic", "playwright")

    def __init__(self) -> None:
        self.project_root = Path(__file__).resolve().parents[2]
        self.status = "stopped"
        self.message = "运行服务尚未启动"
        self.checked_at: str | None = None
        self.checks: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.status == "ready",
            "message": self.message,
            "checked_at": self.checked_at,
            "python": sys.executable,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "checks": self.checks,
        }

    async def start(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self.status == "ready" and not force:
                return self.snapshot()

            self.status = "starting"
            self.message = "正在检查并初始化运行环境"
            checks: list[dict[str, Any]] = []

            python_ok = sys.version_info >= (3, 11)
            checks.append({
                "name": "Python 可执行环境",
                "ok": python_ok,
                "detail": f"{sys.version.split()[0]} · {sys.executable}",
            })

            missing = [name for name in self.REQUIRED_MODULES if importlib.util.find_spec(name) is None]
            checks.append({
                "name": "核心依赖",
                "ok": not missing,
                "detail": "已安装" if not missing else f"缺少：{', '.join(missing)}",
            })

            main_file = self.project_root / "main.py"
            checks.append({
                "name": "采集程序",
                "ok": main_file.is_file(),
                "detail": str(main_file),
            })

            database = self.project_root / "database" / "sqlite_tables.db"
            database.parent.mkdir(parents=True, exist_ok=True)
            checks.append({
                "name": "本地数据目录",
                "ok": database.parent.is_dir(),
                "detail": str(database),
            })

            command_ok = False
            command_detail = "未执行"
            if python_ok and not missing and main_file.is_file():
                command_ok, command_detail = await self._check_command(main_file)
            checks.append({
                "name": "命令执行检查",
                "ok": command_ok,
                "detail": command_detail,
            })

            self.checks = checks
            self.checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
            if all(item["ok"] for item in checks):
                self.status = "ready"
                self.message = "运行服务已就绪，可以生成报告或启动采集"
            else:
                self.status = "error"
                self.message = "运行环境检查未通过，请查看检查项"
            return self.snapshot()

    async def _check_command(self, main_file: Path) -> tuple[bool, str]:
        process: asyncio.subprocess.Process | None = None
        try:
            matplotlib_cache = Path(tempfile.gettempdir()) / "mediacrawler-matplotlib"
            matplotlib_cache.mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(main_file),
                "--help",
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "MPLCONFIGDIR": str(matplotlib_cache)},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode == 0:
                return True, "main.py --help 执行成功"
            output = (stderr or stdout).decode("utf-8", errors="replace").strip()
            return False, output[-500:] or f"退出码 {process.returncode}"
        except asyncio.TimeoutError:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()
            return False, "执行超时（30 秒）"
        except OSError as exc:
            return False, str(exc)


runtime_manager = RuntimeManager()
