#!/usr/bin/env python3
"""每日强势股判定邮件 — 昨日涨幅≥5%的全部币种 × 模型续涨概率逐个判定
数据源: data/daily_predictions.json (含all_long全量概率, 8:05生产) + K线缓存昨日涨幅"""
import os, sys, json, requests
from datetime import datetime

GAIN_MIN = 5.0
VOL_EXCLUDE = {'BTCUSDT', 'ETHUSDT', 'USDCUSDT', 'USDPUSDT', 'USDSUSDT', 'FDUSDUSDT',
               'TUSDUSDT', 'AEURUSDT', 'EURUSDT', 'USDYUSDT', 'BTCDOMUSDT',
               'SOLUSDT', 'SNDKUSDT', 'CLUSDT', 'SPCXUSDT', 'DOGEUSDT',
               'ZECUSDT', 'HYPEUSDT', 'XRPUSDT', '1000SHIBUSDT',
               'XAUUSDT', 'XAGUSDT', 'TSLAUSDT', 'NVDAUSDT', 'AAPLUSDT', 'AMZNUSDT',
               'GOOGLUSDT', 'METAUSDT', 'MSFTUSDT', 'MUUSDT', 'COINUSDT', 'MSTRUSDT',
               'AMDUSDT', 'INTCUSDT', 'QCOMUSDT', 'AVGOUSDT', 'ORCLUSDT', 'NFLXUSDT',
               'DISUSDT', 'JPMUSDT', 'PLTRUSDT', 'HOODUSDT', 'COPPERUSDT', 'PAXGUSDT',
               'SKHYNIXUSDT'}

def _vol_top10():
    """每日资金榜: 合约24h成交额Top10 (排除名单内)"""
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=20)
        if r.status_code != 200:
            return []
        rows = [{'symbol': t['symbol'], 'qv': float(t['quoteVolume']),
                 'chg': float(t['priceChangePercent'])}
                for t in r.json()
                if t['symbol'].endswith('USDT') and t['symbol'] not in VOL_EXCLUDE]
        rows.sort(key=lambda x: -x['qv'])
        return rows[:10]
    except Exception:
        return []

def _fmt(v):
    return f'{v/1e8:.1f}亿' if v >= 1e8 else f'{v/1e4:.0f}万'

def build_momentum_body():
    """强势股判定 + 每日资金榜 板块内容 (供本脚本和晨报总览复用)"""
    pred_file = '/home/myuser/websocket_new/data/daily_predictions.json'
    if not os.path.exists(pred_file):
        return '(预测文件不存在)'
    pred = json.load(open(pred_file))
    probs = {s: float(p) for s, p in pred.get('all_long', [])}  # float(): 兼容旧存档中numpy被json序列化成字符串的概率
    # 7/30起概率饱和regime: 数值普遍≥0.9, 概率阈值失效, 改用排名判定
    _ranked = sorted(probs.items(), key=lambda x: -x[1])
    ranks = {s: i + 1 for i, (s, _) in enumerate(_ranked)}
    n_scored = len(_ranked)
    klines = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']

    # 昨日涨幅≥5%的全部币种 (按昨日成交额降序, 连续2日≥5%标记🔥)
    rows = []
    for sym, kls in klines.items():
        if len(kls) < 3:
            continue
        prev, last = kls[-3], kls[-2]  # 前收/昨收 (末根为今日未收盘)
        if prev['c'] > 0:
            g = (last['c'] - prev['c']) / prev['c'] * 100
            if g >= GAIN_MIN:
                streak = (len(kls) >= 4 and kls[-4]['c'] > 0
                          and (prev['c'] - kls[-4]['c']) / kls[-4]['c'] * 100 >= GAIN_MIN)
                rows.append((sym, g, probs.get(sym), last.get('q', 0.0), streak))
    rows.sort(key=lambda x: -x[3])

    top10set = {t['symbol'] for t in pred.get('top10_long', [])}
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f'=== 昨日强势股 · 模型续涨判定 (预测日 {pred.get("date")}, 按成交额降序) ===\n',
             f'{"币种":<15}{"昨日涨幅":<9}{"成交额":<11}{"续涨概率":<10}{"排名":<9}{"判定":<8}']
    for sym, g, p, q, streak in rows:
        r = ranks.get(sym)
        rtxt = f'{r}/{n_scored}' if r else '—'
        if p is None or r is None:
            verdict, ptxt = '无评分', '—'
        elif r <= 10:
            verdict, ptxt = '✓强', f'{p:.1f}%'
        elif r <= max(10, n_scored // 10):
            verdict, ptxt = '~中', f'{p:.1f}%'
        else:
            verdict, ptxt = '✗弱', f'{p:.1f}%'
        star = '★' if sym in top10set else ' '
        fire = '🔥' if streak else ''
        lines.append(f'{star}{sym:<14}{g:+.1f}%{"":<3}{_fmt(q):<11}{ptxt:<10}{rtxt:<9}{verdict}{fire}')
    n_top10 = sum(1 for sym, _, p, _, _ in rows if p is not None and (ranks.get(sym) or 9999) <= 10)
    n_streak = sum(1 for r in rows if r[4])
    lines.append(f'\n昨日涨幅≥{GAIN_MIN}%共 {len(rows)} 个, 排名Top10 {n_top10} 个, 连续2日≥5% {n_streak} 个')
    lines.append('(★ = 入选模型 LONG Top10, 🔥 = 连续2日涨幅≥5%; 7/30起概率饱和, 判定按排名: ✓强=Top10, ~中=Top10%, ✗弱=其余)')

    # 每日资金榜 (合约24h成交额Top10)
    lines.append('\n=== 每日资金榜 (合约24h成交额 Top10) ===')
    for i, t in enumerate(_vol_top10(), 1):
        lines.append(f"{i:<3}{t['symbol']:<14}{_fmt(t['qv']):<12}{t['chg']:+.1f}%")
    return '\n'.join(lines)

def main():
    body = build_momentum_body()
    now = datetime.now().strftime('%m-%d %H:%M')
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


def build_momentum_body_html():
    """强势股 + 资金榜 → HTML 表格版(晨报富文本, 黑体窄列)"""
    pred_file = '/home/myuser/websocket_new/data/daily_predictions.json'
    if not os.path.exists(pred_file):
        return '<p>(预测文件不存在)</p>'
    pred = json.load(open(pred_file))
    probs = {s: float(p) for s, p in pred.get('all_long', [])}
    _ranked = sorted(probs.items(), key=lambda x: -x[1])
    ranks = {s: i + 1 for i, (s, _) in enumerate(_ranked)}
    n_scored = len(_ranked)
    klines = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']

    rows = []
    for sym, kls in klines.items():
        if len(kls) < 3:
            continue
        prev, last = kls[-3], kls[-2]
        if prev['c'] > 0:
            g = (last['c'] - prev['c']) / prev['c'] * 100
            if g >= GAIN_MIN:
                streak = (len(kls) >= 4 and kls[-4]['c'] > 0
                          and (prev['c'] - kls[-4]['c']) / kls[-4]['c'] * 100 >= GAIN_MIN)
                rows.append((sym, g, probs.get(sym), last.get('q', 0.0), streak))
    rows.sort(key=lambda x: -x[3])
    top10set = {t['symbol'] for t in pred.get('top10_long', [])}

    def esc(v):
        return str(v).replace('<', '&lt;').replace('>', '&gt;')
    style = ("border-collapse:collapse;font-family:'SimHei','Microsoft YaHei';font-size:11px;white-space:nowrap;")
    td = "border:1px solid #ddd;padding:2px 5px;text-align:left;"
    th = "border:1px solid #999;padding:2px 5px;background:#f5f5f5;"

    h = ['<b>昨日强势股 · 模型续涨判定</b><br>',
         f'<table style="{style}"><tr>']
    for c in ['币种', '昨日涨幅', '成交额', '续涨概率', '排名', '判定']:
        h.append(f'<th style="{th}">{c}</th>')
    h.append('</tr>')
    for sym, g, p, q, streak in rows:
        r = ranks.get(sym)
        rtxt = f'{r}/{n_scored}' if r else '—'
        if p is None or r is None:
            verdict, ptxt = '无评分', '—'
        elif r <= 10:
            verdict, ptxt = '✓强', f'{p:.1f}%'
        elif r <= max(10, n_scored // 10):
            verdict, ptxt = '~中', f'{p:.1f}%'
        else:
            verdict, ptxt = '✗弱', f'{p:.1f}%'
        star = '★' if sym in top10set else ''
        fire = '🔥' if streak else ''
        h.append(f"<tr><td style='{td}'>{star}{esc(sym)} {fire}</td>"
                 f"<td style='{td}'>{g:+.1f}%</td>"
                 f"<td style='{td}'>{_fmt(q)}</td>"
                 f"<td style='{td}'>{ptxt}</td>"
                 f"<td style='{td}'>{rtxt}</td>"
                 f"<td style='{td}'>{verdict}</td></tr>")
    h.append('</table>')
    h.append(f"<p style='font-size:11px;color:#666;'>昨日涨幅≥{GAIN_MIN}%共 {len(rows)} 个; "
             f"★=入选 LONG Top10, 🔥=连续2日≥5%; 判定按排名: ✓强=Top10, ~中=Top10%, ✗弱=其余</p>")

    h.append('<b>每日资金榜 (合约24h成交额 Top10)</b><br>')
    h.append(f'<table style="{style}"><tr>')
    for c in ['#', '币种', '成交额', '24h涨跌']:
        h.append(f'<th style="{th}">{c}</th>')
    h.append('</tr>')
    for i, t in enumerate(_vol_top10(), 1):
        h.append(f"<tr><td style='{td}'>{i}</td>"
                 f"<td style='{td}'>{esc(t['symbol'])}</td>"
                 f"<td style='{td}'>{_fmt(t['qv'])}</td>"
                 f"<td style='{td}'>{t['chg']:+.1f}%</td></tr>")
    h.append('</table>')
    return ''.join(h)
