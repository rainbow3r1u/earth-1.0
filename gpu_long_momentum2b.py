#!/usr/bin/env python3
"""LONG 闸门变体对打 · 交易模拟 SANITY(winsor+成交量过滤)口径 (gpu_long_momentum 的 Q3 修正)
标签命中口径会误导(底部币先砸后拉也算命中但实盘已止损), 改为:
  每日各闸门内取LONG prob最高币, 开盘价入场, -5%止损/+10%止盈/48h收盘, 成本0.5%
  指标: 总pnl / 胜率 / Sharpe / 有信号天数
"""
import os, sys, json, glob, time
import numpy as np
from xgboost import XGBClassifier
sys.path.insert(0, '/home/linux/websocket_new')
import daily_predictor as dp

HOME = os.path.expanduser('~')
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
WF_DAYS = 180; TRAIN_WINDOW = 180
STOP_LOSS = 5.0; TAKE_PROFIT = 10.0; COST = 0.5
XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  min_child_weight=1, reg_lambda=10, reg_alpha=10,
                  subsample=0.8, colsample_bytree=0.6, device='cuda', verbosity=0)

def log(m): print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)
def prep(X):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    return X

log('加载K线...')
with open(KLINE_CACHE) as f:
    klines = json.load(f)['klines']
kmap = {s: {k['t']: i for i, k in enumerate(kls)} for s, kls in klines.items()}

files = sorted(glob.glob(f'{CACHE_DIR}/*.npz'))
sdays = [int(os.path.basename(f).replace('.npz', '')) for f in files]

def trade_long(sym, ts):
    """aligned: ts开盘入场, 后2日高低检查止损止盈"""
    kls = klines.get(sym); km = kmap.get(sym)
    if not kls or ts * 1000 not in km: return None
    ki = km[ts * 1000]
    if ki >= len(kls) - 2: return None
    ep = kls[ki]['o']
    if ep <= 0: return None
    for off in (1, 2):
        k = kls[ki + off]
        if k['l'] <= ep * (1 - STOP_LOSS / 100): return -STOP_LOSS - COST
        if k['h'] >= ep * (1 + TAKE_PROFIT / 100): return TAKE_PROFIT - COST
    pnl = (kls[ki + 2]['c'] / ep - 1) * 100
    return max(-STOP_LOSS, min(TAKE_PROFIT, pnl)) - COST

gates = {
    '无闸门':             lambda s, p: True,
    '现闸门 stk2+ pp>.7': lambda s, p: s >= 2 and p > 0.7,
    'G1 stk2+ pp>.6':     lambda s, p: s >= 2 and p > 0.6,
    'G2 stk3+ pp>.6':     lambda s, p: s >= 3 and p > 0.6,
    'G3 stk2+ pp>.8':     lambda s, p: s >= 2 and p > 0.8,
    'G4 stk1+ pp>.5':     lambda s, p: s >= 1 and p > 0.5,
    'G5 stk3+ pp>.7':     lambda s, p: s >= 3 and p > 0.7,
}
pnls = {g: [] for g in gates}

start = max(30, len(sdays) - WF_DAYS - 1)
eval_days = sdays[start:]
log(f'WF {len(eval_days)} 天, 每日1个LONG模型 × {len(gates)}个闸门...')
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
    Xg = prep(np.concatenate(Xg)); yg = np.concatenate(yg).astype(np.int32)
    pg = int(yg.sum())
    if pg < 5: continue
    _b = dp._fast_winsor_bounds(Xg)
    Xg = dp._apply_winsor(Xg, _b)
    m = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yg) - pg) / pg, random_state=42)
    m.fit(Xg, yg)
    dd = np.load(f'{CACHE_DIR}/{pred_ts}.npz')
    Xp = dp._apply_winsor(prep(dd['feats']), _b)
    syms = dd['syms']
    prob = m.predict_proba(Xp)[:, 1]
    stk_ = Xp[:, 7]; pp_ = Xp[:, 5]
    # 成交量过滤 (与harness一致)
    vol_ok = np.zeros(len(syms), dtype=bool)
    for _i, _s in enumerate(syms):
        _kls = klines.get(str(_s)); _km = kmap.get(str(_s))
        if not _kls or pred_ts * 1000 not in _km: continue
        _ki = _km[pred_ts * 1000]
        if _ki < 5: continue
        if np.mean([k['q'] for k in _kls[_ki-5:_ki]]) >= 500000:
            vol_ok[_i] = True
    for gname, gfn in gates.items():
        mask = np.array([gfn(s, p) for s, p in zip(stk_, pp_)]) & vol_ok
        if mask.sum() == 0: continue
        sub = np.where(mask)[0]
        ib = sub[int(np.argmax(prob[sub]))]
        pnl = trade_long(str(syms[ib]), pred_ts)
        if pnl is not None:
            pnls[gname].append(pnl)
    if (di + 1) % 30 == 0:
        log(f'  {di+1}/{len(eval_days)} ({time.time()-t0:.0f}s)')

print('\n' + '=' * 78)
print(f'LONG 闸门变体对打 · 交易模拟 SANITY(winsor+成交量过滤) (WF {len(eval_days)}天, -5%/+10%/48h, 成本0.5%)')
print(f'{"闸门":>20} {"天数":>5} {"总pnl":>9} {"均笔":>7} {"胜率":>6} {"Sharpe":>7}')
rows = []
for gname, p in pnls.items():
    if not p: continue
    p = np.array(p)
    sharpe = (p.mean() / (p.std() + 1e-6)) * np.sqrt(365) if len(p) > 1 else 0
    rows.append((sharpe, gname, len(p), p.sum(), p.mean(), (p > 0).mean()))
for sharpe, gname, n, tot, avg, wr in sorted(rows, reverse=True):
    print(f'{gname:>20} {n:>5} {tot:>+8.1f}% {avg:>+6.2f}% {wr*100:>5.0f}% {sharpe:>7.2f}')
print('=' * 78)
