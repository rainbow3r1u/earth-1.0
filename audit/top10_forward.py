#!/usr/bin/env python3
"""TOP10 多空全开前向结算 (1m口径) — 供晨报邮件板块使用
口径:
  - 入场: 预测日 00:21 UTC (北京 08:21) 开盘价, LONG/SHORT TOP10 全开
  - SL -5% / TP +10% / 48h到期(按到期1m开盘价), SHORT反向
  - 历史日结果缓存于 data/top10_forward_cache.json, 每天只增量结算新到期的天
  - 成本(手续费+滑点)由调用方备注, 本模块输出毛收益
独立CLI: python3 top10_forward.py  (结算并打印汇总表)
"""
import os, json, time
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
PRED_DIR = os.path.join(WS, 'data')
CACHE_FILE = os.path.join(WS, 'data', 'top10_forward_cache.json')
START_DATE = '2026-08-03'

SL_PCT, TP_PCT = -5.0, 10.0
HOLD_MS = 48 * 3600 * 1000


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    tmp = CACHE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_FILE)


def fetch_1m(sym, start_ms, end_ms):
    """分页拉取1m K线, 返回 [[ts,o,h,l,c],...]"""
    out = []
    s = start_ms
    while s < end_ms:
        e = min(end_ms, s + 999 * 60000)
        try:
            r = requests.get('https://fapi.binance.com/fapi/v1/klines',
                             params={'symbol': sym, 'interval': '1m',
                                     'startTime': s, 'endTime': e, 'limit': 1000},
                             timeout=15)
            js = r.json()
        except Exception:
            js = []
        if isinstance(js, dict):
            js = []
        for k in js:
            out.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])])
        if len(js) < 900:
            break
        s = e + 1
        time.sleep(0.12)
    return out


def settle_one(sym, side, entry_ts):
    """结算单笔: 返回 (state, pnl_pct) state: TP/SL/EXP48/NO_DATA"""
    k1 = fetch_1m(sym, entry_ts, entry_ts + HOLD_MS + 2 * 3600 * 1000)
    k1 = [k for k in k1 if k[0] >= entry_ts]
    if not k1:
        return 'NO_DATA', None
    ep = k1[0][1]
    if ep <= 0:
        return 'NO_DATA', None
    end_ts = entry_ts + HOLD_MS
    for ts, o, h, l, c in k1:
        if ts > end_ts:
            break
        if side == 'LONG':
            if l <= ep * (1 + SL_PCT / 100):
                return 'SL', SL_PCT
            if h >= ep * (1 + TP_PCT / 100):
                return 'TP', TP_PCT
        else:
            if h >= ep * (1 + 5.0 / 100):
                return 'SL', SL_PCT
            if l <= ep * (1 - 10.0 / 100):
                return 'TP', TP_PCT
    # 48h到期: 取到期那根1m的开盘价
    for ts, o, h, l, c in k1:
        if ts >= end_ts:
            p = o
            break
    else:
        # 数据未覆盖到期时刻(48h未满): 不定结算, 防止提前回填脏数据
        return 'NO_DATA', None
    ret = (p / ep - 1) * 100 if side == 'LONG' else (ep / p - 1) * 100
    return 'EXP48', round(ret, 2)


def pred_path(date_str):
    return os.path.join(PRED_DIR, f'pred_{date_str}.json')


def pending_days(now=None):
    """8/3起、48h已到期的日期(入场+48h <= now), 返回需要结算的日期列表"""
    if now is None:
        now = datetime.now(timezone.utc)
    days = []
    d = datetime.strptime(START_DATE, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    while True:
        ds = d.strftime('%Y-%m-%d')
        if ds > now.strftime('%Y-%m-%d'):
            break
        # 入场 00:21 UTC + 48h = 第3天 00:21 UTC 到期
        maturity = d + timedelta(days=2, minutes=21)
        if maturity <= now and os.path.exists(pred_path(ds)):
            days.append(ds)
        d += timedelta(days=1)
    return days


def settle_day(date_str):
    """结算某天TOP10多空全开, 返回 {'long':[...], 'short':[...]} 或 None"""
    try:
        pred = json.load(open(pred_path(date_str)))
    except Exception:
        return None
    entry = int(datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                .timestamp() * 1000) + (21 * 60 * 1000)  # 00:21 UTC
    res = {'long': [], 'short': []}
    for key, side in (('top10_long', 'LONG'), ('top10_short', 'SHORT')):
        for item in (pred.get(key) or []):
            sym = item.get('symbol', '')
            st, pnl = settle_one(sym, side, entry)
            res[side.lower()].append({'sym': sym, 'prob': item.get('prob'),
                                      'state': st, 'pnl': pnl})
    return res


def update():
    """增量结算: 返回完整 cache dict"""
    cache = load_cache()
    for ds in pending_days():
        if ds in cache:
            continue
        r = settle_day(ds)
        if r is None:
            continue
        cache[ds] = r
        save_cache(cache)
    return cache


def agg(cache):
    """聚合: 返回 {'LONG': {...}, 'SHORT': {...}, 'ALL': {...}}
    每组: trades, set, tp, sl, exp, avg, cum(逐日等权复利%)"""
    daily = {}  # date -> {'LONG': [pnl...], 'SHORT': [pnl...]}
    for ds, r in sorted(cache.items()):
        for side in ('long', 'short'):
            pnls = [t['pnl'] for t in r[side] if t['pnl'] is not None]
            if pnls:
                daily.setdefault(ds, {})[side.upper()] = pnls
    out = {}
    for side in ('LONG', 'SHORT', 'ALL'):
        trades = setn = tp = sl = exp = 0
        sums = 0.0
        cum = 1.0
        days = 0
        for ds in sorted(daily):
            if side == 'ALL':
                pnls = daily[ds].get('LONG', []) + daily[ds].get('SHORT', [])
            else:
                pnls = daily[ds].get(side, [])
            if not pnls:
                continue
            trades += len(pnls)
            sums += sum(pnls)
            days += 1
            cum *= (1 + sum(pnls) / len(pnls) / 100)
        # tp/sl/exp 逐笔统计
        for ds, r in cache.items():
            sides = ('long', 'short') if side == 'ALL' else (side.lower(),)
            for s in sides:
                for t in r.get(s, []):
                    if t['pnl'] is None:
                        continue
                    setn += 1
                    if t['state'] == 'TP':
                        tp += 1
                    elif t['state'] == 'SL':
                        sl += 1
                    elif t['state'] == 'EXP48':
                        exp += 1
        out[side] = {'trades': trades, 'settled': setn, 'tp': tp, 'sl': sl,
                     'exp': exp, 'days': days,
                     'avg': round(sums / trades, 2) if trades else 0.0,
                     'cum': round((cum - 1) * 100, 1)}
    return out


def main():
    cache = update()
    a = agg(cache)
    print(f"{'组':<8}{'笔数':>6}{'TP':>5}{'SL':>5}{'48h':>5}{'笔均%':>8}{'累计复利%':>10}")
    for side in ('LONG', 'SHORT', 'ALL'):
        g = a[side]
        print(f"{side:<8}{g['settled']:>6}{g['tp']:>5}{g['sl']:>5}{g['exp']:>5}"
              f"{g['avg']:>8.2f}{g['cum']:>10.1f}")
    print("(1m口径 SL-5%/TP+10%/48h, 毛收益未扣0.2%成本)")


if __name__ == '__main__':
    main()
