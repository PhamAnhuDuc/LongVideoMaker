#!/usr/bin/env bash
# Khởi động Long Video Maker và mở trình duyệt
cd "$(dirname "$(readlink -f "$0")")" || exit 1
PORT=8501
if ! (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
  .venv/bin/streamlit run app.py --server.port $PORT --server.headless true >streamlit.log 2>&1 &
  for _ in $(seq 1 30); do (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break; sleep 1; done
fi
xdg-open "http://localhost:$PORT" >/dev/null 2>&1
