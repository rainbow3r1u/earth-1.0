#!/usr/bin/env python3
"""Regime-IC 日常数据采集 (2026-09-11 立项)

目的: 在上 regime 闸门前, 先稳定沉淀 daily regime 指标与 IC_L/AUC_L/IC_S/AUC_S 的对应关系。
口径:
  - 只用已收盘日K (t < 今日 00:00 UTC)
  - 山寨宇宙: 非BTC且K线>=60根
  - 指标与 5.5b / 四灯同源, 避免口径分叉
输出: data/regime_ic_history.json
用法: python3 audit/regime_ic_log.py          # 幂等重算全部历史
"""
import os, sys, json, datetime
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KLINE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
IC_PATH = os.path.join(BASE, 'data', 'forward_ic_history_48h.json')
FT_PATH = os.path.join(BASE, 'data', 'forward_tracker.json')
OUT = os.path.join(BASE, 'data', 'regime_ic_history.json')
START = '2026-08-03'   # 与 forward_ic / hybrid 影子臂起始日一致


def _iso_day(ts_ms: int) -> str:
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).strftime('%Y-%m-%d')


def load_klines():
    with open(KLINE) as f:
        return json.load(f)['klines']


def build_metrics(kl):
    # 2026-09-12 修复: 原用 date.today()(服务器CST)冒充UTC日期, CST 00:00~07:59 运行时
    # cutoff 多让一天 -> 把未收盘的当日UTC K线当已收盘算指标(9/11行曾因此被部分日数据污染)。
    # 必须用 UTC 当日零点, 保证只纳入 t < 今日00:00Z 的已收盘日K。
    today0 = int(datetime.datetime.combine(datetime.datetime.now(datetime.timezone.utc).date(), datetime.time(), tzinfo=datetime.timezone.utc).timestamp() * 1000)
    btc = next(kl[s] for s in kl if s.startswith('BTCUSDT') and len(kl[s]) > 200)
    btc_rows = [r for r in btc if r['t'] < today0]
    if len(btc_rows) < 65:
        raise SystemExit('BTC K线不足65根')
    dates = [_iso_day(r['t']) for r in btc_rows]
    idx = {d: i for i, d in enumerate(dates)}

    # BTC daily returns / vol / trend
    closes = [r['c'] for r in btc_rows]
    rets = [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]

    # alt universe daily medians / breadth / dispersion
    xs = {r['t']: [] for r in btc_rows}
    for sym, kls in kl.items():
        if sym.startswith('BTCUSDT') or len(kls) < 60:
            continue
        for r in kls:
            if r['t'] in xs and r['o'] > 0:
                xs[r['t']].append((r['c'] / r['o'] - 1) * 100)

    rows = []
    for i, r in enumerate(btc_rows):
        d = dates[i]
        if d < START:
            continue
        a = xs[r['t']]
        alt_med = float(np.median(a)) if len(a) >= 50 else None
        alt_up = float(sum(1 for x in a if x > 0) / len(a) * 100) if a else None
        alt_crash = float(sum(1 for x in a if x < -5) / len(a) * 100) if a else None
        alt_disp = float(np.std(a)) if a else None

        btc_ret = rets[i]
        btc_vol5 = float(np.std(rets[max(0, i - 4): i + 1]))
        btc_cum60 = float(sum(rets[max(0, i - 59): i + 1]))
        ma20 = float(np.mean(closes[max(0, i - 19): i + 1]))
        ma20_dev = (closes[i] / ma20 - 1) * 100 if ma20 else None
        # 21d alt excess vs BTC (median cumulative)
        if i >= 20:
            alt_cum = 0.0
            btc_cum = 0.0
            for j in range(i - 20, i + 1):
                aa = xs[btc_rows[j]['t']]
                if aa:
                    alt_cum = (1 + alt_cum) * (1 + float(np.median(aa)) / 100) - 1
                btc_cum = (1 + btc_cum) * (1 + rets[j] / 100) - 1
            alt_excess_21d = (alt_cum - btc_cum) * 100
        else:
            alt_excess_21d = None

        rows.append({
            'date': d,
            'btc_ret_1d': round(btc_ret, 4),
            'btc_vol5': round(btc_vol5, 4),
            'btc_cum60': round(btc_cum60, 2),
            'btc_ma20_dev': round(ma20_dev, 2) if ma20_dev is not None else None,
            'alt_med_ret': round(alt_med, 4) if alt_med is not None else None,
            'alt_up_pct': round(alt_up, 2) if alt_up is not None else None,
            'alt_crash5_pct': round(alt_crash, 2) if alt_crash is not None else None,
            'alt_disp': round(alt_disp, 4) if alt_disp is not None else None,
            'alt_excess_21d': round(alt_excess_21d, 2) if alt_excess_21d is not None else None,
            'n_alt': len(a),
        })
    return rows


def attach_mae(rows):
    """从 forward_tracker 提取 LONG 侧 MAE 分布 (max_retrace_no_sl 口径)."""
    if not os.path.exists(FT_PATH):
        return rows
    ft = json.load(open(FT_PATH))
    for r in rows:
        day = ft.get(r['date'])
        if not day:
            continue
        vals = [t.get('max_retrace_no_sl') for t in day.get('trades', [])
                if t.get('direction') == 'LONG' and t.get('max_retrace_no_sl') is not None]
        if not vals:
            continue
        vals = sorted(vals)
        r['mae_long_n'] = len(vals)
        r['mae_long_median'] = round(float(np.median(vals)), 2)
        r['mae_long_p90'] = round(float(np.percentile(vals, 90)), 2)
        r['mae_long_gt5_pct'] = round(sum(1 for x in vals if x > 5) / len(vals) * 100, 2)
        r['mae_long_gt8_pct'] = round(sum(1 for x in vals if x > 8) / len(vals) * 100, 2)
        r['mae_long_gt10_pct'] = round(sum(1 for x in vals if x > 10) / len(vals) * 100, 2)
    return rows


def attach_ic(rows):
    if not os.path.exists(IC_PATH):
        return rows
    ic = {e['date']: e for e in json.load(open(IC_PATH))['days']}
    for r in rows:
        e = ic.get(r['date'])
        if e:
            r['ic_long'] = e.get('ic_long')
            r['auc_long'] = e.get('auc_long')
            r['ic_short'] = e.get('ic_short')
            r['auc_short'] = e.get('auc_short')
            r['top1_long_ret'] = e.get('top1_long_ret')
            r['short5_avg_ret'] = e.get('short5_avg_ret')
    return rows


def main(quiet: bool = False):
    kl = load_klines()
    rows = build_metrics(kl)
    rows = attach_ic(rows)
    rows = attach_mae(rows)
    out = {'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'days': rows}
    tmp = f'{OUT}.tmp.{os.getpid()}'   # pid后缀: 防两个钩子/手动并发写同名tmp互踩
    with open(tmp, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    if not quiet:
        print(f'写入 {OUT} 共 {len(rows)} 天')


if __name__ == '__main__':
    main()
