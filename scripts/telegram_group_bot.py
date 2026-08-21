#!/usr/bin/env python3
"""Telegram 群定时推送: 今日20单信号 + 8/3以来每日收益.
环境变量:
  TG_BOT_TOKEN   @BotFather 给的 token
  TG_CHAT_ID     群/频道 id
不配置时本地预览。
"""
import json, os, sys, glob
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data'
CACHE = DATA / 'top10_forward_cache.json'

def _load_env_file():
    env_path = Path.home() / '.telegram_bot.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env_file()


def load_today_signals(date_str=None):
    if date_str is None:
        files = sorted(glob.glob(str(DATA / 'pred_2026-*.json')))
        if not files:
            return None, '无预测文件'
        date_str = Path(files[-1]).stem.replace('pred_', '')
    p = DATA / f'pred_{date_str}.json'
    if not p.exists():
        return None, f'无 {date_str} 预测文件'
    pred = json.load(open(p))
    lines = [f"📊 {date_str} 开仓信号（多空各TOP10）", "=== LONG ==="]
    for i, item in enumerate(pred.get('top10_long', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("=== SHORT ===")
    for i, item in enumerate(pred.get('top10_short', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("⚠️ 仅作研究信号，不构成投资建议")
    return date_str, '\n'.join(lines)

def load_daily_pnl():
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

def send_tg(message):
    token = os.getenv('TG_BOT_TOKEN')
    chat_id = os.getenv('TG_CHAT_ID')
    if not token or not chat_id:
        print("========== 本地预览 ==========")
        print(message)
        print("==============================")
        return
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={'chat_id': chat_id, 'text': message}, timeout=15)
    print('TG send status', r.status_code, r.text[:200])

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'signal'
    if mode == 'pnl':
        send_tg(load_daily_pnl())
    else:
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        ds, msg = load_today_signals(date_str)
        send_tg(msg)
