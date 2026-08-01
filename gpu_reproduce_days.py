#!/usr/bin/env python3
"""逐日复现验证: 回测模型在指定日期选什么币 vs 生产公证预测
对目标日期T: 用sdays[idx-180:idx-2]训练(aligned截止d-3, 与生产T-3一致),
给T日的特征(全新构建, 与生产_build_feat_impl同源)打分, 输出LONG/SHORT Top5。
不看结果, 只看选币是否与生产公证一致。
"""
import os, sys, json, time
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
sys.path.insert(0, f'{HOME}/websocket_new')
import daily_predictor as dp

CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
TARGET_DAYS = ['2026-07-29', '2026-07-30', '2026-07-31']
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
        lo, hi = float(col[k1]), float(col[k99])
        if lo == 0.0 and hi == 0.0:
            cmin, cmax = float(col.min()), float(col.max())
            if cmin < 0.0 or cmax > 0.0: lo, hi = cmin, cmax
        b.append((lo, hi))
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
    b = qbounds(X)
    lo = np.array([x[0] for x in b]); hi = np.array([x[1] for x in b])
    X = np.clip(X, lo, hi)
    ml = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yL)-pL)/pL, random_state=42).fit(X, yL)
    ms = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yS)-pS)/pS, random_state=43).fit(X, yS)
    return ml, ms, b

log('加载K线+费率...')
klines = json.load(open(KLINE_CACHE))['klines']
with open(f'{HOME}/backtester/data_cache/funding_hist.json') as f:
    fund_data = {s: sorted(r) for s, r in json.load(f).items()}
oi_raw = json.load(open(f'{HOME}/backtester/data_cache/oi_daily.json'))
oi_data = {s: {int(k): float(v) for k, v in r.items()} for s, r in oi_raw.items()}
sector_map = dp._load_sector_map()
dp._sector_map_cache = sector_map
sector_heats = dp._precompute_sector_heats(klines, sector_map)
btc_rets = dp._compute_returns([k['c'] for k in klines['BTCUSDT']])

# 与生产一致: 加载dp各外部特征到dp全局 (与harness _build_coin_samples相同初始化)
dp._kr_features = {}
dp._etf_features = dp._load_etf_features(); dp._chain_features = dp._load_chain_features()
dp._sent_features = dp._load_sent_features(); dp._fg_features = dp._load_fear_greed()
dp._st_features = dp._load_stablecoin_netflow(); dp._cb_features = dp._load_coinbase_premium()
dp._cbg_features = dp._load_cb_gap_features(); dp._bd_features = dp._load_btc_mcap()
dp._kg_features = dp._load_korea_premium(); dp._hr_features = dp._load_hashrate_features()
dp._liq_features = dp._load_liquidation_features(); dp._tvl_features = dp._load_chain_tvl()
dp._ma_features = dp._load_macro_assets(); dp._ab_features = dp._load_btc_dominance_proxy()

import auto_dual_trade as adt

def build_day_features(sym, kls, oi_map, ts):
    """截断到目标日蜡烛后构建特征 — 复现生产当日时点的特征视角"""
    kls_t = [k for k in kls if k['t'] <= ts * 1000]
    if len(kls_t) < 35:
        return None
    res = adt._build_feat_impl(sym, kls_t, oi_map, btc_rets, sector_map, sector_heats)
    return res[-1] if res and res[-1][0] == ts else None

sdays = sorted(int(f.replace('.npz', '')) for f in os.listdir(CACHE_DIR) if f.endswith('.npz'))
log(f'缓存天数: {len(sdays)}, 最新: {datetime.fromtimestamp(sdays[-1], tz=timezone.utc):%Y-%m-%d}')

models_cache = {}
def get_models(idx):
    if idx not in models_cache:
        log(f'  训练窗口至 {datetime.fromtimestamp(sdays[idx-3], tz=timezone.utc):%Y-%m-%d} ...')
        models_cache[idx] = train_pair(sdays[max(0, idx-180):idx-2])
    return models_cache[idx]

for day in TARGET_DAYS:
    ts = int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    if ts not in sdays:
        # 该日样本不在缓存(标签不全), 用最新训练窗(截止day-3)仍可打分
        idx = max(i for i, t in enumerate(sdays) if t < ts)
        train_idx = idx + 3  # 近似: 训练截止需=day-3
    else:
        idx = sdays.index(ts)
        train_idx = idx
    log(f'\n=== {day} ===')
    ml, ms, b = get_models(train_idx)
    # 构建当日全部币特征
    Xp, syms = [], []
    for sym, kls in klines.items():
        if len(kls) < 35: continue
        hit = build_day_features(sym, kls, oi_data.get(sym, {}), ts)
        if hit:
            Xp.append(list(hit[2])); syms.append(sym)
    if not Xp:
        log(f'  {day} 无样本(特征日蜡烛未收盘?)')
        continue
    Xp = prep(np.array(Xp, dtype=np.float32), b)
    pl = ml.predict_proba(Xp)[:, 1]; ps = ms.predict_proba(Xp)[:, 1]
    orderL = np.argsort(-pl)[:5]; orderS = np.argsort(-ps)[:5]
    print(f'  LONG Top5:  {[(syms[i], round(float(pl[i])*100,1)) for i in orderL]}')
    print(f'  SHORT Top5: {[(syms[i], round(float(ps[i])*100,1)) for i in orderS]}')
    # 与生产公证对比
    pf = f'/home/myuser/websocket_new/data/pred_{day}.json'
    if os.path.exists(pf.replace('/home/myuser', HOME)):
        prod = json.load(open(pf.replace('/home/myuser', HOME)))
        bl, bs = prod.get('best_long'), prod.get('best_short')
        top_l = {syms[i] for i in orderL}; top_s = {syms[i] for i in orderS}
        print(f'  生产公证: best_long={bl}, best_short={bs}')
        if bl: print(f'    LONG复现: {"✅" if bl["symbol"] in top_l else "❌ 不在Top5"}')
        if bs: print(f'    SHORT复现: {"✅" if bs["symbol"] in top_s else "❌ 不在Top5"}')
