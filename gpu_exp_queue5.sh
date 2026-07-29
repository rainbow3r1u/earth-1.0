#!/bin/bash
# 队列5: 原始涨幅特征实验 (先建949维缓存, 再两臂)
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=1 SL_PCT=5 RAW_RET_FEATS=1"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
run rawr_winq001 WINSOR_Q=0.001   # 首跑会重建949维缓存(~1h)
run rawr_nowin WINSOR_OFF=1       # 复用缓存
echo "Q5 DONE $(date '+%m-%d %H:%M:%S')"
