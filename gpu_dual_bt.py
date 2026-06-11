#!/usr/bin/env python3
"""原始 dual_backtest 365天 — 离线版（含 _fast_winsor_bounds 优化）"""
import os, sys, json, time

sys.path.insert(0, '/root/reasonix-projects/websocket_new')
sys.stdout.reconfigure(line_buffering=True)

# GPU XGBoost
from xgboost import XGBClassifier as _O
class _G(_O):
    def __init__(self, **kw):
        kw.setdefault('device', 'cuda')
        kw.setdefault('tree_method', 'hist')
        super().__init__(**kw)
import xgboost
xgboost.XGBClassifier = _G
import daily_predictor as dp
dp.XGBClassifier = _G

# 离线: 拦截 Binance API
import requests
_real_get = requests.get
def _offline_get(url, *a, **kw):
    if 'binance' in url:
        raise ConnectionError("offline")
    return _real_get(url, *a, **kw)
requests.get = _offline_get

# 强制 OI 读缓存
def _cached_oi(syms, limit=500):
    cp = "/root/reasonix-projects/backtester/data_cache/oi_daily.json"
    out = {}
    if os.path.exists(cp):
        with open(cp) as f:
            lc = json.load(f)
        for s in syms:
            if s in lc and lc[s]:
                cc = lc[s]
                sts = sorted(cc.keys(), reverse=True)[:limit]
                out[s] = {int(t): float(cc[t]) for t in sts}
    return out
dp.fetch_oi = _cached_oi

print(f"[{time.strftime('%H:%M:%S')}] 原始 dual_backtest(365, stride=1) 启动")
t0 = time.time()
dp.dual_backtest(days=365, stride=1)
print(f"[{time.strftime('%H:%M:%S')}] 完成, 耗时 {(time.time()-t0)/60:.1f}分钟")
