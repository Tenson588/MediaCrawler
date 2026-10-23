"""Run a focused REV helmet issue monitor as an independent dashboard task."""

from __future__ import annotations

from .crawler_manager import CrawlerManager, crawler_manager
from .dealer_monitor import DealerMonitorManager


REV_MONITOR_TOPICS = (
    "负面评价",
    "投诉与售后",
    "开裂与破损",
    "松动与固定问题",
    "撞击保护反馈",
    "佩戴与视野问题",
)

REV_MONITOR_KEYWORDS = (
    "REV头盔",
    "REV滑雪头盔",
    "REV头盔问题",
    "REV头盔投诉",
    "REV头盔开裂",
    "REV头盔松动",
    "REV头盔安全",
    "REV头盔避雷",
)


class RevMonitorManager(DealerMonitorManager):
    """Reuse keyword progress tracking while keeping REV status independent."""

    def __init__(self, crawler: CrawlerManager = crawler_manager):
        super().__init__(crawler)
        self.message = "可单独执行 REV 头盔问题监控。"

    async def start(self) -> bool:
        started = await super().start(list(REV_MONITOR_KEYWORDS))
        if started:
            self.message = "正在抓取 REV 头盔负面、投诉和使用问题。"
        return started

    async def _watch(self) -> None:
        await super()._watch()
        if self.status == "completed":
            self.message = "REV 头盔监控已完成，本地数据已更新。"
        elif self.status == "error":
            self.message = self.message.replace("经销商抓取", "REV 头盔监控")

    def snapshot(self) -> dict:
        result = super().snapshot()
        result["topics"] = list(REV_MONITOR_TOPICS)
        return result


rev_monitor_manager = RevMonitorManager()
