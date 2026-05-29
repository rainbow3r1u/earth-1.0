#!/bin/bash
# 数据收集器 cron 安装 — 第六轮审计修复
set -e
WS=/home/myuser/websocket_new
(crontab -l 2>/dev/null || true) > /tmp/crontab_bak
cat >> /tmp/crontab_bak <<CRONS
# === 数据收集器 (第六轮审计补充) ===
0 1 * * * cd $WS && python3 fear_greed_collector.py >> /tmp/fear_greed_cron.log 2>&1
0 2 * * * cd $WS && python3 collect_macro_assets.py >> /tmp/macro_assets_cron.log 2>&1
0 3 * * * cd $WS && python3 sector_fetcher.py >> /tmp/sector_fetch_cron.log 2>&1
0 4 * * * cd $WS && python3 collect_btc_dominance.py >> /tmp/btc_dom_cron.log 2>&1
CRONS
sort -u /tmp/crontab_bak | crontab -
echo "Crons installed"
