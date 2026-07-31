#!/usr/bin/env python3
"""入场价现实性验证 (修正GPT任务6的时区错误版)
GPT任务6误设实盘=08:00 UTC; 实际生产=北京08:05=00:05 UTC (日K开盘后5分钟)。
正确口径: 回测入场=00:00开盘价 vs 实盘入场≈00:15(首根15m收盘, 00:05的保守上界)。
从00:15起模拟48h止盈止损(15m粒度), 对比两口径的真实差距。
"""
import json, time, math, statistics as st
from datetime import datetime, timezone
import requests

STOP_LOSS, TAKE_PROFIT, COST = 5.0, 10.0, 0.5
API = 'https://fapi.binance.com/fapi/v1/klines'
TRADES_FILE = '/tmp/trades_fix_nogate.json'

def fetch_15m(sym, ts0, ts1):
    rows = []
    cur = ts0 * 1000
    while cur < ts1 * 1000:
        for _ in range(3):
            try:
                r = requests.get(API, params={'symbol': sym, 'interval': '15m',
                    'startTime': cur, 'endTime': ts1 * 1000, 'limit': 1500}, timeout=20)
                batch = r.json()
                if isinstance(batch, list) and batch:
                    rows += batch
                    cur = batch[-1][0] + 15000
                    break
            except Exception:
                time.sleep(2)
        else:
            return None
        time.sleep(0.05)
    return rows

def simulate(rows, ep, t_start, t_end, direction):
    if ep <= 0: return None
    for k in rows:
        kt = k[0] // 1000
        if kt < t_start or kt >= t_end: continue
        h, l = float(k[2]), float(k[3])
        if direction == 'long':
            if l <= ep * (1 - STOP_LOSS/100): return -STOP_LOSS - COST
            if h >= ep * (1 + TAKE_PROFIT/100): return TAKE_PROFIT - COST
        else:
            if h >= ep * (1 + STOP_LOSS/100): return -STOP_LOSS - COST
            if l <= ep * (1 - TAKE_PROFIT/100): return TAKE_PROFIT - COST
    c_last = float(rows[-1][4]) if rows else ep
    pnl = (c_last/ep - 1)*100 if direction == 'long' else (1 - c_last/ep)*100
    return max(-STOP_LOSS, min(TAKE_PROFIT, pnl)) - COST

def stats(pnls, name):
    if not pnls: return
    wins = sum(1 for p in pnls if p > 0)
    sh = (st.mean(pnls)/(st.stdev(pnls)+1e-9))*math.sqrt(365) if len(pnls) > 1 else 0
    print(f'{name}: {len(pnls)}笔 胜率{wins/len(pnls)*100:.0f}% 累计{sum(pnls):+.0f}% Sharpe={sh:.2f}')

d = json.load(open(TRADES_FILE))
trades = d['trades']
print(f'总交易: {len(trades)}')
open_pnls, live_pnls, gaps = [], [], []
fails = 0
for i, t in enumerate(trades):
    sym, day, direction = t['symbol'], t['day'], t['direction']
    day0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    rows = fetch_15m(sym, day0, day0 + 3 * 86400)
    if not rows:
        fails += 1
        continue
    first = rows[0]
    ep_open = float(first[1])   # 00:00开盘价(回测口径)
    ep_015 = float(first[4])    # 00:15收盘价(≈实盘00:05入场的保守上界)
    t_live_start = day0 + 900   # 00:15起 (首根内波动不计, 持仓未开)
    t_end = day0 + 48 * 3600 + 900
    pnl_open = simulate(rows, ep_open, day0 + 900, t_end, direction)
    pnl_live = simulate(rows, ep_015, t_live_start, t_end, direction)
    if pnl_open is None or pnl_live is None:
        fails += 1
        continue
    open_pnls.append(pnl_open)
    live_pnls.append(pnl_live)
    gaps.append((ep_015 / ep_open - 1) * 100)
    if (i + 1) % 30 == 0:
        print(f'  进度 {i+1}/{len(trades)}')

print('\n=== 对照结果 (15m粒度, 修正口径含入场日) ===')
stats(open_pnls, '开盘价入场 (回测口径)')
stats(live_pnls, '00:15入场 (≈实盘00:05上界)')
g = sorted(gaps)
n = len(g)
print(f'\n开盘→00:15漂移: 中位{g[n//2]:+.2f}%  p25={g[n//4]:+.2f}%  p75={g[3*n//4]:+.2f}%')
print(f'漂移>2%: {sum(1 for x in g if abs(x)>2)}/{n}  >5%: {sum(1 for x in g if abs(x)>5)}/{n}')
print(f'拉取失败: {fails}笔')
json.dump({'open': open_pnls, 'live015': live_pnls, 'gaps': gaps},
          open('/tmp/live_805_verify.json', 'w'))
