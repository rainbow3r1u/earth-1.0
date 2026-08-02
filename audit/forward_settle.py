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
    """返回 dict: 入场/触发类型/时间/价格/结果%; 未触发 → 当前浮盈
    附加: 方向判定(dir_ok=价格最终朝开仓方向走) + 不止损48h收益(dir_ret)"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)
    t_end = t0 + 3 * 86400000
    k = fetch_1m(sym, t0, t_end)
    if len(k) < 3:
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'result': '无数据', 'entry': None, 'dir_ok': None, 'dir_ret': None, 'max_retrace': None}
    # 未到期判定: T+2 日线收盘 = 预测日 +3 天 00:00 UTC; 未到期不给具体结果(8/2 用户要求)
    expiry_ms = ts_utc(*map(int, date_str.split('-')), 0, 0) + 3 * 86400000
    if datetime.now(timezone.utc).timestamp() * 1000 < expiry_ms:
        return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                'entry': float(k[0][1]), 'result': '⏳未到期', 'trigger': '进行中',
                'time': '-', 'price': None, 'reason_note': '48h 窗口未走完',
                'dir_ok': None, 'dir_ret': None, 'max_retrace': None}
    entry = float(k[0][1])
    sl_hi, sl_lo = entry * 1.05, entry * 0.95
    tp_hi, tp_lo = entry * 1.10, entry * 0.90
    # 48h 判定价: 窗口内最后一根收盘 (T+2 已过则为收盘, 未过则为当前最新)
    cur = float(k[-1][4])
    # 最大反向深度: 48h 窗口内价格相对入场最深反向(不设止损时的最深不利波动)
    max_h = max(float(x[2]) for x in k)
    min_l = min(float(x[3]) for x in k)
    if direction == 'SHORT':
        max_retrace = (max_h - entry) / entry * 100
        dir_ok = cur < entry
        dir_ret = (entry - cur) / entry * 100
    else:
        max_retrace = (entry - min_l) / entry * 100
        dir_ok = cur > entry
        dir_ret = (cur - entry) / entry * 100
    for x in k:
        h, l = float(x[2]), float(x[3])
        if direction == 'SHORT':
            if h >= sl_hi:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '-5.0%', 'trigger': '止损',
                        'time': fmt(x[0]), 'price': h, 'reason_note': 'high 打穿 +5%',
                        'dir_ok': dir_ok, 'dir_ret': dir_ret, 'max_retrace': max_retrace}
            if l <= tp_lo:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '+10.0%', 'trigger': '止盈',
                        'time': fmt(x[0]), 'price': l, 'reason_note': 'low 打穿 -10%',
                        'dir_ok': dir_ok, 'dir_ret': dir_ret, 'max_retrace': max_retrace}
        else:
            if l <= sl_lo:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '-5.0%', 'trigger': '止损',
                        'time': fmt(x[0]), 'price': l, 'reason_note': 'low 打穿 -5%',
                        'dir_ok': dir_ok, 'dir_ret': dir_ret, 'max_retrace': max_retrace}
            if h >= tp_hi:
                return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
                        'entry': entry, 'result': '+10.0%', 'trigger': '止盈',
                        'time': fmt(x[0]), 'price': h, 'reason_note': 'high 打穿 +10%',
                        'dir_ok': dir_ok, 'dir_ret': dir_ret, 'max_retrace': max_retrace}
    c = float(k[-1][4])
    ret = (entry - c) / entry * 100 if direction == 'SHORT' else (c - entry) / entry * 100
    return {'sym': sym, 'date': date_str, 'direction': direction, 'prob': prob,
            'entry': entry, 'result': f'{ret:+.2f}%', 'trigger': '未到期/未触发',
            'time': fmt(k[-1][0]), 'price': c, 'reason_note': '48h 窗口未触发, 当前价结算',
            'dir_ok': dir_ok, 'dir_ret': dir_ret, 'max_retrace': max_retrace}

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

    print(f"\n{'日期':<5}{'方向':<4}{'币':<10}{'概率':<4}{'结果':<5}{'对错':<6}{'无止损48h'}")
    print('-' * 38)
    for r in results:
        if r.get('dir_ok') is None:
            dir_s = '-'
        else:
            dir_s = '✅扫损' if (r['dir_ok'] and r['trigger'] == '止损') else ('✅对' if r['dir_ok'] else '❌错')
        dir_ret = f"{r['dir_ret']:+.1f}%" if r.get('dir_ret') is not None else '-'
        print(f"{r['date'][5:]:<5}{r['direction']:<4}{r['sym']:<10}{r['prob']:<4.1f}"
              f"{r['result']:<5}{dir_s:<6}{dir_ret}")
    # 汇总
    closed = [r for r in results if r['result'] in ('-5.0%', '+10.0%')]
    stops = [r for r in closed if r['result'] == '-5.0%']
    takes = [r for r in closed if r['result'] == '+10.0%']
    swept = [r for r in stops if r.get('dir_ok')]
    print(f"\n已到期: {len(closed)}/{len(results)} 笔 | 止损 {len(stops)} / 止盈 {len(takes)}")
    if stops:
        print(f"止损中方向对(扫损): {len(swept)}/{len(stops)} | 方向错: {len(stops)-len(swept)}")

    # ===== 止损建议 =====
    with_retrace = [r for r in results if r.get('max_retrace') is not None and r.get('dir_ret') is not None]
    if with_retrace:
        print("\n===== 止损建议 (基于最大反向深度) =====")
        print(f"{'日期':<7}{'币':<12}{'向':<4}{'止损':<7}{'反向':<8}{'方向':<6}{'无止损48h'}")
        for r in with_retrace:
            d = '✅对' if r.get('dir_ok') else '❌错'
            trig = '✅止损' if r['result'] == '-5.0%' else ('✅止盈' if r['result'] == '+10.0%' else '⏳未')
            print(f"{r['date'][5:]:<7}{r['sym']:<12}{r['direction']:<4}{trig:<7}{r['max_retrace']:>4.1f}%  {d:<5}{r['dir_ret']:+.1f}%")
        # 最大止损建议: 扫损单所需最小止损 = max(扫损单反向深度) + 缓冲
        swept_r = [r for r in with_retrace if r.get('dir_ok') and r['result'] == '-5.0%']
        if swept_r:
            mx = max(r['max_retrace'] for r in swept_r)
            p90 = sorted(r['max_retrace'] for r in swept_r)[-1]  # 样本少, 用 max
            print(f"\n📏 最大止损建议: {mx:.1f}% + 缓冲 ≈ {mx+2:.1f}% (扫损单反向最深 {mx:.2f}%, 止损放这以上基本不被扫)")
        # 综合止损建议: 模拟不同 SL 下总收益 (SL' < 反向深度 → 被扫亏 -SL'; SL' >= 反向深度 → 持有到48h 得 dir_ret; 止盈单不受影响 +10%)
        print("\n📊 综合止损模拟 (SL 3%~20%, 每档总收益):")
        best_sl, best_pnl = None, -1e9
        for sl in range(3, 21):
            tot = 0.0
            for r in with_retrace:
                if r['result'] == '+10.0%':
                    tot += 10.0
                elif r.get('max_retrace') is not None and sl >= r['max_retrace']:
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
