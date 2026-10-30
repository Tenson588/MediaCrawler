# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/services/crawler_manager.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import subprocess
import signal
import os
import sys
import tempfile
import re
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, List
from datetime import datetime
from pathlib import Path

from ..schemas import CrawlerStartRequest, LogEntry


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


class CrawlerManager:
    """Crawler process manager"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.status = "idle"
        self.started_at: Optional[datetime] = None
        self.current_config: Optional[CrawlerStartRequest] = None
        self._log_id = 0
        self._logs: List[LogEntry] = []
        self._read_task: Optional[asyncio.Task] = None
        # Project root directory
        self._project_root = Path(__file__).parent.parent.parent
        # Log queue - for pushing to WebSocket
        self._log_queue: Optional[asyncio.Queue] = None
        self._disk_log: Optional[RotatingFileHandler] = None
        self._log_write_error: Optional[str] = None
        self._access_signals: list[dict[str, str]] = []

    @staticmethod
    def _sanitize_log(message: str) -> str:
        # Raw note/search payloads can contain personal data, signed URLs and cookies.
        if any(marker in message for marker in (
            "Search notes response:", "Search notes res:", "Note details:",
            "xhs note:", "xhs note comment:", "creator:{",
        )):
            return message.split("]", 1)[0] + "] 内容载荷已省略"
        message = re.sub(
            r"(?i)(?:--cookies\s+|\b(?:cookie|set-cookie|authorization)\s*['\"]?\s*[:=]).*",
            "[登录凭据已隐藏]", message,
        )
        message = re.sub(
            r"(?i)((?:[\w-]*(?:token|password|passwd|api[_-]?key|secret)|verifyuuid|a1|web_session)"
            r"['\"]?\s*[:=]\s*['\"]?)[^\s,'\"&}\]]+",
            r"\1[REDACTED]", message,
        )
        message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", message)
        message = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", message)
        message = message.replace(str(Path.home()), "[LOCAL_USER]")
        return message

    def _open_disk_log(self) -> None:
        directory = self._project_root / ".runtime"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "crawler.log"
        # New files are private from creation, including on log rotation.
        self._disk_log = PrivateRotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, delay=True)
        if path.exists():
            path.chmod(0o600)

    def failure_reason(self, return_code: int) -> str | None:
        """Keep the actionable cause separate from the generic process exit code."""
        if self._access_signals:
            kind = self._access_signals[-1]["type"]
            return f"检测到平台访问限制（{kind}），本轮已停止。请人工检查账号或完成平台验证后再决定是否重试。"
        if any("profile is already in use" in e.message.lower()
               or "opening in existing browser session" in e.message.lower() for e in self._logs):
            return "人工验证窗口仍在使用采集登录资料。请关闭人工验证窗口后再重试。"
        if return_code:
            errors = [e.message for e in self._logs if e.level == "error"]
            detail = errors[-1][-240:] if errors else "详细原因请查看本地 .runtime/crawler.log"
            return f"采集进程退出码 {return_code}：{detail}"
        return None

    @property
    def logs(self) -> List[LogEntry]:
        return self._logs

    def get_log_queue(self) -> asyncio.Queue:
        """Get or create log queue"""
        if self._log_queue is None:
            self._log_queue = asyncio.Queue()
        return self._log_queue

    def _create_log_entry(self, message: str, level: str = "info") -> LogEntry:
        """Create log entry"""
        self._log_id += 1
        entry = LogEntry(
            id=self._log_id,
            timestamp=datetime.now().strftime("%H:%M:%S"),
            level=level,
            message=self._sanitize_log(message)
        )
        signal_patterns = (
            ("HTTP 401", r"HTTP\s*401|HTTP/[^ ]+ 401|status(?:_code)?[=: ]+401"),
            ("HTTP 403", r"HTTP\s*403|HTTP/[^ ]+ 403|status(?:_code)?[=: ]+403"),
            ("HTTP 429", r"HTTP\s*429|HTTP/[^ ]+ 429|status(?:_code)?[=: ]+429"),
            ("验证码", r"CAPTCHA|验证码|verifytype|verifyuuid|需要人工验证"),
            ("IP 限制", r"IPBlock|IP\s*(?:blocked|ban)|IP.*(?:限制|封禁)"),
            ("账号或访问受限", r"PlatformAccessError|access restrict|HTTP\s*(?:461|471)|账号违规"),
        )
        for label, pattern in signal_patterns:
            if re.search(pattern, entry.message, re.I):
                self._access_signals.append({"type": label, "message": entry.message[-300:]})
                self._access_signals = self._access_signals[-5:]
                break
        if self._disk_log is not None:
            try:
                # Use the handler's rotation logic, but surface write errors in status.
                record = logging.LogRecord("crawler", 20, "", 0,
                                           f"{datetime.now().isoformat()} {level} {entry.message}", (), None)
                if self._disk_log.shouldRollover(record):
                    self._disk_log.doRollover()
                if self._disk_log.stream is None:
                    self._disk_log.stream = self._disk_log._open()
                self._disk_log.stream.write(record.getMessage() + "\n")
                self._disk_log.flush()
            except OSError:
                self._log_write_error = "采集日志写入失败，请检查 .runtime 目录权限及磁盘空间"
        self._logs.append(entry)
        # Keep last 500 logs
        if len(self._logs) > 500:
            self._logs = self._logs[-500:]
        return entry

    async def _push_log(self, entry: LogEntry):
        """Push log to queue"""
        if self._log_queue is not None:
            try:
                self._log_queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def _parse_log_level(self, line: str) -> str:
        """Parse log level"""
        line_upper = line.upper()
        if "ERROR" in line_upper or "FAILED" in line_upper:
            return "error"
        elif "WARNING" in line_upper or "WARN" in line_upper:
            return "warning"
        elif "SUCCESS" in line_upper or "完成" in line or "成功" in line:
            return "success"
        elif "DEBUG" in line_upper:
            return "debug"
        return "info"

    async def start(self, config: CrawlerStartRequest) -> bool:
        """Start crawler process"""
        async with self._lock:
            if self.process and self.process.poll() is None:
                return False

            # Clear old logs
            self._logs = []
            self._log_id = 0
            self._access_signals = []
            self._log_write_error = None
            try:
                if self._disk_log:
                    self._disk_log.close()
                self._open_disk_log()
            except OSError:
                self._log_write_error = "采集日志无法创建，请检查 .runtime 目录权限及磁盘空间"
                self.status = "error"
                self._create_log_entry(self._log_write_error, "error")
                return False

            # Clear pending queue (don't replace object to avoid WebSocket broadcast coroutine holding old queue reference)
            if self._log_queue is None:
                self._log_queue = asyncio.Queue()
            else:
                try:
                    while True:
                        self._log_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            # Build command line arguments
            cmd = self._build_command(config)

            # Log start information
            entry = self._create_log_entry(f"Starting crawler: {' '.join(cmd)}", "info")
            await self._push_log(entry)

            try:
                matplotlib_cache = Path(tempfile.gettempdir()) / "mediacrawler-matplotlib"
                matplotlib_cache.mkdir(parents=True, exist_ok=True)
                # Start subprocess
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    bufsize=1,
                    cwd=str(self._project_root),
                    env={
                        **os.environ,
                        "PYTHONUNBUFFERED": "1",
                        "MPLCONFIGDIR": str(matplotlib_cache),
                    }
                )

                self.status = "running"
                self.started_at = datetime.now()
                self.current_config = config

                entry = self._create_log_entry(
                    f"Crawler started on platform: {config.platform.value}, type: {config.crawler_type.value}",
                    "success"
                )
                await self._push_log(entry)

                # Start log reading task
                self._read_task = asyncio.create_task(self._read_output())

                return True
            except Exception as e:
                self.status = "error"
                entry = self._create_log_entry(f"Failed to start crawler: {str(e)}", "error")
                await self._push_log(entry)
                return False

    async def stop(self) -> bool:
        """Stop crawler process"""
        async with self._lock:
            if not self.process or self.process.poll() is not None:
                return False

            self.status = "stopping"
            entry = self._create_log_entry("Sending SIGTERM to crawler process...", "warning")
            await self._push_log(entry)

            try:
                self.process.send_signal(signal.SIGTERM)

                # Wait for graceful exit (up to 15 seconds)
                for _ in range(30):
                    if self.process.poll() is not None:
                        break
                    await asyncio.sleep(0.5)

                # If still not exited, force kill
                if self.process.poll() is None:
                    entry = self._create_log_entry("Process not responding, sending SIGKILL...", "warning")
                    await self._push_log(entry)
                    self.process.kill()

                entry = self._create_log_entry("Crawler process terminated", "info")
                await self._push_log(entry)

            except Exception as e:
                entry = self._create_log_entry(f"Error stopping crawler: {str(e)}", "error")
                await self._push_log(entry)

            self.status = "idle"
            self.current_config = None

            # Cancel log reading task
            if self._read_task:
                self._read_task.cancel()
                self._read_task = None

            return True

    def get_status(self) -> dict:
        """Get current status"""
        return {
            "status": self.status,
            "platform": self.current_config.platform.value if self.current_config else None,
            "crawler_type": self.current_config.crawler_type.value if self.current_config else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "error_message": self._log_write_error or self.failure_reason(
                self.process.returncode if self.process and self.process.returncode is not None else 0
            ),
            "log_path": ".runtime/crawler.log",
            "platform_access": self.get_access_diagnostics(),
        }

    def get_access_diagnostics(self) -> dict:
        """Summarize explicit access-control signals from this process's logs."""
        return {
            "suspected": bool(self._access_signals),
            "summary": (
                "检测到平台访问限制，请人工检查账号状态；本轮不会自动继续。"
                if self._access_signals else "未发现明确的平台访问限制信号。"
            ),
            "signals": list(self._access_signals),
        }
    def _build_command(self, config: CrawlerStartRequest) -> list:
        """Build main.py command line arguments"""
        # Reuse the interpreter that successfully started the API. This avoids
        # depending on a separate `uv` executable after the environment check.
        cmd = [sys.executable, "main.py"]

        cmd.extend(["--platform", config.platform.value])
        cmd.extend(["--lt", config.login_type.value])
        cmd.extend(["--type", config.crawler_type.value])
        cmd.extend(["--save_data_option", config.save_option.value])

        # Pass different arguments based on crawler type
        if config.crawler_type.value == "search" and config.keywords:
            cmd.extend(["--keywords", config.keywords])
        elif config.crawler_type.value == "detail" and config.specified_ids:
            cmd.extend(["--specified_id", config.specified_ids])
        elif config.crawler_type.value == "creator" and config.creator_ids:
            cmd.extend(["--creator_id", config.creator_ids])

        if config.start_page != 1:
            cmd.extend(["--start", str(config.start_page)])

        cmd.extend(["--get_comment", "true" if config.enable_comments else "false"])
        cmd.extend(["--get_sub_comment", "true" if config.enable_sub_comments else "false"])
        cmd.extend(["--get_media", "true" if config.enable_media else "false"])

        if config.max_notes_count is not None:
            cmd.extend(["--crawler_max_notes_count", str(config.max_notes_count)])

        if config.max_comments_count is not None:
            cmd.extend(["--max_comments_count_singlenotes", str(config.max_comments_count)])

        if config.cookies:
            cmd.extend(["--cookies", config.cookies])

        cmd.extend(["--headless", "true" if config.headless else "false"])

        return cmd

    async def _read_output(self):
        """Asynchronously read process output"""
        loop = asyncio.get_event_loop()

        try:
            while self.process and self.process.poll() is None:
                # Read a line in thread pool
                line = await loop.run_in_executor(
                    None, self.process.stdout.readline
                )
                if line:
                    line = line.strip()
                    if line:
                        level = self._parse_log_level(line)
                        entry = self._create_log_entry(line, level)
                        await self._push_log(entry)

            # Read remaining output
            if self.process and self.process.stdout:
                remaining = await loop.run_in_executor(
                    None, self.process.stdout.read
                )
                if remaining:
                    for line in remaining.strip().split('\n'):
                        if line.strip():
                            level = self._parse_log_level(line)
                            entry = self._create_log_entry(line.strip(), level)
                            await self._push_log(entry)

            # Process ended
            if self.status == "running":
                exit_code = self.process.returncode if self.process else -1
                reason = self.failure_reason(exit_code)
                if not reason:
                    entry = self._create_log_entry("Crawler completed successfully", "success")
                else:
                    entry = self._create_log_entry(reason, "error")
                await self._push_log(entry)
                self.status = "idle"

        except asyncio.CancelledError:
            pass
        except Exception as e:
            entry = self._create_log_entry(f"Error reading output: {str(e)}", "error")
            await self._push_log(entry)


# Global singleton
crawler_manager = CrawlerManager()
