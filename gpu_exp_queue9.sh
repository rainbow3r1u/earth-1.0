#!/bin/bash
# 队列9: 背离家族 (等队列8完成后跑, 避免抢资源)
cd /home/linux/websocket_new
while ! grep -q "Q8 DONE" ~/exp_queue8.log 2>/dev/null; do sleep 120; done
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 SL_PCT=5 WINSOR_Q=0.001"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
run div_all EXT_FEATS=1 DIV_FEATS=1   # 首跑建957维缓存(含ext+div), 对齐ext_all对比
echo "Q9 DONE $(date '+%m-%d %H:%M:%S')"
