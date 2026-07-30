#!/bin/bash
# 队列8: 特征家族扩展A/B + TP20复核 (基线=winq001_nogate 42.45)
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 SL_PCT=5 WINSOR_Q=0.001"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
echo '[946,947,948,949]' > prune_voltop.json
echo '[950,951,952]' > prune_resid.json
run ext_all EXT_FEATS=1              # 首跑建953维缓存
run ext_voltop EXT_FEATS=1 PRUNE_COLS=/home/linux/websocket_new/prune_resid.json   # 只留量能见顶4维
run ext_resid EXT_FEATS=1 PRUNE_COLS=/home/linux/websocket_new/prune_voltop.json   # 只留残差家族3维
run tp20_off180 TP_PCT=20 WF_OFFSET=180   # TP20换时段复核 (非ext缓存)
run tp20_cost1 TP_PCT=20 COST_PCT=1.0     # TP20成本臂
echo "Q8 DONE $(date '+%m-%d %H:%M:%S')"
