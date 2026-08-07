#!/usr/bin/env python3
"""手动单审计 (2026-08-07): 按时间窗把仓位分成系统单/手动单

规则(用户拍板): 系统单 = open_ts 落在 08:15-08:35(北京时间)窗口; 其余 = 手动单。
用途: ① 前向评审(8/10)只统计系统单; ② 手动干预对照 data/manual_ledger.json 台账。

用法: python3 manual_trade_audit.py
"""
import json, os
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser('~/.local/share/auto_trade/state.json')
LEDGER = os.path.join(BASE, 'data', 'manual_ledger.json')
BJT = timezone(timedelta(hours=8))
WIN_START, WIN_END = 8 * 60 + 15, 8 * 60 + 35   # 分钟

def classify(ts):
    t = datetime.fromtimestamp(ts, tz=BJT)
    m = t.hour * 60 + t.minute
    return '系统' if WIN_START <= m <= WIN_END else '手动'

def main():
    state = json.load(open(STATE))
    ledger = json.load(open(LEDGER))
    ledger_keys = {(e['date'], e['symbol']) for e in ledger['entries']}
    print(f"{'仓位':<22}{'开仓时间(北京)':<20}{'判定':<6}{'台账'}")
    for key, p in state.get('positions', {}).items():
        ts = p.get('open_ts', 0)
        t = datetime.fromtimestamp(ts, tz=BJT)
        verdict = classify(ts)
        date_str = t.strftime('%Y-%m-%d')
        in_ledger = '✅已录' if (date_str, p['symbol']) in ledger_keys else ('—' if verdict == '系统' else '❌未录入台账!')
        print(f"{key:<22}{t.strftime('%m-%d %H:%M:%S'):<20}{verdict:<6}{in_ledger}")
    manual_unlogged = [k for k, p in state.get('positions', {}).items()
                       if classify(p.get('open_ts', 0)) == '手动'
                       and (datetime.fromtimestamp(p['open_ts'], tz=BJT).strftime('%Y-%m-%d'), p['symbol']) not in ledger_keys]
    if manual_unlogged:
        print(f"\n⚠️ 以下手动仓位未录入台账, 请补录 data/manual_ledger.json: {manual_unlogged}")
    else:
        print('\n✅ 无遗漏: 所有手动仓位均已录入台账')

if __name__ == '__main__':
    main()
