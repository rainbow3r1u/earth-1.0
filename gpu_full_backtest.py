#!/usr/bin/env python3
"""全特征 915维 365天回测 — 12核并行样本 + GPU XGBoost + 止损"""
import os, sys, json, time, numpy as np
from multiprocessing import Pool
from collections import defaultdict
sys.path.insert(0, '/root/reasonix-projects/websocket_new')
sys.stdout.reconfigure(line_buffering=True)

# ── GPU XGBoost ──
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
from experiments.knn_market_structure import precompute_knn_features, feature_names as knn_feature_names

# ── 离线: 拦截 Binance API ──
import requests
_real_get = requests.get
def _offline_get(url, *a, **kw):
    if 'binance' in url: raise ConnectionError("offline")
    return _real_get(url, *a, **kw)
requests.get = _offline_get

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

STOP_LOSS = -10.0

# ── 并行样本构建 (每币一个任务) ──
def _build_coin_samples(args):
    sym, kls, oi_map, btc_rets, sm, sh = args
    cl = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
    op = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
    hi = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
    lo = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
    vl = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
    ts = [k.get('t', 0)//1000 if isinstance(k, dict) else int(k[0])//1000 for k in kls]
    cr = dp._compute_returns(cl)
    n = len(kls)
    samples = []
    for i in range(25, n-2):
        try:
            r1 = (cl[i]-cl[i-1])/cl[i-1] if cl[i-1] > 0 else 0
            r3 = (cl[i]-cl[max(0,i-3)])/cl[max(0,i-3)] if cl[max(0,i-3)] > 0 else 0
            r5 = (cl[i]-cl[max(0,i-5)])/cl[max(0,i-5)] if cl[max(0,i-5)] > 0 else 0
            r20 = [(cl[j]-cl[j-1])/cl[j-1] if cl[j-1] > 0 else 0 for j in range(i-19,i+1)] if i >= 20 else [0]
            v20 = float(np.std(r20)) if i >= 20 else 0.02
            vf = max(v20, 0.002)
            r1n = round(r1/vf, 4)
            r3n = round(r3/(vf*1.732), 4)
            r5n = round(r5/(vf*2.236), 4)
            dr = [(cl[j]-cl[j-1])/cl[j-1] if cl[j-1] > 0 else 0 for j in range(i-4,i+1)] if i >= 5 else [0]
            vol = np.std(dr) if len(dr) > 1 else 0.02
            vr = vl[i]/np.mean(vl[max(0,i-5):i]) if i >= 5 and np.mean(vl[max(0,i-5):i]) > 0 else 1
            c20 = cl[i-20:i+1] if i >= 20 else [cl[i]]
            pp = (cl[i]-min(c20))/(max(c20)-min(c20)) if max(c20) != min(c20) else 0.5
            amp = (hi[i]-lo[i])/op[i] if op[i] > 0 else 0
            # streak
            st = 0
            for j in range(i, max(0, i-7)-1, -1):
                if cl[j] > op[j]: st += 1
                else: break
            ds = 1 if (cl[i] > cl[i-3] and vl[i] < vl[i-3]*0.7) else 0
            t = ts[i]
            on = oi_map.get(t, 0)
            oip = oi_map.get(t-86400, 0)
            oc = (on-oip)/oip if oip > 0 else 0
            if sym == 'BTCUSDT':
                b, a, r2, res = 1.0, 0.0, 1.0, 0.0
            else:
                b, a, r2, res = dp._regression_features(btc_rets, cr, i-1)
            sf = dp._get_sector_features(sym, t-86400, sm, sh)
            mf = dp._get_macro_features(t)
            mf = dp._apply_chain_tvl(mf, sym, t)
            r7 = dp._compute_rsi(cl, 7, i)
            r14 = dp._compute_rsi(cl, 14, i)
            r30 = dp._compute_rsi(cl, 30, i)
            rs = dp._compute_rsi_series(cl, 14)
            rd = dp._compute_rsi_divergence(cl, rs, i, window=20)
            vc = dp._compute_vol_clustering(cl, i)
            feat = [r1n, r3n, r5n, vol, vr, pp, amp, st, ds, oc] + vc + [b, a, r2, res, r7, r14, r30] + rd + sf + mf
            # KNN市场结构特征 (15维)
            knn_fd = knn_cache.get(sym, {}).get(t, {})
            feat += [float(knn_fd.get(k, 0)) for k in knn_feature_names()]
            nr = (cl[i+2]-cl[i])/cl[i] if cl[i] > 0 and i+2 < n else 0
            if abs(nr) > 5.0:
                continue
            samples.append((t, sym, feat, 1 if nr > 0.05 else 0, 1 if nr < -0.05 else 0, nr*100))
        except Exception:
            continue
    return samples

# ── 一次性加载所有数据 ──
_cache = None

def load_all():
    global _cache
    if _cache:
        return _cache
    k = dp.fetch_klines()
    o = dp.fetch_oi(list(k.keys()))
    sm = dp._load_sector_map()
    dp._sector_map_cache = sm
    sh = dp._precompute_sector_heats(k, sm) if sm else {}
    dp._etf_features = dp._load_etf_features()
    dp._chain_features = dp._load_chain_features()
    dp._sent_features = dp._load_sent_features()
    dp._fg_features = dp._load_fear_greed()
    dp._st_features = dp._load_stablecoin_netflow()
    dp._cb_features = dp._load_coinbase_premium()
    dp._cbg_features = dp._load_cb_gap_features()
    dp._bd_features = dp._load_btc_mcap()
    dp._kg_features = dp._load_korea_premium()
    dp._hr_features = dp._load_hashrate_features()
    dp._liq_features = dp._load_liquidation_features()
    dp._tvl_features = dp._load_chain_tvl()
    dp._ma_features = dp._load_macro_assets()
    dp._ab_features = dp._load_btc_dominance_proxy()
    ats = set()
    for kl in k.values():
        if len(kl) < 30:
            continue
        for kk in kl:
            ats.add(kk.get('t', 0)//1000 if isinstance(kk, dict) else int(kk[0])//1000)
    dp._precompute_kronos_features(list(ats))
    # KNN市场结构特征预计算
    knn_cache = {}
    for sym, kl in k.items():
        if len(kl) >= 200:
            knn_cache[sym] = precompute_knn_features(kl)
    print(f"[KNN] 预计算: {len(knn_cache)}币种")
    bkl = k.get('BTCUSDT', [])
    bc = [kk['c'] if isinstance(kk, dict) else float(kk[4]) for kk in bkl]
    br = dp._compute_returns(bc) if len(bc) > 1 else []
    _cache = (k, o, sm, sh, br)
    return _cache

# ── 主回测 ──
print(f"[{time.strftime('%H:%M:%S')}] 全特征 915维 365天回测 启动")
print(f"[{time.strftime('%H:%M:%S')}] 策略: 12核并行样本 + GPU XGBoost + -10%止损")

t_start = time.time()

# 1. 加载数据
t0 = time.time()
k, o, sm, sh, br = load_all()
print(f"[{time.strftime('%H:%M:%S')}] 数据加载: {time.time()-t0:.0f}s ({len(k)}币种)")

# 2. 并行构建样本
t0 = time.time()
work = [(sym, kl, o.get(sym, {}), br, sm, sh) for sym, kl in k.items() if len(kl) >= 30]
alls = []
with Pool(12) as pool:
    for cs in pool.imap_unordered(_build_coin_samples, work):
        alls.extend(cs)
print(f"[{time.strftime('%H:%M:%S')}] 样本构建: {len(alls)}条 ({time.time()-t0:.0f}s)")

# 3. 按日期分组
bd = defaultdict(list)
for ts, sym, feat, ll, ls, ret in alls:
    bd[ts].append((sym, feat, ll, ls, ret))
sd = sorted(bd.keys())
START = max(30, len(sd)-366)
trades = []
last_pct = -1

print(f"[{time.strftime('%H:%M:%S')}] Walk-forward: {len(sd)-START}天")

for d in range(START, len(sd)-1):
    pct = (d-START)*100 // max(1, len(sd)-START-1)
    if pct > last_pct and pct % 10 == 0:
        print(f"  {pct}% ({d-START}/{len(sd)-START-1}) trades={len(trades)}", flush=True)
        last_pct = pct

    tts = sd[max(0, d-500):d]
    pts = sd[d]

    Xt, yl, ys = [], [], []
    for ts in tts:
        if ts+2*86400 > pts:
            continue
        for sym, feat, ll, ls, ret in bd[ts]:
            Xt.append(feat)
            yl.append(ll)
            ys.append(ls)
    Xt = np.array(Xt)
    if Xt.shape[1] == 0:
        continue
    bds = dp._fast_winsor_bounds(Xt)
    Xt = dp._apply_winsor(Xt, bds)
    pl, ps = sum(yl), sum(ys)
    if pl < 5 or ps < 5:
        continue

    ml = dp.XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                          scale_pos_weight=(len(yl)-pl)/pl if pl else 1,
                          n_jobs=-1, random_state=42)
    ms = dp.XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                          scale_pos_weight=(len(ys)-ps)/ps if ps else 1,
                          n_jobs=-1, random_state=42)
    ml.fit(Xt, yl)
    ms.fit(Xt, ys)

    Xp, psyms = [], []
    for sym, feat, _, _, _ in bd[pts]:
        Xp.append(feat)
        psyms.append(sym)
    if not Xp:
        continue
    Xp = np.array(Xp)
    Xp = dp._apply_winsor(Xp, bds)
    plo = ml.predict_proba(Xp)[:, 1]
    psh = ms.predict_proba(Xp)[:, 1]
    bl, bs = int(np.argmax(plo)), int(np.argmax(psh))

    def get_open(sym, tt):
        for kk in k.get(sym, []):
            t = kk.get('t', 0)//1000 if isinstance(kk, dict) else int(kk[0])//1000
            if t >= tt:
                return kk['o'] if isinstance(kk, dict) else float(kk[1])
        return None

    for idx, dr in [(bl, 'L'), (bs, 'S')]:
        sym = psyms[idx]
        prob = plo[idx] if dr == 'L' else psh[idx]
        if prob < 0.6:
            continue
        entry = get_open(sym, pts)
        if not entry or entry <= 0:
            continue
        exit_ts = pts+2*86400
        sl_price = entry*(1+STOP_LOSS/100) if dr == 'L' else entry*(1-STOP_LOSS/100)
        stopped = False
        exit_p = None
        for kk in k.get(sym, []):
            t = kk.get('t', 0)//1000 if isinstance(kk, dict) else int(kk[0])//1000
            if t < pts:
                continue
            if t > exit_ts:
                break
            l = kk['l'] if isinstance(kk, dict) else float(kk[3])
            h = kk['h'] if isinstance(kk, dict) else float(kk[2])
            if dr == 'L' and l <= sl_price:
                stopped = True
                exit_p = sl_price
                break
            if dr == 'S' and h >= sl_price:
                stopped = True
                exit_p = sl_price
                break
            exit_p = kk['o'] if isinstance(kk, dict) else float(kk[1])
        if exit_p is None:
            continue
        pnl = (exit_p-entry)/entry*100 if dr == 'L' else (entry-exit_p)/entry*100
        trades.append({'s': sym, 'd': dr, 'p': pnl, 'pr': float(prob), 'st': stopped})

# 4. 统计
tp = sum(t['p'] for t in trades)
ws = [t for t in trades if t['p'] > 0]
sh = (np.mean([t['p'] for t in trades])/np.std([t['p'] for t in trades])*np.sqrt(365/2)) if len(trades) > 5 else 0
cum = 0
pk = 0
dd = 0
for t in trades:
    cum += t['p']
    pk = max(pk, cum)
    dd = max(dd, pk-cum)
sl = sum(1 for t in trades if t.get('st'))

elapsed = (time.time()-t_start)/60
result = {
    'pnl': round(tp, 2), 'sharpe': round(sh, 2),
    'win': round(len(ws)/len(trades)*100, 1) if trades else 0,
    'dd': round(dd, 2), 'trades': len(trades), 'sl': sl
}

print(f"\n[{'='*50}]")
print(f"  全特征 915维 (83基线 + 832 Kronos)")
print(f"  总收益: {result['pnl']}%  Sharpe: {result['sharpe']}")
print(f"  胜率: {result['win']}%  最大回撤: {result['dd']}%")
print(f"  交易: {result['trades']}笔  止损: {result['sl']}次")
print(f"  耗时: {elapsed:.1f}分钟")
print(f"[{'='*50}]")

# 保存
out = {'summary': result, 'trades': trades}
dst = '/root/reasonix-projects/websocket_new/data/full_915dim_365d.json'
with open(dst, 'w') as f:
    json.dump(out, f)
print(f"Saved → {dst}")
