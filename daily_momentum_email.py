#!/usr/bin/env python3
"""每日强势股邮件 — 昨日涨幅≥5% 且 模型预计2日内再涨5% 的候选
数据源: data/daily_predictions.json (8:05生产) + K线缓存昨日涨幅"""
import os, sys, json
from datetime import datetime

def main():
    pred_file = '/home/myuser/websocket_new/data/daily_predictions.json'
    if not os.path.exists(pred_file):
        print('预测文件不存在')
        return
    pred = json.load(open(pred_file))
    klines = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']

    def yesterday_gain(sym):
        kls = klines.get(sym, [])
        if len(kls) < 3:
            return None
        prev, last = kls[-3], kls[-2]  # 前收/昨收 (最后一根是今日未收盘)
        return (last['c'] - prev['c']) / prev['c'] * 100 if prev['c'] > 0 else 0

    rows = []
    for t in pred.get('top10_long', []):
        sym = t['symbol']
        g = yesterday_gain(sym)
        if g is not None and g >= 5.0:
            rows.append((sym, g, float(t['prob'])))
    rows.sort(key=lambda x: -x[2])

    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f'=== 昨日强势 + 今日续涨候选 ({now}, 预测日 {pred.get("date")}) ===\n']
    if rows:
        lines.append(f'{"币种":<14}{"昨日涨幅":<10}{"续涨概率":<8}')
        for sym, g, p in rows:
            lines.append(f'{sym:<14}{g:+.1f}%{"":<4}{p:.1f}%')
        lines.append(f'\n共 {len(rows)} 个 (LONG Top10 ∩ 昨日涨幅≥5%)')
    else:
        lines.append('今日无交集 (LONG Top10 中昨日涨幅≥5% 的币种为0)')
        lines.append('模型今日 LONG Top10:')
        for t in pred.get('top10_long', [])[:5]:
            g = yesterday_gain(t['symbol'])
            lines.append(f"  {t['symbol']:<14} 概率{float(t['prob']):.1f}% (昨日 {g:+.1f}%)" if g is not None else f"  {t['symbol']}")

    body = '\n'.join(lines)
    try:
        sys.path.insert(0, '/home/myuser/websocket_new')
        os.chdir('/home/myuser/websocket_new')
        from alert_monitor import send_email
        send_email(f'强势股续涨候选 {now}', body, priority='info')
        print(f'[{now}] 已发送, {len(rows)}个交集')
    except Exception as e:
        print(f'发送失败: {e}')
        print(body)

if __name__ == '__main__':
    main()
