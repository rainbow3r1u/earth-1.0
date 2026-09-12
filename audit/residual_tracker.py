#!/usr/bin/env python3
"""RESIDUAL 影子臂结算 (2026-09-01 上线, GPU 180d双窗A/B胜出的残差标签方案)

假设来源: LONG标签的beta假阳性 — "48h涨过+5%"在普涨市里垃圾币跟涨也命中,
模型学的是高波动高热度而非选币alpha (8月两轮冲击: 8/30 LONG 10笔全灭-158.8U).
GPU验证: 原窗 Sharpe 32.75 vs 基线21.33; 换窗OFF30 26.89 vs 22.12; LONG砍头5笔后 +1309%(基线+870%).

结构: 与主臂混合结构完全相同的出场规则, 唯一差异 = LONG用残差标签模型的TOP10
     (SHORT与主臂相同, 不重复结算)
  LONG: 无止盈, SL -5%, 持有到48h终点

数据: 每日 pred 文件的 top10_long_residual 字段 (auto_dual_trade.py 影子模型旁路输出)
存档: data/residual_tracker.json
cron: 55 8 * * * (主臂hybrid_tracker 08:45 之后, 晨报09:00之前)
晨报: daily_digest_email.py 3.9节
"""
import os, sys, json, time, glob
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from forward_settle import PRED_DIR, ts_utc, fmt

TRACKER = os.path.join(BASE, '..', 'data', 'residual_tracker.json')
START = '2026-09-02'
NOTIONAL = 300.0

import requests
S = requests.Session()
DAY_MS = 86400000

def fetch_1m(sym, start_ms, end_ms, max_tries=3):
    """分页拉取1m K线; 数据不完整时返回 None(上层按"无数据"处理, 绝不用截断窗口结算)。

    2026-09-12 加固: 原实现在 3 次失败后 `break` 返回**已取部分**, 上层只查 `len(k) < 3` →
    截断发生在到期前时 SL 漏扫、用 k48[-1] 充当到期价, 且该日被 is_all_settled 标记已结算
    **永不重算**(永久错账)。现: 重试有上限 + 覆盖度校验, 不达标返回 None。
    """
    out = []; s = start_ms
    while s < end_ms:
        b = None
        for _ in range(max_tries):
            try:
                r = S.get('https://fapi.binance.com/fapi/v1/klines',
                          params={'symbol': sym, 'interval': '1m', 'startTime': s,
                                  'endTime': min(end_ms, s + 999*60000), 'limit': 1000},
                          timeout=15)
                if r.status_code == 200:
                    b = r.json(); break
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    print(f'[tracker] {sym} HTTP {r.status_code}, 不重试', flush=True)
                    return None
            except Exception:
                pass
            time.sleep(2)
        if b is None:
            print(f'[tracker] {sym} 重试{max_tries}次仍失败(已取{len(out)}根), 判为不完整', flush=True)
            return None
        if not b:
            break
        out.extend(b)
        s = b[-1][0] + 60000
        time.sleep(0.10)
    if not out:
        return out
    expect_end = min(end_ms, int(time.time() * 1000))
    if out[-1][0] < expect_end - 3 * 60000:
        print(f'[tracker] {sym} 覆盖不足(最后 {fmt(out[-1][0])} vs 应到 {fmt(expect_end)}), 判为不完整', flush=True)
        return None
    return out

def fetch_funding(sym, start_ms, end_ms):
    try:
        r = S.get('https://fapi.binance.com/fapi/v1/fundingRate',
                  params={'symbol': sym, 'startTime': start_ms, 'endTime': end_ms, 'limit': 1000},
                  timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def settle_one(sym, date_str, direction, prob):
    """与 hybrid_tracker.settle_hybrid 相同的结算规则 (LONG无TP/SL5/48h)"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)
    t_end = t0 + 3 * DAY_MS
    k = fetch_1m(sym, t0, t_end)

    def _nodata():
        # 2026-09-12: 统一"无数据"出口(含 fetch 不完整返回 None); '无数据' 不会被 is_all_settled
        # 视为已结算 → 次日重算(自愈), 是刻意设计。
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'entry': None, 'time': '-', 'net_u': None}

    if k is None or len(k) < 3:
        return _nodata()
    funding = fetch_funding(sym, t0, t_end)
    expiry = t0 + 2 * DAY_MS
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    k48 = [x for x in k if t0 <= x[0] < expiry]
    if not k48:
        return _nodata()
    entry = float(k48[0][1])
    if entry <= 0:
        return _nodata()

    sl_lo = entry * 0.95
    # 2026-09-12 修: 原按 [t0, expiry) 全窗口扣 funding → 提前止损的单也被扣满 48h 资金费。
    # 实测 IOSTUSDT(小时级资金费)一笔入场 24 分钟即止损的单被多扣 7.2U。现只计到实际离场时点。
    fund_events = [(int(e['fundingTime']), float(e['fundingRate'])) for e in funding]

    def _fund_cost(upto_ms):
        return sum(f for ft, f in fund_events if t0 < ft < min(upto_ms, expiry))

    fund_cost = _fund_cost(expiry)   # 默认=持满到到期; 止损分支按实际离场分钟重算
    FEE = 0.001 + 0.0002
    in_prog = now_ms < expiry
    scan = [x for x in k48 if x[0] <= now_ms] if in_prog else k48

    triggered = None
    for x in scan:
        l = float(x[3])
        if l <= sl_lo:
            triggered = x; break

    def net(gross, slippage=0.0002):
        return NOTIONAL * (gross - FEE - slippage - fund_cost)

    if triggered is not None:
        fund_cost = _fund_cost(triggered[0])   # 只计到触发分钟为止的资金费
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '-5.0%', 'trigger': '止损', 'time': fmt(triggered[0]),
                'net_u': round(net(-0.05, 0.0005), 2)}
    if in_prog:
        return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '⏳未到期', 'trigger': '进行中', 'time': '-', 'net_u': None}
    exp_bar = next((x for x in k if x[0] == expiry), None)
    exit_p = float(exp_bar[1]) if exp_bar is not None else float(k48[-1][4])
    gross = (exit_p/entry - 1)
    return {'symbol': sym, 'direction': direction, 'prob': prob, 'entry': entry,
            'result': f'{gross*100:+.1f}%', 'trigger': '到期', 'time': fmt(expiry),
            'net_u': round(net(gross), 2)}

def load_tracker():
    try:
        return json.load(open(TRACKER))
    except Exception:
        return {}

def save_tracker(t):
    # 2026-09-12 修: 原直接 open(TRACKER,'w') 非原子写, 中途被杀/磁盘满 → JSON 截断 →
    # 下次 load_tracker() 静默回 {} → 全量重算(下架币历史永久丢失)。改用 tmp+os.replace。
    tmp = TRACKER + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TRACKER)

def is_all_settled(day_entry):
    trs = day_entry.get('trades', [])
    return bool(trs) and all(r.get('result') not in ('⏳未到期', '无数据') for r in trs)

def main():
    # 2026-09-12: --force / TRACKER_FORCE=1 = 回刷模式(忽略"已全结算则跳过", 全量重算)。
    #   用途: 结算口径修正后把历史账面统一到新口径(如 funding 只计到实际离场)。
    #   安全保护: 重算某笔若得到"无数据"(历史 1m 已过期/币下架), 而旧记录有真实 net_u → 沿用旧值,
    #   绝不用"无数据"覆盖已有好数据。
    force = ('--force' in sys.argv) or os.environ.get('TRACKER_FORCE') == '1'
    tracker = load_tracker()
    days = sorted(glob.glob(os.path.join(PRED_DIR, 'pred_*.json')))
    days = [os.path.basename(f).replace('pred_', '').replace('.json', '') for f in days
            if os.path.basename(f) >= f'pred_{START}.json']
    now = datetime.now(timezone.utc).isoformat()
    settled = pending = 0
    for day in days:
        if day in tracker and is_all_settled(tracker[day]) and not force:
            settled += 1
            continue
        pf = os.path.join(PRED_DIR, f'pred_{day}.json')
        try:
            d = json.load(open(pf))
        except Exception:
            continue
        cands = d.get('top10_long_residual', [])
        if not cands:
            if day not in tracker:
                tracker[day] = {'updated': now, 'day_pnl_u': 0.0, 'n_settled': 0, 'n_total': 0,
                                 'trades': [], 'note': '无top10_long_residual字段'}
            continue
        trades = [settle_one(item['symbol'], day, 'LONG', float(item['prob'])) for item in cands]
        if force and day in tracker:
            old_map = {(t.get('symbol'), t.get('direction')): t for t in tracker[day].get('trades', [])}
            guarded, kept = [], 0
            for r in trades:
                if r.get('net_u') is None:
                    o = old_map.get((r.get('symbol'), r.get('direction')))
                    if o is not None and o.get('net_u') is not None:
                        guarded.append(o); kept += 1; continue
                guarded.append(r)
            trades = guarded
            if kept:
                print(f'  {day}: 重算无数据的 {kept} 笔沿用旧记录(防历史数据丢失)', flush=True)
        ok = [t for t in trades if t.get('net_u') is not None]
        day_u = sum(t['net_u'] for t in ok)
        tracker[day] = {'updated': now, 'day_pnl_u': round(day_u, 1),
                        'n_settled': len(ok), 'n_total': len(trades), 'trades': trades}
        if is_all_settled(tracker[day]):
            settled += 1
        else:
            pending += 1
        print(f'  {day}: {len(ok)}/{len(trades)}笔已结算, 当日累计 {day_u:+.1f}U', flush=True)
    save_tracker(tracker)
    try:
        hb = json.load(open(os.path.join(BASE, '..', 'data', 'hybrid_tracker.json')))

        def _settled(d):
            return {x for x in d if d[x].get('n_settled', 0) >= d[x].get('n_total', 99) and d[x].get('trades')}

        # 2026-09-12 修: 原先两侧各算各的"全结算日集合"(主臂含SHORT, 集合大小常不同)却标成"同期"→
        # 两臂实际不是同一窗口、对比失真。现取两臂交集, 保证同日对比。
        common = sorted(x for x in (_settled(hb) & _settled(tracker)) if x >= START)
        hb_long = sum(t['net_u'] for x in common for t in hb[x].get('trades', [])
                      if t.get('net_u') is not None and t['direction'] == 'LONG')
        rs_tot = sum(tracker[x].get('day_pnl_u', 0) for x in common)
        print(f'[residual_tracker] 完成: 已到期{settled} 未到期{pending} | '
              f'同期(两臂交集{len(common)}天) 影子臂LONG {rs_tot:+.1f}U vs 主臂LONG {hb_long:+.1f}U')
    except Exception as e:
        print(f'[residual_tracker] 完成: 已到期{settled} 未到期{pending}, 主臂对照读取失败: {e}')

if __name__ == '__main__':
    main()
