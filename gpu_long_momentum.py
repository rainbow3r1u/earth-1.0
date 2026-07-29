#!/usr/bin/env python3
"""LONG 追涨方向分析 (GPU)
回答:
  Q1 LONG alpha 在哪个动量区间? (stk连涨 × pp20日位置 × 昨日涨幅 的基准命中率曲面)
  Q2 模型为什么选底部币? (Top1 picks 的动量分布 + 分区间校准: 模型概率 vs 实际频率)
  Q3 现闸门(stk≥2,pp>0.7)是最优吗? (多组闸门变体 WF 对打 P@1)
"""
import os, sys, json, glob, time
import numpy as np
from xgboost import XGBClassifier

HOME = os.path.expanduser('~')
CACHE_DIR = f'{HOME}/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund'
KLINE_CACHE = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
WF_DAYS = 180; TRAIN_WINDOW = 180
XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  min_child_weight=1, reg_lambda=10, reg_alpha=10,
                  subsample=0.8, colsample_bytree=0.6, device='cuda', verbosity=0)

def log(m): print(f'[{time.strftime("%H:%M:%S")}] {m}', flush=True)
def prep(X):
    X = np.nan_to_num(X.astype(np.float32), nan=0.0)
    X[:, 100:932] = 0.0; X[:, 72:91] = 0.0
    return X

files = sorted(glob.glob(f'{CACHE_DIR}/*.npz'))
sdays = [int(os.path.basename(f).replace('.npz', '')) for f in files]

# ===== Q1: 基准命中率曲面 (近400天) =====
log('Q1: 动量区间基准命中率 (近400天)...')
# feat索引: 5=pp(20日位置) 7=stk(连涨天数); label0=LONG(2日≥5%)
buckets = {}
for f in files[-400:]:
    d = np.load(f)
    feats, labels = d['feats'], d['labels'][:, 0].astype(np.int32)
    pp = feats[:, 5]; stk = feats[:, 7]
    for sk_lo, sk_hi, sk_name in [(0, 1, 'stk0-1'), (2, 2, 'stk2'), (3, 99, 'stk3+')]:
        for p_lo, p_hi, p_name in [(0, .3, 'pp<.3'), (.3, .5, '.3-.5'), (.5, .7, '.5-.7'), (.7, .9, '.7-.9'), (.9, 1.01, '>.9')]:
            m = (stk >= sk_lo) & (stk <= sk_hi) & (pp >= p_lo) & (pp < p_hi)
            n = int(m.sum())
            if n:
                k = (sk_name, p_name)
                buckets.setdefault(k, [0, 0])
                buckets[k][0] += int(labels[m].sum()); buckets[k][1] += n
print(f'{"":>8}' + ''.join(f'{p:>12}' for p in ['pp<.3', '.3-.5', '.5-.7', '.7-.9', '>.9']))
for sk in ['stk0-1', 'stk2', 'stk3+']:
    row = f'{sk:>8}'
    for p in ['pp<.3', '.3-.5', '.5-.7', '.7-.9', '>.9']:
        h, n = buckets.get((sk, p), [0, 0])
        row += f'{h}/{n}({h/max(1,n)*100:.0f}%)'.rjust(12)
    print(row)

# ===== Q2+Q3: WF 校准 + Top1 动量分布 + 闸门变体 =====
log('Q2/Q3: WF 180天 (每日1个LONG模型, cuda)...')
start = max(30, len(sdays) - WF_DAYS - 1)
eval_days = sdays[start:]
cal = {}          # (bucket) -> [prob_sum, hit_sum, n]
top1_mom, top1_bot = 0, 0
gates = {  # 闸门变体: name -> fn(stk, pp)
    '现闸门 stk2+ pp>.7': lambda s, p: s >= 2 and p > 0.7,
    'G1 stk2+ pp>.6':     lambda s, p: s >= 2 and p > 0.6,
    'G2 stk3+ pp>.6':     lambda s, p: s >= 3 and p > 0.6,
    'G3 stk2+ pp>.8':     lambda s, p: s >= 2 and p > 0.8,
    'G4 stk1+ pp>.5':     lambda s, p: s >= 1 and p > 0.5,
}
gate_stats = {g: [0, 0] for g in gates}   # hits, n (P@1)
gate_stats['无闸门'] = [0, 0]
t0 = time.time()
for di, pred_ts in enumerate(eval_days):
    d_idx = sdays.index(pred_ts)
    train_ts = sdays[max(0, d_idx - TRAIN_WINDOW):d_idx - 2]
    Xg, yg = [], []
    for ts in train_ts:
        fp = f'{CACHE_DIR}/{ts}.npz'
        if not os.path.exists(fp): continue
        dd = np.load(fp)
        Xg.append(dd['feats']); yg.append(dd['labels'][:, 0])
    if not Xg: continue
    Xg = prep(np.concatenate(Xg)); yg = np.concatenate(yg).astype(np.int32)
    pg = int(yg.sum())
    if pg < 5: continue
    m = XGBClassifier(**XGB_PARAMS, scale_pos_weight=(len(yg) - pg) / pg, random_state=42)
    m.fit(Xg, yg)

    fp = f'{CACHE_DIR}/{pred_ts}.npz'
    if not os.path.exists(fp): continue
    dd = np.load(fp)
    Xp = prep(dd['feats']); yp = dd['labels'][:, 0].astype(np.int32)
    pp_ = Xp[:, 5]; stk_ = Xp[:, 7]
    prob = m.predict_proba(Xp)[:, 1]

    # 校准: 分区间 mean(prob) vs 实际命中率
    for b_lo, b_hi in [(0, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]:
        for sk_lo, sk_hi, skn in [(0, 1, 'lo'), (2, 99, 'hi')]:
            mm = (pp_ >= b_lo) & (pp_ < b_hi) & (stk_ >= sk_lo) & (stk_ <= sk_hi)
            if mm.sum() >= 5:
                k = (skn, f'{b_lo}-{b_hi}')
                cal.setdefault(k, [0.0, 0, 0])
                cal[k][0] += float(prob[mm].sum()); cal[k][1] += int(yp[mm].sum()); cal[k][2] += int(mm.sum())

    # Top1 的动量归属
    i1 = int(np.argmax(prob))
    if stk_[i1] >= 2 and pp_[i1] > 0.7: top1_mom += 1
    else: top1_bot += 1

    # 闸门变体 P@1: 各闸门内取 prob 最高者
    for gname, gfn in gates.items():
        mask = np.array([gfn(s, p) for s, p in zip(stk_, pp_)])
        if mask.sum() == 0: continue
        sub = np.where(mask)[0]
        ib = sub[int(np.argmax(prob[sub]))]
        gate_stats[gname][0] += int(yp[ib]); gate_stats[gname][1] += 1
    i_no = int(np.argmax(prob))
    gate_stats['无闸门'][0] += int(yp[i_no]); gate_stats['无闸门'][1] += 1

    if (di + 1) % 30 == 0:
        log(f'  {di+1}/{len(eval_days)} ({time.time()-t0:.0f}s)')

print('\n' + '=' * 70)
print(f'Q2a 模型分区间校准 (mean概率 vs 实际命中率, WF {len(eval_days)}天)')
for k in sorted(cal.keys()):
    ps_, hs, n = cal[k]
    if n >= 100:
        print(f'  stk={k[0]:>3} pp={k[1]:>9}: 模型{ps_/n*100:.1f}% vs 实际{hs/n*100:.1f}% (n={n})  偏差{(ps_-hs)/n*100:+.1f}%')
print(f'\nQ2b 通用模型 Top1 的动量归属: 高动量 {top1_mom} 天 vs 其他(底部/震荡) {top1_bot} 天')
print(f'    → 模型主动追高比例仅 {top1_mom/max(1,top1_mom+top1_bot)*100:.0f}%')
print(f'\nQ3 闸门变体对打 (P@1 命中率 / 有信号天数):')
for gname, (h, n) in sorted(gate_stats.items(), key=lambda x: -x[1][0]/max(1, x[1][1])):
    print(f'  {gname:>18}: {h}/{n} = {h/max(1,n)*100:.1f}%命中  (覆盖{n}/{len(eval_days)}天)')
print('=' * 70)
