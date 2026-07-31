#!/usr/bin/env python3
"""每日宇宙快照 — Earth-Guard 第6层: 防幸存者偏差/Universe泄露
记录当天币宇宙(币种+K线天数+最近收盘日成交额), 供未来point-in-time回测重建当日宇宙。
历史退市币不可追讨, 但从此日起每日快照 = 未来的回测不再有宇宙偏差。
cron: 每日7:10 (6:00采集完成后)
"""
import json, os
from datetime import datetime, timezone

KLINES = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
OUT_DIR = '/home/myuser/websocket_new/data/universe'

SECTOR_SRC = '/home/myuser/websocket_new/data/crypto_sectors.json'

def main():
    k = json.load(open(KLINES))['klines']
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    coins = {}
    for sym, kls in k.items():
        if len(kls) < 2:
            continue
        coins[sym] = {'days': len(kls), 'q': kls[-2].get('q', 0)}  # kls[-1]=今日未收盘, 取昨日收盘成交额
    os.makedirs(OUT_DIR, exist_ok=True)
    out = f'{OUT_DIR}/{day}.json'
    json.dump({'date': day, 'n': len(coins), 'coins': coins}, open(out, 'w'))
    print(f'宇宙快照: {len(coins)}币 → {out}')
    # 板块地图快照: sector_map每日漂移会回写历史板块热度, 需point-in-time冻结
    sector = json.load(open(SECTOR_SRC))
    sout = f'{OUT_DIR}/sector_{day}.json'
    json.dump({'date': day, 'n': len(sector), 'map': sector}, open(sout, 'w'))
    print(f'板块地图快照: {len(sector)}币 → {sout}')

if __name__ == '__main__':
    main()
