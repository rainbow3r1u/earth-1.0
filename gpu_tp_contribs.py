#!/usr/bin/env python3
"""止盈单特征贡献分析 (GPU) — 最高Sharpe配置(winq001无闸门)的LONG止盈单,
逐笔计算 XGBoost pred_contribs, 找模型选中它们时到底在看什么
聚合: TP单 vs SL单的贡献签名差异, vol_raw(944) 的统治度
"""
import os, sys, json, glob, time
import numpy as np
import xgboost as xgb
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
sys.path.insert(0, f'{HOME}/websocket_new')
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
WF_DAYS = 180; TRAIN_WINDOW = 180
STOP_LOSS = 5.0; TAKE_PROFIT = 10.0; COST = 0.5
XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  min_child_weight=1, reg_lambda=10, reg_alpha=10,
                  subsample=0.8, colsample_bytree=0.6, device='cuda', verbosity=0)

try:
    from auto_dual_trade import _FEATURE_NAMES as FN, _FEATURE_NAMES_CN as FNC
except Exception:
    FN, FNC = None, {}

def fname(i):
    if FN and i < len(FN):
        en = FN[i]
        return f'{FNC.get(en, en)}({en})'
    return f'feat_{i}'

def log(m): print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)

def qbounds(X, q=0.001):
    n, m = X.shape
    k1, k99 = max(0, int(n*q)), min(n-1, int(n*(1-q)))
    b = []
    Xc = X.copy()
    for j in range(m):
        col = Xc[:, j]; col.partition([k1, k99])
        b.append((float(col[k1]), float(col[k99])))
    return b

def prep(X, bounds):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    lo = np.array([b[0] for b in bounds]); hi = np.array([b[1] for b in bounds])
    return np.clip(X, lo, hi)

log('加载K线...')
with open(KLINE_CACHE) as f:
    klines = json.load(f)['klines']
kmap = {s: {k['t']: i for i, k in enumerate(kls)} for s, kls in klines.items()}

def trade_long(sym, ts):
    kls = klines.get(sym); km = kmap.get(sym)
    if not kls or ts * 1000 not in km: return None
    ki = km[ts * 1000]
    if ki >= len(kls) - 2: return None
    ep = kls[ki]['o']
    if ep <= 0: return None
    for off in (1, 2):
        k = kls[ki + off]
        if k['l'] <= ep * (1 - STOP_LOSS/100): return 'stop'
        if k['h'] >= ep * (1 + TAKE_PROFIT/100): return 'take'
    return 'hold'

files = sorted(glob.glob(f'{CACHE_DIR}/*.npz'))
sdays = [int(os.path.basename(f).replace('.npz', '')) for f in files]
start = max(30, len(sdays) - WF_DAYS - 1)
eval_days = sdays[start:]
log(f'WF {len(eval_days)} 天, 每日1个LONG模型 + 逐笔贡献...')

tp_contribs, sl_contribs = [], []
tp_vol_in_top5 = 0; tp_n = 0
t0 = time.time()
for di, pred_ts in enumerate(eval_days):
    d_idx = sdays.index(pred_ts)
    Xg, yg = [], []
    for ts in sdays[max(0, d_idx - TRAIN_WINDOW):d_idx - 2]:
        fp = f'{CACHE_DIR}/{ts}.npz'
        if not os.path.exists(fp): continue
        dd = np.load(fp)
        Xg.append(dd['feats']); yg.append(dd['labels'][:, 0])
    if not Xg: continue
    Xg = np.nan_to_num(np.concatenate(Xg).astype(np.float32), nan=0.0)
    Xg[:, 100:932] = 0.0; Xg[:, 72:91] = 0.0
    yg = np.concatenate(yg).astype(np.int32)
    pg = int(yg.sum())
    if pg < 5: continue
    b = qbounds(Xg)
    lo = np.array([x[0] for x in b]); hi = np.array([x[1] for x in b])
    Xg = np.clip(Xg, lo, hi)
    m = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yg)-pg)/pg, random_state=42)
    m.fit(Xg, yg)
    dd = np.load(f'{CACHE_DIR}/{pred_ts}.npz')
    Xp_raw = dd['feats']; syms = dd['syms']
    Xp = prep(Xp_raw, b)
    prob = m.predict_proba(Xp)[:, 1]
    # 成交量过滤
    best = None
    for idx in range(len(syms)):
        sym = str(syms[idx])
        kls = klines.get(sym); km = kmap.get(sym)
        if not kls or pred_ts * 1000 not in km: continue
        ki = km[pred_ts * 1000]
        if ki < 5: continue
        if np.mean([k['q'] for k in kls[ki-5:ki]]) < 500000: continue
        if best is None or prob[idx] > best[1]:
            best = (idx, prob[idx])
    if best is None: continue
    idx = best[0]
    sym = str(syms[idx])
    outcome = trade_long(sym, pred_ts)
    if outcome not in ('take', 'stop'): continue
    row = Xp[idx:idx+1]
    c = m.get_booster().predict(xgb.DMatrix(row), pred_contribs=True)[0][:-1]
    if outcome == 'take':
        tp_contribs.append(c)
        top5 = set(np.argsort(-np.abs(c))[:5])
        tp_n += 1
        if 944 in top5: tp_vol_in_top5 += 1
    else:
        sl_contribs.append(c)
    if (di + 1) % 30 == 0:
        log(f'  {di+1}/{len(eval_days)} TP={tp_n} SL={len(sl_contribs)} ({time.time()-t0:.0f}s)')

def report(name, cs):
    cs = np.array(cs)
    mc = cs.mean(axis=0)
    order = np.argsort(-np.abs(mc))[:15]
    print(f'\n== {name} (n={len(cs)}) 平均贡献Top15 ==')
    for i in order:
        print(f'  {fname(i):<38} {mc[i]:+.4f}')

print('\n' + '=' * 70)
report('止盈单 TP', tp_contribs)
report('止损单 SL', sl_contribs)
print(f'\nvol_raw(944) 进入当日pick贡献Top5的比例: {tp_vol_in_top5}/{tp_n} = {tp_vol_in_top5/max(1,tp_n)*100:.0f}%')
print('=' * 70)
