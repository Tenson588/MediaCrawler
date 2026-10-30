"""Run the major ski-brand monitor as a separate, sequential crawl task."""

from __future__ import annotations

import asyncio
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
BRAND_MONITOR_BRANDS = (
    "Bogner",
    "Atomic / Aomic",
    "Salomon（雪具）",
    "HEAD",
    "Burton",
    "ThirtyTwo",
    "卢西诺 / Rossignol",
)
BRAND_MONITOR_KEYWORDS = (
    "Bogner滑雪",
    "Atomic雪具",
    "Aomic雪具",
    "Salomon雪具",
    "HEAD雪具",
    "Burton官方",
    "ThirtyTwo雪鞋",
    "卢西诺雪具",
    "Rossignol雪具",
)
BURTON_ACTIVITY_NOTE_ID = "6ad1db26000000001a021851"
BRAND_MONITOR_SEEDS = (
    {
        "title": "Burton 官方活动：滑雪的尽头是“折”学",
        "url": "https://xhslink.cn/o/5aBhw4nwzrS",
        "resolved_url": f"https://www.xiaohongshu.com/explore/{BURTON_ACTIVITY_NOTE_ID}",
        "note_id": BURTON_ACTIVITY_NOTE_ID,
    },
)


class BrandMonitorManager:
    """Coordinate a known-post fetch followed by a focused brand search."""

    def __init__(self, crawler: CrawlerManager = crawler_manager):
        self.crawler = crawler
        self.status = "idle"
        self.phase: str | None = None
        self.message = "可单独执行主要品牌监控。"
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self._lock = asyncio.Lock()

    def phase_configs(self) -> list[tuple[str, str, CrawlerStartRequest]]:
        common = {
            "platform": PlatformEnum.XHS,
            "login_type": LoginTypeEnum.QRCODE,
            "enable_comments": False,
            "enable_sub_comments": False,
            "enable_media": False,
            "save_option": SaveDataOptionEnum.SQLITE,
            "headless": False,
        }
        return [
            (
                "known_post",
                "正在补采已知 Burton 官方活动",
                CrawlerStartRequest(
                    **common,
                    crawler_type=CrawlerTypeEnum.DETAIL,
                    specified_ids=BRAND_MONITOR_SEEDS[0]["resolved_url"],
                ),
            ),
            (
                "brand_search",
                "正在搜索主要品牌最新笔记",
                CrawlerStartRequest(
                    **common,
                    crawler_type=CrawlerTypeEnum.SEARCH,
                    keywords=",".join(BRAND_MONITOR_KEYWORDS),
                    max_notes_count=5,
                ),
            ),
        ]

    def snapshot(self) -> dict[str, Any]:
        logs = self.crawler.logs
        diagnose_access = getattr(self.crawler, "get_access_diagnostics", None)
        platform_access = (
            diagnose_access()
            if callable(diagnose_access)
            else {"suspected": False, "summary": "本次没有可供判断的采集日志", "signals": []}
        )
        return {
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "brands": list(BRAND_MONITOR_BRANDS),
            "keywords": list(BRAND_MONITOR_KEYWORDS),
            "seeds": list(BRAND_MONITOR_SEEDS),
            "last_log": logs[-1].message if logs else None,
            "platform_access": platform_access,
        }

    async def start(self) -> bool:
        async with self._lock:
            if self._task and not self._task.done():
                return False
            if self.crawler.process and self.crawler.process.poll() is None:
                return False
            self.status = "running"
            self.phase = "preparing"
            self.message = "品牌专项监控正在准备。"
            self.started_at = datetime.now(TZ)
            self.finished_at = None
            self._stop_requested = False
            self._task = asyncio.create_task(self._run())
            return True

    async def stop(self) -> bool:
        async with self._lock:
            if not self._task or self._task.done():
                return False
            self._stop_requested = True
            self.status = "stopping"
            self.message = "正在停止品牌专项监控。"
            if self.crawler.process and self.crawler.process.poll() is None:
                await self.crawler.stop()
            return True

    async def _run(self) -> None:
        try:
            for phase, message, config in self.phase_configs():
                if self._stop_requested:
                    break
                self.phase = phase
                self.message = message
                if not await self.crawler.start(config):
                    raise RuntimeError("已有其他采集任务正在运行")
                while self.crawler.process and self.crawler.process.poll() is None:
                    if self._stop_requested:
                        break
                    await asyncio.sleep(0.5)
                while self.crawler.status == "running":
                    await asyncio.sleep(0.1)
                if self._stop_requested:
                    break
                return_code = self.crawler.process.returncode if self.crawler.process else -1
                reason = self.crawler.failure_reason(return_code)
                if reason:
                    raise RuntimeError(f"{message}已停止：{reason}")
                if return_code != 0:
                    raise RuntimeError(f"{message}失败，采集进程退出码 {return_code}")

            self.finished_at = datetime.now(TZ)
            self.phase = None
            if self._stop_requested:
                self.status = "stopped"
                self.message = "品牌专项监控已停止。"
            else:
                self.status = "completed"
                self.message = "品牌专项监控已完成，数据已写入本地 SQLite。"
                if any("XHS_BUDGET_REACHED" in e.message for e in self.crawler.logs):
                    self.message = "品牌监控已按本轮限额完成，剩余关键词未采集。"
        except Exception as exc:
            self.finished_at = datetime.now(TZ)
            self.status = "error"
            self.message = str(exc)


brand_monitor_manager = BrandMonitorManager()
