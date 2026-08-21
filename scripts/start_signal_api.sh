#!/bin/bash
# 启动 Signal API（读取 .signal_api.env）
cd /home/myuser/websocket_new
set -a
source /home/myuser/.signal_api.env
set +a
nohup /usr/bin/python3 -m uvicorn scripts.signal_api:app --host 0.0.0.0 --port 8080 >> logs/signal_api.log 2>&1 &
echo $! > /tmp/signal_api.pid
