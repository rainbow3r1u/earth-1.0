#!/usr/bin/env python3
"""RESIDUAL 影子臂结算 (2026-09-01 上线, GPU 180d双窗A/B胜出的残差标签方案)

假设来源: LONG标签的beta假阳性 — "48h涨过+5%"在普涨市里垃圾币跟涨也命中,
模型学的是高波动高热度而非选币alpha (8月两轮冲击: 8/30 LONG 10笔全灭-158.8U).
GPU验证: 原窗 Sharpe 32.75 vs 基线21.33; 换窗OFF30 26.89 vs 22.12; LONG砍头5笔后 +1309%(基线+870%).

结构: 与主臂混合结构完全相同的出场规则, 唯一差异 = LONG用残差标签模型的TOP10
     (SHORT与主臂相同, 不重复结算)
  LONG: 无止盈, SL -5%, 持有到48h终点

数据: 每日 pred 文件的 top10_long_residual 字段 (auto_dual_trade.py 影子模型旁路输出)
存档: data/residual_tracker.json
cron: 55 8 * * * (主臂hybrid_tracker 08:45 之后, 晨报09:00之前)
晨报: daily_digest_email.py 3.9节
"""
import os, sys, json, time, glob
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from forward_settle import PRED_DIR, ts_utc, fmt

TRACKER = os.path.join(BASE, '..', 'data', 'residual_tracker.json')
START = '2026-09-02'
NOTIONAL = 300.0

import requests
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

def settle_one(sym, date_str, direction, prob):
    """与 hybrid_tracker.settle_hybrid 相同的结算规则 (LONG无TP/SL5/48h)"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)
    t_end = t0 + 3 * DAY_MS
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}
    funding = fetch_funding(sym, t0, t_end)
    expiry = t0 + 2 * DAY_MS
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    k48 = [x for x in k if t0 <= x[0] < expiry]
    if not k48:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}
    entry = float(k48[0][1])
    if entry <= 0:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'net_u': None}

    sl_lo = entry * 0.95
    frates = [float(e['fundingRate']) for e in funding if t0 < int(e['fundingTime']) < expiry]
    fund_cost = sum(frates)
    FEE = 0.001 + 0.0002
    in_prog = now_ms < expiry
    scan = [x for x in k48 if x[0] <= now_ms] if in_prog else k48

    triggered = None
    for x in scan:
        l = float(x[3])
        if l <= sl_lo:
            triggered = x; break

    def net(gross, slippage=0.0002):
        return NOTIONAL * (gross - FEE - slippage - fund_cost)

    if triggered is not None:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '-5.0%', 'trigger': '止损', 'time': fmt(triggered[0]),
                'net_u': round(net(-0.05, 0.0005), 2)}
    if in_prog:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '⏳未到期', 'trigger': '进行中', 'time': '-', 'net_u': None}
    exp_bar = next((x for x in k if x[0] == expiry), None)
    exit_p = float(exp_bar[1]) if exp_bar is not None else float(k48[-1][4])
    gross = (exit_p/entry - 1)
    return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
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
        try:
            d = json.load(open(pf))
        except Exception:
            continue
        cands = d.get('top10_long_residual', [])
        if not cands:
            if day not in tracker:
                tracker[day] = {'updated': now, 'day_pnl_u': 0.0, 'n_settled': 0, 'n_total': 0,
                                 'trades': [], 'note': '无top10_long_residual字段'}
            continue
        trades = [settle_one(item['symbol'], day, 'LONG', float(item['prob'])) for item in cands]
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
    try:
        hb = json.load(open(os.path.join(BASE, '..', 'data', 'hybrid_tracker.json')))
        hb_days = [x for x in hb if x >= START and hb[x].get('n_settled', 0) >= hb[x].get('n_total', 99)]
        hb_long = sum(t['net_u'] for x in hb_days for t in hb[x].get('trades', [])
                      if t.get('net_u') is not None and t['direction'] == 'LONG')
        rs_days = [x for x in tracker if tracker[x].get('n_settled', 0) >= tracker[x].get('n_total', 99) and tracker[x].get('trades')]
        rs_tot = sum(tracker[x]['day_pnl_u'] for x in rs_days)
        print(f'[residual_tracker] 完成: 已到期{settled} 未到期{pending} | 影子臂LONG累计 {rs_tot:+.1f}U vs 主臂LONG累计 {hb_long:+.1f}U (同期{len(hb_days)}天)')
    except Exception as e:
        print(f'[residual_tracker] 完成: 已到期{settled} 未到期{pending}, 主臂对照读取失败: {e}')

if __name__ == '__main__':
    main()
