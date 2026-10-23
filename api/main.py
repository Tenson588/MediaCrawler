# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/main.py
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

"""
MediaCrawler WebUI API Server
Start command: uvicorn api.main:app --port 8080 --reload
Or: python -m api.main
"""
import os
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routers import crawler_router, data_router, websocket_router
from .routers.reports import router as reports_router
from .services.crawler_manager import crawler_manager
from .services.runtime_manager import runtime_manager
from .services.server_control import ServerControl
from .services.xhs_verification import XhsVerificationLauncher

# Project root directory used by the API and local runtime checks.
PROJECT_ROOT = Path(__file__).parent.parent

app = FastAPI(
    title="MediaCrawler WebUI API",
    description="API for controlling MediaCrawler from WebUI",
    version="1.0.0"
)
server_control = ServerControl(crawler_manager.stop)
xhs_verification = XhsVerificationLauncher(PROJECT_ROOT)

# Get webui static files directory
WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

# CORS configuration - allow frontend dev server access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",  # Backup port
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "null",  # A locally opened reports.html uses an opaque file:// origin.
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(reports_router, prefix="/api")
app.include_router(crawler_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(websocket_router, prefix="/api")


@app.get("/reports")
async def serve_reports():
    return FileResponse(PROJECT_ROOT / "api" / "static" / "reports.html")


@app.get("/")
async def serve_frontend():
    """Return frontend page"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return FileResponse(PROJECT_ROOT / "api" / "static" / "reports.html")



@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/env/check")
async def check_environment():
    """Run the same executable-environment check used by the dashboard."""
    result = await runtime_manager.start(force=True)
    return {
        **result,
        "platform_access": crawler_manager.get_access_diagnostics(),
        "success": result["ready"],
        "output": result["message"],
        "error": None if result["ready"] else result["message"],
    }


@app.get("/api/runtime/status")
async def runtime_status():
    return {
        **runtime_manager.snapshot(),
        "platform_access": crawler_manager.get_access_diagnostics(),
    }


@app.post("/api/runtime/start")
async def start_runtime(force: bool = False):
    """Prepare the crawler runtime. Safe to call repeatedly from page load."""
    return {
        **(await runtime_manager.start(force=force)),
        "platform_access": crawler_manager.get_access_diagnostics(),
    }


@app.post("/api/runtime/restart")
async def restart_runtime():
    """Restart this local API process and stop any active crawler first."""
    scheduled = server_control.schedule_restart()
    return {
        "status": "restarting" if scheduled else "pending",
        "message": "服务正在重启，页面会自动重新连接。",
    }


@app.post("/api/runtime/stop")
async def stop_runtime():
    """Stop this local API process and its active crawler child process."""
    scheduled = server_control.schedule_stop()
    return {
        "status": "stopping" if scheduled else "pending",
        "message": "服务正在关闭。再次使用时请双击启动器。",
    }


@app.post("/api/runtime/xhs-verification")
async def open_xhs_verification():
    """Open a visible browser using the crawler profile for manual CAPTCHA handling."""
    if crawler_manager.process and crawler_manager.process.poll() is None:
        await crawler_manager.stop()
    return xhs_verification.open()


@app.post("/api/runtime/xhs-verification/close")
async def close_xhs_verification():
    """Close the manual browser so the crawler can reuse its persisted profile."""
    return xhs_verification.close()


@app.get("/api/config/platforms")
async def get_platforms():
    """Get list of supported platforms"""
    return {
        "platforms": [
            {"value": "xhs", "label": "Xiaohongshu", "icon": "book-open"},
            {"value": "dy", "label": "Douyin", "icon": "music"},
            {"value": "ks", "label": "Kuaishou", "icon": "video"},
            {"value": "bili", "label": "Bilibili", "icon": "tv"},
            {"value": "wb", "label": "Weibo", "icon": "message-circle"},
            {"value": "tieba", "label": "Baidu Tieba", "icon": "messages-square"},
            {"value": "zhihu", "label": "Zhihu", "icon": "help-circle"},
        ]
    }


@app.get("/api/config/options")
async def get_config_options():
    """Get all configuration options"""
    return {
        "login_types": [
            {"value": "qrcode", "label": "QR Code Login"},
            {"value": "cookie", "label": "Cookie Login"},
        ],
        "crawler_types": [
            {"value": "search", "label": "Search Mode"},
            {"value": "detail", "label": "Detail Mode"},
            {"value": "creator", "label": "Creator Mode"},
        ],
        "save_options": [
            {"value": "jsonl", "label": "JSONL File"},
            {"value": "json", "label": "JSON File"},
            {"value": "csv", "label": "CSV File"},
            {"value": "excel", "label": "Excel File"},
            {"value": "sqlite", "label": "SQLite Database"},
            {"value": "db", "label": "MySQL Database"},
            {"value": "mongodb", "label": "MongoDB Database"},
        ],
    }


# Mount static resources - must be placed after all routes
if os.path.exists(WEBUI_DIR):
    assets_dir = os.path.join(WEBUI_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    # Mount logos directory
    logos_dir = os.path.join(WEBUI_DIR, "logos")
    if os.path.exists(logos_dir):
        app.mount("/logos", StaticFiles(directory=logos_dir), name="logos")
    # Mount other static files (e.g., vite.svg)
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="webui-static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
