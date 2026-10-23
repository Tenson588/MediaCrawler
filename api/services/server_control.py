"""Gracefully stop or replace the local dashboard server process."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable


StopCrawler = Callable[[], Awaitable[bool]]


class ServerControl:
    """Schedule process control after an HTTP response can be returned."""

    def __init__(self, stop_crawler: StopCrawler) -> None:
        self._stop_crawler = stop_crawler
        self._pending: asyncio.Task[None] | None = None

    def schedule_restart(self) -> bool:
        if self._pending and not self._pending.done():
            return False
        self._pending = asyncio.create_task(self._restart())
        return True

    def schedule_stop(self) -> bool:
        if self._pending and not self._pending.done():
            return False
        self._pending = asyncio.create_task(self._stop())
        return True

    async def _stop_active_crawler(self) -> None:
        try:
            await self._stop_crawler()
        except Exception:
            # Server recovery must still work if crawler cleanup itself is stuck.
            pass

    async def _restart(self) -> None:
        await asyncio.sleep(0.35)
        await self._stop_active_crawler()
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
            ],
        )

    async def _stop(self) -> None:
        await asyncio.sleep(0.35)
        await self._stop_active_crawler()
        os.kill(os.getpid(), signal.SIGTERM)
