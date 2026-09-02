#!/usr/bin/env python3
"""48h vs 72h 终点重算实验 (2026-09-02)
数据: data/hybrid_tracker.json 8/3起620笔已结算交易 (主臂混合结构)
方法: 对每笔单重新拉1m K线, 同规则重放两种持有窗口:
  48h口径 = 现行结算 (tracker已存net_u, 作为基准)
  72h口径 = LONG无止盈SL5%持有到t0+3天08:00终点; SHORT TP10/SL5%(触发即出, 不到期持到72h终点)
  LONG的SL在前48h触发过就触发(两口径一致), 差异只来自"第3天"
输出: 分侧对比总U + 逐日差异 + 尾部分析
纯只读实验: 不改任何生产文件
"""
import os, sys, json, time
import requests

S = requests.Session()
DAY_MS = 86400000
NOTIONAL = 300.0

def ts_utc(y, m, d, h, mi):
    import datetime
    dt = datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)

def fetch_1m(sym, start_ms, end_ms):
    out = []; s = start_ms
    while s < end_ms:
        for _ in range(3):
            try:
                r = S.get('https://fapi.binance.com/fapi/v1/klines',
                          params={'symbol': sym, 'interval': '1m', 'startTime': s,
                                  'endTime': min(end_ms, s + 999*60000), 'limit': 1000},
                          timeout=15)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            break
        b = r.json()
        if not b:
            break
        out.extend(b)
        s = b[-1][0] + 60000
        time.sleep(0.08)
    return out

def fetch_funding(sym, start_ms, end_ms):
    try:
        r = S.get('https://fapi.binance.com/fapi/v1/fundingRate',
                  params={'symbol': sym, 'startTime': start_ms, 'endTime': end_ms, 'limit': 1000},
                  timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def settle(sym, date_str, direction, hold_days):
    """重放一笔: t0=00:21 UTC, 持有hold_days天到终点(开盘价), LONG无TP/SL5, SHORT TP10/SL5"""
    y, m, d = map(int, date_str.split('-'))
    t0 = ts_utc(y, m, d, 0, 21)
    t_end = t0 + (hold_days + 1) * DAY_MS
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return None
    funding = fetch_funding(sym, t0, t_end)
    expiry = t0 + hold_days * DAY_MS
    k_hold = [x for x in k if t0 <= x[0] < expiry]
    if not k_hold:
        return None
    entry = float(k_hold[0][1])
    if entry <= 0:
        return None
    frates = [float(e['fundingRate']) for e in funding if t0 < int(e['fundingTime']) < expiry]
    fund_cost = sum(frates)
    FEE = 0.001 + 0.0002
    sl_lo, sl_hi = entry * 0.95, entry * 1.05
    tp_hi = entry * 1.10

    def net(gross, slippage=0.0002):
        return NOTIONAL * (gross - FEE - slippage - fund_cost)

    # 扫描整个持有窗, SL/TP优先(与tracker一致)
    triggered = None; tp_hit = None
    for x in k_hold:
        h_, l_ = float(x[2]), float(x[3])
        if direction == 'LONG':
            if l_ <= sl_lo:
                triggered = x; break
        else:
            if h_ >= sl_hi:
                triggered = x; break
            if h_ >= tp_hi:
                tp_hit = x; break
    if triggered is not None:
        return net(-0.05, 0.0005)
    if direction == 'SHORT' and tp_hit is not None:
        return net(0.10)
    # 到期: 终点=expiry那根bar的开盘价
    exp_bar = next((x for x in k if x[0] == expiry), None)
    exit_p = float(exp_bar[1]) if exp_bar is not None else float(k_hold[-1][4])
    gross = (exit_p / entry - 1) if direction == 'LONG' else (1 - exit_p / entry)
    return net(gross)

def main():
    with open('/home/myuser/websocket_new/data/hybrid_tracker.json') as f:
        hb = json.load(f)
    days = sorted(hb.keys())
    # 只取已完全到期的日子 (48h已结算)
    settled_days = [d for d in days if hb[d].get('n_settled', 0) >= hb[d].get('n_total', 99)]
    print(f'已到期天数: {len(settled_days)} ({settled_days[0]} → {settled_days[-1]})', flush=True)
    tot48 = {'LONG': 0.0, 'SHORT': 0.0}
    tot72 = {'LONG': 0.0, 'SHORT': 0.0}
    n = {'LONG': 0, 'SHORT': 0}
    diffs = []
    cache = {}
    for di, day in enumerate(settled_days):
        day48 = 0.0; day72 = 0.0
        for t in hb[day].get('trades', []):
            if t.get('net_u') is None:
                continue
            d_ = t['direction']
            u48 = t['net_u']
            key = (t['symbol'], day)
            u72 = settle(t['symbol'], day, d_, 3)
            if u72 is None:
                u72 = u48  # 数据缺失退化
            tot48[d_] += u48; tot72[d_] += u72; n[d_] += 1
            day48 += u48; day72 += u72
        diffs.append((day, day48, day72))
        print(f'  [{di+1}/{len(settled_days)}] {day}: 48h={day48:+.1f} 72h={day72:+.1f} diff={day72-day48:+.1f}', flush=True)
    print()
    print('=' * 62)
    print(f'{"":12s} {"48h口径":>12s} {"72h口径":>12s} {"差(72-48)":>12s} {"笔数":>6s}')
    for side in ('LONG', 'SHORT'):
        d_ = tot72[side] - tot48[side]
        print(f'{side:12s} {tot48[side]:+11.1f}U {tot72[side]:+11.1f}U {d_:+11.1f}U {n[side]:6d}')
    all48 = sum(tot48.values()); all72 = sum(tot72.values())
    print(f'{"合计":12s} {all48:+11.1f}U {all72:+11.1f}U {all72-all48:+11.1f}U {sum(n.values()):6d}')
    # 差异日分布
    dd = [d[2] - d[1] for d in diffs]
    import statistics
    if dd:
        pos = sum(1 for x in dd if x > 0); neg = sum(1 for x in dd if x < 0)
        print(f'\n逐日差异: 72h更优 {pos}天 / 48h更优 {neg}天 / 持平 {len(dd)-pos-neg}天')
        print(f'差异分布: 中位 {statistics.median(dd):+.1f}U | 最大 {max(dd):+.1f}U | 最小 {min(dd):+.1f}U')

if __name__ == '__main__':
    main()
