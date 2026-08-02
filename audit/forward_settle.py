#!/usr/bin/env python3
"""前向结算工具 (forward-eval-rule v2, 2026-08-02 固化)

口径 (见 Obsidian forward-eval-rule):
- 每天信号 = 多空 TOP1 中 prob 高者 (用户 8/1 定: 单笔等额, 不管 SHORT 多仓)
- 入场 = 预测日 08:21 北京 (UTC 00:21) 1m K 线开盘价
- 1m 粒度逐根: 先查止损再查止盈, 先触发者为准; 48h 到 T+2 收盘未触发按收盘价
  - SHORT: 止损 high ≥ entry×1.05; 止盈 low ≤ entry×0.90
  - LONG:  止损 low ≤ entry×0.95;  止盈 high ≥ entry×1.10
- 触发价显示: 止损显示 high (SHORT/LONG 触发根), 止盈显示 low (SHORT 触发根) / high (LONG 触发根)

用法:
  python3 audit/forward_settle.py                      # 结算全部 pred_*.json 的 TOP1 (多空取高者)
  python3 audit/forward_settle.py 2026-08-01           # 只结算指定预测日
  python3 audit/forward_settle.py --top10 2026-08-01   # 结算 Top10 (LONG+SHORT 分开)
"""
import sys, os, json, glob, time
import requests
from datetime import datetime, timezone

BASE = '/home/myuser/websocket_new'
PRED_DIR = os.path.join(BASE, 'data')

def ts_utc(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)

def fmt(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime('%m-%d %H:%M')

def fetch_1m(sym, start_ms, end_ms):
    out = []; s = start_ms
    while s < end_ms:
        r = requests.get('https://fapi.binance.com/fapi/v1/klines',
            params={'symbol': sym, 'interval': '1m', 'startTime': s,
                    'endTime': min(end_ms, s + 999*60000), 'limit': 1000}, timeout=15)
        if r.status_code != 200:
            time.sleep(2); continue
        b = r.json()
        if not b: break
        out.extend(b); s = b[-1][0] + 60000
        time.sleep(0.12)
    return out

def settle(sym, date_str, direction, prob):
    """返回 dict: 入场/触发类型/时间/价格/结果%; 未触发 → 当前浮盈"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)
    t_end = t0 + 3 * 86400000
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'result': '无数据', 'entry': None}
    entry = float(k[0][1])
    sl_hi, sl_lo = entry * 1.05, entry * 0.95
    tp_hi, tp_lo = entry * 1.10, entry * 0.90
    for x in k:
        h, l = float(x[2]), float(x[3])
        if direction == 'SHORT':
            if h >= sl_hi:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '-5.0%', 'trigger': '止损',
                        'time': fmt(x[0]), 'price': h, 'reason_note': 'high 打穿 +5%'}
            if l <= tp_lo:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '+10.0%', 'trigger': '止盈',
                        'time': fmt(x[0]), 'price': l, 'reason_note': 'low 打穿 -10%'}
        else:
            if l <= sl_lo:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '-5.0%', 'trigger': '止损',
                        'time': fmt(x[0]), 'price': l, 'reason_note': 'low 打穿 -5%'}
            if h >= tp_hi:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '+10.0%', 'trigger': '止盈',
                        'time': fmt(x[0]), 'price': h, 'reason_note': 'high 打穿 +10%'}
    c = float(k[-1][4])
    ret = (entry - c) / entry * 100 if direction == 'SHORT' else (c - entry) / entry * 100
    return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
            'entry': entry, 'result': f'{ret:+.2f}%', 'trigger': '未到期/未触发',
            'time': fmt(k[-1][0]), 'price': c, 'reason_note': '48h 窗口未触发, 当前价结算'}

def get_days(args):
    days = [a for a in args if not a.startswith('-')]
    if days:
        return days
    files = sorted(glob.glob(os.path.join(PRED_DIR, 'pred_*.json')))
    return [os.path.basename(f).replace('pred_', '').replace('.json', '') for f in files]

def main():
    args = sys.argv[1:]
    top10 = '--top10' in args
    days = get_days(args)
    results = []
    for day in days:
        pf = os.path.join(PRED_DIR, f'pred_{day}.json')
        if not os.path.exists(pf):
            print(f'跳过 {day}: 无预测文件')
            continue
        d = json.load(open(pf))
        if top10:
            for side, key in [('LONG', 'top10_long'), ('SHORT', 'top10_short')]:
                for item in d.get(key, []):
                    results.append(settle(item['symbol'], day, side, float(item['prob'])))
        else:
            bl, bs = d.get('best_long'), d.get('best_short')
            cands = []
            if bl:
                cands.append(('LONG', bl['symbol'], float(bl['prob'])))
            if bs:
                cands.append(('SHORT', bs['symbol'], float(bs['prob'])))
            if not cands:
                print(f'{day}: 无信号')
                continue
            # 多空 TOP1 取 prob 高者
            direction, sym, prob = max(cands, key=lambda x: x[2])
            results.append(settle(sym, day, direction, prob))

    print(f"\n{'预测日':<12}{'方向':<6}{'币':<16}{'prob':<7}{'入场':<12}{'触发':<8}{'时间':<14}{'触发价':<12}{'结果':<9}")
    print('-' * 100)
    for r in results:
        entry = f"{r['entry']:.6g}" if r['entry'] else '-'
        print(f"{r['date']:<12}{r['direction']:<6}{r['sym']:<16}{r['prob']:<7.1f}{entry:<12}"
              f"{r['trigger']:<8}{r['time']:<14}{r['price']:<12.6g}{r['result']:<9}")
    # 汇总
    closed = [r for r in results if r['result'] in ('-5.0%', '+10.0%')]
    print(f"\n已到期: {len(closed)}/{len(results)} 笔 | "
          f"止损 {sum(1 for r in closed if r['result']=='-5.0%')} / "
          f"止盈 {sum(1 for r in closed if r['result']=='+10.0%')}")

if __name__ == '__main__':
    main()
