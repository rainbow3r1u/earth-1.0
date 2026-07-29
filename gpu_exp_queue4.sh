#!/bin/bash
# 队列4: 成本/滑点敏感性 (WINSOR_OFF=1 全系统)
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=1 SL_PCT=5 WINSOR_OFF=1"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
run nowin_cost1 COST_PCT=1.0
run nowin_cost2 COST_PCT=2.0
run nowin_slip SLIP_SL=1.2
run nowin_cost2_slip COST_PCT=2.0 SLIP_SL=1.2
echo "Q4 DONE $(date '+%m-%d %H:%M:%S')"
