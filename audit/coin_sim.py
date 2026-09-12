#!/usr/bin/env python3
"""逐币模拟器 (2026-09-13 建) — 口径已对齐影子档, 一致率 93.4%

用途: 把"每日 TOP10 榜单"里的**每一个币**独立结算, 得到 ~10 倍于影子档的样本量,
      用于回答影子档(每日仅 1 个批)样本不足的结构级问题。

═══════════════════════════════════════════════════════════════════════════════
⚠️ 口径标定过程 (2026-09-13, 血泪教训 — 改口径前务必重读)
═══════════════════════════════════════════════════════════════════════════════
第一版模拟器用「入场 = 预测日 T+1 开盘, 扫 T+1..T+3」, 与影子档止损判定一致率仅 66.9%,
据此得出的"纠缠闸门成立"(甚至方向相反)结论**全部作废**。定位过程:

  ① 影子档的 entry 价 100% 落在 **T0(预测日当天)** 的日K [low,high] 区间内, T+1 一个都没有
     → 入场是**预测日当天**, 不是次日
  ② 逐一比对 5 种口径组合(290 笔配对)的止损判定一致率:
       入场T0 + 扫T0..T2 + 平T2  → **93.4%**  ✅ 采用
       入场T0 + 扫T0..T3 + 平T3  → 90.0%
       入场T0 + 扫T1..T2 + 平T2  → 87.6%
       入场T1 + 扫T1..T3 + 平T3  → 66.9%   ❌ 第一版用的(错位一天)
       入场T1 + 扫T2..T3 + 平T3  → 63.8%
  ③ 对标验证: 本模拟器止损率 45.9% vs 影子档 ~46%  ✅

⚠️ 已知残余偏差: 影子档用 1m 级数据(入场价≈08:21成交价 "08:21:00+滑点"), 本模拟器用日K
   开盘价; 日K 的 T0 那根包含 08:21 之前的 8 小时(UTC 日切) → 入场日"开盘前扎针"的假止损
   两臂共担, 但**不对称**, 故绝对水位仍有偏, 只可用于**相对 A/B 排序**。

⚠️ 铁律 4 适用: 本模拟器**绝对水位不可作为生产预期**, 只用于管道内 A/B 相对排序。
   任何"结构级"结论仍建议用 GPU 端 TOP10 模拟器(须显式 SL_PCT=8)做二次确认。

═══════════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════════
  python3 audit/coin_sim.py                    # 全量结算 + 自检对标
  python3 audit/coin_sim.py --btc-gate 1.75    # 附: 按 |gap| 分档统计(纠缠闸门追踪)
  python3 audit/coin_sim.py --json out.json    # 导出逐笔明细

依赖: 仅本机数据 —— data/pred_*.json(榜单) + backtester/data_cache/notusdt_1d_full.json(宇宙日K)
      + Binance 公开 API(BTC 日线, 用于 |gap|; 不可达时自动降级为"无 BTC 分档")
"""
import os, sys, json, glob, re, argparse, datetime

BASE = '/home/myuser/websocket_new'
KLINE_CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
BTC_CACHE = '/tmp/btc_1d.json'          # 复用; 缺失时从 API 取
SL_PCT = 0.08                            # 与实盘/官方变体档一致 (2026-09-07 由 5% 调)
HOLD_DAYS = 3                            # 入场日 T0 起算; 扫 T0..T2, 平 T2
DAY_MS = 86400000


def _dt(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')


def load_universe():
    kl = json.load(open(KLINE_CACHE))['klines']
    return {s: {_dt(r['t']): r for r in rows} for s, rows in kl.items()}


def btc_gap_series():
    """BTC EMA7-EMA28 gap%(取已收盘bar). 不可用返回 {}"""
    if not os.path.exists(BTC_CACHE):
        try:
            import urllib.request
            with urllib.request.urlopen(
                    'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=500',
                    timeout=20) as r:
                json.dump(json.load(r), open(BTC_CACHE, 'w'))
        except Exception as e:
            print(f'[warn] BTC 日线获取失败({e}), |gap| 分档降级跳过', file=sys.stderr)
            return {}
    try:
        raw = json.load(open(BTC_CACHE))
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        # ⚠️ 排除未收盘 bar: 缓存当日那根的 close 是盘中中间值
        bars = [r for r in raw if int(r[0]) + DAY_MS <= now]
        if len(bars) < 40:
            return {}
        closes = [float(r[4]) for r in bars]
        dates = [_dt(int(r[0])) for r in bars]

        def _ema(vals, n):
            k = 2.0 / (n + 1)
            out, prev = [], None
            for v in vals:
                prev = v if prev is None else v * k + prev * (1 - k)
                out.append(prev)
            return out
        e7, e28 = _ema(closes, 7), _ema(closes, 28)
        return {dates[i]: (e7[i] - e28[i]) / e28[i] * 100 for i in range(len(closes))}
    except Exception as e:
        print(f'[warn] BTC gap 计算失败({e})', file=sys.stderr)
        return {}


def payout(entry, slp, trig, exit_price):
    """返回净收益% (与影子档同为"价格口径", 不含手续费; 影子档 netU 含费, 故绝对值有系统差)"""
    px = slp if trig else exit_price
    return (px / entry - 1) * 100


def settle_all(universe, sl=SL_PCT, hold=HOLD_DAYS, max_date=None, min_bars=3):
    """按标定口径结算所有 pred 存档里的 top10_long
    口径: 入场 = T0 open; 扫 T0..T(HOLD-1) 的 low <= entry*(1-sl) 即止损; 否则平 T(HOLD-1) open
    """
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    out = []
    for f in sorted(glob.glob(os.path.join(BASE, 'data/pred_2026-*.json'))):
        m = re.search(r'(\d{4}-\d{2}-\d{2})', f)
        if not m:
            continue
        d = m.group(1)
        if max_date and d > max_date:
            continue
        try:
            pred = json.load(open(f))
        except Exception:
            continue
        for x in (pred.get('top10_long') or []):
            s = x.get('symbol')
            if not s or s not in universe or d not in universe[s]:
                continue
            order = sorted(universe[s])
            oi = order.index(d)
            if oi + hold - 1 >= len(order):
                continue
            last = universe[s][order[oi + hold - 1]]
            # 出场 bar 必须已收盘, 否则用半根 bar 的 open 会污染结果
            if last['t'] + DAY_MS > now:
                continue
            entry = universe[s][order[oi]]['o']
            if not entry or entry <= 0:
                continue
            slp = entry * (1 - sl)
            trig, low = None, None
            for j in range(0, hold):
                r = universe[s][order[oi + j]]
                lr = (r['l'] / entry - 1) * 100
                low = lr if low is None else min(low, lr)
                if trig is None and r['l'] <= slp:
                    trig = j
            try:
                prob = float(x.get('prob'))
            except (TypeError, ValueError):
                prob = None
            out.append(dict(date=d, symbol=s, prob=prob, entry=entry, sl_price=slp,
                            trigger=trig, stop_day=None if trig is None else f'T+{trig}',
                            net_pct=payout(entry, slp, trig, last['o']),
                            low_pct=low, exit_date=order[oi + hold - 1]))
    return out


def _pearson(xs, ys):
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--btc-gate', type=float, default=None,
                    help='按前日 |gap| 该阈值分档统计(原纠缠闸门口径 1.75)')
    ap.add_argument('--json', default=None, help='导出逐笔明细')
    ap.add_argument('--sl', type=float, default=SL_PCT)
    ap.add_argument('--hold', type=int, default=HOLD_DAYS)
    ap.add_argument('--max-date', default=None, help='只结算到该日期(YYYY-MM-DD)')
    a = ap.parse_args()

    uni = load_universe()
    trades = settle_all(uni, sl=a.sl, hold=a.hold, max_date=a.max_date)
    if not trades:
        print('无样本'); return 1
    days = sorted(set(t['date'] for t in trades))
    n = len(trades)
    stops = sum(1 for t in trades if t['trigger'] is not None)
    nets = [t['net_pct'] for t in trades]
    print(f'=== 逐币模拟器 (口径: 入场T0 open / SL-{a.sl:.0%} / 扫T0..T{a.hold-1} / 平T{a.hold-1} open) ===')
    print(f'  样本: {n} 笔 / {len(days)} 日  ({days[0]} ~ {days[-1]})')
    print(f'  止损率 {stops/n:.1%}   (对标影子档 ~46% ← 校准基准)')
    print(f'  每笔净收益 均值 {sum(nets)/n:+.3f}%  中位 {sorted(nets)[n//2]:+.3f}%')
    pos = [x for x in nets if x > 0]
    print(f'  正收益 {len(pos)}/{n} = {len(pos)/n:.1%}  其中 >=+20% 的 {sum(1 for x in pos if x >= 20)} 笔')
    print()

    if a.btc_gate is not None:
        gap = btc_gap_series()
        if not gap:
            print('[skip] BTC gap 不可用'); return 0
        bd = sorted(gap)
        def prevgap(d):
            if d not in gap:
                return None
            i = bd.index(d)
            return abs(gap[bd[i - 1]]) if i >= 1 else None
        BYD = {}
        for t in trades:
            g = prevgap(t['date'])
            if g is None:
                continue
            BYD.setdefault(t['date'], []).append((t['net_pct'], t['trigger'] is not None, g))
        dys = sorted(BYD)
        lo = [d for d in dys if BYD[d][0][2] <= a.btc_gate]
        hi = [d for d in dys if BYD[d][0][2] > a.btc_gate]
        print(f'=== |gap| 分档追踪 (阈值 {a.btc_gate}%) — ⚠️ 未证实, 仅追踪 ===')
        for lbl, g in [('贴近(≤阈值)', lo), ('分离(>阈值)', hi)]:
            if not g:
                continue
            nn = [x[0] for d in g for x in BYD[d]]
            sr = sum(1 for d in g for x in BYD[d] if x[1]) / len(nn)
            print(f'  {lbl}: {len(g):2d}日/{len(nn):3d}笔  每笔 {sum(nn)/len(nn):+6.2f}%  止损率 {sr:.1%}')
        print('  注: 2026-09-13 的 649笔/65日 复核中该分档不显著(95%CI 跨零), 只作零成本追踪')
    if a.json:
        json.dump(trades, open(a.json, 'w'), ensure_ascii=False)
        print(f'  明细已导出: {a.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
