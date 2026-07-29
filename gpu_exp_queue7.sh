#!/bin/bash
# 队列7: TP敏感性 (winq001_nogate配置, 止盈币48h中位冲高+50.5%)
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 SL_PCT=5 WINSOR_Q=0.001"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
run winq_nogate_tp15 TP_PCT=15
run winq_nogate_tp20 TP_PCT=20
run winq_nogate_tp30 TP_PCT=30
echo "Q7 DONE $(date '+%m-%d %H:%M:%S')"
