#!/usr/bin/env python3
"""
Kronos Top-N 爬坡实验 — 验证有效维度数

用法:
  python3 ablate_kronos_832.py --kronos-top 10
  python3 ablate_kronos_832.py --kronos-top 50
  python3 ablate_kronos_832.py --kronos-top 0    # baseline
  python3 ablate_kronos_832.py --kronos-top 832  # full
"""
import os, sys, json, time, numpy as np
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(line_buffering=True)
import daily_predictor as dp

# ── 参数 ──
DAYS = 30
STRIDE = 1
TOPN = int(sys.argv[sys.argv.index('--kronos-top') + 1]) if '--kronos-top' in sys.argv else 832

# ── 加载特征重要性排名 ──
IMP_FILE = os.path.expanduser('~/.local/share/auto_trade/kronos_importance_log.json')
RANK_FILE = '/tmp/kronos_dim_ranking.json'

# 聚合排名 (只做一次)
if not os.path.exists(RANK_FILE):
    with open(IMP_FILE) as f:
        data = json.load(f)
    agg = {}
    n = 0
    for date, models in data.items():
        for label, dims in models.items():
            n += 1
            for k, v in dims.items():
                agg[k] = agg.get(k, 0) + v
    for k in agg: agg[k] /= n
    ranked = sorted(agg.items(), key=lambda x: -x[1])
    ranking = {dim: rank for rank, (dim, _) in enumerate(ranked)}
    with open(RANK_FILE, 'w') as f:
        json.dump({'ranking': ranking, 'ranked_dims': [d for d,_ in ranked]}, f)
    ranked_dims = [d for d,_ in ranked]
    print(f'排名已缓存: {RANK_FILE}', flush=True)
else:
    with open(RANK_FILE) as f:
        rank_data = json.load(f)
    ranked_dims = rank_data['ranked_dims']

# ── 确定保留哪些Kronos维度 ──
keep_set = set(ranked_dims[:TOPN]) if TOPN < 832 else set(ranked_dims)
label = f'Kronos Top{TOPN}' if TOPN < 832 else 'FULL (832维)'
if TOPN == 0:
    keep_set = set()
    label = 'BASELINE (0维)'

print(f"\n{'='*60}", flush=True)
print(f" Kronos 爬坡: {label}  days={DAYS} stride={STRIDE}", flush=True)
print(f"{'='*60}\n", flush=True)

# ── Monkey-patch: 只保留TopN维度 ──
_orig_get_macro = dp._get_macro_features
def _patched(ts):
    feats = _orig_get_macro(ts)
    ks = len(feats) - dp.EMBEDDING_DIM - 4
    for i in range(dp.EMBEDDING_DIM):
        dim_name = f'kronos_emb_{i}'
        if dim_name not in keep_set:
            feats[ks + i] = 0.0
    return feats
dp._get_macro_features = _patched

t0 = time.time()
dp.dual_backtest(days=DAYS, stride=STRIDE)
elapsed = (time.time() - t0) / 60
print(f"\n[{label}] 耗时: {elapsed:.1f}min", flush=True)

dp._get_macro_features = _orig_get_macro
