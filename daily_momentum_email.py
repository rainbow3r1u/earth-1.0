#!/usr/bin/env python3
"""每日强势股判定邮件 — 昨日涨幅≥5%的全部币种 × 模型续涨概率逐个判定
数据源: data/daily_predictions.json (含all_long全量概率, 8:05生产) + K线缓存昨日涨幅"""
import os, sys, json
from datetime import datetime

GAIN_MIN = 5.0

def main():
    pred_file = '/home/myuser/websocket_new/data/daily_predictions.json'
    if not os.path.exists(pred_file):
        print('预测文件不存在')
        return
    pred = json.load(open(pred_file))
    probs = {s: p for s, p in pred.get('all_long', [])}
    klines = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']

    # 昨日涨幅≥5%的全部币种
    rows = []
    for sym, kls in klines.items():
        if len(kls) < 3:
            continue
        prev, last = kls[-3], kls[-2]  # 前收/昨收 (末根为今日未收盘)
        if prev['c'] > 0:
            g = (last['c'] - prev['c']) / prev['c'] * 100
            if g >= GAIN_MIN:
                rows.append((sym, g, probs.get(sym)))
    rows.sort(key=lambda x: -(x[2] if x[2] is not None else -999))

    top10set = {t['symbol'] for t in pred.get('top10_long', [])}
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f'=== 昨日强势股 · 模型续涨判定 ({now}, 预测日 {pred.get("date")}) ===\n',
             f'{"币种":<15}{"昨日涨幅":<9}{"续涨概率":<10}{"判定":<8}']
    for sym, g, p in rows:
        if p is None:
            verdict, ptxt = '无评分', '—'
        elif p >= 60:
            verdict, ptxt = '✓看涨', f'{p:.1f}%'
        elif p >= 45:
            verdict, ptxt = '~观望', f'{p:.1f}%'
        else:
            verdict, ptxt = '✗不看好', f'{p:.1f}%'
        star = '★' if sym in top10set else ' '
        lines.append(f'{star}{sym:<14}{g:+.1f}%{"":<3}{ptxt:<10}{verdict}')
    n_up = sum(1 for _, _, p in rows if p is not None and p >= 60)
    lines.append(f'\n昨日涨幅≥{GAIN_MIN}%共 {len(rows)} 个, 模型看涨(≥60%) {n_up} 个')
    lines.append('(★ = 入选模型 LONG Top10)')

    body = '\n'.join(lines)
    try:
        sys.path.insert(0, '/home/myuser/websocket_new')
        os.chdir('/home/myuser/websocket_new')
        from alert_monitor import send_email
        send_email(f'昨日强势股判定 {now}', body, priority='info')
        print(f'[{now}] 已发送: {len(rows)}个昨日强势股, 看涨{n_up}个')
    except Exception as e:
        print(f'发送失败: {e}')
        print(body)

if __name__ == '__main__':
    main()
