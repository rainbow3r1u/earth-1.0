#!/usr/bin/env python3
"""纠缠闸门扩展样本检验 (2026-09-13) — 在 GPU 上跑完 350 日 walk-forward 后, 本地跑这个分析。

背景: 2026-09-13 的 65 天样本(649笔)无法判定"BTC EMA7/EMA28 纠缠 = 系统阿尔法回归"这一命题
      (批级 t=1.23 · 币级 95%CI 跨零 · 止损率四档无差别)。GPU 端 by_day 缓存有 455 天可用,
      walk-forward 重训约 10 分钟即可把样本扩到 ~7 倍。

前置: GPU 上执行(约 10 分钟)
    cd ~/websocket_new && env NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 \\
        LONG_MOM_FILTER=0 SL_PCT=8 TOP10_LONG=1 GATE_DUMP=/tmp/gate_cands.json \\
        python3 gpu_backtest_exp.py 350 1
    然后取回: scp -P 22172 linux@175.155.64.171:/tmp/gate_cands.json .

用法: python3 audit/gate_ext_test.py --cands gate_cands.json [--btc /tmp/btc_1d.json]

结算口径与 audit/coin_sim.py 完全一致(已与影子档标定 93.4%):
    入场 = T0 open; 扫 T0..T2 的 low <= entry*(1-SL) 即止损; 否则平 T2 open
⚠️ 绝对水位不可作生产预期(铁律 4); 只用于 A/B 相对判定。
"""
import os, sys, json, argparse, random

SL_PCT = 0.08
HOLD_DAYS = 3
DAY_MS = 86400000


def _dt(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')


def load_btc_gap(path):
    """BTC EMA7-EMA28 gap%(只取已收盘 bar)"""
    import datetime
    raw = json.load(open(path))
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    bars = [r for r in raw if int(r[0]) + DAY_MS <= now]
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


def trades_from_settled(cands):
    """消费 gate_dump_run.py 的输出: 每行已含 settled{sym:{net,trig}}
    (GPU 侧边跑边结算, 生产 SOUP 口径, 入场 T0 open / 扫 T0..T2 / 平 T2 open)。"""
    out = []
    for rec in cands:
        d = rec.get('day')
        st = rec.get('settled') or {}
        probs = rec.get('probs') or []
        for i, s in enumerate(rec.get('syms') or []):
            if s not in st:
                continue
            v = st[s]
            out.append(dict(date=d, symbol=s,
                            prob=probs[i] if i < len(probs) else None,
                            trig=v.get('trig'), net=v.get('net')))
    return out


def settle(cands, klines):
    """按标定口径逐币结算(本地重算, 用于校验); 返回逐笔列表"""
    out = []
    for rec in cands:
        d = rec.get('day')
        probs = rec.get('probs') or [None] * len(rec.get('syms') or [])
        for i, s in enumerate(rec.get('syms') or []):
            if s not in klines or d not in klines[s]:
                continue
            order = sorted(klines[s])
            oi = order.index(d)
            if oi + HOLD_DAYS - 1 >= len(order):
                continue
            entry = klines[s][order[oi]]['o']
            if not entry or entry <= 0:
                continue
            slp = entry * (1 - SL_PCT)
            trig = None
            for j in range(0, HOLD_DAYS):
                r = klines[s][order[oi + j]]
                if trig is None and r['l'] <= slp:
                    trig = j
            exitp = slp if trig is not None else klines[s][order[oi + HOLD_DAYS - 1]]['o']
            out.append(dict(date=d, symbol=s, prob=probs[i] if i < len(probs) else None,
                            trig=trig, net=(exitp / entry - 1) * 100))
    return out


def boot_ci(a, b, n=4000, seed=42):
    random.seed(seed)
    bs = []
    for _ in range(n):
        bs.append(sum(random.choice(a) for _ in a) / len(a)
                  - sum(random.choice(b) for _ in b) / len(b))
    bs.sort()
    k = int(0.025 * len(bs))
    return bs[k], bs[-1 - k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cands', required=True, help='GPU 导出的候选 json')
    ap.add_argument('--btc', default='/tmp/btc_1d.json')
    ap.add_argument('--klines', default='/home/myuser/backtester/data_cache/notusdt_1d_full.json')
    ap.add_argument('--threshold', type=float, default=1.75)
    ap.add_argument('--settlement', choices=['local', 'gpu'], default='local',
                    help='local=本地标定结算(默认, 与影子档93.4%); gpu=GPU侧settled(已知有bug, 仅对照)')
    ap.add_argument('--json', default=None)
    a = ap.parse_args()

    kl = json.load(open(a.klines))['klines']
    K = {s: {_dt(r['t']): r for r in rows} for s, rows in kl.items()}
    cands = json.load(open(a.cands))
    print(f'候选天数: {len(cands)}  ({cands[0]["day"]} ~ {cands[-1]["day"]})')
    has_settled = any('settled' in (c or {}) for c in cands)
    use_gpu_settled = has_settled and a.settlement == 'gpu'
    if has_settled and not use_gpu_settled:
        print('  [口径] GPU侧 settled 已知有bug(止损率100%), 按 §8.2 规程改用本地标定结算')
    if use_gpu_settled:
        # gate_dump_run.py 的输出: 已含独立结算(GPU 侧, 生产 SOUP 口径)
        trades = trades_from_settled(cands)
        if not trades:
            print('无已结算样本(可能天数不足 3)'); return 1
        print(f'逐币结算: {len(trades)} 笔  [来源=GPU 侧独立结算, 生产 SOUP 口径]')
    else:
        trades = settle(cands, K)
        if not trades:
            print('无结算样本'); return 1
        src = '本地标定结算(与影子档一致率93.4%)'
        print(f'逐币结算: {len(trades)} 笔  [来源={src}]')
    sr = sum(1 for t in trades if t['trig'] is not None) / len(trades)
    print(f'止损率 {sr:.1%}   (基准对标影子档 ~46%)')
    print()

    gap = load_btc_gap(a.btc)
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
        BYD.setdefault(t['date'], []).append((t['net'], t['trig'] is not None, g))
    days = sorted(BYD)
    print(f'带 |gap| 的交易日: {len(days)}  ({days[0]} ~ {days[-1]})')
    if len(days) < 20:
        print('样本不足'); return 1
    print()

    def report(lbl, lo_days, hi_days):
        if len(lo_days) < 5 or len(hi_days) < 5:
            print(f'  {lbl}: 样本不足'); return
        a_dm = [sum(x[0] for x in BYD[d]) / len(BYD[d]) for d in lo_days]
        b_dm = [sum(x[0] for x in BYD[d]) / len(BYD[d]) for d in hi_days]
        na = [x[0] for d in lo_days for x in BYD[d]]
        nb = [x[0] for d in hi_days for x in BYD[d]]
        r1 = sum(1 for d in lo_days for x in BYD[d] if x[1]) / len(na)
        r2 = sum(1 for d in hi_days for x in BYD[d] if x[1]) / len(nb)
        diff = sum(a_dm) / len(a_dm) - sum(b_dm) / len(b_dm)
        lo, hi = boot_ci(a_dm, b_dm)
        sig = '✅ 显著' if (lo > 0 or hi < 0) else '❌ 含0'
        print(f'  {lbl}')
        print(f'    贴近(BTC无方向) {len(lo_days):3d}日/{len(na):4d}笔  每笔 {sum(na)/len(na):+6.2f}%  止损率 {r1:.1%}')
        print(f'    分离(BTC有方向) {len(hi_days):3d}日/{len(nb):4d}笔  每笔 {sum(nb)/len(nb):+6.2f}%  止损率 {r2:.1%}')
        print(f'    → 差 {diff:+.2f}pp  日级bootstrap 95%CI [{lo:+.2f}, {hi:+.2f}]  {sig}')
        print()

    print(f'=== 检验1: |gap| 阈值 {a.threshold}% (原始闸门口径) ===')
    report(f'≤{a.threshold}% vs >{a.threshold}%',
           [d for d in days if BYD[d][0][2] <= a.threshold],
           [d for d in days if BYD[d][0][2] > a.threshold])

    import statistics as st
    mg = st.median([BYD[d][0][2] for d in days])
    print(f'=== 检验2: 日中位分割 ({mg:.2f}%) ===')
    report(f'≤{mg:.2f}% vs >{mg:.2f}%',
           [d for d in days if BYD[d][0][2] <= mg],
           [d for d in days if BYD[d][0][2] > mg])

    print('=== 检验3: 四分位单调性 ===')
    qs = sorted(days, key=lambda d: BYD[d][0][2])
    k = len(qs) // 4
    for i in range(4):
        g = qs[i * k:(i + 1) * k] if i < 3 else qs[3 * k:]
        if not g:
            continue
        ns = [x[0] for d in g for x in BYD[d]]
        r = sum(1 for d in g for x in BYD[d] if x[1]) / len(ns)
        print(f'  Q{i+1} |gap| {BYD[g[0]][0][2]:5.2f}~{BYD[g[-1]][0][2]:5.2f}%  {len(g):3d}日/{len(ns):4d}笔'
              f'  每笔 {sum(ns)/len(ns):+6.2f}%  止损率 {r:.1%}')
    print()

    print('=== 检验4: 纯态月对照(缓存内单月混杂度最低的月份) ===')
    from collections import defaultdict
    mo = defaultdict(list)
    for d in days:
        mo[d[:7]].append(d)
    for m in sorted(mo):
        g = mo[m]
        if len(g) < 8:
            continue
        gs = [BYD[d][0][2] for d in g]
        near = sum(1 for x in gs if x <= a.threshold)
        ns = [x[0] for d in g for x in BYD[d]]
        r = sum(1 for d in g for x in BYD[d] if x[1]) / len(ns)
        tag = '纯贴近' if near >= len(g) * 0.8 else ('纯分离' if near <= len(g) * 0.2 else '')
        print(f'  {m}  {len(g):2d}日  贴近 {near:2d}/{len(g):2d} {tag:6s} 每笔 {sum(ns)/len(ns):+6.2f}%  止损率 {r:.1%}')

    if a.json:
        json.dump(trades, open(a.json, 'w'), ensure_ascii=False)
        print(f'\n逐笔明细 -> {a.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
