#!/usr/bin/env python3
"""续涨专用模型切片分析 (GPU, 分支: post-pump continuation)
问题: 在"昨日涨幅≥5%"的切片上, 专用模型(只在该切片训练)是否比通用模型更准?
切片: 样本日前一根蜡烛涨幅≥5%; 标签: 沿用aligned LONG标签(open[T]→close[T+2] ≥5%)
评估: 最近180天WF, 每日两模型(通用/专用)对切片样本打分, 比较 precision@10 和 AUC
附带: 🔥连涨2日第三日续涨率, 成交额与续涨的相关性, 切片特征重要性
"""
import os, sys, json, glob, time
from datetime import datetime, timezone
import numpy as np
from collections import defaultdict
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
sys.path.insert(0, f'{HOME}/websocket_new')
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
PUMP_MIN = 5.0        # 昨日涨幅≥5% 入切片
WF_DAYS = 180
TRAIN_WINDOW = 180

XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  min_child_weight=1, reg_lambda=10, reg_alpha=10,
                  subsample=0.8, colsample_bytree=0.6, device='cuda', verbosity=0)

def log(m): print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)

log('加载K线缓存...')
with open(KLINE_CACHE) as f:
    klines = json.load(f)['klines']
# 每币: ts(ms)→当日开盘索引, 用于算"昨日涨幅"
kmap = {}
for sym, kls in klines.items():
    kmap[sym] = ({k['t']: i for i, k in enumerate(kls)}, kls)

def yesterday_gain(sym, ts_ms):
    """样本日ts前一根收盘蜡烛的日涨幅%"""
    km = kmap.get(sym)
    if not km: return None
    i = km[0].get(ts_ms)
    if i is None or i < 2: return None
    kls = km[1]
    prev, pprev = kls[i-1], kls[i-2]  # 样本日的前一根=昨日(已收盘)
    if pprev['c'] <= 0: return None
    return (prev['c'] - pprev['c']) / pprev['c'] * 100

files = sorted(glob.glob(f'{CACHE_DIR}/*.npz'))
sdays = [int(os.path.basename(f).replace('.npz', '')) for f in files]
log(f'缓存 {len(sdays)} 天')

# 预扫: 切片规模 + 基准续涨率 + 🔥统计 (全历史, 但只用样本元数据)
log('预扫切片...')
slice_days = {}   # ts -> [(idx_in_npz, gain)]
base_pos, base_neg = 0, 0
slice_pos, slice_neg = 0, 0
streak_pos, streak_neg = 0, 0   # 🔥: 昨日≥5% 且 前日也≥5%
vol_rank, vol_ret = [], []      # 成交额 vs 续涨 (切片内)
for f in files[-400:]:          # 最近400天足够代表+省时间
    ts = int(os.path.basename(f).replace('.npz', ''))
    d = np.load(f)
    syms, labels = d['syms'], d['labels']
    feats = d['feats']
    sel = []
    for idx in range(len(syms)):
        g = yesterday_gain(str(syms[idx]), ts * 1000)
        ll = int(labels[idx, 0])
        if g is None:
            continue
        if g >= PUMP_MIN:
            sel.append(idx)
            if ll: slice_pos += 1
            else: slice_neg += 1
            # 🔥: 再前一天也≥5%
            km = kmap.get(str(syms[idx]))
            i = km[0].get(ts * 1000)
            if i >= 3:
                kls = km[1]
                g2 = (kls[i-2]['c'] - kls[i-3]['c']) / kls[i-3]['c'] * 100 if kls[i-3]['c'] > 0 else 0
                if g2 >= PUMP_MIN:
                    if ll: streak_pos += 1
                    else: streak_neg += 1
            q = feats[idx, 944] if feats.shape[1] > 944 else 0  # vol_raw
            vol_rank.append(q); vol_ret.append(1 if ll else 0)
        else:
            if ll: base_pos += 1
            else: base_neg += 1
    if sel:
        slice_days[ts] = sel

n_slice = slice_pos + slice_neg
n_base = base_pos + base_neg
log(f'近400天: 切片样本 {n_slice} (日均{n_slice/max(1,len(slice_days)):.0f}币), 基准(非pump)样本 {n_base}')
log(f'续涨率(2日≥5%): pump后 {slice_pos/max(1,n_slice)*100:.1f}% vs 非pump {base_pos/max(1,n_base)*100:.1f}%')
n_st = streak_pos + streak_neg
log(f'🔥连涨2日后第三日续涨率: {streak_pos/max(1,n_st)*100:.1f}% (n={n_st})')
if vol_rank:
    from scipy.stats import spearmanr
    rho, pv = spearmanr(vol_rank, vol_ret)
    log(f'切片内 成交额×续涨 spearman: rho={rho:+.4f} (p={pv:.2e}) — {"有" if abs(rho)>0.02 and pv<0.01 else "无"}预测力')

# ===== WF 对比: 通用 vs 专用 =====
def prep(X):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    return X

def load_day(ts):
    fp = f'{CACHE_DIR}/{ts}.npz'
    if not os.path.exists(fp): return None
    d = np.load(fp)
    return d['feats'], d['labels'][:, 0].astype(np.int32)

start = max(30, len(sdays) - WF_DAYS - 1)
eval_days = [ts for ts in sdays[start:] if ts in slice_days]
log(f'WF评估日: {len(eval_days)} 天')

gen_hits, gen_n = 0, 0
spc_hits, spc_n = 0, 0
gen_prob_all, spc_prob_all, y_all = [], [], []
imp_sum = None
t0 = time.time()
for di, pred_ts in enumerate(eval_days):
    d_idx = sdays.index(pred_ts)
    train_ts = sdays[max(0, d_idx - TRAIN_WINDOW):d_idx - 2]
    # 通用: 全部样本训练
    Xg, yg = [], []
    # 专用: 只pump切片样本
    Xs_, ys_ = [], []
    for ts in train_ts:
        r = load_day(ts)
        if r is None: continue
        feats, y = r
        Xg.append(feats); yg.append(y)
        sel = slice_days.get(ts)
        if sel:
            Xs_.append(feats[sel]); ys_.append(y[sel])
    if not Xg: continue
    Xg = prep(np.concatenate(Xg)); yg = np.concatenate(yg)
    pg = int(yg.sum())
    if pg < 5: continue
    mg = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yg)-pg)/pg, random_state=42)
    mg.fit(Xg, yg)

    ms = None
    if Xs_:
        Xs_ = prep(np.concatenate(Xs_)); ys_ = np.concatenate(ys_)
        ps_ = int(ys_.sum())
        if ps_ >= 5 and len(ys_) >= 200:
            ms = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(ys_)-ps_)/ps_, random_state=42)
            ms.fit(Xs_, ys_)
            if imp_sum is None: imp_sum = ms.feature_importances_.copy()
            else: imp_sum += ms.feature_importances_

    # 预测当日切片
    r = load_day(pred_ts)
    if r is None: continue
    feats, y = r
    sel = slice_days[pred_ts]
    Xp = prep(feats[sel]); yp = y[sel]
    prob_g = mg.predict_proba(Xp)[:, 1]
    order_g = np.argsort(-prob_g)[:10]
    gen_hits += int(yp[order_g].sum()); gen_n += len(order_g)
    gen_prob_all.extend(prob_g.tolist())
    if ms is not None:
        prob_s = ms.predict_proba(Xp)[:, 1]
        order_s = np.argsort(-prob_s)[:10]
        spc_hits += int(yp[order_s].sum()); spc_n += len(order_s)
        spc_prob_all.extend(prob_s.tolist())
    y_all.extend(yp.tolist())
    if (di + 1) % 30 == 0:
        log(f'  {di+1}/{len(eval_days)} 通用P@10={gen_hits}/{gen_n} 专用P@10={spc_hits}/{spc_n} ({time.time()-t0:.0f}s)')

y_all = np.array(y_all)
print('\n' + '=' * 60)
print(f'续涨切片 WF 对比 ({len(eval_days)}天, 切片=昨日≥{PUMP_MIN}%)')
print(f'  通用模型 P@10: {gen_hits}/{gen_n} = {gen_hits/max(1,gen_n)*100:.1f}%')
print(f'  专用模型 P@10: {spc_hits}/{spc_n} = {spc_hits/max(1,spc_n)*100:.1f}%')
from sklearn.metrics import roc_auc_score
if len(set(y_all)) > 1:
    print(f'  通用模型 AUC(切片): {roc_auc_score(y_all, gen_prob_all):.4f}')
    if spc_prob_all:
        print(f'  专用模型 AUC(切片): {roc_auc_score(y_all, spc_prob_all):.4f}')
if imp_sum is not None:
    top = np.argsort(-imp_sum)[:15]
    print(f'  切片特征重要性 Top15 (列号): {[int(i) for i in top]}')
print('=' * 60)
