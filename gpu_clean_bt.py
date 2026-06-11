#!/usr/bin/env python3
"""干净回测 365天 GPU版 — 真止损+真止盈, 全529币种, ML only"""
import os, sys, json, time, numpy as np
from datetime import datetime, timezone
from collections import defaultdict
from xgboost import XGBClassifier

sys.path.insert(0, '/root/reasonix-projects/websocket_new')
sys.stdout.reconfigure(line_buffering=True)
import daily_predictor as dp

# ── GPU XGBoost ──
class _G(XGBClassifier):
    def __init__(self, **kw):
        kw.setdefault('device', 'cuda')
        kw.setdefault('tree_method', 'hist')
        super().__init__(**kw)
import xgboost; xgboost.XGBClassifier = _G
dp.XGBClassifier = _G
from experiments.knn_market_structure import precompute_knn_features, feature_names as knn_feature_names

# ── 离线补丁 ──
import requests
_real_get = requests.get
def _offline(url, *a, **kw):
    if 'binance' in url: raise ConnectionError("offline")
    return _real_get(url, *a, **kw)
requests.get = _offline

def _cached_oi(syms, limit=500):
    cp = "/root/reasonix-projects/backtester/data_cache/oi_daily.json"
    out = {}
    if os.path.exists(cp):
        with open(cp) as f:
            lc = json.load(f)
        for s in syms:
            if s in lc and lc[s]:
                cc = lc[s]; sts = sorted(cc.keys(), reverse=True)[:limit]
                out[s] = {int(t): float(cc[t]) for t in sts}
    return out
dp.fetch_oi = _cached_oi

# ── 参数 ──
STOP_LOSS = 10.0
TAKE_PROFIT = 10.0
PROB_THRESHOLD = 60.0
TRADE_COST = 0.2
TRAIN_DAYS = 500
MIN_HISTORY = 60
MIN_VOLUME = 500000
RECENT_DAYS = 365

def _check_exit(kls_daily, entry_i, entry_price, direction):
    """真止损止盈: 2天内日K最高/低价触发"""
    if direction == 'long':
        stop_price = entry_price * (1 - STOP_LOSS/100)
        take_price = entry_price * (1 + TAKE_PROFIT/100)
        for offset in [1, 2]:
            idx = entry_i + offset
            if idx >= len(kls_daily): continue
            k = kls_daily[idx]
            lo = k['l'] if isinstance(k, dict) else float(k[3])
            hi = k['h'] if isinstance(k, dict) else float(k[2])
            if lo <= stop_price: return -STOP_LOSS, offset, 'stop'
            if hi >= take_price: return +TAKE_PROFIT, offset, 'take'
        idx = entry_i + 2
        if idx < len(kls_daily):
            k = kls_daily[idx]
            c = k['c'] if isinstance(k, dict) else float(k[4])
            return (c/entry_price-1)*100, 2, 'hold'
        return 0, 0, 'expire'
    else:
        stop_price = entry_price * (1 + STOP_LOSS/100)
        take_price = entry_price * (1 - TAKE_PROFIT/100)
        for offset in [1, 2]:
            idx = entry_i + offset
            if idx >= len(kls_daily): continue
            k = kls_daily[idx]
            hi = k['h'] if isinstance(k, dict) else float(k[2])
            lo = k['l'] if isinstance(k, dict) else float(k[3])
            if hi >= stop_price: return -STOP_LOSS, offset, 'stop'
            if lo <= take_price: return +TAKE_PROFIT, offset, 'take'
        idx = entry_i + 2
        if idx < len(kls_daily):
            k = kls_daily[idx]
            c = k['c'] if isinstance(k, dict) else float(k[4])
            return (entry_price-c)/entry_price*100, 2, 'hold'
        return 0, 0, 'expire'

print(f"[{time.strftime('%H:%M:%S')}] 干净回测 365天 启动 (真止损±10% + 真止盈+10%)")
t0 = time.time()

# ── 加载数据 ──
klines = dp.fetch_klines()
fut_syms = list(klines.keys())
print(f"K线: {len(klines)}币种")
oi_data = dp.fetch_oi(fut_syms)
print(f"OI: {len(oi_data)}币种")

sector_map = dp._load_sector_map(); dp._sector_map_cache = sector_map
try:
    with open('/home/myuser/defillama_data/protocol_map.json') as pf:
        dp._proto_map_local = {k: v[0] for k, v in json.load(pf).items()}
except: pass
sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}
dp._etf_features = dp._load_etf_features(); dp._chain_features = dp._load_chain_features()
dp._sent_features = dp._load_sent_features(); dp._fg_features = dp._load_fear_greed()
dp._st_features = dp._load_stablecoin_netflow(); dp._cb_features = dp._load_coinbase_premium()
dp._cbg_features = dp._load_cb_gap_features(); dp._bd_features = dp._load_btc_mcap()
dp._kg_features = dp._load_korea_premium(); dp._hr_features = dp._load_hashrate_features()
dp._liq_features = dp._load_liquidation_features(); dp._tvl_features = dp._load_chain_tvl()
dp._ma_features = dp._load_macro_assets(); dp._ab_features = dp._load_btc_dominance_proxy()

bkl = klines.get('BTCUSDT', [])
bc = [k['c'] if isinstance(k, dict) else float(k[4]) for k in bkl]
btc_rets = dp._compute_returns(bc) if len(bc) > 1 else []

ats = set()
for kls in klines.values():
    if len(kls) < MIN_HISTORY: continue
    for k in kls:
        ats.add(k.get('t',0)//1000 if isinstance(k, dict) else int(k[0])//1000)
dp._precompute_kronos_features(list(ats))
# KNN市场结构特征预计算
knn_cache = {}
for sym, kl in klines.items():
    if len(kl) >= 200:
        knn_cache[sym] = precompute_knn_features(kl)
print(f"[{time.strftime('%H:%M:%S')}] KNN预计算: {len(knn_cache)}币种")

# ── 构建样本 ──
print(f"[{time.strftime('%H:%M:%S')}] 构建样本...")
all_samples = []
for sym, kls in klines.items():
    if len(kls) < MIN_HISTORY: continue
    oi_map = oi_data.get(sym, {})
    cl = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
    op = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
    hi = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
    lo = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
    vl = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
    ts_list = [k.get('t',0)//1000 if isinstance(k, dict) else int(k[0])//1000 for k in kls]
    cr = dp._compute_returns(cl); n = len(kls)
    for i in range(25, n-2):
        try:
            j = i-1
            r1 = (cl[j]-cl[j-1])/cl[j-1] if cl[j-1]>0 else 0
            r3 = (cl[j]-cl[max(0,j-3)])/cl[max(0,j-3)] if cl[max(0,j-3)]>0 else 0
            r5 = (cl[j]-cl[max(0,j-5)])/cl[max(0,j-5)] if cl[max(0,j-5)]>0 else 0
            r20 = [(cl[k]-cl[k-1])/cl[k-1] if cl[k-1]>0 else 0 for k in range(j-18,j+1)] if j>=20 else [0]
            v20 = float(np.std(r20)) if j>=20 else 0.02; vf = max(v20, 0.002)
            r1n = round(r1/vf,4); r3n = round(r3/(vf*1.732),4); r5n = round(r5/(vf*2.236),4)
            dr = [(cl[k]-cl[k-1])/cl[k-1] if cl[k-1]>0 else 0 for k in range(j-3,j+1)] if j>=5 else [0]
            vol = np.std(dr) if len(dr)>1 else 0
            vr = vl[j]/np.mean(vl[max(0,j-5):j]) if j>=5 and np.mean(vl[max(0,j-5):j])>0 else 1
            c20 = cl[j-19:j+1] if j>=20 else [cl[j]]
            pp = (cl[j]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
            amp = (hi[j]-lo[j])/op[j] if op[j]>0 else 0
            st = 0
            for k in range(j, max(0,j-7)-1, -1):
                if cl[k]>op[k]: st+=1
                else: break
            ds = 1 if (cl[j]>cl[j-3] and vl[j]<vl[j-3]*0.7) else 0
            ts = ts_list[i]
            on = oi_map.get(ts_list[j], 0); oip = oi_map.get(ts_list[j-1], 0)
            oc = (on-oip)/oip if oip>0 else 0
            if sym=='BTCUSDT': b,a,r2,res = 1.0,0.0,1.0,0.0
            else: b,a,r2,res = dp._regression_features(btc_rets, cr, j)
            sf = dp._get_sector_features(sym, ts-86400, sector_map, sector_heats_all)
            mf = dp._get_macro_features(ts); mf = dp._apply_chain_tvl(mf, sym, ts)
            r7 = dp._compute_rsi(cl, 7, j); r14 = dp._compute_rsi(cl, 14, j); r30 = dp._compute_rsi(cl, 30, j)
            rs = dp._compute_rsi_series(cl, 14); rd = dp._compute_rsi_divergence(cl, rs, j, window=20)
            vc = dp._compute_vol_clustering(cl, j)
            feat = [r1n,r3n,r5n,vol,vr,pp,amp,st,ds,oc]+vc+[b,a,r2,res,r7,r14,r30]+rd+sf+mf
            # KNN市场结构特征 (15维)
            knn_fd = knn_cache.get(sym, {}).get(ts, {})
            feat += [float(knn_fd.get(k, 0)) for k in knn_feature_names()]
            nr = (cl[i+1]-cl[j])/cl[j] if cl[j]>0 and i+1<n else 0
            if abs(nr)>5.0: continue
            all_samples.append((ts, sym, feat, 1 if nr>0.05 else 0, 1 if nr<-0.05 else 0, nr*100))
        except: continue

by_day = defaultdict(list)
for ts, sym, feat, ll, ls, ret in all_samples: by_day[ts].append((sym, feat, ll, ls, ret))
sorted_days = sorted(by_day.keys())
recent_days = sorted_days[-RECENT_DAYS:]
print(f"[{time.strftime('%H:%M:%S')}] 样本: {len(all_samples)}条, 回测: {len(recent_days)}天")

# ── Walk-forward ──
trades = []
for i, pred_ts in enumerate(recent_days):
    if i % 30 == 0: print(f"  {i}/{len(recent_days)} trades={len(trades)}", flush=True)

    train_ts_list = [ts for ts in sorted_days if ts < pred_ts][-TRAIN_DAYS:]
    if len(train_ts_list) < 10: continue

    X_train, y_long, y_short = [], [], []
    for ts in train_ts_list:
        if ts + 2*86400 > pred_ts: continue
        for sym, feat, ll, ls, ret in by_day[ts]:
            X_train.append(feat); y_long.append(ll); y_short.append(ls)
    X_train = np.array(X_train)
    bounds = dp._fast_winsor_bounds(X_train)
    X_train = dp._apply_winsor(X_train, bounds)
    pl, ps = sum(y_long), sum(y_short)
    if pl < 5 or ps < 5: continue

    ml = dp.XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                          scale_pos_weight=(len(y_long)-pl)/pl, random_state=42, verbosity=0)
    ms = dp.XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                          scale_pos_weight=(len(y_short)-ps)/ps, random_state=43, verbosity=0)
    ml.fit(X_train, y_long); ms.fit(X_train, y_short)

    pred_samples = by_day[pred_ts]
    X_pred = np.array([s[1] for s in pred_samples])
    X_pred = dp._apply_winsor(X_pred, bounds)
    plo = ml.predict_proba(X_pred)[:,1]; psh = ms.predict_proba(X_pred)[:,1]

    best_long = None; best_short = None
    for idx, ((sym, feat, ll, ls, ret), plv, psv) in enumerate(zip(pred_samples, plo, psh)):
        kd = klines.get(sym, [])
        if len(kd) < MIN_HISTORY: continue
        ki = dp._find_kline_index(kd, pred_ts)
        if ki is None or ki < 5: continue
        v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kd[ki-5:ki]]
        if np.mean(v) < MIN_VOLUME: continue
        if best_long is None or plv > best_long[1]: best_long = (sym, plv, ret)
        if best_short is None or psv > best_short[1]: best_short = (sym, psv, ret)

    lp = best_long[1]*100 if best_long else 0
    sp = best_short[1]*100 if best_short else 0
    mp = max(lp, sp)
    if mp < PROB_THRESHOLD: continue

    if best_long and (not best_short or lp >= sp): direction, sym, prob = 'long', best_long[0], lp
    else: direction, sym, prob = 'short', best_short[0], sp

    kd = klines.get(sym, [])
    ki = dp._find_kline_index(kd, pred_ts)
    if ki is None or ki < 1: continue
    entry_price = kd[ki]['o'] if isinstance(kd[ki], dict) else float(kd[ki][1])
    pnl_raw, exit_day, reason = _check_exit(kd, ki, entry_price, direction)
    pnl = pnl_raw - TRADE_COST
    day_str = datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    trades.append({'day': day_str, 'direction': direction, 'symbol': sym,
                   'prob': float(round(prob,1)), 'pnl': float(round(pnl,2)),
                   'stopped': reason=='stop', 'exit_reason': reason})

# ── 统计 ──
pnls = [t['pnl'] for t in trades]
total = sum(pnls)
wins = [p for p in pnls if p>0]
dd = 0; cum = 0; pk = 0; cap = 100; eq = 100; peak_eq = 100; dd_cap = 0
for p in pnls:
    cum+=p; pk=max(pk,cum); dd=max(dd,pk-cum)
    eq *= (1 + p/100); peak_eq = max(peak_eq, eq)
    dd_cap = max(dd_cap, (peak_eq - eq)/peak_eq * 100)
sh = np.mean(pnls)/np.std(pnls)*np.sqrt(365) if len(pnls)>5 else 0
stops = sum(1 for t in trades if t.get('exit_reason')=='stop')
takes = sum(1 for t in trades if t.get('exit_reason')=='take')
longs = sum(1 for t in trades if t['direction']=='long')
shorts = sum(1 for t in trades if t['direction']=='short')
elapsed = (time.time()-t0)/60

print(f"\n{'='*60}")
print(f"  干净回测 365天 (真止损±10% + 真止盈+10%)")
print(f"  总收益: {total:+.1f}%  Sharpe: {sh:.2f}  胜率: {len(wins)/len(trades)*100:.1f}%")
print(f"  总资金最大回撤: {dd_cap:.1f}%  交易: {len(trades)}笔")
print(f"  止损: {stops}次  止盈: {takes}次  做多: {longs}  做空: {shorts}")
print(f"  耗时: {elapsed:.1f}分钟")
print(f"{'='*60}")

out = {'summary': {'total_pnl': float(round(total,2)), 'sharpe': float(round(sh,2)),
        'win_rate': float(round(len(wins)/len(trades)*100,1)), 'max_dd_cum': float(round(dd,2)), 'max_dd_cap': float(round(dd_cap,2)),
        'stop_count': int(stops), 'take_count': int(takes), 'total_trades': int(len(trades)),
        'long_days': int(longs), 'short_days': int(shorts)}, 'trades': trades}
dst = '/root/reasonix-projects/websocket_new/data/clean_365d.json'
with open(dst, 'w') as f: json.dump(out, f)
print(f"Saved → {dst}")
