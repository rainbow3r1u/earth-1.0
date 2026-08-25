#!/usr/bin/env python3
"""混合结构影子臂结算 (2026-08-24 上线)

结构 (基于 8/3~8/22 400笔 1m 回测结论, 每条有市场机制解释):
  LONG:  无止盈, SL -5%, 未触发则持有到 48h 终点平仓   → 吃终点趋势尾巴 (标签语义)
  SHORT: 现行 TP +10% / SL -5%                        → 吃瞬时下杀, 50%会V回故快速止盈

口径: 入场 00:05 UTC (08:05 CST), strict48 半开区间 [entry, entry+48h),
      同分钟双触发 SL_FIRST, TIMEOUT=到期分钟open, 含费(taker0.1%+滑点0.02%+SL0.05%)+真实资金费。
存档: data/hybrid_tracker.json  (与 forward_tracker.json 独立, 只读预测文件)
cron: 20 9 * * * (forward_tracker 之后), 幂等: 已全到期日跳过。
晨报: daily_digest_email.py 4a5 节读取。
"""
import os, sys, json, time, glob, requests
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from forward_settle import PRED_DIR, ts_utc, fmt, _adverse_pct

TRACKER = os.path.join(BASE, '..', 'data', 'hybrid_tracker.json')
START = '2026-08-03'          # 幽灵修复后首日
NOTIONAL = 300.0              # 每笔名义 (与晨报 forward 口径一致)
S = requests.Session()
DAY_MS = 86400000

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
        time.sleep(0.10)
    return out

def fetch_funding(sym, start_ms, end_ms):
    try:
        r = S.get('https://fapi.binance.com/fapi/v1/fundingRate',
                  params={'symbol': sym, 'startTime': start_ms, 'endTime': end_ms, 'limit': 1000},
                  timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def settle_hybrid(sym, date_str, direction, prob):
    """混合结构结算: LONG 无TP(SL5%/到期平) / SHORT TP10%/SL5%。
    返回 dict: entry/result/trigger/net_pnl_u(300U名义)
    8/24 晚: 入场 00:05→00:21 UTC (08:21 CST), 与 3.7/实盘口径对齐(用户拍板)。"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)   # 00:21 UTC 入场
    t_end = t0 + 3 * DAY_MS
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return {'sym': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}
    funding = fetch_funding(sym, t0, t_end)
    expiry = t0 + 2 * DAY_MS
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    k48 = [x for x in k if t0 <= x[0] < expiry]
    if not k48:
        return {'sym': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}
    entry = float(k48[0][1])
    if entry <= 0:
        return {'sym': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}

    sl_hi, sl_lo = entry * 1.05, entry * 0.95
    tp_hi, tp_lo = entry * 1.10, entry * 0.90
    frates = [float(e['fundingRate']) for e in funding
              if t0 < int(e['fundingTime']) < expiry]
    fund_cost = sum(frates) if direction == 'LONG' else -sum(frates)
    FEE = 0.001 + 0.0002   # taker×2 + 入场滑点
    in_prog = now_ms < expiry
    scan = [x for x in k48 if x[0] <= now_ms] if in_prog else k48

    triggered = None
    for x in scan:
        h, l = float(x[2]), float(x[3])
        hit_sl = (l <= sl_lo) if direction == 'LONG' else (h >= sl_hi)
        hit_tp = (h >= tp_hi) if direction == 'LONG' else (l <= tp_lo)
        if hit_sl:                     # 同分钟双触发 SL_FIRST
            triggered = ('SL', x); break
        # LONG 无止盈: 直接跳过 TP 检测
        if direction == 'SHORT' and hit_tp:
            triggered = ('TP', x); break

    def net(gross, slippage=0.0002):
        return NOTIONAL * (gross - FEE - slippage - fund_cost)

    if triggered:
        kind, x = triggered
        if kind == 'SL':
            return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                    'result': '-5.0%', 'trigger': '止损', 'time': fmt(x[0]),
                    'net_u': round(net(-0.05, 0.0005), 2)}
        return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '+10.0%', 'trigger': '止盈', 'time': fmt(x[0]),
                'net_u': round(net(0.10), 2)}
    if in_prog:
        return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '⏳未到期', 'trigger': '进行中', 'time': '-', 'net_u': None}
    # TIMEOUT: 到期分钟 open
    exp_bar = next((x for x in k if x[0] == expiry), None)
    exit_p = float(exp_bar[1]) if exp_bar is not None else float(k48[-1][4])
    gross = (exit_p/entry - 1) if direction == 'LONG' else (1 - exit_p/entry)
    return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
            'result': f'{gross*100:+.1f}%', 'trigger': '到期', 'time': fmt(expiry),
            'net_u': round(net(gross), 2)}

def load_tracker():
    try:
        return json.load(open(TRACKER))
    except Exception:
        return {}

def save_tracker(t):
    with open(TRACKER, 'w') as f:
        json.dump(t, f, ensure_ascii=False, indent=1)

def is_all_settled(day_entry):
    trs = day_entry.get('trades', [])
    return bool(trs) and all(r.get('result') not in ('⏳未到期', '无数据') for r in trs)

def main():
    tracker = load_tracker()
    days = sorted(glob.glob(os.path.join(PRED_DIR, 'pred_*.json')))
    days = [os.path.basename(f).replace('pred_', '').replace('.json', '') for f in days
            if os.path.basename(f) >= f'pred_{START}.json']
    now = datetime.now(timezone.utc).isoformat()
    settled = pending = 0
    for day in days:
        if day in tracker and is_all_settled(tracker[day]):
            settled += 1
            continue
        pf = os.path.join(PRED_DIR, f'pred_{day}.json')
        d = json.load(open(pf))
        trades = []
        for side, key in [('LONG', 'top10_long'), ('SHORT', 'top10_short')]:
            for item in d.get(key, []):
                r = settle_hybrid(item['symbol'], day, side, float(item['prob']))
                r['symbol'] = r.pop('sym')
                trades.append(r)
        # 日汇总
        ok = [t for t in trades if t.get('net_u') is not None]
        day_u = sum(t['net_u'] for t in ok)
        tracker[day] = {'updated': now, 'day_pnl_u': round(day_u, 1),
                        'n_settled': len(ok), 'n_total': len(trades), 'trades': trades}
        if is_all_settled(tracker[day]):
            settled += 1
        else:
            pending += 1
        print(f'  {day}: {len(ok)}/{len(trades)}笔已结算, 当日累计 {day_u:+.1f}U', flush=True)
    save_tracker(tracker)
    # 累计
    tot = sum(v.get('day_pnl_u', 0) for v in tracker.values())
    print(f'[hybrid_tracker] 完成: 已到期 {settled}, 未到期 {pending}, 累计 {tot:+.1f}U, 存档 {TRACKER}')

if __name__ == '__main__':
    main()
