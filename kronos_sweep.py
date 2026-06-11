#!/usr/bin/env python3
"""Kronos 爬坡实验 — 串行跑 Top10/20/50/100/200"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(line_buffering=True)
import daily_predictor as dp

IMP_FILE = os.path.expanduser('~/.local/share/auto_trade/kronos_importance_log.json')
RANK_FILE = '/tmp/kronos_dim_ranking.json'

with open(IMP_FILE) as f: data = json.load(f)
agg = {}; n = 0
for date, models in data.items():
    for label, dims in models.items():
        n += 1
        for k, v in dims.items(): agg[k] = agg.get(k, 0) + v
for k in agg: agg[k] /= n
ranked_dims = [d for d, _ in sorted(agg.items(), key=lambda x: -x[1])]
with open(RANK_FILE, 'w') as f: json.dump({'ranked_dims': ranked_dims}, f)
print(f'排名: {len(ranked_dims)}维, {n}模型聚合', flush=True)

_orig = dp._get_macro_features

for topn in [10, 20, 50, 100, 200]:
    keep = set(ranked_dims[:topn])
    def patched(ts, keep=keep):
        feats = _orig(ts)
        ks = len(feats) - dp.EMBEDDING_DIM - 4
        for i in range(dp.EMBEDDING_DIM):
            if f'kronos_emb_{i}' not in keep:
                feats[ks + i] = 0.0
        return feats
    dp._get_macro_features = patched
    print(f'\n===== Kronos Top{topn} =====', flush=True)
    t0 = time.time()
    dp.dual_backtest(days=30, stride=1)
    elapsed = (time.time()-t0)/60
    # 保存副本防止被覆盖
    import shutil
    src = f'{os.path.dirname(__file__)}/data/dual_backtest_30d.json'
    dst = f'{os.path.dirname(__file__)}/data/kronos_sweep_top{topn}_30d.json'
    shutil.copy(src, dst)
    print(f'Top{topn} 结果已保存: {dst}', flush=True)
    print(f'Top{topn} 耗时: {elapsed:.1f}min', flush=True)

dp._get_macro_features = _orig
print('\n全部完成!', flush=True)
