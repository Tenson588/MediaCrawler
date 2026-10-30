"""Manage a visible Xiaohongshu browser that shares the crawler login profile."""

from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from tools.xhs_profile import xhs_profile_dir


class XhsVerificationLauncher:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.process: subprocess.Popen | None = None

    @property
    def profile_dir(self) -> Path:
        return xhs_profile_dir(self.project_root)

    @property
    def status_file(self) -> Path:
        return self.project_root / ".runtime" / "xhs_verification_status.json"

    def _profile_process_ids(self) -> list[int]:
        if sys.platform == "win32":
            return []
        result = subprocess.run(
            ["pgrep", "-f", str(self.profile_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(value) for value in result.stdout.split() if value.isdigit()]

    def close(self) -> dict[str, str]:
        process_ids = set(self._profile_process_ids())
        if self.process and self.process.poll() is None:
            process_ids.add(self.process.pid)
        for process_id in process_ids:
            if process_id == os.getpid():
                continue
            try:
                os.kill(process_id, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if process_ids:
            time.sleep(0.5)
        self.process = None
        return {"status": "closed", "message": "人工验证窗口已关闭，可以重新启动专项抓取。"}

    def open(self) -> dict[str, str]:
        self.close()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "api.services.xhs_verification_browser",
            str(self.profile_dir),
            str(self.status_file),
        ]
        self.process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.status_file.exists():
                state = json.loads(self.status_file.read_text(encoding="utf-8"))
                if state.get("status") == "ready":
                    break
                raise RuntimeError(state.get("message") or "人工验证页面打开失败")
            if self.process.poll() is not None:
                raise RuntimeError("人工验证浏览器提前退出，请重新尝试。")
            time.sleep(0.1)
        else:
            raise RuntimeError("人工验证页面打开超时，请重新尝试。")
        return {
            "status": "opened",
            "message": "已打开小红书人工验证窗口。完成验证后请点击“完成验证并关闭窗口”，再重新启动抓取。",
        }
