#!/bin/bash
# 队列6: 嫌疑③NaN保留(无需重建) → 嫌疑①MIN_KLINES=35(等Q5缓存建完再建_mk35宇宙)
cd /home/linux/websocket_new
BASE_ENV="NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=1 SL_PCT=5"
run() {
  name=$1; shift
  echo "===== $name 开始 $(date '+%m-%d %H:%M:%S') ====="
  env $BASE_ENV "$@" python3 gpu_backtest_exp.py 180 1 > ~/exp_$name.log 2>&1
  grep -E "回测完成|Sharpe=|Trades=" ~/exp_$name.log | tail -3
}
run nan_winq001 NAN_RAW=1 WINSOR_Q=0.001
# 等Q5的949维缓存建完(避免两个12核构建打架), 再建35天宇宙缓存
while ! grep -q "缓存命中\|预序列化" ~/exp_rawr_winq001.log 2>/dev/null; do sleep 120; done
run mk35_winq001 MIN_KLINES=35 CACHE_SUFFIX=_mk35 WINSOR_Q=0.001
echo "Q6 DONE $(date '+%m-%d %H:%M:%S')"
