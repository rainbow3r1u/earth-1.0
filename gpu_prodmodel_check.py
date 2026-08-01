#!/usr/bin/env python3
"""三分解验证: 生产原模型 + GPU特征 → 是否复现生产公证预测
逐币对比概率(公证文件含全量all_long/all_short), 定位分歧在模型还是特征
"""
import os, sys, json, pickle, time
import numpy as np
from datetime import datetime, timezone
sys.path.insert(0, '/home/linux/websocket_new')
import daily_predictor as dp

HOME = os.path.expanduser('~')
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
MODEL_DIR = '/tmp'

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

def window_bounds(last_train_day):
    """按生产口径: 训练窗[day-180, day-3]的X_train计算winsor边界"""
    X = []
    sdays = sorted(int(f.replace('.npz', '')) for f in os.listdir(CACHE_DIR) if f.endswith('.npz'))
    idx = sdays.index(last_train_day)
    for ts in sdays[max(0, idx-179):idx+1]:
        d = np.load(f'{CACHE_DIR}/{ts}.npz')
        X.append(d['feats'])
    X = np.nan_to_num(np.concatenate(X).astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    return qbounds(X)

def prep(X, b):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    lo = np.array([x[0] for x in b]); hi = np.array([x[1] for x in b])
    return np.clip(X, lo, hi)

log('初始化特征环境...')
klines = json.load(open(KLINE_CACHE))['klines']
oi_raw = json.load(open(f'{HOME}/backtester/data_cache/oi_daily.json'))
oi_data = {s: {int(k): float(v) for k, v in r.items()} for s, r in oi_raw.items()}
sector_map = dp._load_sector_map()
dp._sector_map_cache = sector_map
sector_heats = dp._precompute_sector_heats(klines, sector_map)
btc_rets = dp._compute_returns([k['c'] for k in klines['BTCUSDT']])
dp._kr_features = {}
dp._etf_features = dp._load_etf_features(); dp._chain_features = dp._load_chain_features()
dp._sent_features = dp._load_sent_features(); dp._fg_features = dp._load_fear_greed()
dp._st_features = dp._load_stablecoin_netflow(); dp._cb_features = dp._load_coinbase_premium()
dp._cbg_features = dp._load_cb_gap_features(); dp._bd_features = dp._load_btc_mcap()
dp._kg_features = dp._load_korea_premium(); dp._hr_features = dp._load_hashrate_features()
dp._liq_features = dp._load_liquidation_features(); dp._tvl_features = dp._load_chain_tvl()
dp._ma_features = dp._load_macro_assets(); dp._ab_features = dp._load_btc_dominance_proxy()
import auto_dual_trade as adt

def feats_for_day(ts):
    Xp, syms = [], []
    for sym, kls in klines.items():
        kls_t = [k for k in kls if k['t'] <= ts * 1000]
        if len(kls_t) < 35: continue
        res = adt._build_feat_impl(sym, kls_t, oi_data.get(sym, {}), btc_rets, sector_map, sector_heats)
        if res and res[-1][0] == ts:
            Xp.append(list(res[-1][2])); syms.append(sym)
    return np.array(Xp, dtype=np.float32), syms

def run_day(day, model_tags, last_train_day):
    ts = int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    log(f'=== {day} (模型: {model_tags}, winsor窗止{last_train_day}) ===')
    b = window_bounds(int(datetime.strptime(last_train_day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()))
    log('  构建当日特征...')
    X, syms = feats_for_day(ts)
    X = prep(X, b)
    pls, pss = [], []
    for tag in model_tags:
        with open(f'{MODEL_DIR}/xgb_daily_long_{tag}.pkl', 'rb') as f: ml = pickle.load(f)
        with open(f'{MODEL_DIR}/xgb_daily_short_{tag}.pkl', 'rb') as f: ms = pickle.load(f)
        pls.append(ml.predict_proba(X)[:, 1]); pss.append(ms.predict_proba(X)[:, 1])
    pl = np.mean(pls, axis=0); ps = np.mean(pss, axis=0)
    # 与公证全量概率逐币对比
    prod = json.load(open(f'/home/myuser/websocket_new/data/pred_{day}.json'))
    prod_l = {s: p for s, p in prod.get('all_long', [])}
    prod_s = {s: p for s, p in prod.get('all_short', [])}
    both = [s for s in syms if s in prod_l]
    diffs = [abs(pl[syms.index(s)]*100 - prod_l[s]) for s in both]
    diffs = np.array(diffs)
    print(f'  可比币数: {len(both)}/{len(prod_l)}')
    print(f'  LONG概率差: 中位{np.median(diffs):.1f}pp 均值{diffs.mean():.1f}pp 最大{diffs.max():.1f}pp  >5pp的币: {(diffs>5).sum()}')
    oL = np.argsort(-pl)[:5]; oS = np.argsort(-ps)[:5]
    print(f'  GPU Top5 LONG:  {[(syms[i], round(float(pl[i])*100,1)) for i in oL]}')
    print(f'  GPU Top5 SHORT: {[(syms[i], round(float(ps[i])*100,1)) for i in oS]}')
    prod_top_l = [t['symbol'] for t in prod.get('top10_long', [])][:5]
    prod_top_s = [t['symbol'] for t in prod.get('top10_short', [])][:5]
    print(f'  公证 Top5 LONG:  {prod_top_l}')
    print(f'  公证 Top5 SHORT: {prod_top_s}')

run_day('2026-07-30', ['20260730'], '2026-07-27')
run_day('2026-07-31', ['20260730', '20260731'], '2026-07-28')
