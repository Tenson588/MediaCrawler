"""Visible browser process used for manual Xiaohongshu login/CAPTCHA handling."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path

from playwright.async_api import async_playwright


XHS_URL = "https://www.xiaohongshu.com/explore"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


async def run(profile_dir: Path, status_file: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass

    async with async_playwright() as playwright:
        executable = CHROME_PATH if Path(CHROME_PATH).exists() else None
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=executable,
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        # Xiaohongshu can keep background resources pending for a long time.
        # A committed navigation is enough to replace about:blank and let the
        # user interact with login or CAPTCHA UI while the page continues loading.
        await page.goto(XHS_URL, wait_until="commit", timeout=30_000)
        await page.bring_to_front()
        status_file.write_text(
            json.dumps({"status": "ready", "url": page.url}, ensure_ascii=False),
            encoding="utf-8",
        )
        while not stop.is_set() and context.pages:
            await asyncio.sleep(0.5)
        await context.close()


if __name__ == "__main__":
    profile = Path(sys.argv[1]).resolve()
    status = Path(sys.argv[2]).resolve()
    try:
        asyncio.run(run(profile, status))
    except Exception as exc:
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(
            json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False),
            encoding="utf-8",
        )
        raise
