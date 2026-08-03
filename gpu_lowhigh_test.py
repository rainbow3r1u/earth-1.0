#!/usr/bin/env python3
"""低点抬高特征 walk-forward 实验(2026-08-03): 基线(946) vs 增强(+3特征=949)
基于 _replay 缓存(540币, MIN_KLINES=35), 与回测同口径: 训练窗<=d-3, 180天, aligned, SOUP, 0.1%winsor
3特征 = 连续抬高天数 / 5日差分强度 / 10日相对位置(基于残差列16序列, 无前视)
"""
import os, sys, glob, json
import numpy as np
from collections import defaultdict
sys.path.insert(0, '/home/linux/websocket_new')
import daily_predictor as dp
from xgboost import XGBClassifier
from datetime import datetime, timezone

CACHE = '/home/linux/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund_replay'
files = sorted(glob.glob(CACHE + '/*.npz'))
ts_list = [int(os.path.basename(f).replace('.npz','')) for f in files]
print('缓存天数:', len(ts_list), flush=True)

# 预加载全部缓存到内存 (2084天 × ~500币 × 946 × 4B ≈ 4GB)
ALL = {}
for f in files:
    ts = int(os.path.basename(f).replace('.npz',''))
    dd = np.load(f)
    ALL[ts] = (dd['feats'].astype(np.float32), dd['labels'], [str(s) for s in dd['syms']])
print('预加载完成', flush=True)
# 残差序列 (按币)
res_series = defaultdict(dict)
for f in files:
    ts = int(os.path.basename(f).replace('.npz',''))
    feats, _, syms = ALL[ts]
    for i, s in enumerate(syms):
        res_series[s][ts] = float(feats[i, 16])

def lowhigh_feats(sym, ts):
    """样本日ts的3特征: 用ts-1及以前残差"""
    d = res_series.get(sym)
    if not d: return None
    keys = sorted(k for k in d if k <= ts - 86400)
    if len(keys) < 11: return None
    vals = [d[k] for k in keys]
    f1 = 0
    for i in range(len(vals)-1, 0, -1):
        if vals[i] > vals[i-1]: f1 += 1
        else: break
    res_5 = vals[-6]
    f2 = (vals[-1] - res_5) / abs(res_5) if abs(res_5) > 1e-9 else 0.0
    w = vals[-10:]
    mn, mx = min(w), max(w)
    f3 = (vals[-1] - mn) / (mx - mn + 1e-6)
    return [f1, f2, f3]

ENH = os.environ.get('ENH', '0') == '1'   # 1=增强(949), 0=基线(946)
DAYS = int(os.environ.get('DAYS', '60')) # 默认60d先看趋势
TRAIN_DAYS = 180

def train(X, y, rs):
    pos = int(y.sum())
    m = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                      min_child_weight=1, reg_lambda=10, reg_alpha=10,
                      subsample=0.8, colsample_bytree=0.6,
                      scale_pos_weight=(len(y)-pos)/pos if pos > 0 else 1,
                      random_state=rs, eval_metric='logloss', verbosity=0, device='cuda')
    m.fit(X, y)
    return m

KLINE_RAW = json.load(open('/home/linux/backtester/data_cache/notusdt_1d_full.json'))['klines']
KLINE_BY_TS = {}
for _s, _kls in KLINE_RAW.items():
    KLINE_BY_TS[_s] = {int(_k['t'])//1000: _k for _k in _kls}
hist_l, hist_s = [], []
n_trades, wins, cum = 0, 0, 0.0
for d_i, d in enumerate(ts_list[-DAYS:]):
    train_ts = [t for t in ts_list if t <= d - 3*86400][-TRAIN_DAYS:]
    X, yl, ys = [], [], []
    syms_cache = {}
    for t in train_ts:
        f_, lbl, syms = ALL[t]
        if ENH:
            aug = []
            for i, s in enumerate(syms):
                lf = lowhigh_feats(s, t)
                aug.append(lf if lf else [0.0, 0.0, 0.0])
            f_ = np.hstack([f_, np.array(aug, dtype=np.float32)])
        X.append(f_)
        yl.append(lbl[:,0].astype(np.int32))
        ys.append(lbl[:,1].astype(np.int32))
    X = np.concatenate(X); yl = np.concatenate(yl); ys = np.concatenate(ys)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    bounds = dp._fast_winsor_bounds(X)
    Xw = dp._apply_winsor(X, bounds)
    ml = train(Xw, yl, 42); ms = train(Xw, ys, 43)
    hist_l.append(ml); hist_s.append(ms)
    if len(hist_l) > 3: hist_l.pop(0); hist_s.pop(0)
    # 预测
    Xp, _, syms = ALL[d]
    if ENH:
        aug = []
        for i, s in enumerate(syms):
            lf = lowhigh_feats(s, d)
            aug.append(lf if lf else [0.0, 0.0, 0.0])
        Xp = np.hstack([Xp, np.array(aug, dtype=np.float32)])
    Xp[:, 100:932] = 0.0; Xp[:, 72:91] = 0.0
    Xpw = dp._apply_winsor(Xp, bounds)
    pl = np.mean([m.predict_proba(Xpw)[:,1] for m in hist_l], axis=0)
    ps = np.mean([m.predict_proba(Xpw)[:,1] for m in hist_s], axis=0)
    # 多空二选一: prob 高者
    p_best = np.maximum(pl, ps)
    i = int(np.argmax(p_best))
    side = 'L' if pl[i] >= ps[i] else 'S'
    sym = syms[i]; prob = p_best[i]
    # 结算: 用 K线 (入场 open[d], 48h 窗口, 止损±5% 止盈±10%)
    if sym not in res_series or d not in res_series.get(sym, {}):
        continue
    # 结算用本地K线(GPU无法访问币安API)
    try:
        kl = KLINE_BY_TS.get(sym, {})
        k0, k1, k2 = kl.get(d), kl.get(d+86400), kl.get(d+2*86400)
        if not k0 or not k1:
            print('CONTINUE: K线缺 sym=%s d=%s k0=%s k1=%s' % (sym, d, k0 is not None, k1 is not None), flush=True)
            continue
        entry = k0['o']
        sl = entry * (1.05 if side == 'S' else 0.95)
        tp = entry * (0.90 if side == 'S' else 1.10)
        hit = None
        for k in (k0, k1, k2):
            if not k: break
            h, l = k['h'], k['l']
            if side == 'S':
                if h >= sl: hit = -5.0; break
                if l <= tp: hit = 10.0; break
            else:
                if l <= sl: hit = -5.0; break
                if h >= tp: hit = 10.0; break
        if hit is None:
            last = k2['c'] if k2 else k1['c']
            hit = (entry - last)/entry*100 if side == 'S' else (last - entry)/entry*100
        n_trades += 1
        if hit > 0: wins += 1
        cum += hit
        print('%s %s %s %.1f%% → %+.1f%%' % (datetime.fromtimestamp(d, tz=timezone.utc).date(), side, sym, prob*100, hit), flush=True)
    except Exception as e:
        print('结算失败', sym, e, flush=True)
    if d_i % 10 == 9:
        print('  [%d/%d] cum=%+.1f%% win=%d/%d' % (d_i+1, DAYS, cum, wins, n_trades), flush=True)

print('\n===== 结果(%s, %dd) =====' % ('增强+3特征' if ENH else '基线', DAYS))
print('交易数: %d | 胜率: %.1f%% | 累计: %+.1f%%' % (n_trades, wins/n_trades*100 if n_trades else 0, cum), flush=True)
