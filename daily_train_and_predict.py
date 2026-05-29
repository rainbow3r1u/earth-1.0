#!/usr/bin/env python3
"""每日训练+预测一体化脚本

用法:
  python3 daily_train_and_predict.py        # 训练+预测（默认）
  python3 daily_train_and_predict.py --predict-only   # 只用现有模型预测（快）
  python3 daily_train_and_predict.py --train-only     # 只训练，不预测

流程:
  1. 拉取最新K线+OI数据
  2. 用全部历史数据重新训练多空模型（walk-forward最后一天）
  3. 用最新模型跑今日预测
  4. 保存模型+预测结果
"""
import os, sys, json, numpy as np, pickle, requests, argparse
from datetime import datetime, timezone
from collections import defaultdict
from xgboost import XGBClassifier
sys.path.insert(0, os.path.dirname(__file__))
import daily_predictor as dp
from utils.feature_builder import assemble_feature_vec

# 模型文件路径
MODEL_LONG = os.path.join(os.path.dirname(__file__), 'data/xgb_daily_model_clean_full.pkl')
MODEL_SHORT = os.path.join(os.path.dirname(__file__), 'data/xgb_short_model_clean_full.pkl')
BOUNDS_FILE = os.path.join(os.path.dirname(__file__), 'data/winsor_bounds_clean_full.json')
PRED_FILE = os.path.join(os.path.dirname(__file__), 'data/prediction_clean_full_today.json')

MIN_HISTORY = 60
MIN_VOLUME = 500000
PROB_THRESHOLD = 60.0


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def build_training_samples(klines, oi_data, sector_map, sector_heats_all, btc_rets):
    """构建训练样本 — 与clean训练对齐"""
    Xall_long, yall_long = [], []
    Xall_short, yall_short = [], []
    for sym, kls in klines.items():
        if len(kls) < MIN_HISTORY:
            continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
        opens = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
        highs = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
        lows = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
        vols = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000 for k in kls]
        coin_rets = dp._compute_returns(closes)
        n = len(kls)

        for i in range(25, n - 2):
            j = i - 1
            try:
                ret_1d = (closes[j] - closes[j - 1]) / closes[j - 1] if closes[j - 1] > 0 else 0
                ret_3d = (closes[j] - closes[max(0, j - 3)]) / closes[max(0, j - 3)] if closes[max(0, j - 3)] > 0 else 0
                ret_5d = (closes[j] - closes[max(0, j - 5)]) / closes[max(0, j - 5)] if closes[max(0, j - 5)] > 0 else 0
                if j >= 20:
                    rets_20 = [(closes[k] - closes[k - 1]) / closes[k - 1] if closes[k - 1] > 0 else 0 for k in
                               range(j - 18, j + 1)]
                    vol_20d = float(np.std(rets_20))
                else:
                    vol_20d = 0.02
                vol_floor = max(vol_20d, 0.002)
                ret_1d_norm = round(ret_1d / vol_floor, 4)
                ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
                ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
                if j >= 5:
                    daily_rets = [(closes[k] - closes[k - 1]) / closes[k - 1] if closes[k - 1] > 0 else 0 for k in
                                  range(j - 3, j + 1)]
                    volatility = np.std(daily_rets)
                else:
                    volatility = 0
                vol_ratio = vols[j] / np.mean(vols[max(0, j - 5):j]) if j >= 5 and np.mean(
                    vols[max(0, j - 5):j]) > 0 else 1
                if j >= 20:
                    c20 = closes[j - 19:j + 1]
                    price_position = (closes[j] - min(c20)) / (max(c20) - min(c20)) if max(c20) != min(c20) else 0.5
                else:
                    price_position = 0.5
                amplitude = (highs[j] - lows[j]) / opens[j] if opens[j] > 0 else 0
                streak = 0
                for k in range(j, max(0, j - 7) - 1, -1):
                    if closes[k] > opens[k]:
                        streak += 1
                    else:
                        break
                div_sign = 1 if (closes[j] > closes[j - 3] and vols[j] < vols[j - 3] * 0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(timestamps[j], 0)
                oi_prev = oi_map.get(timestamps[j - 1], 0)
                oi_chg = (oi_now - oi_prev) / oi_prev if oi_prev > 0 else 0

                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = dp._regression_features(btc_rets, coin_rets, j)

                ts_prev = ts - 86400
                sector_feats = dp._get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
                macro_feats = dp._get_macro_features(ts)
                macro_feats = dp._apply_chain_tvl(macro_feats, sym, ts)
                rsi7 = dp._compute_rsi(closes, 7, j)
                rsi14 = dp._compute_rsi(closes, 14, j)
                rsi30 = dp._compute_rsi(closes, 30, j)
                rsi14_series = dp._compute_rsi_series(closes, 14)
                rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, j, window=20)
                vol_col = dp._compute_vol_clustering(closes, j)

                feat = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude,
                        streak, div_sign, oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats

                # FIX: 2日收益标签，与回测对齐
                next_ret = (closes[i + 1] - closes[j]) / closes[j] if closes[j] > 0 and i + 1 < n else 0
                if abs(next_ret) > 5.0:
                    continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                Xall_long.append(feat)
                yall_long.append(label_long)
                Xall_short.append(feat)
                yall_short.append(label_short)
            except Exception:
                continue
    return Xall_long, yall_long, Xall_short, yall_short


def train_models(klines, oi_data, sector_map, sector_heats_all, btc_rets):
    """用最新全量数据重新训练多空模型"""
    log("构建训练样本...")
    Xl, yl, Xs, ys = build_training_samples(klines, oi_data, sector_map, sector_heats_all, btc_rets)
    log(f"做多样本: {len(yl)} 涨>5%: {sum(yl)} ({sum(yl)/len(yl)*100:.1f}%)")
    log(f"做空样本: {len(ys)} 跌>5%: {sum(ys)} ({sum(ys)/len(ys)*100:.1f}%)")
    log(f"特征维度: {len(Xl[0])}")

    Xl_arr = np.array(Xl)
    Xs_arr = np.array(Xs)
    bounds = []
    for j in range(Xl_arr.shape[1]):
        col = Xl_arr[:, j]
        bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
    Xl_arr = dp._apply_winsor(Xl_arr, bounds)
    Xs_arr = dp._apply_winsor(Xs_arr, bounds)

    with open(BOUNDS_FILE, 'w') as f:
        json.dump(bounds, f)
    log(f"winsor bounds已保存: {BOUNDS_FILE}")

    # 训练做多模型
    pos_l = sum(yl)
    model_long = XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.05,
                               scale_pos_weight=(len(yl) - pos_l) / pos_l,
                               random_state=42, eval_metric='logloss', verbosity=0)
    model_long.fit(Xl_arr, np.array(yl))
    with open(MODEL_LONG, 'wb') as f:
        pickle.dump(model_long, f)
    log(f"做多模型已保存: {MODEL_LONG}")

    # 训练做空模型
    pos_s = sum(ys)
    model_short = XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.05,
                                scale_pos_weight=(len(ys) - pos_s) / pos_s,
                                random_state=43, eval_metric='logloss', verbosity=0)
    model_short.fit(Xs_arr, np.array(ys))
    with open(MODEL_SHORT, 'wb') as f:
        pickle.dump(model_short, f)
    log(f"做空模型已保存: {MODEL_SHORT}")

    return model_long, model_short, bounds


def build_today_features(klines, oi_data, sector_map, sector_heats_all, btc_rets):
    """构建今日特征"""
    X, syms, tss = [], [], []
    for sym, kls in klines.items():
        if len(kls) < 60:
            continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
        opens = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
        highs = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
        lows = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
        vols = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000 for k in kls]
        n = len(kls)
        i = n - 2
        if i < 25:
            continue
        try:
            ret_1d = (closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] > 0 else 0
            ret_3d = (closes[i] - closes[max(0, i - 3)]) / closes[max(0, i - 3)] if closes[max(0, i - 3)] > 0 else 0
            ret_5d = (closes[i] - closes[max(0, i - 5)]) / closes[max(0, i - 5)] if closes[max(0, i - 5)] > 0 else 0
            if i >= 20:
                rets_20 = [(closes[j] - closes[j - 1]) / closes[j - 1] if closes[j - 1] > 0 else 0 for j in range(i - 19, i + 1)]
                vol_20d = float(np.std(rets_20))
            else:
                vol_20d = 0.02
            vol_floor = max(vol_20d, 0.002)
            ret_1d_norm = round(ret_1d / vol_floor, 4)
            ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
            ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
            if i >= 5:
                daily_rets = [(closes[j] - closes[j - 1]) / closes[j - 1] if closes[j - 1] > 0 else 0 for j in range(i - 4, i + 1)]
                volatility = np.std(daily_rets)
            else:
                volatility = 0
            vol_ratio = vols[i] / np.mean(vols[max(0, i - 5):i]) if i >= 5 and np.mean(vols[max(0, i - 5):i]) > 0 else 1
            if i >= 20:
                c20 = closes[i - 20:i + 1]
                price_position = (closes[i] - min(c20)) / (max(c20) - min(c20)) if max(c20) != min(c20) else 0.5
            else:
                price_position = 0.5
            amplitude = (highs[i] - lows[i]) / opens[i] if opens[i] > 0 else 0
            streak = 0
            for j in range(i, max(0, i - 7) - 1, -1):
                if closes[j] > opens[j]:
                    streak += 1
                else:
                    break
            div_sign = 1 if (closes[i] > closes[i - 3] and vols[i] < vols[i - 3] * 0.7) else 0
            ts = timestamps[i]
            oi_now = oi_map.get(ts, 0)
            oi_prev = oi_map.get(ts - 86400, 0)
            oi_chg = (oi_now - oi_prev) / oi_prev if oi_prev > 0 else 0

            coin_rets = dp._compute_returns(closes)
            if sym == 'BTCUSDT':
                beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
            else:
                beta, alpha, r2, residual = dp._regression_features(btc_rets, coin_rets, i - 1)

            ts_prev = ts - 86400
            sector_feats = dp._get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
            macro_feats = dp._get_macro_features(ts)
            macro_feats = dp._apply_chain_tvl(macro_feats, sym, ts)

            rsi7 = dp._compute_rsi(closes, 7, i)
            rsi14 = dp._compute_rsi(closes, 14, i)
            rsi30 = dp._compute_rsi(closes, 30, i)
            rsi14_series = dp._compute_rsi_series(closes, 14)
            rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, i, window=20)
            vol_col = dp._compute_vol_clustering(closes, i)

            feat = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude,
                    streak, div_sign, oi_chg] + vol_col + [
                    beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
            X.append(feat)
            syms.append(sym)
            tss.append(ts)
        except Exception:
            continue

    X_arr = np.array(X)
    bounds = None
    try:
        with open(BOUNDS_FILE) as f:
            bounds = json.load(f)
    except:
        pass
    X_arr = dp._apply_winsor(X_arr, bounds)
    return X_arr, syms, tss


def predict(model_long, model_short, klines, oi_data, sector_map, sector_heats_all, btc_rets):
    """用最新模型跑今日预测"""
    log("构建今日特征...")
    X, syms, tss = build_today_features(klines, oi_data, sector_map, sector_heats_all, btc_rets)
    log(f"候选币种: {len(syms)}")

    # 过滤成交量
    filtered_idx = []
    for idx, sym in enumerate(syms):
        kls = klines.get(sym, [])
        if len(kls) < 60:
            continue
        vols = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls[-5:]]
        if np.mean(vols) < MIN_VOLUME:
            continue
        filtered_idx.append(idx)

    X_filt = X[filtered_idx]
    syms_filt = [syms[i] for i in filtered_idx]
    log(f"过滤后: {len(syms_filt)}币种 (>=60天, >=50万U日均成交)")

    probs_long = model_long.predict_proba(X_filt)[:, 1]
    probs_short = model_short.predict_proba(X_filt)[:, 1]

    long_results = []
    short_results = []
    for i, sym in enumerate(syms_filt):
        long_results.append({'symbol': sym, 'prob': round(float(probs_long[i]) * 100, 1)})
        short_results.append({'symbol': sym, 'prob': round(float(probs_short[i]) * 100, 1)})

    long_results.sort(key=lambda x: -x['prob'])
    short_results.sort(key=lambda x: -x['prob'])

    return long_results, short_results


def print_results(long_results, short_results):
    print("\n" + "=" * 60)
    print("  做多 TOP15")
    print("=" * 60)
    for i, r in enumerate(long_results[:15]):
        print(f"  {i + 1:2d}. {r['symbol']:<18s} {r['prob']:5.1f}%")

    print("\n" + "=" * 60)
    print("  做空 TOP15")
    print("=" * 60)
    for i, r in enumerate(short_results[:15]):
        print(f"  {i + 1:2d}. {r['symbol']:<18s} {r['prob']:5.1f}%")

    print("\n" + "=" * 60)
    print("  🎯 多空二选一推荐")
    print("=" * 60)
    best_long = long_results[0] if long_results else {'symbol': 'N/A', 'prob': 0}
    best_short = short_results[0] if short_results else {'symbol': 'N/A', 'prob': 0}

    if max(best_long['prob'], best_short['prob']) < PROB_THRESHOLD:
        print(f"  ⚪ 概率均低于{PROB_THRESHOLD}%阈值，建议空仓")
        recommendation = None
    else:
        if best_long['prob'] >= best_short['prob']:
            recommendation = ('long', best_long['symbol'], best_long['prob'])
            print(f"  🟢 做多: {best_long['symbol']}  概率={best_long['prob']:.1f}%")
            print(f"     对照: 做空TOP1 {best_short['symbol']} {best_short['prob']:.1f}%")
        else:
            recommendation = ('short', best_short['symbol'], best_short['prob'])
            print(f"  🔴 做空: {best_short['symbol']}  概率={best_short['prob']:.1f}%")
            print(f"     对照: 做多TOP1 {best_long['symbol']} {best_long['prob']:.1f}%")

    # 保存
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    result = {
        'date': today,
        'model': 'clean_full_daily_retrain',
        'long_top15': long_results[:15],
        'short_top15': short_results[:15],
        'recommendation': {
            'direction': recommendation[0] if recommendation else None,
            'symbol': recommendation[1] if recommendation else None,
            'prob': recommendation[2] if recommendation else None,
        } if recommendation else None,
    }
    with open(PRED_FILE, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"\n预测已保存: {PRED_FILE}")

    return recommendation


def main():
    parser = argparse.ArgumentParser(description='每日训练+预测')
    parser.add_argument('--predict-only', action='store_true', help='只预测，不重新训练')
    parser.add_argument('--train-only', action='store_true', help='只训练，不预测')
    args = parser.parse_args()

    print("=" * 60)
    print(f"  每日训练+预测 — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print("=" * 60)

    # 1. 拉数据
    log("拉取最新K线...")
    klines = dp.fetch_klines()
    try:
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT' and s.get('contractType') == 'PERPETUAL']
    except Exception:
        fut_syms = list(klines.keys())

    log(f"拉取OI: {len(fut_syms)}个币种...")
    oi_data = dp.fetch_oi(fut_syms)

    # 2. 加载宏观特征
    sector_map = dp._load_sector_map()
    dp._sector_map_cache = sector_map
    if not dp._proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                dp._proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except:
            pass

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

    btc_kls = klines.get('BTCUSDT', [])
    btc_closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in btc_kls]
    btc_rets = dp._compute_returns(btc_closes) if len(btc_closes) > 1 else []

    # 3. Kronos预计算
    all_ts_for_kronos = set()
    for kls in klines.values():
        if len(kls) < MIN_HISTORY:
            continue
        for k in kls:
            ts = k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000
            all_ts_for_kronos.add(ts)
    dp._precompute_kronos_features(list(all_ts_for_kronos))

    sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}

    # 4. 训练
    model_long = None
    model_short = None
    bounds = None

    if not args.predict_only:
        log("=" * 40)
        log("【训练模式】用最新数据重新训练模型")
        log("=" * 40)
        model_long, model_short, bounds = train_models(klines, oi_data, sector_map, sector_heats_all, btc_rets)
    else:
        log("【预测模式】加载已有模型（不重新训练）")
        if not os.path.exists(MODEL_LONG) or not os.path.exists(MODEL_SHORT):
            log("错误: 模型文件不存在，请先训练")
            return
        with open(MODEL_LONG, 'rb') as f:
            model_long = pickle.load(f)
        with open(MODEL_SHORT, 'rb') as f:
            model_short = pickle.load(f)
        try:
            with open(BOUNDS_FILE) as f:
                bounds = json.load(f)
        except:
            pass
        log(f"加载模型: {MODEL_LONG}, {MODEL_SHORT}")

    # 5. 预测
    if not args.train_only:
        log("=" * 40)
        log("【预测模式】用最新模型跑今日预测")
        log("=" * 40)
        long_results, short_results = predict(model_long, model_short, klines, oi_data, sector_map, sector_heats_all, btc_rets)
        print_results(long_results, short_results)
    else:
        log("训练完成，跳过预测")

    log("全部完成")


if __name__ == '__main__':
    main()
