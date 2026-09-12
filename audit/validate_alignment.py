#!/usr/bin/env python3
"""仪器合格性验证 (AGENTS.md §8.2 第3条): GPU walk-forward TOP10 vs 生产 pred 存档 重合度。
达标线: 重合度 >80% (逐日 |gpu∩prod|/10 的均值与中位)。
用法: python3 audit/validate_alignment.py --cands gate_fixed.json
"""
import json, argparse, glob, re, statistics as st

ap = argparse.ArgumentParser()
ap.add_argument('--cands', required=True)
ap.add_argument('--pred-dir', default='data')
ap.add_argument('--verbose', action='store_true')
a = ap.parse_args()

gpu = {r['day']: set(r['syms']) for r in json.load(open(a.cands))}
loc = {}
for f in sorted(glob.glob(f'{a.pred_dir}/pred_2026-*.json')):
    d = re.search(r'(\d{4}-\d{2}-\d{2})', f).group(1)
    p = json.load(open(f))
    L = p.get('top10_long') or []
    if L:
        loc[d] = set(x['symbol'] for x in L)

common = sorted(set(gpu) & set(loc))
print(f'GPU 候选 {len(gpu)} 天, 生产存档 {len(loc)} 天, 共同 {len(common)} 天')
if not common:
    print('无重叠日'); raise SystemExit(1)
print(f'重叠区间: {common[0]} ~ {common[-1]}')
print()

ov = []
for d in common:
    o = len(gpu[d] & loc[d])
    ov.append((d, o, len(gpu[d] | loc[d])))
vals = [x[1] / min(10, x[2]) for x in ov]
mean, med = st.mean(vals), st.median(vals)
print(f'=== 重合度 (|gpu∩prod| / 10) ===')
print(f'  均值 {mean:.1%}   中位 {med:.1%}')
print(f'  分布: 10/10 的 {sum(1 for v in vals if v == 1.0)} 天 | 8~9/10 的 {sum(1 for v in vals if 0.8 <= v < 1.0)} 天 '
      f'| 5~7/10 的 {sum(1 for v in vals if 0.5 <= v < 0.8)} 天 | <5/10 的 {sum(1 for v in vals if v < 0.5)} 天')
ok = mean > 0.8 and med > 0.8
print()
print(f'判定: {"✅ 仪器合格 (>80%)" if ok else "❌ 仪器不合格 (§8.2 第3条: 禁止用此仪器跑分析)"}')

# 8月专项 (用户指定考场: 纠缠+盈利月, 生产存档齐全)
aug = [x for x in ov if x[0][:7] == '2026-08']
if aug:
    av = [x[1] / min(10, x[2]) for x in aug]
    print(f'\n=== 8月专项 (n={len(aug)}天) ===')
    print(f'  重合度 均值 {st.mean(av):.1%}  中位 {st.median(av):.1%}  '
          f'≥8/10 的 {sum(1 for v in av if v >= 0.8)}/{len(av)} 天')
    print(f'  判定: {"✅ 8月对齐" if st.mean(av) > 0.8 else "❌ 8月不对齐"}')

if a.verbose:
    print()
    for d, o, u in ov:
        mark = '✅' if o >= 8 else ('⚠️' if o >= 5 else '❌')
        only_g = sorted(gpu[d] - loc[d])[:4]
        only_p = sorted(loc[d] - gpu[d])[:4]
        print(f'  {mark} {d}  {o}/10   GPU独有: {only_g} | 生产独有: {only_p}')
