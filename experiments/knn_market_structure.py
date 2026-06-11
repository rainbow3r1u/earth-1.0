#!/usr/bin/env python3
"""KNN Market Architecture 简化版特征提取器 (含预计算缓存)"""
import numpy as np

def _find_pivots(highs, lows, window):
    n = len(highs)
    if n < 2 * window + 1: return [], []
    ph, pl = [], []
    for i in range(window, n - window):
        is_high = all(highs[i] > highs[i-j] for j in range(1, window+1)) and all(highs[i] > highs[i+j] for j in range(1, window+1))
        is_low = all(lows[i] < lows[i-j] for j in range(1, window+1)) and all(lows[i] < lows[i+j] for j in range(1, window+1))
        if is_high: ph.append((i, highs[i]))
        if is_low: pl.append((i, lows[i]))
    return ph, pl

def precompute_knn_features(klines):
    """预计算每个时间戳的KNN特征, O(n) 复杂度"""
    if len(klines) < 200: return {}
    closes = np.array([k['c'] for k in klines], dtype=float)
    highs = np.array([k['h'] for k in klines], dtype=float)
    lows = np.array([k['l'] for k in klines], dtype=float)
    opens = np.array([k['o'] for k in klines], dtype=float)
    volumes = np.array([k['q'] for k in klines], dtype=float)
    timestamps = [k['t'] // 1000 for k in klines]
    n = len(closes); cache = {}; win = 7; lb = 60

    ph, pl = _find_pivots(highs, lows, win)

    for i in range(200, n):
        ts = timestamps[i]; cc = closes[i]
        rph = [(idx, price) for idx, price in ph if i - lb < idx < i]
        rpl = [(idx, price) for idx, price in pl if i - lb < idx < i]
        feats = {'pivot_high_count': len(rph), 'pivot_low_count': len(rpl),
                 'pivot_total_count': len(rph)+len(rpl), 'pivot_window': win}
        if rph:
            nh = max(rph, key=lambda x: x[1])
            feats['nearest_pivot_high_dist'] = (cc - nh[1]) / nh[1] * 100
            feats['nearest_pivot_high_age'] = i - nh[0]
        else: feats['nearest_pivot_high_dist'] = 0; feats['nearest_pivot_high_age'] = 0
        if rpl:
            nl = min(rpl, key=lambda x: x[1])
            feats['nearest_pivot_low_dist'] = (cc - nl[1]) / nl[1] * 100
            feats['nearest_pivot_low_age'] = i - nl[0]
        else: feats['nearest_pivot_low_dist'] = 0; feats['nearest_pivot_low_age'] = 0

        bu = sum(1 for idx, price in rph for k in range(idx+1, i) if closes[k] > price)
        bd = sum(1 for idx, price in rpl for k in range(idx+1, i) if closes[k] < price)
        feats['bos_up_count'] = bu; feats['bos_down_count'] = bd

        if rph:
            bi, _ = max(rph, key=lambda x: x[1]); delta = 0; tv = 0
            for k in range(bi+1, i):
                tv += volumes[k]
                d = 1 if closes[k]>opens[k] else (-1 if closes[k]<opens[k] else 0)
                delta += d * volumes[k]
            feats['delta_tank_high'] = abs(delta)/tv*100 if tv>0 else 0
            feats['delta_tank_high_raw'] = int(delta)
        else: feats['delta_tank_high'] = 0; feats['delta_tank_high_raw'] = 0

        if rpl:
            bi, _ = min(rpl, key=lambda x: x[1]); delta = 0; tv = 0
            for k in range(bi+1, i):
                tv += volumes[k]
                d = 1 if closes[k]>opens[k] else (-1 if closes[k]<opens[k] else 0)
                delta += d * volumes[k]
            feats['delta_tank_low'] = abs(delta)/tv*100 if tv>0 else 0
            feats['delta_tank_low_raw'] = int(delta)
        else: feats['delta_tank_low'] = 0; feats['delta_tank_low_raw'] = 0

        if rph and rpl:
            ph_ = max(rph, key=lambda x: x[1])[1]; pl_ = min(rpl, key=lambda x: x[1])[1]
            sr = ph_ - pl_; feats['price_in_range_pct'] = (cc-pl_)/sr*100 if sr>0 else 50
        else: feats['price_in_range_pct'] = 50
        cache[ts] = feats
    return cache

def extract_market_structure_features(klines, lookback=60):  # kept for compat
    if len(klines) < 200: return _empty_features()
    return precompute_kn_features(klines) or _empty_features()

def _empty_features():
    return {'pivot_high_count':0,'pivot_low_count':0,'pivot_total_count':0,'pivot_window':5,
            'nearest_pivot_high_dist':0,'nearest_pivot_high_age':0,
            'nearest_pivot_low_dist':0,'nearest_pivot_low_age':0,
            'bos_up_count':0,'bos_down_count':0,'delta_tank_high':0,'delta_tank_high_raw':0,
            'delta_tank_low':0,'delta_tank_low_raw':0,'price_in_range_pct':50}

def feature_names(): return list(_empty_features().keys())

def feature_vector(fd): return [float(fd.get(k,0)) for k in feature_names()]
