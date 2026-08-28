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

def _adverse_pct(entry, direction, bars):
    """持仓期最大反向 %: LONG=最低价相对entry跌幅; SHORT=最高价相对entry涨幅."""
    if not bars or entry <= 0:
        return 0.0
    if direction == 'SHORT':
        return (max(float(x[2]) for x in bars) - entry) / entry * 100
    return (entry - min(float(x[3]) for x in bars)) / entry * 100


def settle(sym, date_str, direction, prob):
    """返回 dict: 入场/触发类型/时间/价格/结果%; 未触发 → 当前浮盈
    附加:
      max_retrace      = 持仓期最大反向 (已平仓单只算到触发分钟; 未触发算到到期/当前)
      max_retrace_no_sl= 假设不止损的 48h 全窗口最大反向 (仅供止损建议模拟)
      dir_ok/dir_ret   = 假设不止损持有到 48h 的方向/收益 (口径 v2)
    """
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)
    t_end = t0 + 3 * 86400000
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'result': '无数据', 'entry': None, 'dir_ok': None, 'dir_ret': None,
                'max_retrace': None, 'max_retrace_no_sl': None}
    expiry_ms = t0 + 2 * 86400000
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    entry = float(k[0][1])

    if now_ms < expiry_ms:
        sl_hi, sl_lo = entry * 1.05, entry * 0.95
        tp_hi, tp_lo = entry * 1.10, entry * 0.90
        k_sofar = [x for x in k if x[0] <= now_ms]
        max_retrace_no_sl = _adverse_pct(entry, direction, k_sofar)
        max_retrace = max_retrace_no_sl
        dir_ok = None
        dir_ret = None
        for x in k_sofar:
            h, l = float(x[2]), float(x[3])
            triggered = None
            if direction == 'SHORT':
                if h >= sl_hi:
                    triggered = ('止损', h, 'high 打穿 +5% (未到期已触发)')
                elif l <= tp_lo:
                    triggered = ('止盈', l, 'low 打穿 -10% (未到期已触发)')
            else:
                if l <= sl_lo:
                    triggered = ('止损', l, 'low 打穿 -5% (未到期已触发)')
                elif h >= tp_hi:
                    triggered = ('止盈', h, 'high 打穿 +10% (未到期已触发)')
            if triggered:
                trig, price, note = triggered
                max_retrace = _adverse_pct(entry, direction, [b for b in k_sofar if b[0] <= x[0]])
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '-5.0%' if trig == '止损' else '+10.0%',
                        'trigger': trig, 'time': fmt(x[0]), 'price': price, 'reason_note': note,
                        'dir_ok': dir_ok, 'dir_ret': dir_ret,
                        'max_retrace': max_retrace, 'max_retrace_no_sl': max_retrace_no_sl}
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'entry': entry, 'result': '⏳未到期', 'trigger': '进行中',
                'time': '-', 'price': None, 'reason_note': '48h 窗口未走完, 暂未触发',
                'dir_ok': None, 'dir_ret': None, 'max_retrace': None, 'max_retrace_no_sl': None}

    sl_hi, sl_lo = entry * 1.05, entry * 0.95
    tp_hi, tp_lo = entry * 1.10, entry * 0.90
    end48 = t0 + 2 * 86400000
    # strict48 半开区间: [entry, expiry) — 到期那一分钟不参与触发/MAE
    k48 = [x for x in k if x[0] < end48]
    if not k48:
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'entry': entry, 'result': '无数据', 'trigger': '无数据',
                'time': '-', 'price': None, 'reason_note': '48h窗口无K线',
                'dir_ok': None, 'dir_ret': None, 'max_retrace': None, 'max_retrace_no_sl': None}
    exp_bar = next((x for x in k if x[0] == end48), None)
    exit_price = float(exp_bar[1]) if exp_bar is not None else float(k48[-1][4])
    cur = exit_price  # 到期时刻可成交价: 到期分钟 open; 缺到期分钟则取最后一根 close
    max_retrace_no_sl = _adverse_pct(entry, direction, k48)
    max_retrace = max_retrace_no_sl  # 未触发时为持仓全窗口; 触发时下面截断
    if direction == 'SHORT':
        dir_ok = cur < entry
        dir_ret = (entry - cur) / entry * 100
    else:
        dir_ok = cur > entry
        dir_ret = (cur - entry) / entry * 100

    # 触发扫描只允许发生在 [entry, expiry) 内 (半开区间)
    for x in k48:
        h, l = float(x[2]), float(x[3])
        triggered = None
        if direction == 'SHORT':
            if h >= sl_hi:
                triggered = ('止损', h, 'high 打穿 +5%')
            elif l <= tp_lo:
                triggered = ('止盈', l, 'low 打穿 -10%')
        else:
            if l <= sl_lo:
                triggered = ('止损', l, 'low 打穿 -5%')
            elif h >= tp_hi:
                triggered = ('止盈', h, 'high 打穿 +10%')
        if triggered:
            trig, price, note = triggered
            max_retrace = _adverse_pct(entry, direction, [b for b in k48 if b[0] <= x[0]])
            return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                    'entry': entry, 'result': '-5.0%' if trig == '止损' else '+10.0%',
                    'trigger': trig, 'time': fmt(x[0]), 'price': price, 'reason_note': note,
                    'dir_ok': dir_ok, 'dir_ret': dir_ret,
                    'max_retrace': max_retrace, 'max_retrace_no_sl': max_retrace_no_sl}

    c = cur
    ret = (entry - c) / entry * 100 if direction == 'SHORT' else (c - entry) / entry * 100
    exit_ts = exp_bar[0] if exp_bar is not None else k48[-1][0]
    return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
            'entry': entry, 'result': f'{ret:+.2f}%', 'trigger': '未触发/48h平仓',
            'time': fmt(exit_ts), 'price': c, 'reason_note': '48h 到期未触发, 按到期分钟open/前一分钟close结算',
            'dir_ok': dir_ok, 'dir_ret': dir_ret,
            'max_retrace': max_retrace, 'max_retrace_no_sl': max_retrace_no_sl}


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

    print(f"\n{'日期':<5}{'方向':<4}{'币':<10}{'概率':<4}{'结果':<5}{'48h自然平仓'}")
    print('-' * 38)
    for r in results:
        # 已止盈/止损的单: 交易已结束, "48h自然平仓"假设收益不再展示(用户 8/3 定)
        if r.get('trigger') in ('止损', '止盈'):
            dir_ret = '-'
        else:
            dir_ret = f"{r['dir_ret']:+.1f}%" if r.get('dir_ret') is not None else '-'
        print(f"{r['date'][5:]:<5}{r['direction']:<4}{r['sym']:<10}{r['prob']:<4.1f}"
              f"{r['result']:<5}{dir_ret}")
    # 汇总
    closed = [r for r in results if r['result'] in ('-5.0%', '+10.0%')]
    stops = [r for r in closed if r['result'] == '-5.0%']
    takes = [r for r in closed if r['result'] == '+10.0%']
    swept = [r for r in stops if r.get('dir_ok')]
    print(f"\n已到期: {len(closed)}/{len(results)} 笔 | 止损 {len(stops)} / 止盈 {len(takes)}")
    if stops:
        print(f"止损中方向对(扫损): {len(swept)}/{len(stops)} | 方向错: {len(stops)-len(swept)}")

    # ===== 止损建议 =====
    with_retrace = [r for r in results if r.get('dir_ret') is not None]
    if with_retrace:
        print("\n===== 止损建议 (48h最大反向=裸奔全窗口MAE; 离场后延伸=触发离场后行情继续逆向的深度) =====")
        print(f"{'日期':<7}{'币':<12}{'向':<4}{'结果':<7}{'48h最大反向':<12}{'离场后延伸':<10}{'方向':<6}{'48h自然平仓'}")
        for r in with_retrace:
            d = '✅对' if r.get('dir_ok') else '❌错'
            trig = '✅止损' if r['result'] == '-5.0%' else ('✅止盈' if r['result'] == '+10.0%' else '48h')
            nosl = f"{r.get('max_retrace_no_sl', 0):.1f}%" if r.get('max_retrace_no_sl') is not None else '-'
            mr = r.get('max_retrace')
            ext = f"{r['max_retrace_no_sl'] - mr:.1f}%" if (r.get('max_retrace_no_sl') is not None and mr is not None
                                                             and r['result'] in ('-5.0%', '+10.0%')) else '-'
            print(f"{r['date'][5:]:<7}{r['sym']:<12}{r['direction']:<4}{trig:<7}{nosl:<12}{ext:<10}{d:<5}{r['dir_ret']:+.1f}%")
        # 最大止损建议: 用"假设不止损"全窗口反向深度
        swept_r = [r for r in with_retrace if r.get('dir_ok') and r['result'] == '-5.0%']
        if swept_r:
            mx = max(r.get('max_retrace_no_sl', 0) for r in swept_r)
            print(f"\n📏 最大止损建议: {mx:.1f}% + 缓冲 ≈ {mx+2:.1f}% (扫损单假设不止损反向最深 {mx:.2f}%)")
        # 综合止损模拟: 用正确的路径口径
        #  - 止盈单: 若更紧的 SL 会在止盈前被扫 -> -SL; 否则 +10%
        #  - 止损单: 若更宽的 SL 能扛过全窗口 -> 48h 自然收益; 否则 -SL
        #  - 未触发单: 全窗口最大反向决定 -SL 或自然收益
        print("\n📊 综合止损模拟 (SL 3%~20%, 每档总收益):")
        best_sl, best_pnl = None, -1e9
        for sl in range(3, 21):
            tot = 0.0
            for r in with_retrace:
                if r['result'] == '+10.0%':
                    if r.get('max_retrace') is not None and sl < r['max_retrace']:
                        tot += -sl
                    else:
                        tot += 10.0
                elif r['result'] == '-5.0%':
                    if sl > 5 and r.get('max_retrace_no_sl') is not None and sl >= r['max_retrace_no_sl']:
                        tot += r['dir_ret']
                    else:
                        tot += -sl
                else:
                    if r.get('max_retrace_no_sl') is not None and sl >= r['max_retrace_no_sl']:
                        tot += r['dir_ret']
                    else:
                        tot += -sl
            bar = ' ◀ 最优' if tot > best_pnl else ''
            if tot > best_pnl:
                best_pnl, best_sl = tot, sl
            print(f"  SL {sl:>2}%: 总收益 {tot:+.1f}%{bar}")
        n_total = len(results)
        n_closed = len(closed)
        cur_pnl = sum(-5 if r['result'] == '-5.0%' else (10 if r['result'] == '+10.0%' else 0) for r in results)
        print(f"\n🎯 综合止损建议: {best_sl}% ({n_closed}单已到期总收益 {best_pnl:+.1f}%; 现 5% 口径 {n_closed}单为 {cur_pnl:+.1f}%)")


if __name__ == '__main__':
    main()


def tables_html(results):
    """生成 HTML 表格(黑体/窄列, 邮件用) — 主表 + 止损建议表"""
    def esc(v):
        return str(v).replace('<', '&lt;').replace('>', '&gt;')
    style = ("border-collapse:collapse;font-family:'SimHei','Microsoft YaHei';font-size:11px;"
             "white-space:nowrap;")
    td = "border:1px solid #ddd;padding:2px 5px;text-align:left;"
    th = "border:1px solid #999;padding:2px 5px;background:#f5f5f5;"
    # 主表
    h = [f'<table style="{style}"><tr>']
    for c in ['日期', '方向', '币', '概率', '结果', '48h自然平仓']:
        h.append(f'<th style="{th}">{c}</th>')
    h.append('</tr>')
    for r in results:
        # 已止盈/止损的单: 交易已结束, "48h自然平仓"假设收益不再展示(用户 8/3 定)
        if r.get('trigger') in ('止损', '止盈'):
            dir_ret = '-'
        else:
            dir_ret = f"{r['dir_ret']:+.1f}%" if r.get('dir_ret') is not None else '-'
        h.append(f"<tr><td style='{td}'>{esc(r['date'][5:])}</td>"
                 f"<td style='{td}'>{r['direction']}</td>"
                 f"<td style='{td}'>{esc(r['sym'])}</td>"
                 f"<td style='{td}'>{r['prob']:.1f}</td>"
                 f"<td style='{td}'>{esc(r['result'])}</td>"
                 f"<td style='{td}'>{dir_ret}</td></tr>")
    h.append('</table>')
    # 止损建议表
    with_r = [r for r in results if r.get('dir_ret') is not None]
    if with_r:
        h.append('<br><b>止损建议 (48h最大反向=裸奔全窗口MAE; 离场后延伸=触发出场后行情继续逆向的深度, "-"=48h到期无离场)</b><br>')
        h.append('<table style="' + style + '"><tr>')
        for c in ['日期', '币', '方向', '结果', '48h最大反向', '离场后延伸', '方向对错', '48h自然平仓']:
            h.append(f'<th style="{th}">{c}</th>')
        h.append('</tr>')
        for r in with_r:
            d = '✅对' if r.get('dir_ok') else ('❌错' if r.get('dir_ok') is not None else '⏳')
            trig = '✅止损' if r['result'] == '-5.0%' else ('✅止盈' if r['result'] == '+10.0%' else '48h到期')
            ret_disp = f"{r['dir_ret']:+.1f}%" if r.get('dir_ret') is not None else '⏳未定'
            nosl = f"{r.get('max_retrace_no_sl', 0):.1f}%" if r.get('max_retrace_no_sl') is not None else '-'
            mr = r.get('max_retrace')
            if r.get('max_retrace_no_sl') is not None and mr is not None and r['result'] in ('-5.0%', '+10.0%'):
                ext = f"{r['max_retrace_no_sl'] - mr:.1f}%"
            else:
                ext = '-'
            # 研究重点高亮: 止损但方向对(扫损单) — 整行黄底, MAE 数字红色加粗; 止盈行 — 整行绿底
            swept = (r['result'] == '-5.0%' and r.get('dir_ok'))
            tp_row = (r['result'] == '+10.0%')
            row_td = td + ('background:#fff3cd;' if swept else ('background:#d9f2d9;' if tp_row else ''))
            mae_td = row_td + ('color:#c00;font-weight:bold;' if swept else '')
            h.append(f"<tr><td style='{row_td}'>{esc(r['date'][5:])}</td>"
                     f"<td style='{row_td}'>{esc(r['sym'])}</td>"
                     f"<td style='{row_td}'>{r['direction']}</td>"
                     f"<td style='{row_td}'>{trig}</td>"
                     f"<td style='{mae_td}'>{nosl}</td>"
                     f"<td style='{row_td}'>{ext}</td>"
                     f"<td style='{row_td}'>{d}</td>"
                     f"<td style='{row_td}'>{ret_disp}</td></tr>")
        h.append('</table>')
        # 盈利汇总: 实际执行口径(TP+10/SL-5/到期=dir_ret) vs 裸奔48h自然平仓口径(全部按dir_ret)
        n_tp = len([r for r in with_r if r['result'] == '+10.0%'])
        n_sl = len([r for r in with_r if r['result'] == '-5.0%'])
        n_48 = len(with_r) - n_tp - n_sl
        exec_pnl = sum(10.0 if r['result'] == '+10.0%' else (-5.0 if r['result'] == '-5.0%' else r['dir_ret'])
                       for r in with_r)
        hold_pnl = sum(r['dir_ret'] for r in with_r)
        diff = hold_pnl - exec_pnl
        c_e = '#0a0' if exec_pnl >= 0 else '#c00'
        c_h = '#0a0' if hold_pnl >= 0 else '#c00'
        c_d = '#0a0' if diff >= 0 else '#c00'
        h.append(f"<div style='font-size:11px;margin-top:4px;padding:3px 6px;background:#f5f5f5;'>"
                 f"💰 盈利汇总(共{len(with_r)}单 = 止盈{n_tp} + 止损{n_sl} + 48h到期{n_48}): "
                 f"① 实际执行(TP10/SL5/到期) <b style='color:{c_e};'>{exec_pnl:+.1f}%</b> | "
                 f"② 裸奔48h自然平仓 <b style='color:{c_h};'>{hold_pnl:+.1f}%</b> | "
                 f"差(②−①) <b style='color:{c_d};'>{diff:+.1f}%</b>"
                 f"{' (裸奔更优 — TP/SL在杀利润)' if diff > 0 else (' (执行更优 — TP/SL在保护)' if diff < 0 else '')}"
                 f"</div>")
        n_swept = len([r for r in with_r if r['result'] == '-5.0%' and r.get('dir_ok')])
        if n_swept:
            maes = [r.get('max_retrace_no_sl', 0) for r in with_r
                    if r['result'] == '-5.0%' and r.get('dir_ok') and r.get('max_retrace_no_sl') is not None]
            h.append(f"<div style='font-size:10px;color:#856404;background:#fff3cd;"
                     f"padding:2px 4px;'>黄底行 = 止损但方向对(扫损单) {n_swept}笔: "
                     f"这些单若裸奔扛过反向, 48h 终点为正收益 — 需承受 MAE "
                     f"中位 {sorted(maes)[len(maes)//2]:.1f}% / 最大 {max(maes):.1f}%</div>")
    return ''.join(h)


def settle_days(days, top10=False):
    """批量结算, 返回 results 列表 (供 main 与邮件 html 共用)"""
    results = []
    for day in days:
        pf = os.path.join(PRED_DIR, f'pred_{day}.json')
        if not os.path.exists(pf):
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
                continue
            direction, sym, prob = max(cands, key=lambda x: x[2])
            results.append(settle(sym, day, direction, prob))
    return results
