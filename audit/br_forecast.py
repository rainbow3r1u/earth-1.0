#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底率预测 (br_forecast) —— 挂晨报用, 纯只读观测, 不参与任何交易决策。

【它预测什么】
  底率(D) = 全宇宙币中, 从 D 日开盘到 D+2 日收盘 涨幅≥+33% 的币所占百分比。
  这个量决定"第二天市场上有没有值得接的肥尾" —— 荒期(<1.0%)系统必亏, 正常期(≥1.5%)才有正期望。

【怎么预测】(2026-09-16 在 585 天历史上实测选定)
  方法: 滚动 OLS, **用特征的原始值**(不做标准化 —— 实测标准化会把"水平"信息洗掉, 样本外从 +0.177 掉到 +0.111)
  特征(5个, 全部只用 ≤D-2 已完整收盘的日线, 避免用半截 bar):
    disp5   离散度_近5日均   横截面日收益标准差 的 5 日均值
    disp20  离散度_近20日均
    br37    底率_近3~7日均    (lag3 起, 窗口不重叠且已收盘)
    br322   底率_近3~22日均
    btcamp  BTC 前一日振幅
  训练窗口: 过去 180 天滚动 → **每日自动校准**(加入新一天、踢掉最老一天, 权重自动漂移)

【实测性能】(样本外 501 天, 2025-04-30 ~ 2026-09-12)
  滚动OLS 样本外 Spearman +0.177 | 朴素基线(近3~7日底率均) +0.146 | 赢 4/6 季度
  五等分: 最低分位底率 0.99%(荒期) vs 最高分位 1.90%(正常) → 1.9 倍
  ⚠️ 但 2026Q2/Q3 都输给基线 → 脚本内置退化监控

【输出】
  data/br_forecast.jsonl   逐日账本(分数/分位/推算档位/实测底率(2天后回填)/OOS表现)
  控制台/晨报一行摘要 + 最近趋势表

用法: python3 audit/br_forecast.py            # 追加今日预测 + 回填实测 + 打印摘要
      python3 audit/br_forecast.py --report   # 只打印, 不写账本
"""
import json, os, sys, math, statistics, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, '..', 'backtester', 'data_cache', 'notusdt_1d_full.json')
LEDGER = os.path.join(BASE, 'data', 'br_forecast.jsonl')
WIN = 180          # 滚动训练窗口(天)
MINW = 60          # 最少训练样本
LAG = 2            # 特征只用 ≤ D-LAG 的日线(避开半截bar)
FE = ['disp5', 'disp20', 'br37', 'br322', 'btcamp']
FE_CN = {'disp5': '离散度5日', 'disp20': '离散度20日', 'br37': '底率3~7日',
         'br322': '底率3~22日', 'btcamp': 'BTC振幅'}

def load_universe():
    path = os.path.normpath(CACHE)
    if not os.path.exists(path):
        path = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    KL = json.load(open(path))['klines']
    K = {}
    for s, rows in KL.items():
        if len(rows) >= 80:
            K[s] = {datetime.datetime.fromtimestamp(r['t']/1000, tz=datetime.timezone.utc).strftime('%Y-%m-%d'): r
                    for r in rows}
    return K

def build(K, now_ms):
    ALLD = sorted({d for m in K.values() for d in m})
    DIDX = {d: i for i, d in enumerate(ALLD)}
    # 已完整收盘的最后一天: 该 UTC 日已结束
    closed = [d for d in ALLD
              if DIDX[d] is not None and
              datetime.datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp()*1000 + 86400000 <= now_ms]
    last = closed[-1]
    # 每日横截面
    D = {}
    for d in ALLD:
        R = []
        for s, m in K.items():
            b = m.get(d)
            if b and b['o'] > 0:
                R.append((b['c']/b['o']-1)*100)
        D[d] = R
    def br_of(d):
        i = DIDX.get(d)
        if i is None or i+2 >= len(ALLD): return None
        d2 = ALLD[i+2]
        if d2 not in closed: return None
        n = hit = 0
        for s, m in K.items():
            if d in m and d2 in m and m[d]['o'] > 0:
                n += 1
                if m[d2]['c']/m[d]['o']-1 >= 0.33: hit += 1
        return hit/n*100 if n >= 300 else None
    BR = {d: br_of(d) for d in ALLD}
    BR = {k: v for k, v in BR.items() if v is not None}
    def prev(d, k=1):
        i = DIDX[d]-k
        return ALLD[i] if i >= 0 else None
    def amp(b): return (b['h']-b['l'])/b['o']*100 if b['o'] > 0 else None
    def feats(d):
        """特征: 只用 ≤ d-LAG 的日线"""
        ref = prev(d, LAG)
        if ref is None: return None
        f = {}
        for W, key in ((5, 'disp5'), (20, 'disp20')):
            vs = [statistics.pstdev(D[prev(ref, k)]) for k in range(0, W)
                  if prev(ref, k) and len(D[prev(ref, k)]) > 300]
            if len(vs) < W-1: return None
            f[key] = statistics.mean(vs)
        v = [BR.get(prev(ref, k)) for k in range(3, 8)]
        v = [x for x in v if x is not None]
        if len(v) < 4: return None
        f['br37'] = statistics.mean(v)
        v = [BR.get(prev(ref, k)) for k in range(3, 23)]
        v = [x for x in v if x is not None]
        if len(v) < 15: return None
        f['br322'] = statistics.mean(v)
        b = K['BTCUSDT'].get(ref)
        if not b: return None
        f['btcamp'] = amp(b)
        return f
    return ALLD, DIDX, D, BR, closed, last, feats

def ols(X, y):
    """最小二乘(含截距), 正规方程 + 部分主元高斯消元"""
    n = len(X); m = len(X[0])+1
    A = [[1.0]+list(r) for r in X]
    M = [[sum(A[k][i]*A[k][j] for k in range(n)) for j in range(m)] for i in range(m)]
    v = [sum(A[k][i]*y[k] for k in range(n)) for i in range(m)]
    for i in range(m):
        p = max(range(i, m), key=lambda r: abs(M[r][i]))
        if abs(M[p][i]) < 1e-12: return None
        M[i], M[p] = M[p], M[i]; v[i], v[p] = v[p], v[i]
        for r in range(i+1, m):
            fct = M[r][i]/M[i][i]
            for c in range(i, m): M[r][c] -= fct*M[i][c]
            v[r] -= fct*v[i]
    b = [0.0]*m
    for i in range(m-1, -1, -1):
        b[i] = (v[i]-sum(M[i][j]*b[j] for j in range(i+1, m)))/M[i][i]
    return b

def pear(x, y):
    n = len(x); mx = sum(x)/n; my = sum(y)/n
    sx = math.sqrt(sum((a-mx)**2 for a in x)); sy = math.sqrt(sum((b-my)**2 for b in y))
    return sum((a-mx)*(b-my) for a, b in zip(x, y))/(sx*sy) if sx*sy else float('nan')
def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i]); r = [0]*len(v)
    for p, i in enumerate(o): r[i] = p+1
    return r
def spear(x, y): return pear(rank(x), rank(y))

def bucket_of(v):
    return '荒期' if v < 1.0 else ('偏紧' if v < 1.5 else '正常')

def main(report_only=False):
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp()*1000)
    K = load_universe()
    ALLD, DIDX, D, BR, closed, last, feats = build(K, now_ms)
    # 构造训练样本(每个有底率的日子 + 特征齐全)
    rows = []
    for d in sorted(BR):
        if d < '2025-03-01': continue
        f = feats(d)
        if not f: continue
        rows.append({'date': d, 'y': BR[d], **f})
    if len(rows) < MINW + 30:
        print(f'[br_forecast] 样本不足({len(rows)}), 跳过'); return
    # ── 滚动 OLS: 逐日预测(样本外)+ 当日预测 ──
    preds = []
    for t in range(len(rows)):
        r = rows[t]; win = rows[max(0, t-WIN):t]
        if len(win) < MINW: continue
        b = ols([[w[k] for k in FE] for w in win], [w['y'] for w in win])
        if not b: continue
        preds.append({'date': r['date'], 'score': b[0]+sum(b[i+1]*r[k] for i, k in enumerate(FE)),
                      'actual': r['y'], 'naive': r['br37'], 'n_win': len(win)})
    # 当日预测: 目标是【今天】(因为今天 08:21 要开仓), 特征只用 ≤ 今天-2 的日线
    today = None
    tgt = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    for d in [tgt] + [x for x in reversed(ALLD) if x <= last]:
        f = feats(d)
        if f is None: continue
        win = rows[-WIN:]
        b = ols([[w[k] for k in FE] for w in win], [w['y'] for w in win])
        if b:
            today = {'date': d, 'score': b[0]+sum(b[i+1]*f[k] for i, k in enumerate(FE)),
                     'feat': f, 'coef': {'intercept': b[0], **{FE[i]: b[i+1] for i in range(len(FE))}},
                     'n_win': len(win)}
        break
    if today is None:
        print('[br_forecast] 无可用特征, 跳过'); return

    # 分位 + 档位
    sc = sorted(p['score'] for p in preds)
    pct = sum(1 for v in sc if v <= today['score'])/len(sc)*100
    # 把分数映射到"同分数区间历史实测底率均值"
    lo_i, hi_i = max(0, int(len(sc)*0.20)-1), min(len(sc)-1, int(len(sc)*0.80))
    same = [p['actual'] for p in preds if sc[lo_i] <= p['score'] <= sc[hi_i]]
    lowq = [p['actual'] for p in preds if p['score'] <= sc[int(len(sc)*0.20)]]
    highq = [p['actual'] for p in preds if p['score'] >= sc[int(len(sc)*0.80)]]
    # OOS 评估
    oos = spear([p['score'] for p in preds], [p['actual'] for p in preds])
    oos_naive = spear([p['naive'] for p in preds], [p['actual'] for p in preds])
    # 滚动 90 天退化监控
    rec = preds[-90:]
    rec_oos = spear([p['score'] for p in rec], [p['actual'] for p in rec])
    rec_nai = spear([p['naive'] for p in rec], [p['actual'] for p in rec])
    degraded = rec_oos < rec_nai and rec_oos < 0.05

    # 最近趋势
    def mean_of(ps, key): 
        return statistics.mean([p[key] for p in ps]) if ps else float('nan')
    last7, prev7 = preds[-7:], preds[-14:-7]
    act7 = [p['actual'] for p in last7]; actp7 = [p['actual'] for p in prev7]

    print('='*104)
    print('【底率预测 br_forecast】只读观测, 不参与交易决策')
    print('='*104)
    print(f"  数据: 本地日线缓存 | 训练样本 {len(rows)} 天 ({rows[0]['date']} ~ {rows[-1]['date']}) | 滚动窗口 {WIN} 天")
    print(f"  已完整收盘的最后一天: {last} | 特征用至: {last} 往前推 {LAG} 天")
    print(f"\n  ★ 今日({today['date']})预测: 底率 ≈ {today['score']:.2f}%   (历史分位 {pct:.0f}%)")
    print(f"    推算档位: {bucket_of(today['score'])}   " +
          f"(若落在历史最低20%区间, 历史实测底率均 {statistics.mean(lowq):.2f}%; 最高20%区间 {statistics.mean(highq):.2f}%)")
    print(f"    特征: " + ' | '.join(f"{FE_CN[k]}={today['feat'][k]:.2f}" for k in FE))
    print(f"\n  【样本外表现】(逐日滚动预测, {len(preds)} 天)")
    print(f"    滚动OLS Spearman {oos:+.3f}  vs  朴素基线(近3~7日底率均) {oos_naive:+.3f}   " +
          ('✅ 赢基线' if oos > oos_naive else '❌ 输基线'))
    print(f"    最近90天: OLS {rec_oos:+.3f} vs 基线 {rec_nai:+.3f}   " +
          ('🔴 已退化(近90天输基线且<0.05)' if degraded else '正常'))
    print(f"\n  【最近趋势】")
    print(f"    预测分  近7日均 {mean_of(last7,'score'):.2f}%  前7日均 {mean_of(prev7,'score'):.2f}%   " +
          f"→ {'上升' if mean_of(last7,'score') > mean_of(prev7,'score') else '下降'}")
    print(f"    实测底率 近7日均 {statistics.mean(act7):.2f}%  前7日均 {statistics.mean(actp7):.2f}%   " +
          f"→ {'上升' if statistics.mean(act7) > statistics.mean(actp7) else '下降'}")
    print(f"\n  {'日期':<12}{'预测分':>8}{'分位':>7}{'推算':>7}{'实测底率':>10}{'实测档位':>9}   近14天逐日")
    for p in preds[-14:]:
        pc = sum(1 for v in sc if v <= p['score'])/len(sc)*100
        print(f"  {p['date']:<12}{p['score']:>8.2f}{pc:>6.0f}%{bucket_of(p['score']):>7}{p['actual']:>10.2f}{bucket_of(p['actual']):>9}")
    # 汇总一行(给晨报)
    line = (f"🔮 底率预测(只读): {today['date']} 预测 {today['score']:.2f}% (分位{pct:.0f}%, {bucket_of(today['score'])}) | "
            f"实测近7日均 {statistics.mean(act7):.2f}% ({bucket_of(statistics.mean(act7))}) | "
            f"趋势 预测{'↑' if mean_of(last7,'score') > mean_of(prev7,'score') else '↓'} 实测{'↑' if statistics.mean(act7) > statistics.mean(actp7) else '↓'} | "
            f"样本外ρ {oos:+.3f} vs 基线 {oos_naive:+.3f}" + ("  🔴近90天已退化" if degraded else ""))
    print('\n' + line)

    if not report_only:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        rec_line = {'date': today['date'], 'ts': now_ms, 'score': round(today['score'], 4),
                    'pct': round(pct, 1), 'bucket': bucket_of(today['score']),
                    'feat': {k: round(today['feat'][k], 4) for k in FE},
                    'coef': {k: round(v, 6) for k, v in today['coef'].items()},
                    'n_train': today['n_win'], 'oos_rho': round(oos, 4),
                    'oos_naive': round(oos_naive, 4), 'oos_rho_90d': round(rec_oos, 4),
                    'degraded': degraded,
                    'low_q_actual_mean': round(statistics.mean(lowq), 4),
                    'high_q_actual_mean': round(statistics.mean(highq), 4)}
        with open(LEDGER, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec_line, ensure_ascii=False) + '\n')
        print(f"\n  ✅ 已写入账本 {LEDGER}")
        # 摘要快照(给晨报快速读取, 免重算)
        summ = {
            'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'target_date': today['date'], 'score': round(today['score'], 3),
            'pct': round(pct, 1), 'bucket': bucket_of(today['score']),
            'low_q_mean': round(statistics.mean(lowq), 3),
            'high_q_mean': round(statistics.mean(highq), 3),
            'oos_rho': round(oos, 4), 'oos_naive': round(oos_naive, 4),
            'oos_rho_90d': round(rec_oos, 4), 'oos_naive_90d': round(rec_nai, 4),
            'degraded': degraded,
            'n_train': len(rows), 'n_pred': len(preds),
            'pred_7d': round(mean_of(last7, 'score'), 3), 'pred_prev7d': round(mean_of(prev7, 'score'), 3),
            'act_7d': round(statistics.mean(act7), 3), 'act_prev7d': round(statistics.mean(actp7), 3),
            'act_7d_bucket': bucket_of(statistics.mean(act7)),
            'recent': [{'date': p2['date'], 'score': round(p2['score'], 3),
                        'bucket': bucket_of(p2['score']), 'actual': round(p2['actual'], 3),
                        'actual_bucket': bucket_of(p2['actual'])} for p2 in preds[-14:]],
            'feat': {k: round(today['feat'][k], 3) for k in FE},
        }
        sp = os.path.join(BASE, 'data', 'br_forecast_summary.json')
        json.dump(summ, open(sp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"  ✅ 已写入摘要 {sp}")

def emit_line():
    """晨报用: 读摘要文件输出一行(不重算, 秒出)"""
    sp = os.path.join(BASE, 'data', 'br_forecast_summary.json')
    if not os.path.exists(sp):
        print('🔮 底率预测: (摘要未生成, 08:00 cron 跑了吗? 见 logs/br_forecast.log)')
        return
    d = json.load(open(sp, encoding='utf-8'))
    B2 = {'荒期': '荒', '偏紧': '偏', '正常': '正'}
    # 近14日实测档位序列(左=最老, 右=最新)
    tr = ''.join(B2.get(r['actual_bucket'], '?') for r in d.get('recent', []))
    arr = '↑' if d['act_7d'] > d['act_prev7d'] else '↓'
    deg = '  🔴近90天已退化' if d.get('degraded') else ''
    print(f"🔮 底率预测(只读): {d['target_date'][5:]} 预测 {d['score']:.2f}% "
          f"(分位{d['pct']:.0f}%·{d['bucket']}) | 实测近7日均 {d['act_7d']:.2f}% ({d['act_7d_bucket']},{arr}) "
          f"vs 前7日 {d['act_prev7d']:.2f}% | 近14日实测档位(旧→新) [{tr}] "
          f"| 样本外ρ {d['oos_rho']:+.3f} vs 基线 {d['oos_naive']:+.3f}{deg}")

if __name__ == '__main__':
    if '--line' in sys.argv:
        emit_line()
    else:
        main('--report' in sys.argv)
