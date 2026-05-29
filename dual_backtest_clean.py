#!/usr/bin/env python3
"""干净回测框架: 对称止盈+止损10%, 2天持仓, Top50币种, 含成本

对照实验:
  1. ML模型 (XGBoost多空二选一)
  2. 总是做多 (Always Long)
  3. 随机预测 (Random)

如果ML在干净框架下Sharpe显著高于随机/总是做多, 说明模型有真实预测能力。
"""
import os, sys, json, numpy as np, random
from datetime import datetime, timezone
from collections import defaultdict
from xgboost import XGBClassifier
sys.path.insert(0, os.path.dirname(__file__))
import daily_predictor as dp

RECENT_DAYS = 365
STOP_LOSS = 10.0
TAKE_PROFIT = 10.0   # 对称止盈 (NEW)
PROB_THRESHOLD = 60.0
FEE_PCT = 0.1        # 往返手续费 0.1% (taker 0.05%×2)
SLIPPAGE_PCT = 0.1   # 往返滑点 0.1%
TRADE_COST = FEE_PCT + SLIPPAGE_PCT  # 每笔交易总成本 0.2%
TRAIN_DAYS = 500
MIN_HISTORY = 60
MIN_VOLUME = 500000


def _fetch_klines_top50():
    """加载市值前49主流币的K线数据"""
    cache = '/home/myuser/backtester/data_cache/notusdt_1d_top50.json'
    with open(cache) as f:
        data = json.load(f)['klines']
    print(f"[Top50] 加载{len(data)}个主流币种K线")
    return data


def _check_exit(kls_daily, entry_i, entry_price, direction, stop_pct, take_pct):
    """对称止盈止损检测: 2天内先触及哪个就按哪个算

    做多: low <= stop_price → -stop_pct; high >= take_price → +take_pct
    做空: high >= stop_price → -stop_pct; low <= take_price → +take_pct
    未触发按第2天收盘价结算
    """
    if direction == 'long':
        stop_price = entry_price * (1 - stop_pct / 100)
        take_price = entry_price * (1 + take_pct / 100)
        for offset in [1, 2]:
            idx = entry_i + offset
            if idx >= len(kls_daily):
                continue
            k = kls_daily[idx]
            low = k['l'] if isinstance(k, dict) else float(k[3])
            high = k['h'] if isinstance(k, dict) else float(k[2])
            # 先检查是否同时触及(罕见), 按不利方向算
            if low <= stop_price:
                return True, -stop_pct, offset, 'stop'
            if high >= take_price:
                return True, +take_pct, offset, 'take'
        # 未触发, 按持仓到期收盘价
        idx = entry_i + 2
        if idx < len(kls_daily):
            k = kls_daily[idx]
            close_2d = k['c'] if isinstance(k, dict) else float(k[4])
            return False, (close_2d / entry_price - 1) * 100, 2, 'hold'
        return False, 0, 0, 'expire'
    else:
        # 做空: 价格上涨=亏损, 价格下跌=盈利
        stop_price = entry_price * (1 + stop_pct / 100)   # 涨10% → 止损
        take_price = entry_price * (1 - take_pct / 100)   # 跌10% → 止盈
        for offset in [1, 2]:
            idx = entry_i + offset
            if idx >= len(kls_daily):
                continue
            k = kls_daily[idx]
            high = k['h'] if isinstance(k, dict) else float(k[2])
            low = k['l'] if isinstance(k, dict) else float(k[3])
            if high >= stop_price:
                return True, -stop_pct, offset, 'stop'
            if low <= take_price:
                return True, +take_pct, offset, 'take'
        idx = entry_i + 2
        if idx < len(kls_daily):
            k = kls_daily[idx]
            close_2d = k['c'] if isinstance(k, dict) else float(k[4])
            return False, (entry_price - close_2d) / entry_price * 100, 2, 'hold'
        return False, 0, 0, 'expire'


def _build_samples(klines, oi_data, sector_map, sector_heats_all, btc_rets):
    """构建回测样本 — 与daily_predictor训练样本对齐"""
    all_samples = []
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

                # 标签: 2日收益
                next_ret = (closes[i + 1] - closes[j]) / closes[j] if closes[j] > 0 and i + 1 < n else 0
                if abs(next_ret) > 5.0:
                    continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                all_samples.append((ts, sym, feat, label_long, label_short, next_ret * 100))
            except:
                continue
    return all_samples


def _run_backtest_variant(by_day, recent_days, klines, variant='ml'):
    """跑回测变体: 'ml', 'always_long', 'random'

    variant='ml': 训练XGBoost多空二选一
    variant='always_long': 每天随机选一个币做多
    variant='random': 随机选方向(多/空/不做)
    """
    trades = []
    total_days = len(recent_days)
    rng = random.Random(42)  # 固定种子保证可复现

    for i, pred_ts in enumerate(recent_days):
        train_ts_list = [ts for ts in sorted(by_day.keys()) if ts < pred_ts][-TRAIN_DAYS:]
        if len(train_ts_list) < 10:
            continue

        X_train, y_long, y_short = [], [], []
        for ts in train_ts_list:
            if ts + 2 * 86400 > pred_ts:
                continue
            for sym, feat, ll, ls, ret in by_day[ts]:
                X_train.append(feat)
                y_long.append(ll)
                y_short.append(ls)

        X_train = np.array(X_train)
        bounds = []
        for j in range(X_train.shape[1]):
            col = X_train[:, j]
            bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
        X_train = dp._apply_winsor(X_train, bounds)

        pos_long = sum(y_long)
        pos_short = sum(y_short)
        if pos_long < 5 or pos_short < 5:
            continue

        if variant == 'ml':
            model_long = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                       scale_pos_weight=(len(y_long) - pos_long) / pos_long,
                                       random_state=42, eval_metric='logloss', verbosity=0)
            model_long.fit(X_train, y_long)

            model_short = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                        scale_pos_weight=(len(y_short) - pos_short) / pos_short,
                                        random_state=43, eval_metric='logloss', verbosity=0)
            model_short.fit(X_train, y_short)

            pred_samples = by_day[pred_ts]
            X_pred = np.array([s[1] for s in pred_samples])
            X_pred = dp._apply_winsor(X_pred, bounds)
            probs_long = model_long.predict_proba(X_pred)[:, 1]
            probs_short = model_short.predict_proba(X_pred)[:, 1]

            best_long = None
            best_short = None
            for idx, ((sym, feat, ll, ls, ret), pl, ps) in enumerate(zip(pred_samples, probs_long, probs_short)):
                kls_data = klines.get(sym, [])
                if len(kls_data) < MIN_HISTORY:
                    continue
                k_idx = dp._find_kline_index(kls_data, pred_ts)
                if k_idx is None or k_idx < 5:
                    continue
                v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls_data[k_idx - 5:k_idx]]
                if np.mean(v) < MIN_VOLUME:
                    continue
                if best_long is None or pl > best_long[1]:
                    best_long = (sym, pl, ret)
                if best_short is None or ps > best_short[1]:
                    best_short = (sym, ps, ret)

            long_prob = best_long[1] * 100 if best_long else 0
            short_prob = best_short[1] * 100 if best_short else 0
            max_prob = max(long_prob, short_prob)

            if max_prob < PROB_THRESHOLD:
                continue

            if best_long is not None and (best_short is None or long_prob >= short_prob):
                direction = 'long'
                sym, prob, ret = best_long
            else:
                direction = 'short'
                sym, prob, ret = best_short

        elif variant == 'always_long':
            # 每天随机选一个币做多
            pred_samples = by_day[pred_ts]
            candidates = []
            for sym, feat, ll, ls, ret in pred_samples:
                kls_data = klines.get(sym, [])
                if len(kls_data) < MIN_HISTORY:
                    continue
                k_idx = dp._find_kline_index(kls_data, pred_ts)
                if k_idx is None or k_idx < 5:
                    continue
                v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls_data[k_idx - 5:k_idx]]
                if np.mean(v) < MIN_VOLUME:
                    continue
                candidates.append((sym, ret))
            if not candidates:
                continue
            sym, ret = rng.choice(candidates)
            direction = 'long'
            prob = 0.5

        elif variant == 'random':
            pred_samples = by_day[pred_ts]
            candidates = []
            for sym, feat, ll, ls, ret in pred_samples:
                kls_data = klines.get(sym, [])
                if len(kls_data) < MIN_HISTORY:
                    continue
                k_idx = dp._find_kline_index(kls_data, pred_ts)
                if k_idx is None or k_idx < 5:
                    continue
                v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls_data[k_idx - 5:k_idx]]
                if np.mean(v) < MIN_VOLUME:
                    continue
                candidates.append((sym, ret))
            if not candidates:
                continue
            # 1/3做多, 1/3做空, 1/3空仓
            roll = rng.random()
            if roll < 0.333:
                continue  # 空仓
            sym, ret = rng.choice(candidates)
            direction = 'long' if roll < 0.667 else 'short'
            prob = 0.5
        else:
            raise ValueError(f"Unknown variant: {variant}")

        # 入场 (开盘价)
        kls_daily = klines.get(sym, [])
        k_idx = dp._find_kline_index(kls_daily, pred_ts)
        if k_idx is not None and k_idx >= 1:
            entry_i = k_idx
            entry_price = kls_daily[entry_i]['o'] if isinstance(kls_daily[entry_i], dict) else float(kls_daily[entry_i][1])
            hit, pnl, exit_day, exit_reason = _check_exit(kls_daily, entry_i, entry_price, direction,
                                                           STOP_LOSS, TAKE_PROFIT)
        else:
            pnl = ret if direction == 'long' else -ret
            # 硬地板/天花板 (fallback)
            if pnl > TAKE_PROFIT:
                pnl = TAKE_PROFIT
            if pnl < -STOP_LOSS:
                pnl = -STOP_LOSS
            hit = pnl <= -STOP_LOSS or pnl >= TAKE_PROFIT
            exit_day = 2
            exit_reason = 'fallback'

        # 扣除成本
        pnl = pnl - TRADE_COST

        day_str = datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%Y-%m-%d')
        trades.append({
            'day': day_str, 'ts': pred_ts,
            'direction': direction, 'symbol': sym,
            'prob': round(prob * 100, 1) if isinstance(prob, float) else prob,
            'pnl': round(pnl, 2),
            'stopped': hit,
            'exit_reason': exit_reason,
        })

        if (i + 1) % 30 == 0:
            print(f"  {variant:12s} 进度: {i+1}/{total_days}", flush=True)

    return trades


def _print_summary(trades, name):
    if not trades:
        print(f"  {name}: 无交易记录")
        return
    INITIAL_CAPITAL = 100
    equity = INITIAL_CAPITAL
    peak_equity = INITIAL_CAPITAL
    max_dd_pct = 0
    max_dd_from_start = 0
    cum_pnl = 0
    win_count = 0
    long_count = 0
    short_count = 0
    stop_count = 0
    take_count = 0

    for t in trades:
        cum_pnl += t['pnl']
        equity += INITIAL_CAPITAL * t['pnl'] / 100
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        if dd > max_dd_pct:
            max_dd_pct = dd
        dd_start = (INITIAL_CAPITAL - equity) / INITIAL_CAPITAL * 100 if equity < INITIAL_CAPITAL else 0
        max_dd_from_start = max(max_dd_from_start, dd_start)

        if t['pnl'] > 0:
            win_count += 1
        if t['direction'] == 'long':
            long_count += 1
        else:
            short_count += 1
        if t.get('exit_reason') == 'stop':
            stop_count += 1
        elif t.get('exit_reason') == 'take':
            take_count += 1

    total_days = len(trades)
    avg_daily = cum_pnl / total_days
    rets = [t['pnl'] for t in trades]
    sharpe = (np.mean(rets) / (np.std(rets) + 1e-6)) * np.sqrt(365) if len(rets) > 1 else 0

    print(f"  {name:12s} | 交易{total_days}天 胜率{win_count}/{total_days}({win_count/total_days*100:.0f}%) "
          f"夏普{sharpe:.2f} 累计{cum_pnl:+.1f}% 日均{avg_daily:+.2f}%")
    print(f"             多{long_count}空{short_count} 止盈{take_count}止损{stop_count} "
          f"回撤(峰值){max_dd_pct:.1f}% 回撤(本金){max_dd_from_start:.1f}%")
    return sharpe, cum_pnl


def run_clean_backtest():
    print("=" * 70)
    print("  干净回测框架: 对称止盈止损10% | 2天持仓 | Top50 | 成本0.2%")
    print("=" * 70)

    klines = _fetch_klines_top50()
    try:
        import requests
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT' and s.get('contractType') == 'PERPETUAL']
    except:
        fut_syms = list(klines.keys())

    print(f"拉取OI: {len(fut_syms)}个币种...")
    oi_data = dp.fetch_oi(fut_syms)

    sector_map = dp._load_sector_map()
    dp._sector_map_cache = sector_map
    if not dp._proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                dp._proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except:
            pass
    sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}

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

    # Kronos预计算
    all_ts_for_kronos = set()
    for kls in klines.values():
        if len(kls) < MIN_HISTORY:
            continue
        for k in kls:
            ts = k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000
            all_ts_for_kronos.add(ts)
    dp._precompute_kronos_features(list(all_ts_for_kronos))

    # 构建样本
    print("\n构建样本 (完整特征)...")
    all_samples = _build_samples(klines, oi_data, sector_map, sector_heats_all, btc_rets)

    by_day = defaultdict(list)
    for ts, sym, feat, ll, ls, ret in all_samples:
        by_day[ts].append((sym, feat, ll, ls, ret))

    sorted_days = sorted(by_day.keys())
    print(f"总交易日: {len(sorted_days)}, 总样本: {len(all_samples)}")
    recent_days = sorted_days[-RECENT_DAYS:]

    # === 对照实验 ===
    results = {}

    print(f"\n--- ML模型 (XGBoost多空二选一) ---")
    trades_ml = _run_backtest_variant(by_day, recent_days, klines, variant='ml')
    s_ml, c_ml = _print_summary(trades_ml, "ML")
    results['ml'] = {'sharpe': s_ml, 'cum': c_ml, 'trades': trades_ml}

    print(f"\n--- 总是做多 (Always Long) ---")
    trades_long = _run_backtest_variant(by_day, recent_days, klines, variant='always_long')
    s_long, c_long = _print_summary(trades_long, "AlwaysLong")
    results['always_long'] = {'sharpe': s_long, 'cum': c_long, 'trades': trades_long}

    print(f"\n--- 随机预测 (Random) ---")
    trades_rand = _run_backtest_variant(by_day, recent_days, klines, variant='random')
    s_rand, c_rand = _print_summary(trades_rand, "Random")
    results['random'] = {'sharpe': s_rand, 'cum': c_rand, 'trades': trades_rand}

    # 保存结果
    result_file = os.path.join(os.path.dirname(__file__), 'data/dual_backtest_clean.json')
    with open(result_file, 'w') as f:
        json.dump({k: {'sharpe': v['sharpe'], 'cum': v['cum'],
                      'trades': [{kk: vv for kk, vv in t.items() if kk != 'ts'} for t in v['trades'][:50]]}
                  for k, v in results.items()}, f, indent=2, default=str)
    print(f"\n结果已保存: {result_file}")

    print("\n" + "=" * 70)
    print("  干净框架对照总结")
    print("=" * 70)
    print(f"  ML模型      Sharpe={s_ml:.2f}  累计={c_ml:+.1f}%")
    print(f"  总是做多    Sharpe={s_long:.2f}  累计={c_long:+.1f}%")
    print(f"  随机预测    Sharpe={s_rand:.2f}  累计={c_rand:+.1f}%")
    print("=" * 70)

    if s_ml > s_long + 0.5 and s_ml > 1.0:
        print("  ✅ ML模型在干净框架下显著优于基准，有真实预测能力")
    elif s_ml > s_long:
        print("  ⚠️ ML略优于基准，但优势不够明显")
    else:
        print("  ❌ ML未跑赢基准，当前模型依赖框架偏差")

    return results


if __name__ == '__main__':
    run_clean_backtest()
