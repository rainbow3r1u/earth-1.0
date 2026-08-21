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
    '龙虾': 'lobster-2',  # 8/11 修复: CoinGecko lobster-2 (symbol=龙虾)
    '币安人生': None, '我踏马来了': None,
    'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 'XRP': 'ripple',
    'DOGE': 'dogecoin', 'PEPE': 'pepe', 'SHIB': 'shiba-inu',
    # 改名/符号不一致手工映射 (7/30清洗审计)
    'MATIC': 'polygon-ecosystem-token',   # MATIC已改名POL, gecko旧条目市值为0
    'RONIN': 'ronin',                     # Binance RONINUSDT → gecko symbol ron
    'RAYSOL': 'raydium',                  # Binance RAYSOLUSDT → gecko raydium
    'IP': 'story-protocol',               # Story Protocol, gecko symbol非ip
    'LUNA2': 'terra-luna-2',              # LUNA2 → terra-luna-2
    'VELODROME': 'velodrome-finance',     # VELODROME → velodrome-finance (非velo)
    'BEAMX': 'beam-2',                    # BEAMX(Merit Circle迁移) → beam-2
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

    # 流通量手工覆盖 (8/15: CoinGecko 个别币 circ 错误, 以币安 App 为准)
    # 流通量覆盖: CMC 优先(与币安App同源, 8/15接入) → 手工覆盖兜底
    # CMC key 从 .env 读; 免费版 333次/天, 批量100币/请求
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
        CMC_KEY = os.environ.get('CMC_API_KEY', '')
    except Exception:
        CMC_KEY = ''
    cmc_circ = {}
    if CMC_KEY:
        import requests as _rq
        try:
            # 分批100币查 CMC (用币安 base symbol)
            bases = [s.replace('USDT', '') for s in matched]
            for i in range(0, len(bases), 100):
                chunk = bases[i:i+100]
                r = _rq.get('https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest',
                            params={'symbol': ','.join(chunk)}, 
                            headers={'X-CMC_PRO_API_KEY': CMC_KEY}, timeout=20)
                d = r.json().get('data', {})
                for sym_up, v in d.items():
                    item = v[0]
                    cs = item.get('circulating_supply')
                    if cs:
                        cmc_circ[sym_up + 'USDT'] = cs
            n_cmc = 0
            for sym in matched:
                if sym in cmc_circ:
                    old = matched[sym].get('circ')
                    matched[sym]['circ'] = cmc_circ[sym]
                    if old and old != cmc_circ[sym]:
                        n_cmc += 1
            print(f'  [CMC] 覆盖 {n_cmc}/{len(matched)} 币的流通量 (与币安App同源)')
        except Exception as e:
            print(f'  [CMC] 拉取失败, 继续用CoinGecko: {e}')

    CIRC_OVERRIDES = {
        'GUAUSDT': 3.14e8,   # 币安App: 3.14亿 (CoinGecko 4500万错, 用户 8/15 核实)
        'PTBUSDT': 8.114e9,  # 币安App: 81.14亿 (CoinGecko 20.71亿错, 用户 8/15 核实)
    }
    for sym, circ in CIRC_OVERRIDES.items():
        if sym in matched and matched[sym].get('circ'):
            old = matched[sym]['circ']
            matched[sym]['circ'] = circ
            print(f'  [覆盖] {sym} circ: {old:,.0f} → {circ:,.0f} (币安App口径)')

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
