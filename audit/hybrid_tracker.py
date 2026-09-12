#!/usr/bin/env python3
"""混合结构影子臂结算 (2026-08-24 上线)

结构 (基于 8/3~8/22 400笔 1m 回测结论, 每条有市场机制解释):
  LONG:  无止盈, SL -5%, 未触发则持有到 48h 终点平仓   → 吃终点趋势尾巴 (标签语义)
  SHORT: 现行 TP +10% / SL -5%                        → 吃瞬时下杀, 50%会V回故快速止盈

口径: 入场 00:05 UTC (08:05 CST), strict48 半开区间 [entry, entry+48h),
      同分钟双触发 SL_FIRST, TIMEOUT=到期分钟open, 含费(taker0.1%+滑点0.02%+SL0.05%)+真实资金费。
存档: data/hybrid_tracker.json  (与 forward_tracker.json 独立, 只读预测文件)
cron: 45 8 * * * (2026-09-12 更正: 原文档写 "20 9 * * *", 与实际 crontab 不符)
晨报: daily_digest_email.py 4a5 节读取。
"""
import os, sys, json, time, glob, requests
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from forward_settle import PRED_DIR, ts_utc, fmt, _adverse_pct

TRACKER = os.path.join(BASE, '..', 'data', 'hybrid_tracker.json')
START = '2026-08-03'          # 幽灵修复后首日
NOTIONAL = 300.0              # 每笔名义 (与晨报 forward 口径一致)
S = requests.Session()
DAY_MS = 86400000

# ==== 影子变体档 (2×2 对照用, 2026-09-12 新增; 默认关闭) ====
# 背景: 实盘已改为 72h+SL-8%(果 9/3、米 9/8), 而本影子臂固定 48h+SL-5% —— 两个变量**同时**不同,
# 导致 10/23 终审做 "72h vs 48h" 时 SL 变量未被控制(归因被污染)。
# 主档 = SL-5%/48h(不变); 设 SHADOW_VARIANT 可另存一个**独立**对照档补齐 2×2 网格:
#   SHADOW_VARIANT=sl8    python3 audit/hybrid_tracker.py   → data/hybrid_tracker_sl8.json    (SL8/48h)
#   SHADOW_VARIANT=h72    python3 audit/hybrid_tracker.py   → data/hybrid_tracker_h72.json    (SL5/72h)
#   SHADOW_VARIANT=sl8h72 python3 audit/hybrid_tracker.py   → data/hybrid_tracker_sl8h72.json (SL8/72h, 对齐实盘)
# 未设该环境变量时(含 cron)参数取默认值 → 行为与旧版逐位一致; 变体档不覆盖主档。
VARIANTS = {
    'sl8':    {'sl_pct': 0.08, 'hold_days': 2, 'file': 'hybrid_tracker_sl8.json',    'tag': 'SL-8%/48h'},
    'h72':    {'sl_pct': 0.05, 'hold_days': 3, 'file': 'hybrid_tracker_h72.json',    'tag': 'SL-5%/72h'},
    'sl8h72': {'sl_pct': 0.08, 'hold_days': 3, 'file': 'hybrid_tracker_sl8h72.json', 'tag': 'SL-8%/72h(对齐实盘)'},
}

def fetch_1m(sym, start_ms, end_ms, max_tries=3):
    """分页拉取1m K线; 数据不完整时返回 None(上层按"无数据"处理, 绝不用截断窗口结算)。

    2026-09-12 加固: 原实现在 3 次失败后 `break` 返回**已取部分**, 而上层只查 `len(k) < 3` →
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

def settle_hybrid(sym, date_str, direction, prob, sl_pct=0.05, hold_days=2):
    """混合结构结算: LONG 无TP(SL/到期平) / SHORT TP10%/SL。默认 SL-5% / 48h —— 与原口径一致。

    2026-09-12 参数化(sl_pct/hold_days): 实盘自 9/3(果)/9/8(米) 起为 72h+SL-8%, 而本影子臂固定
    48h+SL-5%, 两个变量同时不同 → 10/23 终审做 "72h vs 48h" 时 SL 变量未被控制(归因被污染)。
    参数化后可输出独立的 2×2 对照档(见文件头 SHADOW_VARIANT 说明); 默认参数与旧行为逐位一致。
    返回 dict: entry/result/trigger/net_pnl_u(300U名义)
    8/24 晚: 入场 00:05→00:21 UTC (08:21 CST), 与 3.7/实盘口径对齐(用户拍板)。"""
    t0 = ts_utc(*map(int, date_str.split('-')), 0, 21)   # 00:21 UTC 入场
    t_end = t0 + 3 * DAY_MS
    k = fetch_1m(sym, t0, t_end)

    def _nodata():
        # 2026-09-12: 统一"无数据"出口(含 fetch 不完整返回 None 的情形); '无数据' 不会被
        # is_all_settled 视为已结算 → 次日会重算(自愈), 是刻意设计。
        return {'sym': sym, 'direction': direction, 'prob': prob, 'result': '无数据',
                'trigger': '无数据', 'entry': None, 'time': '-', 'net_u': None}

    if k is None or len(k) < 3:
        return _nodata()
    funding = fetch_funding(sym, t0, t_end)
    expiry = t0 + hold_days * DAY_MS
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    k48 = [x for x in k if t0 <= x[0] < expiry]
    if not k48:
        return _nodata()
    entry = float(k48[0][1])
    if entry <= 0:
        return _nodata()

    sl_hi, sl_lo = entry * (1 + sl_pct), entry * (1 - sl_pct)
    tp_hi, tp_lo = entry * 1.10, entry * 0.90
    # 2026-09-12 修: 原按 [t0, expiry) 全窗口扣 funding → 提前 SL/TP 出场的单也被扣满 48h 资金费,
    # 影子账面系统性高估成本(而它正是用户每日对照的基准)。现只计到"实际离场时点"。
    fund_events = [(int(e['fundingTime']), float(e['fundingRate'])) for e in funding]

    def _fund_cost(upto_ms):
        s = sum(f for ft, f in fund_events if t0 < ft < min(upto_ms, expiry))
        return s if direction == 'LONG' else -s

    fund_cost = _fund_cost(expiry)   # 默认=持满到到期; 触发分支按实际离场分钟重算
    FEE = 0.001 + 0.0002   # taker×2 + 入场滑点
    in_prog = now_ms < expiry
    scan = [x for x in k48 if x[0] <= now_ms] if in_prog else k48

    triggered = None
    for x in scan:
        h, l = float(x[2]), float(x[3])
        hit_sl = (l <= sl_lo) if direction == 'LONG' else (h >= sl_hi)
        hit_tp = (h >= tp_hi) if direction == 'LONG' else (l <= tp_lo)
        if hit_sl:                     # 同分钟双触发 SL_FIRST
            triggered = ('SL', x); break
        # LONG 无止盈: 直接跳过 TP 检测
        if direction == 'SHORT' and hit_tp:
            triggered = ('TP', x); break

    def net(gross, slippage=0.0002):
        return NOTIONAL * (gross - FEE - slippage - fund_cost)

    if triggered:
        kind, x = triggered
        fund_cost = _fund_cost(x[0])   # 只计到触发分钟为止的资金费
        if kind == 'SL':
            return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                    'result': f'-{sl_pct*100:.1f}%', 'trigger': '止损', 'time': fmt(x[0]),
                    'net_u': round(net(-sl_pct, 0.0005), 2)}
        return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '+10.0%', 'trigger': '止盈', 'time': fmt(x[0]),
                'net_u': round(net(0.10), 2)}
    if in_prog:
        return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
                'result': '⏳未到期', 'trigger': '进行中', 'time': '-', 'net_u': None}
    # TIMEOUT: 到期分钟 open
    exp_bar = next((x for x in k if x[0] == expiry), None)
    exit_p = float(exp_bar[1]) if exp_bar is not None else float(k48[-1][4])
    gross = (exit_p/entry - 1) if direction == 'LONG' else (1 - exit_p/entry)
    return {'sym': sym, 'direction': direction, 'prob': prob, 'entry': entry,
            'result': f'{gross*100:+.1f}%', 'trigger': '到期', 'time': fmt(expiry),
            'net_u': round(net(gross), 2)}

def load_tracker(path=None):
    try:
        return json.load(open(path or TRACKER))
    except Exception:
        return {}

def save_tracker(t, path=None):
    # 2026-09-12 修: 原直接 open(TRACKER,'w') 非原子写, 中途被杀/磁盘满 → JSON 截断,
    # 下次 load_tracker() 静默回 {} → 全量重算(下架币历史将永久丢失)。改用 tmp+os.replace。
    p = path or TRACKER
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)

def is_all_settled(day_entry):
    trs = day_entry.get('trades', [])
    return bool(trs) and all(r.get('result') not in ('⏳未到期', '无数据') for r in trs)

def main():
    # ==== 2×2 对照档(可选, 默认关闭) ====
    # 背景: 实盘 9/3(果)/9/8(米) 起 72h+SL-8%, 影子臂固定 48h+SL-5% → 10/23 终审做
    # "72h vs 48h" 时 SL 变量未被控制。为补对照, 支持输出**独立**变体档(不覆盖现有 hybrid_tracker.json):
    #   用法: SHADOW_VARIANT=sl8h72 python3 audit/hybrid_tracker.py
    #   输出: data/hybrid_tracker_sl8h72.json (SL-8%/72h; 与现有档同源同日, 可直接 2×2 比较)
    # 未设该环境变量时(含cron), 参数取默认值 → 与旧行为逐位一致。
    var = VARIANTS.get(os.environ.get('SHADOW_VARIANT', '').strip())
    # 2026-09-12: --force / TRACKER_FORCE=1 = 回刷模式(忽略"已全结算则跳过", 全量重算),
    # 用于结算口径修正后把历史账面统一到新口径。安全保护见下方"沿用旧记录"。
    force = ('--force' in sys.argv) or os.environ.get('TRACKER_FORCE') == '1'
    path = TRACKER if var is None else os.path.join(os.path.dirname(TRACKER), var['file'])
    settle_kw = {} if var is None else {'sl_pct': var['sl_pct'], 'hold_days': var['hold_days']}
    if var:
        print(f'[hybrid_tracker] 变体档 {var["tag"]} → {var["file"]}', flush=True)
    if force:
        print('[hybrid_tracker] 回刷模式: 全量重算(含已全结算日)', flush=True)
    tracker = load_tracker(path)
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
        # 2026-09-12 修: 原无 try/except → 单个 pred 损坏(或写到一半)会让整轮崩、save_tracker 不执行。
        # (residual_tracker 侧本就有保护, 这里补齐)
        try:
            d = json.load(open(pf))
        except Exception as e:
            print(f'  {day}: pred 读取失败({e}), 跳过该日', flush=True)
            continue
        trades = []
        for side, key in [('LONG', 'top10_long'), ('SHORT', 'top10_short')]:
            for item in d.get(key, []):
                r = settle_hybrid(item['symbol'], day, side, float(item['prob']), **settle_kw)
                r['symbol'] = r.pop('sym')
                trades.append(r)
        if force and day in tracker:
            # 回刷保护: 重算得"无数据"(历史1m过期/币下架)而旧记录有真实 net_u → 沿用旧值
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
        # 日汇总
        ok = [t for t in trades if t.get('net_u') is not None]
        day_u = sum(t['net_u'] for t in ok)
        tracker[day] = {'updated': now, 'day_pnl_u': round(day_u, 1),
                        'n_settled': len(ok), 'n_total': len(trades), 'trades': trades}
        if is_all_settled(tracker[day]):
            settled += 1
        else:
            pending += 1
        print(f'  {day}: {len(ok)}/{len(trades)}笔已结算, 当日累计 {day_u:+.1f}U', flush=True)
    save_tracker(tracker, path)
    # 累计
    tot = sum(v.get('day_pnl_u', 0) for v in tracker.values())
    print(f'[hybrid_tracker] 完成{"[" + var["tag"] + "]" if var else ""}: 已到期 {settled}, 未到期 {pending}, '
          f'累计 {tot:+.1f}U, 存档 {path}')

if __name__ == '__main__':
    main()
