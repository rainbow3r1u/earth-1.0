#!/usr/bin/env python3
"""山寨共振度提取 (2026-09-13) — 纯市场结构, 不用任何模型/特征, 只读K线。

问题: "BTC EMA7/EMA28 纠缠(无方向) 时, 山寨是否更容易'共振'(一起动)?"
      这是用户命题"BTC没方向 → 山寨开始动"的**结构层**检验, 与模型/选币完全无关。

共振度指标(逐日, 全宇宙):
  1. disp     = 横截面日收益 std%          ← 主指标: 一起动则 disp 小
  2. iqr      = 横截面日收益 四分位距%
  3. up_pct   = 上涨币占比%   (同涨同跌的直接度量)
  4. med      = 横截面中位收益%
  5. res1     = 60日滚动"第一主成分解释方差占比" (真·共振, 需多日面板; 单独算)

⚠️ 数据源: ~/backtester/data_cache/notusdt_1d_full.json (全宇宙日K)
     该文件**不含 BTC**; BTC 由本地 /tmp/btc_1d.json(Binance API) 提供 → 只用于算 |gap|
用法: python3 day_structure_dump.py [起始日]
产出: /tmp/day_structure.json  [{day,n,med,disp,iqr,up_pct}, ...]
"""
import os, sys, json, time, datetime, statistics as st

HOME = os.path.expanduser('~')
KL = f'{HOME}/backtester/data_cache/notusdt_1d_full.json'
OUT = os.environ.get('OUT', '/tmp/day_structure.json')
START = sys.argv[1] if len(sys.argv) > 1 else '2025-06-01'
MIN_N = 100          # 当日最少币数


def _dt(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')


def main():
    t0 = time.time()
    print(f'加载 K 线: {KL}', flush=True)
    kl = json.load(open(KL))['klines']
    print(f'  币数 {len(kl)}  用时 {time.time()-t0:.0f}s', flush=True)

    byday = {}
    for sym, rows in kl.items():
        if not sym.endswith('USDT'):
            continue
        for r in rows:
            d = _dt(r['t'])
            if d < START:
                continue
            o = r['o']
            if not o or o <= 0:
                continue
            byday.setdefault(d, []).append(r['c'] / o - 1)
    print(f'  日期数 {len(byday)}', flush=True)

    out = []
    for d in sorted(byday):
        rs = [x for x in byday[d] if -1.0 < x < 5.0]      # 剔除脏值
        if len(rs) < MIN_N:
            continue
        m = st.mean(rs)
        sd = st.pstdev(rs)
        q1, q3 = st.quantiles(rs, n=4)[0], st.quantiles(rs, n=4)[2]
        out.append(dict(day=d, n=len(rs),
                        med=round(st.median(rs) * 100, 4),
                        disp=round(sd * 100, 4),
                        iqr=round((q3 - q1) * 100, 4),
                        up_pct=round(sum(1 for x in rs if x > 0) / len(rs) * 100, 2)))
    with open(OUT, 'w') as f:
        json.dump(out, f)
    print(f'完成: {len(out)} 天 ({out[0]["day"]} ~ {out[-1]["day"]}) -> {OUT}  用时 {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
