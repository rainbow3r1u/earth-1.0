#!/usr/bin/env python3
"""每小时山寨资金榜 — U本位合约24h成交额 Top10 (排除BTC/ETH/稳定币)
标记相比上小时的排名变化与新进/掉出, 邮件推送"""
import os, sys, json, requests
from datetime import datetime

PREV_FILE = '/tmp/vol_top10_prev.json'
EXCLUDE = {'BTCUSDT', 'ETHUSDT', 'USDCUSDT', 'USDPUSDT', 'USDSUSDT', 'FDUSDUSDT',
           'TUSDUSDT', 'AEURUSDT', 'EURUSDT', 'USDYUSDT', 'BTCDOMUSDT',
           'SOLUSDT', 'SNDKUSDT', 'CLUUSDT', 'SPCXUSDT', 'DOGEUSDT'}

def fmt(v):
    return f'{v/1e8:.1f}亿' if v >= 1e8 else f'{v/1e4:.0f}万'

def main():
    r = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=20)
    if r.status_code != 200:
        print(f'API失败: HTTP {r.status_code}')
        return
    rows = []
    for t in r.json():
        sym = t['symbol']
        if not sym.endswith('USDT') or sym in EXCLUDE:
            continue
        rows.append({'symbol': sym, 'qv': float(t['quoteVolume']),
                     'chg': float(t['priceChangePercent'])})
    rows.sort(key=lambda x: -x['qv'])
    top = rows[:10]

    # 与上小时对比: 排名变化 + 新进/掉出
    prev = []
    if os.path.exists(PREV_FILE):
        try:
            prev = json.load(open(PREV_FILE)).get('symbols', [])
        except Exception:
            pass
    cur_syms = [t['symbol'] for t in top]
    new_in = [s for s in cur_syms if s not in prev]
    dropped = [s for s in prev if s not in cur_syms]

    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f'{"#":<3}{"币种":<14}{"24h成交额":<12}{"24h涨跌":<10}{"较上小时":<8}']
    for i, t in enumerate(top, 1):
        if t['symbol'] in new_in:
            mark = '新进' if prev else '-'
        else:
            diff = prev.index(t['symbol']) - i if t['symbol'] in prev else 0
            mark = f'↑{diff}' if diff > 0 else (f'↓{-diff}' if diff < 0 else '=')
        lines.append(f"{i:<3}{t['symbol']:<14}{fmt(t['qv']):<12}{t['chg']:+.1f}%{'':<4}{mark}")
    if new_in and prev:
        lines.append(f'\n新进榜: {", ".join(new_in)}')
    if dropped and prev:
        lines.append(f'掉出榜: {", ".join(dropped)}')

    with open(PREV_FILE, 'w') as f:
        json.dump({'symbols': cur_syms, 'ts': now}, f)

    body = f'=== 山寨合约资金榜 ({now}) ===\n\n' + '\n'.join(lines)
    try:
        sys.path.insert(0, '/home/myuser/websocket_new')
        os.chdir('/home/myuser/websocket_new')
        from alert_monitor import send_email
        send_email(f'山寨资金榜 Top10 {now}', body, priority='info')
        print(f'[{now}] 已发送: {cur_syms}')
    except Exception as e:
        print(f'邮件发送失败: {e}')
        print(body)

if __name__ == '__main__':
    main()
