#!/usr/bin/env python3
"""币安 U本位合约 exchangeInfo 每日采集
采集每个合约的 onboardDate(上币日期, 币龄权威来源) + underlyingSubType(币安板块分类) + 状态
输出: websocket_new/data/exchange_info.json
cron: 每日6:12
"""
import json, urllib.request
from datetime import datetime, timezone

OUT = '/home/myuser/websocket_new/data/exchange_info.json'

def main():
    r = json.load(urllib.request.urlopen('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15))
    info = {}
    for s in r['symbols']:
        if s.get('contractType') != 'PERPETUAL':
            continue
        info[s['symbol']] = {
            'onboard': s.get('onboardDate'),   # ms epoch
            'subtype': s.get('underlyingSubType', []),
            'status': s.get('status'),
            'base': s.get('baseAsset'),
        }
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    json.dump({'date': day, 'n': len(info), 'coins': info}, open(OUT, 'w'))
    print(f'[{day}] exchangeInfo: {len(info)}个永续合约 → {OUT}')

if __name__ == '__main__':
    main()
