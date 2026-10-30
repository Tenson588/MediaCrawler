"""Run dealer keyword collection and expose dashboard-friendly progress."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from api.schemas import (
    CrawlerStartRequest,
    CrawlerTypeEnum,
    LoginTypeEnum,
    PlatformEnum,
    SaveDataOptionEnum,
)

from .crawler_manager import CrawlerManager, crawler_manager


TZ = ZoneInfo("Asia/Shanghai")
CURRENT_KEYWORD_PATTERN = re.compile(r"Current search keyword:\s*(.+)$")


class DealerMonitorManager:
    """Coordinate one dealer search and report keyword-level progress."""

    def __init__(self, crawler: CrawlerManager = crawler_manager):
        self.crawler = crawler
        self.status = "idle"
        self.message = "可使用当前关键词启动经销商抓取。"
        self.keywords: list[str] = []
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _progress(self) -> tuple[int, str | None, int]:
        budget_limited = any("XHS_BUDGET_REACHED" in e.message for e in self.crawler.logs)
        if self.status == "completed" and not budget_limited:
            return 100, self.keywords[-1] if self.keywords else None, len(self.keywords)
        if self.status not in {"running", "stopping", "completed"}:
            return 0, None, 0

        current: str | None = None
        completed = 0
        for entry in self.crawler.logs:
            match = CURRENT_KEYWORD_PATTERN.search(entry.message)
            if not match:
                continue
            current = match.group(1).strip()
            try:
                completed = self.keywords.index(current)
            except ValueError:
                continue
        if not self.keywords:
            return 0, current, completed
        if current is None:
            return 2, None, 0
        if self.status == "completed" and budget_limited:
            completed += 1
            return round(completed / len(self.keywords) * 100), current, completed
        progress = min(95, max(3, round((completed + 0.5) / len(self.keywords) * 100)))
        return progress, current, completed

    def snapshot(self) -> dict[str, Any]:
        progress, current, completed = self._progress()
        logs = self.crawler.logs
        diagnose_access = getattr(self.crawler, "get_access_diagnostics", None)
        platform_access = (
            diagnose_access()
            if callable(diagnose_access)
            else {"suspected": False, "summary": "本次没有可供判断的采集日志", "signals": []}
        )
        return {
            "status": self.status,
            "message": self.message,
            "progress": progress,
            "current_keyword": current,
            "completed_keywords": completed,
            "total_keywords": len(self.keywords),
            "keywords": self.keywords,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_log": logs[-1].message if logs else None,
            "platform_access": platform_access,
            "budget_limited": any("XHS_BUDGET_REACHED" in e.message for e in logs),
        }

    async def start(self, keywords: list[str]) -> bool:
        async with self._lock:
            if self._task and not self._task.done():
                return False
            if self.crawler.process and self.crawler.process.poll() is None:
                return False
            self.keywords = list(dict.fromkeys(keyword.strip() for keyword in keywords if keyword.strip()))
            if not self.keywords:
                raise ValueError("请至少输入一个抓取关键词")

            config = CrawlerStartRequest(
                platform=PlatformEnum.XHS,
                login_type=LoginTypeEnum.QRCODE,
                crawler_type=CrawlerTypeEnum.SEARCH,
                keywords=",".join(self.keywords),
                enable_comments=False,
                enable_sub_comments=False,
                enable_media=False,
                save_option=SaveDataOptionEnum.SQLITE,
                headless=False,
                max_notes_count=5,
            )
            if not await self.crawler.start(config):
                return False

            self.status = "running"
            self.message = "正在抓取经销商最新笔记。"
            self.started_at = datetime.now(TZ)
            self.finished_at = None
            self._task = asyncio.create_task(self._watch())
            return True

    async def _watch(self) -> None:
        try:
            while self.crawler.process and self.crawler.process.poll() is None:
                await asyncio.sleep(0.5)
            while self.crawler.status == "running":
                await asyncio.sleep(0.1)
            return_code = self.crawler.process.returncode if self.crawler.process else -1
            self.finished_at = datetime.now(TZ)
            explain_failure = getattr(self.crawler, "failure_reason", None)
            reason = explain_failure(return_code) if callable(explain_failure) else None
            if return_code == 0 and not reason:
                self.status = "completed"
                self.message = "经销商抓取已完成，本地数据已更新。"
                if any("XHS_BUDGET_REACHED" in e.message for e in self.crawler.logs):
                    self.message = "经销商抓取已按本轮限额完成，剩余关键词未采集。"
            else:
                self.status = "error"
                if reason:
                    self.message = f"经销商抓取已停止：{reason}"
                    return
                profile_busy = any(
                    "profile is already in use" in entry.message.lower()
                    or "opening in existing browser session" in entry.message.lower()
                    for entry in self.crawler.logs
                )
                if profile_busy:
                    self.message = (
                        "经销商抓取无法启动：人工验证窗口仍在使用采集登录资料。"
                        "请关闭人工验证窗口后再重试。"
                    )
                else:
                    self.message = f"经销商抓取失败，采集进程退出码 {return_code}。"
        except Exception as exc:
            self.finished_at = datetime.now(TZ)
            self.status = "error"
            self.message = str(exc)


dealer_monitor_manager = DealerMonitorManager()
