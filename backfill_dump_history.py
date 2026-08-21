#!/usr/bin/env python3
"""历史出货样本回填 (2026-08-10): 用 5/10 起的 OI 历史 + 日线, 按同一套规则找历史出货日

规则与 altcoin_volume_alert.calc_cost_and_dump 一致:
- 多头增仓日 (ΔOI>0 且 收≥开) VWAP 加权 → 累计成本
- 出货日 = 当日 ΔOI<0 (OI 下降) 且 当日收盘 > 累计成本 (盈利状态平仓)
- 记录: date, sym, oi_circ_pct (当日 OI / 流通量)
写入 data/dump_oi_history.json (与每日监控共用同一文件)
"""
import json, os, sys, datetime as dt
from collections import defaultdict

BASE = '/home/myuser/websocket_new'
sys.path.insert(0, BASE)
import altcoin_volume_alert as ava

KL = ava.KLINES
OI = ava.OI
MCAP = ava.MCAP
DUMP_HIST = ava.DUMP_HIST

kl = json.load(open(KL))['klines']
oi_raw = json.load(open(OI))
mc = json.load(open(MCAP))
circ_map = {k: (v.get('circ') or 0) for k, v in mc.get('coins', {}).items()}
top50 = ava.load_mcap_top(50)

def day_str_ms(ts): return dt.datetime.fromtimestamp(ts/1000, tz=dt.timezone.utc).strftime('%Y-%m-%d')
def day_str_sec(ts): return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime('%Y-%m-%d')

records = []
n_dump_days = 0
for sym, rows in kl.items():
    if sym in top50 or sym not in oi_raw:
        continue
    recs = oi_raw[sym]
    if not isinstance(recs, dict) or len(recs) < 10:
        continue
    oi_by_day = {(dt.datetime.fromtimestamp(int(k), tz=dt.timezone.utc) - dt.timedelta(days=1)).strftime('%Y-%m-%d'): float(v) for k, v in recs.items()}  # 8/13: 快照减一天对齐K线
    kl_by_day = {day_str_ms(r['t']): r for r in rows}
    days = sorted(set(oi_by_day) & set(kl_by_day))
    if len(days) < 10:
        continue
    cost_acc = 0.0
    vol_acc = 0.0
    prev_oi = None
    # 8/11 回滚: 全程累计增仓成本 (不用20天窗口)
    for i, d in enumerate(days):
        r = kl_by_day[d]
        if r['v'] <= 0:
            prev_oi = oi_by_day[d]
            continue
        vwap = r['q'] / r['v']
        oi_v = oi_by_day[d]
        if prev_oi is not None:
            d_oi = oi_v - prev_oi
            if d_oi > 0 and r['c'] >= r['o']:
                cost_acc += d_oi * vwap
                vol_acc += d_oi
            # 出货判定 (8/12 改: TUT 模板 — 先堆积后派发, 与 altcoin_volume_alert 一致)
            elif d_oi < 0 and prev_oi > 0:
                # ① 当天降幅
                drop = -d_oi / prev_oi
                # ② 近10日曾暴增 (堆积证明)
                oi_10d_ago = oi_by_day.get(days[max(0, i-9)], 0)
                peak10 = max(oi_by_day.get(days[j], 0) for j in range(max(0, i-9), i+1))
                has_stack_big = oi_10d_ago > 0 and peak10 / oi_10d_ago >= 1.50
                # ③ 当日涨幅 < 20% (排除空头被轧)
                c4 = (r['c'] / r['o'] - 1) * 100 < 20 if r['o'] > 0 else True
                if drop >= 0.30 and has_stack_big and c4:
                    circ = circ_map.get(sym, 0)
                    # 8/13: 补乘数 + circ下限过滤, 与 altcoin_volume_alert 一致
                    if circ < 10000:
                        prev_oi = oi_v
                        continue
                    mult = 1
                    import re as _re
                    base = sym[:-4] if sym.endswith('USDT') else sym
                    _m = _re.match(r'^(1000000|10000|1000|100)([A-Z0-9]{2,})', base)
                    if _m and not _m.group(2).isdigit():
                        mult = int(_m.group(1))
                    pct = oi_v * mult / circ * 100
                    if circ > 0:  # 8/11: 不再过滤 >100% (OI可真实超流通, 如GUA 321%)
                        records.append({'date': d, 'sym': sym, 'oi_circ_pct': round(pct, 2)})
                        n_dump_days += 1
        prev_oi = oi_v

# 去重 (同一天同币只留一条) + 排序
seen = set()
uniq = []
for rec in sorted(records, key=lambda x: x['date']):
    key = (rec['date'], rec['sym'])
    if key not in seen:
        seen.add(key)
        uniq.append(rec)

# 与现有记录合并 (保留已有的每日新增)
existing = ava.load_dump_hist()
merged = { (r['date'], r['sym']): r for r in existing.get('records', []) }
for rec in uniq:
    merged[(rec['date'], rec['sym'])] = rec
all_recs = sorted(merged.values(), key=lambda x: x['date'])[-365:]

pcts = [r['oi_circ_pct'] for r in all_recs if isinstance(r.get('oi_circ_pct'), (int, float))]
avg = sum(pcts)/len(pcts) if pcts else None

ava.save_dump_hist({'records': all_recs, 'avg_pct': round(avg, 2) if avg else None})
print(f'回填完成: 新增 {len(uniq)} 条历史出货样本 (来自 {n_dump_days} 个出货日), 合计 {len(all_recs)} 条')
print(f'出货基准均值: {avg:.2f}% (n={len(pcts)})')
print('\n样本分布 (前15条):')
for r in all_recs[:15]:
    print(f"  {r['date']}  {r['sym']:<14} OI占流通 {r['oi_circ_pct']:.1f}%")
print(f'\n最近5条:')
for r in all_recs[-5:]:
    print(f"  {r['date']}  {r['sym']:<14} OI占流通 {r['oi_circ_pct']:.1f}%")
