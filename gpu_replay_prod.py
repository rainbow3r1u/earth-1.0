#!/usr/bin/env python3
"""生产逐日重放(2026-08-03): 用 GPU 缓存(8/3视角, 修复版winsor)复刻生产每天的
训练+SOUP+预测, 与生产 pred_*.json 存档逐日对比 TOP1。
对齐点: 训练窗<today_ts、180天、置零Kronos832+liq19、0.1%/99.9% winsor(修复版)、
SOUP=最近3个模型概率平均、XGB 生产参数。"""
import os, sys, glob, json
import numpy as np
sys.path.insert(0, '/home/linux/websocket_new')
import daily_predictor as dp
from xgboost import XGBClassifier
from datetime import datetime, timezone

CACHE = '/home/linux/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund_replay'
files = sorted(glob.glob(CACHE + '/*.npz'))
ts_list = [int(os.path.basename(f).replace('.npz','')) for f in files]
print('缓存天数:', len(ts_list), '| 范围:', datetime.fromtimestamp(ts_list[0], tz=timezone.utc).date(),
      '~', datetime.fromtimestamp(ts_list[-1], tz=timezone.utc).date(), flush=True)

def day_ts(s):
    return int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())

replay_days = ['2026-07-28','2026-07-29','2026-07-30','2026-07-31','2026-08-01','2026-08-02','2026-08-03']
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

hist_l, hist_s = [], []  # SOUP 历史模型

for ds in replay_days:
    d = day_ts(ds)
    if d not in ts_list:
        print(f'== {ds}: 缓存无该样本日', flush=True); continue
    # 训练窗 = 样本日 <= d-3天 (生产当时可实现的标签, 无前视; 与回测 d-3 截止一致)
    train_ts = [t for t in ts_list if t <= d - 3*86400][-TRAIN_DAYS:]
    X, yl, ys = [], [], []
    for t in train_ts:
        dd = np.load(CACHE + f'/{t}.npz')
        X.append(dd['feats'].astype(np.float32))
        yl.append(dd['labels'][:,0].astype(np.int32))
        ys.append(dd['labels'][:,1].astype(np.int32))
    X = np.concatenate(X); yl = np.concatenate(yl); ys = np.concatenate(ys)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    bounds = dp._fast_winsor_bounds(X)
    Xw = dp._apply_winsor(X, bounds)
    ml = train(Xw, yl, 42)
    ms = train(Xw, ys, 43)
    hist_l.append(ml); hist_s.append(ms)
    if len(hist_l) > 3: hist_l.pop(0); hist_s.pop(0)
    # 预测: 样本日 d (与生产 pred_samples = by_day[today_ts] 一致)
    pred = np.load(CACHE + f'/{d}.npz')
    Xp = pred['feats'].astype(np.float32)
    syms = [str(s) for s in pred['syms']]
    Xp[:, 100:932] = 0.0; Xp[:, 72:91] = 0.0
    Xpw = dp._apply_winsor(Xp, bounds)
    pl = np.mean([m.predict_proba(Xpw)[:,1] for m in hist_l], axis=0)   # SOUP
    ps = np.mean([m.predict_proba(Xpw)[:,1] for m in hist_s], axis=0)
    il, is_ = int(np.argmax(pl)), int(np.argmax(ps))
    pf = f'/home/linux/websocket_new/data/pred_{ds}.json'
    prod = json.load(open(pf)) if os.path.exists(pf) else None
    if not prod:
        print(f'== {ds}: 无生产存档(重放: L={syms[il]} {pl[il]*100:.1f}% S={syms[is_]} {ps[is_]*100:.1f}%)', flush=True)
        continue
    bpl, bps = prod.get('best_long'), prod.get('best_short')
    def rank_of(sym, probs, syms_):
        if sym not in syms_: return None
        return sorted(range(len(probs)), key=lambda i: -probs[i]).index(syms_.index(sym))
    rl = rank_of(bpl['symbol'], pl, syms) if bpl else None
    rs_ = rank_of(bps['symbol'], ps, syms) if bps else None
    m_l = '✓' if bpl and bpl['symbol'] == syms[il] else '✗'
    m_s = '✓' if bps and bps['symbol'] == syms[is_] else '✗'
    print(f'== {ds} ==', flush=True)
    print(f'  LONG : 重放TOP1={syms[il]} {pl[il]*100:.1f}% | 生产={bpl["symbol"] if bpl else None} {bpl["prob"] if bpl else ""}% {m_l} (生产币重放排名={rl})', flush=True)
    print(f'  SHORT: 重放TOP1={syms[is_]} {ps[is_]*100:.1f}% | 生产={bps["symbol"] if bps else None} {bps["prob"] if bps else ""}% {m_s} (生产币重放排名={rs_})', flush=True)
print('完成', flush=True)
