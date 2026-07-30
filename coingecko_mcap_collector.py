#!/usr/bin/env python3
"""CoinGecko 市值/流通量每日采集器 (两阶段全量版)
阶段1: /coins/list 全量(~19k)建立 symbol→id 候选映射
阶段2: /coins/markets?ids= 分块拉取候选币的市值/流通量, 冲突取市值最大者
数据清理: 1000/1000000/1M前缀剥离, gecko symbol冲突取最大市值, 未匹配名单记录
输出: coingecko_data/mcap/YYYY-MM-DD.json + mcap_latest.json
cron: 每日6:10
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

OUT_DIR = '/home/myuser/coingecko_data/mcap'
LATEST = '/home/myuser/coingecko_data/mcap_latest.json'
KLINES = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
OVERRIDES = {
    '龙虾': None, '币安人生': None, '我踏马来了': None,
    'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'XRP': 'ripple',
    'DOGE': 'dogecoin', 'PEPE': 'pepe', 'SHIB': 'shiba-inu',
}
PREFIXES = ['1000000', '1000']
UA = {'User-Agent': 'mcap-collector/1.0'}


def get(url, timeout=30, retries=3):
    """带429退避重试"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f'  429限速, 等待{wait}s重试...')
                time.sleep(wait)
            else:
                raise
    return None


def base_variants(sym):
    base = sym[:-4] if sym.endswith('USDT') else sym
    out = [base]
    for pre in PREFIXES:
        if base.startswith(pre) and len(base) > len(pre):
            out.append(base[len(pre):])
    if base.startswith('1M') and len(base) > 2:
        out.append(base[2:])
    return out


def main():
    binance_syms = sorted(json.load(open(KLINES))['klines'].keys())

    # 阶段1: 全量币表, 建 symbol.lower() → [gecko ids]
    full = get('https://api.coingecko.com/api/v3/coins/list', timeout=60)
    by_sym = {}
    for c in full:
        by_sym.setdefault(c['symbol'].lower(), []).append(c['id'])
    print(f'gecko全量币表: {len(full)}个')

    # 收集候选id
    want_ids = set()
    sym_cands = {}
    for sym in binance_syms:
        base = base_variants(sym)[0]
        if base in OVERRIDES:
            gid = OVERRIDES[base]
            sym_cands[sym] = [gid] if gid else []
            want_ids.update(sym_cands[sym])
            continue
        ids = []
        for cand in base_variants(sym):
            ids.extend(by_sym.get(cand.lower(), []))
        ids = list(dict.fromkeys(ids))  # 去重保序
        sym_cands[sym] = ids
        want_ids.update(ids)
    print(f'候选gecko id: {len(want_ids)}个, 分块拉取市值...')

    # 阶段2: 分块拉市值 (URL长度控制, 每块100个id)
    ids = sorted(want_ids)
    market = {}
    for i in range(0, len(ids), 100):
        chunk = ','.join(ids[i:i+100])
        data = get(f'https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={chunk}&sparkline=false')
        for c in data:
            market[c['id']] = c
        time.sleep(6)  # 免费额度保守限速
    print(f'市值返回: {len(market)}个')

    # 匹配: 候选id里取市值最大者
    matched, unmatched = {}, []
    for sym in binance_syms:
        cands = [market[i] for i in sym_cands[sym] if i in market]
        hit = max(cands, key=lambda c: c.get('market_cap') or 0) if cands else None
        if hit and hit.get('market_cap'):
            matched[sym] = {'gecko_id': hit['id'], 'mcap': hit['market_cap'],
                            'circ': hit.get('circulating_supply'), 'total': hit.get('total_supply')}
        else:
            unmatched.append(sym)

    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = {'date': day, 'matched': len(matched), 'unmatched_n': len(unmatched),
           'unmatched': unmatched, 'coins': matched}
    with open(f'{OUT_DIR}/{day}.json', 'w') as f:
        json.dump(doc, f)
    with open(LATEST, 'w') as f:
        json.dump(doc, f)
    print(f'[{day}] 匹配 {len(matched)}/{len(binance_syms)} ({len(matched)/len(binance_syms)*100:.0f}%), '
          f'未匹配 {len(unmatched)}: {unmatched[:12]}')


if __name__ == '__main__':
    main()
