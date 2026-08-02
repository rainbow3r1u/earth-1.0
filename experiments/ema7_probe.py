#!/usr/bin/env python3
"""EMA7 特征旁路验证 (2026-08-02, 用户选先验证有效性再正式加特征)

问题: EMA7 日线(周期7)能否为模型提供增量 alpha?
方法:
  1) 对训练宇宙每币每样本日(特征日 j = 样本日-1, 已收盘), 计算 EMA7 指标:
     - ema7_dist  = (close[j] - ema7[j]) / ema7[j]   价格相对 EMA7 偏离
     - ema7_slope = (ema7[j] - ema7[j-1]) / ema7[j-1]  EMA7 斜率
     - ema7_above = 1 if close[j] > ema7[j] else 0     多空排列
  2) 标签(与 aligned 一致): 样本日 ts=j+1, 未来2日收益 = (open[ts+2] - open[ts]) / open[ts]
  3) 统计: Spearman IC / 5分组收益单调性 / 与现有特征(price_position, ret_1d/3d/5d_norm, rsi14)的相关
  4) 只读, 不改生产。
"""
import json
import numpy as np
from scipy.stats import spearmanr

KLINE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
klines = json.load(open(KLINE))['klines']

def ema_series(closes, period=7):
    k = 2.0 / (period + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(out[-1] + k * (c - out[-1]))
    return out

# 收集样本 (特征日 j, 需 j>=30 保证 EMA 稳定 + 20日窗)
feats = {'dist': [], 'slope': [], 'above': [], 'pp': [], 'r1n': [], 'r3n': [], 'r5n': [], 'rsi14': []}
labels = []
n_syms = 0
for sym, kls in klines.items():
    if len(kls) < 100:
        continue
    closes = [k['c'] for k in kls]
    opens = [k['o'] for k in kls]
    emas = ema_series(closes, 7)
    n_syms += 1
    # 只用最近 400 天(与训练窗量级一致), 减少旧 regime 干扰
    start = max(30, len(kls) - 400)
    for j in range(start, len(kls) - 3):
        # 样本日 ts = j+1, 标签 open[ts+2]-open[ts] (对齐 aligned: 入场=预测日开盘, 48h)
        lab = (opens[j + 3] - opens[j + 1]) / opens[j + 1]
        if abs(lab) > 0.5:
            continue
        e = emas[j]
        if e <= 0:
            continue
        feats['dist'].append((closes[j] - e) / e)
        feats['slope'].append((emas[j] - emas[j - 1]) / emas[j - 1])
        feats['above'].append(1.0 if closes[j] > e else 0.0)
        # 现有特征对照
        c20 = closes[j - 19:j + 1]
        feats['pp'].append((closes[j] - min(c20)) / (max(c20) - min(c20)) if max(c20) != min(c20) else 0.5)
        ret1 = (closes[j] - closes[j - 1]) / closes[j - 1]
        ret3 = (closes[j] - closes[j - 3]) / closes[j - 3]
        ret5 = (closes[j] - closes[j - 5]) / closes[j - 5]
        v20 = float(np.std([(closes[x] - closes[x - 1]) / closes[x - 1] for x in range(j - 18, j + 1)]))
        clip = max(v20, 0.002)
        feats['r1n'].append(ret1 / clip)
        feats['r3n'].append(ret3 / (clip * 1.732))
        feats['r5n'].append(ret5 / (clip * 2.236))
        # rsi14 简化(用近似)
        gains = [max(closes[x] - closes[x - 1], 0) for x in range(j - 13, j + 1)]
        losses = [max(closes[x - 1] - closes[x], 0) for x in range(j - 13, j + 1)]
        ag, al = np.mean(gains), np.mean(losses)
        feats['rsi14'].append(100 - 100 / (1 + ag / al) if al > 0 else 100.0)
        labels.append(lab)

labels = np.array(labels)
print(f'样本: {len(labels)} 条, 币种: {n_syms}, 标签均值: {labels.mean()*100:.2f}%')

print(f"\n{'指标':<12}{'Spearman IC':<12}{'方向':<8}{'分组收益(5档, 低→高)':<45}")
print('-' * 90)
for name in ['dist', 'slope', 'above', 'pp', 'r1n', 'r3n', 'r5n', 'rsi14']:
    x = np.array(feats[name])
    ic, p = spearmanr(x, labels)
    # 5 分组
    q = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
    bins = np.digitize(x, q)
    grp = [labels[bins == i].mean() * 100 for i in range(5)]
    # 单调性
    monotone = all(grp[i] <= grp[i + 1] for i in range(4)) or all(grp[i] >= grp[i + 1] for i in range(4))
    sign = '+' if ic > 0 else '-'
    print(f"{name:<12}{ic:<12.4f}{sign:<8}" + ' '.join(f'{g:+6.2f}' for g in grp) + ('  单调' if monotone else ''))

# 冗余度: EMA7 指标 vs 现有特征
print(f"\n=== 冗余度 (EMA7 vs 现有特征 Spearman 相关) ===")
for name in ['pp', 'r1n', 'r3n', 'r5n', 'rsi14']:
    x1 = np.array(feats['dist']); x2 = np.array(feats[name])
    ic, _ = spearmanr(x1, x2)
    print(f"  ema7_dist vs {name:<6}: {ic:+.3f}")
for name in ['r1n', 'r3n', 'r5n']:
    x1 = np.array(feats['slope']); x2 = np.array(feats[name])
    ic, _ = spearmanr(x1, x2)
    print(f"  ema7_slope vs {name:<6}: {ic:+.3f}")
