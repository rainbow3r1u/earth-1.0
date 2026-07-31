#!/usr/bin/env python3
"""冻结时间测试 (GPU) — 冻结 vs 每日重训 双臂对照
冻结臂: 2025-06起180天训练一个模型, 之后60天不再重训, 量alpha半衰期
WF臂: 同样的60天里每天重训(与生产一致) — 差值=重训的价值/漂移成本
配置对齐生产完全体: aligned + volraw + fund + winsor0.1% + 无闸门 + SL5/TP10
"""
import os, sys, json, glob, time
import numpy as np
from datetime import datetime, timezone
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
sys.path.insert(0, f'{HOME}/websocket_new')
import daily_predictor as dp
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
TRAIN_WINDOW = 180; FORWARD_DAYS = 60
STOP_LOSS = 5.0; TAKE_PROFIT = 10.0; COST = 0.5; MIN_VOLUME = 500000
XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  min_child_weight=1, reg_lambda=10, reg_alpha=10,
                  subsample=0.8, colsample_bytree=0.6, device='cuda', verbosity=0)

def log(m): print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)

def qbounds(X, q=0.001):
    n, m = X.shape
    k1, k99 = max(0, int(n*q)), min(n-1, int(n*(1-q)))
    b = []; Xc = X.copy()
    for j in range(m):
        col = Xc[:, j]; col.partition([k1, k99])
        b.append((float(col[k1]), float(col[k99])))
    return b

def prep(X, b):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    lo = np.array([x[0] for x in b]); hi = np.array([x[1] for x in b])
    return np.clip(X, lo, hi)

def train_pair(train_ts_list):
    X_train, yL, yS = [], [], []
    for ts in train_ts_list:
        fp = f'{CACHE_DIR}/{ts}.npz'
        if not os.path.exists(fp): continue
        d = np.load(fp)
        X_train.append(d['feats']); lbl = d['labels']
        yL.append(lbl[:, 0]); yS.append(lbl[:, 1])
    X = np.nan_to_num(np.concatenate(X_train).astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    yL = np.concatenate(yL).astype(np.int32); yS = np.concatenate(yS).astype(np.int32)
    pL, pS = int(yL.sum()), int(yS.sum())
    if pL < 5 or pS < 5: return None
    b = qbounds(X)
    lo = np.array([x[0] for x in b]); hi = np.array([x[1] for x in b])
    X = np.clip(X, lo, hi)
    ml = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yL)-pL)/pL, random_state=42).fit(X, yL)
    ms = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yS)-pS)/pS, random_state=43).fit(X, yS)
    return ml, ms, b

log('加载K线...')
klines = json.load(open(KLINE_CACHE))['klines']
kmap = {s: {k['t']: i for i, k in enumerate(v)} for s, v in klines.items()}

def trade(sym, direction, ts):
    kls = klines.get(sym); km = kmap.get(sym)
    if not kls or ts*1000 not in km: return None
    ki = km[ts*1000]
    if ki >= len(kls)-2: return None
    ep = kls[ki]['o']
    if ep <= 0: return None
    # FIX 2026-07-31(GPT审计发现): 止盈止损检查必须含入场日当天(off=0), 原从ki+1起豁免当天止损
    for off in (0, 1, 2):
        k = kls[ki+off]
        if direction == 'long':
            if k['l'] <= ep*(1-STOP_LOSS/100): return -STOP_LOSS-COST
            if k['h'] >= ep*(1+TAKE_PROFIT/100): return TAKE_PROFIT-COST
        else:
            if k['h'] >= ep*(1+STOP_LOSS/100): return -STOP_LOSS-COST
            if k['l'] <= ep*(1-TAKE_PROFIT/100): return TAKE_PROFIT-COST
    c2 = kls[ki+2]['c']
    pnl = (c2/ep-1)*100 if direction == 'long' else (1-c2/ep)*100
    return max(-STOP_LOSS, min(TAKE_PROFIT, pnl)) - COST

def run_day(models, pred_ts):
    ml, ms, b = models
    fp = f'{CACHE_DIR}/{pred_ts}.npz'
    if not os.path.exists(fp): return None
    d = np.load(fp)
    X = prep(d['feats'], b)
    syms = d['syms']
    pl = ml.predict_proba(X)[:, 1]; ps = ms.predict_proba(X)[:, 1]
    bl = bs = None
    for idx in range(len(syms)):
        sym = str(syms[idx]); kls = klines.get(sym); km = kmap.get(sym)
        if not kls or pred_ts*1000 not in km: continue
        ki = km[pred_ts*1000]
        if ki < 5: continue
        if np.mean([k['q'] for k in kls[ki-5:ki]]) < MIN_VOLUME: continue
        if bl is None or pl[idx] > bl[1]: bl = (sym, pl[idx])
        if bs is None or ps[idx] > bs[1]: bs = (sym, ps[idx])
    if bl is None and bs is None: return None
    lp = bl[1]*100 if bl else 0; sp = bs[1]*100 if bs else 0
    if max(lp, sp) < 60.0: return None
    direction = 'long' if (bl and (not bs or lp >= sp)) else 'short'
    sym = bl[0] if direction == 'long' else bs[0]
    pnl = trade(sym, direction, pred_ts)
    if pnl is None: return None
    return {'day': datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%m-%d'),
            'direction': direction, 'symbol': sym, 'pnl': pnl}

sdays = sorted(int(os.path.basename(f).replace('.npz', '')) for f in glob.glob(f'{CACHE_DIR}/*.npz'))
# 冻结点: 前向起点 = 2025-11-29 (训练窗2025-06-02起180天, aligned截止)
start_date = int(datetime(2025, 11, 29, tzinfo=timezone.utc).timestamp())
d0 = min(range(len(sdays)), key=lambda i: abs(sdays[i]-start_date))
fwd_days = sdays[d0:d0+FORWARD_DAYS]
log(f'冻结点训练窗: {datetime.fromtimestamp(sdays[d0-180],tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(sdays[d0-3],tz=timezone.utc):%Y-%m-%d}')
log(f'前向区间: {datetime.fromtimestamp(fwd_days[0],tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(fwd_days[-1],tz=timezone.utc):%Y-%m-%d} ({len(fwd_days)}天)')

log('=== 冻结臂: 训练一次... ===')
frozen = train_pair(sdays[max(0, d0-TRAIN_WINDOW):d0-2])
frozen_trades = [r for r in (run_day(frozen, ts) for ts in fwd_days) if r]
log(f'冻结臂完成: {len(frozen_trades)}笔')

log('=== WF臂: 每日重训... ===')
wf_trades = []
for i, ts in enumerate(fwd_days):
    di = sdays.index(ts)
    m = train_pair(sdays[max(0, di-TRAIN_WINDOW):di-2])
    if m is None: continue
    r = run_day(m, ts)
    if r: wf_trades.append(r)
    if (i+1) % 15 == 0: log(f'  WF {i+1}/{len(fwd_days)}')
log(f'WF臂完成: {len(wf_trades)}笔')

def stats(trades, name):
    print(f'\n--- {name} ---')
    segs = [(0, 15), (15, 30), (30, 45), (45, 60)]
    print(f'{"时段":>12} {"笔数":>4} {"胜率":>6} {"累计":>9} {"Sharpe":>7}')
    out = []
    for a, b_ in segs:
        p = np.array([t['pnl'] for t in trades[a:b_]])
        if len(p) < 2: continue
        sh = p.mean()/(p.std()+1e-6)*np.sqrt(365)
        print(f'D{a+1:>2}-D{b_:<3} {len(p):>5} {(p>0).mean()*100:>5.0f}% {p.sum():>+8.1f}% {sh:>7.2f}')
        out.append({'seg': f'D{a+1}-D{b_}', 'n': len(p), 'win': float((p>0).mean()), 'cum': float(p.sum()), 'sharpe': float(sh)})
    p = np.array([t['pnl'] for t in trades])
    sh = p.mean()/(p.std()+1e-6)*np.sqrt(365)
    print(f'{"全程":>12} {len(p):>4} {(p>0).mean()*100:>5.0f}% {p.sum():>+8.1f}% {sh:>7.2f}')
    return out

print('\n' + '='*60)
print(f'冻结时间测试: 冻结于{datetime.fromtimestamp(sdays[d0-3],tz=timezone.utc):%Y-%m-%d}, 前向{len(fwd_days)}天')
r1 = stats(frozen_trades, '冻结臂(不重训)')
r2 = stats(wf_trades, 'WF臂(每日重训)')
print('='*60)
json.dump({'frozen': frozen_trades, 'wf': wf_trades},
          open(f'{HOME}/websocket_new/data/frozen_test.json', 'w'), indent=1)
