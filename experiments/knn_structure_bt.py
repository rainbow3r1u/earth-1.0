#!/usr/bin/env python3
"""
KNN 市场结构特征 365天回测对比

Before: 现有 916 维特征 (Kronos 832 + 基特征 84)
After:  916 + 15 维市场结构特征 = 931 维

对比两个模型的 Sharpe/收益/胜率/止损。
"""
import sys, os, json, time, math, numpy as np
from datetime import datetime, timezone as tz
from collections import defaultdict
from xgboost import XGBClassifier

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import daily_predictor as dp
from experiments.knn_market_structure import extract_market_structure_features, feature_names as knn_feature_names

# ====== CONFIG ======
KLINE_CACHE = "/home/myuser/backtester/data_cache/notusdt_1d_full.json"
RECENT_DAYS = 30
STOP_LOSS = 10.0; TAKE_PROFIT = 10.0
PROB_THRESHOLD = 60.0
FEE_PCT = 0.1; SLIPPAGE_PCT = 0.1
TRADE_COST = FEE_PCT + SLIPPAGE_PCT
TRAIN_DAYS = 365; MIN_HISTORY = 60; MIN_VOLUME = 500000
TOP_N = 150

def log(msg, flush=True):
    print(msg, flush=flush)

# ====== RANKING ======
def precompute_rankings(klines, all_days):
    """每天用前30天数据算流动性排名"""
    log(f"  排名: 0/{len(all_days)}", flush=True)
    rankings = {}
    for di, day_ts in enumerate(all_days):
        scores = []
        for sym, kls in klines.items():
            w = [k for k in kls if k['t'] // 1000 < day_ts][-30:]
            if len(w) < 30: continue
            c = [k['c'] for k in w]; h = [k['h'] for k in w]; l = [k['l'] for k in w]
            v = [k['q'] for k in w]
            rng = [(h[i] - l[i]) / c[i] if c[i] > 0 else 0 for i in range(len(c))]
            rets = [(c[i] - c[i - 1]) / c[i - 1] if c[i - 1] > 0 else 0 for i in range(1, len(c))]
            avg_r = np.mean(rng) * 100; vol_r = np.std(rets) * 100 if rets else 0
            av = np.mean(v); cv_v = np.std(v) / av if av > 0 else 999
            score = (avg_r * 0.4 + vol_r * 0.4) * math.log(max(av, 1)) / (1 + cv_v)
            scores.append((sym, score))
        scores.sort(key=lambda x: -x[1])
        rankings[day_ts] = set(s[0] for s in scores[:TOP_N])
        if (di + 1) % 500 == 0: log(f"  排名: {di + 1}/{len(all_days)}", flush=True)
    log(f"  排名完成", flush=True)
    return rankings

# ====== FEATURE BUILDING ======
def as_list(v, n):
    if isinstance(v, list): return v[:n] if len(v) >= n else v + [0.0] * (n - len(v))
    if isinstance(v, (int, float)): return [float(v)]
    return [0.0] * n

def build_samples(klines, rankings, add_knn=False):
    """构建样本, add_knn=True 时追加 15 维市场结构特征"""
    # 预加载辅助数据
    smap = dp._load_sector_map(); dp._sector_map_cache = smap
    sheats = dp._precompute_sector_heats(klines, smap) if smap else {}
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

    oi_data = dp.fetch_oi(list(klines.keys()))
    btc_kls = klines.get('BTCUSDT', [])
    btc_c = [k['c'] for k in btc_kls]
    btc_rets = dp._compute_returns(btc_c) if len(btc_c) > 1 else []

    # 只构建涉及到的币种 (两种排名方法的并集)
    all_syms = set()
    for top_set in rankings.values():
        all_syms.update(top_set)
    need_syms = {s for s in all_syms if s in klines and len(klines[s]) >= MIN_HISTORY}
    log(f"  目标币种: {len(need_syms)}/{len(all_syms)}", flush=True)

    samples_by_day = defaultdict(list)
    counter = 0
    total_syms = len(need_syms)

    for si, sym in enumerate(sorted(need_syms)):
        kls = klines[sym]
        if len(kls) < MIN_HISTORY: continue
        om = oi_data.get(sym, {})
        c = [k['c'] for k in kls]; o = [k['o'] for k in kls]
        h = [k['h'] for k in kls]; l = [k['l'] for k in kls]
        v = [k['q'] for k in kls]; ts = [k['t'] // 1000 for k in kls]
        crets = dp._compute_returns(c)
        n = len(kls)

        for i in range(25, n - 2):
            j = i - 1
            try:
                r1 = (c[j] - c[j - 1]) / c[j - 1] if c[j - 1] > 0 else 0
                r3 = (c[j] - c[max(0, j - 3)]) / c[max(0, j - 3)] if c[max(0, j - 3)] > 0 else 0
                r5 = (c[j] - c[max(0, j - 5)]) / c[max(0, j - 5)] if c[max(0, j - 5)] > 0 else 0
                v20 = np.std([(c[k] - c[k - 1]) / c[k - 1] if c[k - 1] > 0 else 0 for k in range(j - 18, j + 1)]) if j >= 20 else 0.02
                vf = max(v20, 0.002)
                r1n = round(r1 / vf, 4); r3n = round(r3 / (vf * 1.732), 4); r5n = round(r5 / (vf * 2.236), 4)
                vola = np.std([(c[k] - c[k - 1]) / c[k - 1] if c[k - 1] > 0 else 0 for k in range(j - 3, j + 1)]) if j >= 5 else 0
                vr = v[j] / np.mean(v[max(0, j - 5):j]) if j >= 5 and np.mean(v[max(0, j - 5):j]) > 0 else 1
                c20 = c[j - 19:j + 1] if j >= 20 else c[:j + 1]
                pp = (c[j] - min(c20)) / (max(c20) - min(c20)) if max(c20) != min(c20) else 0.5
                amp = (h[j] - l[j]) / o[j] if o[j] > 0 else 0
                streak = sum(1 for k in range(j, max(0, j - 7) - 1, -1) if c[k] > o[k])
                div_sig = 1 if (c[j] > c[j - 3] and v[j] < v[j - 3] * 0.7) else 0
                oin = om.get(ts[j], 0); oip = om.get(ts[j - 1], 0)
                oic = (oin - oip) / oip if oip > 0 else 0
                beta, alpha, r2, res = dp._regression_features(btc_rets, crets, j) if sym != 'BTCUSDT' else (1.0, 0.0, 1.0, 0.0)

                sf = dp._get_sector_features(sym, ts[i] - 86400, smap, sheats)
                prev_date = datetime.fromtimestamp(ts[i] - 86400, tz=tz.utc).strftime('%Y-%m-%d')
                mf = (
                    as_list(dp._etf_features.get(prev_date, 0), 2) +
                    as_list(dp._chain_features.get(prev_date, 0), 4) +
                    as_list(dp._sent_features.get(prev_date, 0), 6) +
                    as_list(dp._fg_features.get(prev_date, 0), 1) +
                    as_list(dp._st_features.get(prev_date, 0), 3) +
                    as_list(dp._cb_features.get(prev_date, 0), 3) +
                    as_list(dp._cbg_features.get(prev_date, 0), 3) +
                    as_list(dp._bd_features.get(prev_date, 0), 3) +
                    as_list(dp._kg_features.get(prev_date, 0), 3) +
                    as_list(dp._hr_features.get(prev_date, 0), 3) +
                    as_list(dp._liq_features.get(prev_date, 0), 7) +
                    as_list(dp._tvl_features.get(prev_date, 0), 6) +
                    as_list(dp._ma_features.get(prev_date, 0), 3) +
                    as_list(dp._ab_features.get(ts[i], 0), 1)
                )
                rsi7 = dp._compute_rsi(c, 7, j); rsi14 = dp._compute_rsi(c, 14, j); rsi30 = dp._compute_rsi(c, 30, j)
                rs = dp._compute_rsi_series(c, 14); rd = dp._compute_rsi_divergence(c, rs, j, window=20)
                vc = dp._compute_vol_clustering(c, j)
                kron = list(dp._kr_features.get(ts[i], [0] * 832))[:832]
                if len(kron) < 832: kron = kron + [0.0] * (832 - len(kron))

                feat = (
                    [r1n, r3n, r5n, vola, vr, pp, amp, streak, div_sig, oic] +
                    vc + [beta, alpha, r2, res, rsi7, rsi14, rsi30] + rd +
                    sf + mf + kron[:832]
                )

                # 可选: 追加 KNN 市场结构特征
                if add_knn:
                    knn_feats = extract_market_structure_features(kls[:i])
                    knn_vec = [float(knn_feats.get(k, 0)) for k in knn_feature_names()]
                    feat = feat + knn_vec

                next_ret = (c[i + 1] - c[j]) / c[j] if c[j] > 0 and i + 1 < n else 0
                if abs(next_ret) > 5.0: continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                samples_by_day[ts[i]].append((sym, feat, label_long, label_short, next_ret * 100))
                counter += 1
            except Exception:
                continue

        if (si + 1) % 100 == 0:
            log(f"  样本: {si + 1}/{total_syms}", flush=True)

    log(f"  样本: {len(samples_by_day)}天, {counter}条", flush=True)
    return samples_by_day

# ====== BACKTEST ENGINE ======
def run_backtest(samples_by_day, klines, rankings, label):
    sorted_days = sorted(samples_by_day.keys())
    recent_days = sorted_days[-RECENT_DAYS:]
    log(f"  {label}: 0/{len(recent_days)}", flush=True)

    trades = []
    for di, pred_ts in enumerate(recent_days):
        # 取候选币
        today_top = rankings.get(pred_ts, set())
        pred_samples = [s for s in samples_by_day.get(pred_ts, []) if s[0] in today_top]
        if not pred_samples: continue

        # 训练集
        train_ts_list = [ts for ts in sorted_days if ts < pred_ts][-TRAIN_DAYS:]
        if len(train_ts_list) < 10: continue

        X_tr, y_l, y_s = [], [], []
        for ts in train_ts_list:
            if ts + 2 * 86400 > pred_ts: continue
            for sym, feat, ll, ls, ret in samples_by_day.get(ts, []):
                if sym not in today_top: continue  # 只用当前Top150的币训练
                X_tr.append(feat); y_l.append(ll); y_s.append(ls)

        if not X_tr or sum(y_l) < 5 or sum(y_s) < 5: continue

        X_tr = np.array(X_tr, dtype=np.float32)
        # Winsorize
        bounds = []
        for col_idx in range(X_tr.shape[1]):
            col = X_tr[:, col_idx]
            bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
        X_tr = dp._apply_winsor(X_tr, bounds)

        # Train
        ml = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                           scale_pos_weight=(len(y_l) - sum(y_l)) / max(sum(y_l), 1),
                           random_state=42, eval_metric='logloss', verbosity=0, n_jobs=6)
        ml.fit(X_tr, y_l)

        ms = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                           scale_pos_weight=(len(y_s) - sum(y_s)) / max(sum(y_s), 1),
                           random_state=43, eval_metric='logloss', verbosity=0, n_jobs=6)
        ms.fit(X_tr, y_s)

        # Predict
        X_pred = np.array([s[1] for s in pred_samples], dtype=np.float32)
        X_pred = dp._apply_winsor(X_pred, bounds)
        pl = ml.predict_proba(X_pred)[:, 1]
        ps = ms.predict_proba(X_pred)[:, 1]

        best_l = None; best_s = None
        for idx, (sym, feat, ll, ls, ret) in enumerate(pred_samples):
            kls_data = klines.get(sym, [])
            if len(kls_data) < MIN_HISTORY: continue
            k_idx = dp._find_kline_index(kls_data, pred_ts)
            if k_idx is None or k_idx < 5: continue
            recent_v = np.mean([k.get('q', 0) for k in kls_data[k_idx - 5:k_idx]])
            if recent_v < MIN_VOLUME: continue
            if best_l is None or pl[idx] > best_l[1]:
                best_l = (sym, float(pl[idx]), ret)
            if best_s is None or ps[idx] > best_s[1]:
                best_s = (sym, float(ps[idx]), ret)

        # 开仓
        if best_l is None and best_s is None: continue
        long_prob = best_l[1] * 100 if best_l else 0
        short_prob = best_s[1] * 100 if best_s else 0

        if max(long_prob, short_prob) < PROB_THRESHOLD: continue

        if long_prob >= short_prob:
            sym, prob, _ = best_l; direction = 'long'; close_side = 'sell'
        else:
            sym, prob, _ = best_s; direction = 'short'; close_side = 'buy'

        # 模拟开仓
        kls_sym = klines[sym]
        k_idx = dp._find_kline_index(kls_sym, pred_ts)
        if k_idx is None or k_idx < 1: continue

        entry_price = kls_sym[k_idx - 1]['c']
        # Check exit in next 2 days
        exit_found = False; exit_ret = 0; exit_type = 'hold'
        for offset in [1, 2]:
            ei = k_idx + offset
            if ei >= len(kls_sym): continue
            k = kls_sym[ei]
            if direction == 'long':
                if k['l'] <= entry_price * (1 - STOP_LOSS / 100):
                    exit_ret = -STOP_LOSS; exit_type = 'stop'; exit_found = True; break
                if k['h'] >= entry_price * (1 + TAKE_PROFIT / 100):
                    exit_ret = TAKE_PROFIT; exit_type = 'take'; exit_found = True; break
            else:
                if k['h'] >= entry_price * (1 + STOP_LOSS / 100):
                    exit_ret = -STOP_LOSS; exit_type = 'stop'; exit_found = True; break
                if k['l'] <= entry_price * (1 - TAKE_PROFIT / 100):
                    exit_ret = TAKE_PROFIT; exit_type = 'take'; exit_found = True; break
        if not exit_found:
            ei = min(k_idx + 2, len(kls_sym) - 1)
            close_price = kls_sym[ei]['c']
            if direction == 'long':
                exit_ret = (close_price / entry_price - 1) * 100
            else:
                exit_ret = (entry_price / close_price - 1) * 100
            exit_type = 'expire'

        exit_ret = exit_ret - TRADE_COST
        trades.append({'ts': pred_ts, 'sym': sym, 'dir': direction, 'ret': exit_ret,
                       'prob': prob, 'exit': exit_type})

        if (di + 1) % 50 == 0:
            log(f"  {label}: {di + 1}/{len(recent_days)}", flush=True)

    return trades

def calc_stats(trades, label):
    if not trades: return 0, 0, 0, 0
    n = len(trades)
    # 简单复利: 每笔盈利 = 保证金×ret/100, 初始100u
    capital = 100.0
    for t in trades:
        capital *= (1 + t['ret'] / 100)
    cum = (capital / 100 - 1) * 100
    rets = [t['ret'] for t in trades]
    sharpe = np.mean(rets) / max(np.std(rets), 0.01) * np.sqrt(365 / max(n, 1)) if rets else 0
    wr = sum(1 for t in trades if t['ret'] > 0) / max(n, 1) * 100
    stops = sum(1 for t in trades if t.get('exit') == 'stop')
    log(f"{label}: {n}笔 Sharpe={sharpe:.2f} 累计={cum:+.1f}% 胜率={wr:.1f}% 止损={stops}", flush=True)
    return sharpe, cum, wr, stops, n

# ====== MAIN ======
def main():
    t0 = time.time()
    log("=" * 60)
    log(" KNN市场结构特征 365天回测对比")
    log("=" * 60)

    # 1. 加载K线
    log("[1/5] 加载K线...")
    with open(KLINE_CACHE) as f:
        full = json.load(f)['klines']
    log(f"  {len(full)}币种")

    # 2. 预计算 K 线日期
    all_ts = set()
    for kls in full.values():
        for k in kls:
            all_ts.add(k['t'] // 1000)
    all_days = sorted(all_ts)
    log(f"  {len(all_days)}个交易日")

    # 3. 预计算每日排名
    log("[2/5] 预计算每日Top150排名...")
    rankings = precompute_rankings(full, all_days)

    # 4. Kronos 预计算
    log("[3/5] 预计算Kronos...")
    dp._precompute_kronos_features(list(all_ts))
    log(f"  Kronos缓存: {len(dp._kr_features)}天")

    # 5. 构建样本 (不含KNN)
    log("[4/5] 构建样本 (不含KNN)...")
    samples_base = build_samples(full, rankings, add_knn=False)
    if not samples_base:
        log("FATAL: 样本为空"); return

    # 6. 构建另一份样本 (含KNN) — 只对需要的时间点计算KNN
    log("[4b/5] 追加KNN市场结构特征...")
    # 复用基础样本，追加 KNN 特征
    samples_knn = defaultdict(list)
    knn_counter = 0
    for ts, items in samples_base.items():
        for sym, feat, ll, ls, ret in items:
            kls = full.get(sym, [])
            if len(kls) < 200:
                knn_feats = extract_market_structure_features([])  # empty
            else:
                # find the index for this ts
                ts_list = [k['t'] // 1000 for k in kls]
                idx = None
                for di, t in enumerate(ts_list):
                    if t == ts:
                        idx = di; break
                if idx is not None:
                    knn_feats = extract_market_structure_features(kls[:idx + 1])
                else:
                    knn_feats = extract_market_structure_features([])
            knn_vec = [float(knn_feats.get(k, 0)) for k in knn_feature_names()]
            feat_knn = feat + knn_vec
            samples_knn[ts].append((sym, feat_knn, ll, ls, ret))
            knn_counter += 1
    log(f"  KNN特征追加完成, {knn_counter}条")

    # 7. 回测
    log(f"[5/5] 回测对比 (近{RECENT_DAYS}天)...")
    trades_before = run_backtest(samples_base, full, rankings, "Before(无KNN)")
    trades_after = run_backtest(samples_knn, full, rankings, "After(有KNN)")

    # 8. 结果
    sb, cb, wb, stb, nb = calc_stats(trades_before, "Before")
    sa, ca, wa, sta, na = calc_stats(trades_after, "After")

    elapsed = (time.time() - t0) / 60
    log(f"\n{'='*60}")
    log(f"  对比总结 (耗时 {elapsed:.0f}min)")
    log(f"{'='*60}")
    log(f"  指标            Before(无KNN)    After(有KNN)     变化")
    log(f"  {'─'*55}")
    log(f"  Sharpe              {sb:+.2f}           {sa:+.2f}        {sa-sb:+.2f}")
    log(f"  累计收益            {cb:+.1f}%          {ca:+.1f}%       {ca-cb:+.1f}%")
    log(f"  胜率                {wb:.1f}%           {wa:.1f}%        {wa-wb:+.1f}%")
    log(f"  止损次数            {stb}              {sta}           {sta-stb:+d}")
    log(f"  交易次数            {nb}              {na}           {na-nb:+d}")

    if sa > sb + 0.3:
        verdict = "✅ KNN市场结构特征显著提升Sharpe"
    elif abs(sa - sb) < 0.3:
        verdict = "➡️ KNN市场结构特征无明显提升"
    else:
        verdict = "❌ KNN市场结构特征降低Sharpe"
    log(f"  {verdict}")

    # 保存
    result_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, 'knn_structure_bt.json'), 'w') as f:
        json.dump({
            'before': {'sharpe': round(sb,2), 'cum': round(cb,1), 'trades': nb, 'wr': round(wb,1), 'stops': stb},
            'after': {'sharpe': round(sa,2), 'cum': round(ca,1), 'trades': na, 'wr': round(wa,1), 'stops': sta},
            'config': {'days': RECENT_DAYS, 'train': TRAIN_DAYS, 'top_n': TOP_N, 'knn_feats': len(knn_feature_names())}
        }, f, indent=2)
    log(f"  结果已保存")

if __name__ == '__main__':
    main()
