"""Start the local MediaCrawler dashboard and open it in the default browser."""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn


HOST = "127.0.0.1"
PORT = 8080
URL = f"http://{HOST}:{PORT}/reports"


def _server_is_running() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _open_when_ready() -> None:
    for _ in range(60):
        if _server_is_running():
            webbrowser.open(URL)
            return
        time.sleep(0.25)


def main() -> None:
    if _server_is_running():
        webbrowser.open(URL)
        print(f"服务已在运行：{URL}")
        return

    with socket.socket() as probe:
        if probe.connect_ex((HOST, PORT)) == 0:
            raise SystemExit(f"端口 {PORT} 已被其他程序占用")

    threading.Thread(target=_open_when_ready, daemon=True).start()
    print(f"正在启动服务：{URL}\n按 Ctrl+C 可停止服务。")
    uvicorn.run("api.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
