#!/bin/bash
# 分支台账实验队列 (2026-07-29) — 串行跑, 每个约15-40min
# 基线 = 地球版1.3生产配置: aligned + volraw + fund + LONG动量闸门 + SL5%
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=1 SL_PCT=5"

run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  if ! grep -q "回测完成" ~/exp_$name.log; then
    echo "$name CUDA失败, 转CPU重试 $(date '+%H:%M:%S')"
    env $BASE_ENV "$@" XGB_DEVICE=cpu python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  fi
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
  echo "===== $name 结束 $(date '+%m-%d %H:%M:%S') ====="
}

run baseline
run rank RANK_MODE=1
run decay TIME_DECAY=60
run dart DART=1
run soup SOUP=1
run lgbm LGBM=1
run prune PRUNE_COLS=/home/linux/websocket_new/prune_cols.json
echo "ALL DONE $(date '+%m-%d %H:%M:%S')"
