#!/usr/bin/env python3
"""最小阻力方向特征原型 + IC 初筛.

假设: 价格倾向朝"上方成交量薄/下方成交量厚"的方向运动.
特征:
  bias_vol = (below_vol - above_vol) / (below_vol + above_vol)
  poc_rel  = (POC - close) / close
  vwap_rel = (VWAP - close) / close
  tbr      = tbq / q  (主动买入占比)
  ret_1d   = close/prev_close - 1
标签: aligned 2日收益 = (close[i+3] - open[i+1]) / open[i+1]
仅用 <= 特征日 i 的数据, 无未来信息.
"""
import json, math, statistics as st
from collections import defaultdict

KLINE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
MIN_BARS = 120
WINDOW = 90          # 成交量分布回看窗口
BINS = 30
SPAN = 0.20          # 上下各20%价格区间

print('加载日线...')
raw = json.load(open(KLINE))['klines']
syms = list(raw.keys())
print('币种:', len(syms))

def typical(k): return (k['h'] + k['l'] + k['c']) / 3.0

rows = []
for sym in syms:
    bars = raw[sym]
    if len(bars) < MIN_BARS:
        continue
    for i in range(MIN_BARS, len(bars) - 3):
        # 特征日 = i, 预测日 = i+1, 未来收益 = open[i+1] -> close[i+3]
        o1 = bars[i+1]['o']
        c3 = bars[i+3]['c']
        if o1 <= 0: continue
        fut = (c3 - o1) / o1 * 100.0
        close = bars[i]['c']
        if close <= 0: continue
        # 成交量分布: 只使用 <= i 的 WINDOW 根
        lo = max(0, i - WINDOW + 1)
        window = bars[lo:i+1]
        # 价格区间
        p_low = close * (1 - SPAN)
        p_high = close * (1 + SPAN)
        # 分箱
        edges = [p_low + (p_high - p_low) * k / BINS for k in range(BINS+1)]
        vol_by_bin = [0.0] * BINS
        total_vol = 0.0
        vwap_num = 0.0
        vwap_den = 0.0
        poc_bin = 0
        max_vol = -1.0
        for b in window:
            tp = typical(b)
            v = b['q'] if b['q'] else 0.0
            if v <= 0: continue
            # 找到所属bin
            idx = int((tp - p_low) / (p_high - p_low) * BINS)
            if idx < 0 or idx >= BINS: continue
            vol_by_bin[idx] += v
            if vol_by_bin[idx] > max_vol:
                max_vol = vol_by_bin[idx]; poc_bin = idx
            total_vol += v
            vwap_num += tp * v
            vwap_den += v
        if total_vol <= 0: continue
        above_vol = sum(vol_by_bin[poc_bin+1:])  # 粗略: 当前价以上
        below_vol = sum(vol_by_bin[:poc_bin])
        # 更合理: 按当前close切分
        # 用bin中心判断在close上/下
        above_vol2 = 0.0; below_vol2 = 0.0
        for k in range(BINS):
            center = p_low + (p_high - p_low) * (k + 0.5) / BINS
            if center > close:
                above_vol2 += vol_by_bin[k]
            else:
                below_vol2 += vol_by_bin[k]
        bias = (below_vol2 - above_vol2) / max(below_vol2 + above_vol2, 1e-9)
        up_w=0.0; down_w=0.0
        for k in range(BINS):
            center = p_low + (p_high - p_low) * (k + 0.5) / BINS
            dist = abs(center - close) / max(close,1e-9)
            w = vol_by_bin[k] / max(dist, 0.001)
            if center > close: up_w += w
            else: down_w += w
        bias_dist = (down_w - up_w) / max(down_w + up_w, 1e-9)
        poc_price = p_low + (p_high - p_low) * (poc_bin + 0.5) / BINS
        vwap = vwap_num / vwap_den if vwap_den > 0 else close
        prev_c = bars[i-1]['c']
        ret1 = (close - prev_c) / prev_c * 100 if prev_c > 0 else 0.0
        tbr = (bars[i].get('tbq',0) or 0) / bars[i]['q'] if bars[i]['q'] else 0.5
        rows.append({
            'sym': sym, 'i': i, 'bias': bias, 'bias_dist': bias_dist,
            'poc_rel': (poc_price - close) / close,
            'vwap_rel': (vwap - close) / close,
            'tbr': tbr, 'ret1': ret1, 'fut': fut,
        })
print('样本:', len(rows))

def spearman(a, b):
    ra = {v:i for i,v in enumerate(sorted(a))}
    rb = {v:i for i,v in enumerate(sorted(b))}
    n = len(a)
    ma = (n-1)/2; mb = (n-1)/2
    da = [ra[x]-ma for x in a]; db = [rb[x]-mb for x in b]
    cov = sum(x*y for x,y in zip(da,db))/n
    va = sum(x*x for x in da)/n; vb = sum(y*y for y in db)/n
    if va*vb <= 0: return 0.0
    return cov / math.sqrt(va*vb)

for feat in ['bias','bias_dist','poc_rel','vwap_rel','tbr','ret1']:
    vals = [r[feat] for r in rows]
    fut = [r['fut'] for r in rows]
    print(f'IC {feat}: spearman={spearman(vals,fut):+.4f} pearson={st.correlation(vals,fut):+.4f}' if hasattr(st,'correlation') else f'IC {feat}: spearman={spearman(vals,fut):+.4f}')

# 按bias分5组
order = sorted(rows, key=lambda r:r['bias'])
n = len(order)
print('\n=== bias 分5组 (未来2日收益%) ===')
for g in range(5):
    seg = order[g*n//5:(g+1)*n//5]
    mean = sum(r['fut'] for r in seg)/len(seg)
    win = sum(1 for r in seg if r['fut']>0)/len(seg)*100
    print(f'组{g+1} (bias低->高): n={len(seg)} mean={mean:+.3f}% 胜率={win:.1f}%')
