#!/usr/bin/env python3
"""特征健康报告 (GPU, 分支③) — 分析 by_day 缓存, 找出死特征/极端值列
输出: 控制台报告 + prune_cols.json (全零/常数列清零清单)
判据: 全零率==100% 或 标准差==0 → 死列(可清); >99.9%零 → 观察名单(可能是合法稀疏二值特征, 不自动清)
"""
import os, sys, json, glob
import numpy as np

HOME = os.path.expanduser('~')
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60  # 分析最近N天样本

files = sorted(glob.glob(f'{CACHE_DIR}/*.npz'))[-DAYS:]
print(f'分析 {len(files)} 天样本: {os.path.basename(files[0])} → {os.path.basename(files[-1])}')

Xs = []
for f in files:
    d = np.load(f)
    Xs.append(d['feats'].astype(np.float32))
X = np.concatenate(Xs)
X = np.nan_to_num(X, nan=0.0)
X[:, 100:932] = 0.0   # 复现训练时 Kronos 置零
X[:, 72:91] = 0.0     # 复现训练时 liq 置零
n, dim = X.shape
print(f'样本矩阵: {n} x {dim}')

zero_rate = (X == 0).mean(axis=0)
std = X.std(axis=0)
extreme_rate = (np.abs(X) > 1e6).mean(axis=0)

dead = sorted(set(np.where(zero_rate >= 0.999999)[0]) | set(np.where(std == 0)[0]))
near_dead = [i for i in range(dim) if zero_rate[i] > 0.999 and i not in dead]
extreme_cols = [(i, float(extreme_rate[i])) for i in range(dim) if extreme_rate[i] > 0.001]

# 排除生产已置零的区域 (100:932 Kronos, 72:91 liq) — 它们不算"新发现"
known_zeroed = set(range(100, 932)) | set(range(72, 91))
dead_new = [int(i) for i in dead if i not in known_zeroed]
near_dead_new = [i for i in near_dead if i not in known_zeroed]
extreme_new = [(i, r) for i, r in extreme_cols if i not in known_zeroed]

print(f'\n== 死列 (全零/常数): 共{len(dead)}个, 其中活跃区新发现 {len(dead_new)} 个 ==')
print(dead_new)
print(f'\n== 准死列 (>99.9%零, 观察名单, 可能是合法稀疏特征): {len(near_dead_new)} 个 ==')
print(near_dead_new)
print(f'\n== 极端值列 (|x|>1e6 比例>0.1%): {len(extreme_new)} 个 ==')
for i, r in extreme_new:
    print(f'  col {i}: {r*100:.2f}% 极端')

# gain importance 尾部 (活跃区)
from xgboost import XGBClassifier
d0 = np.load(files[-1])
# 用全部天的 LONG 标签训一个快模型看重要性
ys = []
for f in files:
    d = np.load(f)
    ys.append(d['labels'][:, 0])
y = np.concatenate(ys).astype(np.int32)
pos = int(y.sum())
m = XGBClassifier(n_estimators=100, max_depth=6, learning_rate=0.1,
                  subsample=0.8, colsample_bytree=0.6, device='cpu',
                  scale_pos_weight=(len(y)-pos)/pos, random_state=42, verbosity=0)
print('\n训练快模型取 gain 重要性 (100树, 仅评估用)...')
m.fit(X, y)
imp = m.feature_importances_
active = [i for i in range(dim) if i not in known_zeroed and i not in dead]
ranked = sorted(active, key=lambda i: imp[i])
print(f'\n== 活跃区 importance 尾部 30 (增益最低) ==')
print([(int(i), round(float(imp[i]), 6)) for i in ranked[:30]])
print(f'\n== 活跃区 importance 头部 15 ==')
print([(int(i), round(float(imp[i]), 6)) for i in ranked[-15:][::-1]])

out = f'{HOME}/websocket_new/prune_cols.json'
with open(out, 'w') as f:
    json.dump(dead_new, f)
print(f'\n死列清单已写: {out} ({len(dead_new)}列) — PRUNE_COLS={out} 可跑清零实验')
