#!/usr/bin/env python3
"""QQ 群定时推送: 今日20单信号 + 8/3以来每日收益.
对接 OneBot v11 HTTP API (NapCat/go-cqhttp).

环境变量:
  QQ_ENDPOINT  如 http://127.0.0.1:3000
  QQ_GROUP_ID  群号
  QQ_TOKEN     可选 access_token
不配置 QQ_ENDPOINT 时只打印消息，用于本地预览。
"""
import json, os, sys, glob
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data'
CACHE = DATA / 'top10_forward_cache.json'
START = '2026-08-03'


def load_today_signals(date_str=None):
    """读取今日 pred_YYYY-MM-DD.json 的 top10_long + top10_short."""
    if date_str is None:
        files = sorted(glob.glob(str(DATA / 'pred_2026-*.json')))
        if not files:
            return None, '无预测文件'
        date_str = Path(files[-1]).stem.replace('pred_', '')
    p = DATA / f'pred_{date_str}.json'
    if not p.exists():
        return None, f'无 {date_str} 预测文件'
    pred = json.load(open(p))
    lines = [f"📊 {date_str} 开仓信号（多空各TOP10）"]
    lines.append("=== LONG ===")
    for i, item in enumerate(pred.get('top10_long', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("=== SHORT ===")
    for i, item in enumerate(pred.get('top10_short', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("⚠️ 仅作研究信号，不构成投资建议")
    return date_str, '\n'.join(lines)


def load_daily_pnl():
    """读取 top10_forward_cache，生成8/3以来每日收益 + 累计."""
    if not CACHE.exists():
        return '暂无 top10_forward_cache'
    cache = json.load(open(CACHE))
    lines = ["📈 多空TOP10全开 每日收益（48h 1m口径）"]
    cum = 0.0
    NOTIONAL, COST = 300.0, 0.002
    total_n = 0
    for ds in sorted(cache):
        r = cache[ds]
        pnls = ([t['pnl'] for t in r.get('long', []) if t.get('pnl') is not None] +
                [t['pnl'] for t in r.get('short', []) if t.get('pnl') is not None])
        if not pnls:
            continue
        day = NOTIONAL * sum(pnls) / 100 - len(pnls) * NOTIONAL * COST
        cum += day
        total_n += len(pnls)
        lines.append(f"{ds}: {day:+.1f}U  累计 {cum:+.1f}U")
    lines.append(f"合计 {total_n}笔, 累计 {cum:+.1f}U")
    lines.append("⚠️ 模拟口径，非实盘收益")
    return '\n'.join(lines)


def send_qq(message):
    endpoint = os.getenv('QQ_ENDPOINT')
    group_id = os.getenv('QQ_GROUP_ID')
    token = os.getenv('QQ_TOKEN', '')
    if not endpoint or not group_id:
        print("========== 本地预览 ==========")
        print(message)
        print("==============================")
        return
    import requests
    url = f"{endpoint.rstrip('/')}/send_group_msg"
    params = {'group_id': group_id, 'message': message}
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    r = requests.post(url, params=params, headers=headers, timeout=10)
    print('QQ send status', r.status_code, r.text[:200])


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'signal'
    if mode == 'pnl':
        send_qq(load_daily_pnl())
    else:
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        ds, msg = load_today_signals(date_str)
        send_qq(msg)
