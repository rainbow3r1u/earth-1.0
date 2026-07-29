#!/bin/bash
# 队列3: winsor变体深挖
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 SL_PCT=5"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
  echo "===== $name 结束 $(date '+%m-%d %H:%M:%S') ====="
}
run nowin_nogate WINSOR_OFF=1
run winq001 WINSOR_Q=0.001 LONG_MOM_FILTER=1
run winq001_nogate WINSOR_Q=0.001
echo "Q3 DONE $(date '+%m-%d %H:%M:%S')"
