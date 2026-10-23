#!/bin/bash
set -u

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/dashboard.pid"
LOG_FILE="$RUNTIME_DIR/dashboard.log"
cd "$PROJECT_DIR" || exit 1

if curl -fsS --max-time 2 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
  open "http://127.0.0.1:8080/reports"
  exit 0
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "未找到 .venv/bin/python，请先完成项目环境安装。"
  read -r -p "按回车键关闭窗口。"
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    OLD_COMMAND="$(ps -p "$OLD_PID" -o command= 2>/dev/null || true)"
    case "$OLD_COMMAND" in
      *"uvicorn api.main:app"*|*"start_dashboard.py"*)
        echo "正在清理未响应的旧服务…"
        kill "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
          kill -0 "$OLD_PID" 2>/dev/null || break
          sleep 0.25
        done
        kill -9 "$OLD_PID" 2>/dev/null || true
        ;;
    esac
  fi
  rm -f "$PID_FILE"
fi

echo "正在启动雪具情报服务…"
nohup .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8080 >"$LOG_FILE" 2>&1 &
SERVICE_PID=$!
echo "$SERVICE_PID" >"$PID_FILE"

for _ in $(seq 1 60); do
  if curl -fsS --max-time 1 http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "服务已启动，正在打开报告页面。"
    open "http://127.0.0.1:8080/reports"
    exit 0
  fi
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    echo "服务启动失败，最近日志："
    tail -20 "$LOG_FILE"
    read -r -p "按回车键关闭窗口。"
    exit 1
  fi
  sleep 0.25
done

echo "服务启动超时，请查看：$LOG_FILE"
read -r -p "按回车键关闭窗口。"
exit 1
