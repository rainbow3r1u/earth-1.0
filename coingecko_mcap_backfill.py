#!/usr/bin/env python3
"""市值历史回填 (免API版)
CoinGecko market_chart 已转付费(401), 改用近似法:
  市值历史[date] = 当日收盘价(本地K线) × 当前流通量(mcap_latest)
近似说明: 流通量按当前值恒定处理, 对大多数币误差小(重解锁币除外);
对目标特征(成交额/市值换手率)量级完全够用。
输出: coingecko_data/mcap_hist/{gecko_id}.json {binance, gecko_id, mcap:{date:val}}
"""
import json, os

LATEST = '/home/myuser/coingecko_data/mcap_latest.json'
KLINES = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
OUT_DIR = '/home/myuser/coingecko_data/mcap_hist'

def main():
    d = json.load(open(LATEST))
    kl = json.load(open(KLINES))['klines']
    os.makedirs(OUT_DIR, exist_ok=True)
    # 清掉API版残留的空文件
    n = 0
    from datetime import datetime, timezone
    for sym, c in d['coins'].items():
        circ = c.get('circ')
        if not circ or circ <= 0 or sym not in kl:
            continue
        hist = {}
        for k in kl[sym]:
            day = datetime.fromtimestamp(k['t']/1000, timezone.utc).strftime('%Y-%m-%d')
            hist[day] = round(k['c'] * circ, 2)
        with open(f'{OUT_DIR}/{c["gecko_id"]}.json', 'w') as f:
            json.dump({'binance': sym, 'gecko_id': c['gecko_id'],
                       'method': 'close*circ(approx)', 'mcap': hist}, f)
        n += 1
    print(f'市值历史回填(近似法): {n}币 → {OUT_DIR}/')

if __name__ == '__main__':
    main()
