#!/usr/bin/env python3
"""修复K线缓存 2026-05下旬以来 OHLC 冻结错误 (2026-07-18)
背景: 追加式日更把每根蜡烛冻结在开盘后~5分钟, o/h/l/c 全错
      (BTC 6/1 收盘偏差3.17%), 污染近2个月的训练标签和价格特征。
修复: v2完整数据覆盖 t<2026-07-16 全部字段 o/h/l/c/v/q/n/tbq;
      API limit=5 覆盖近端。已下架币不受此bug影响(无新追加), 跳过。
"""
import json, os, time, shutil
import concurrent.futures
import requests

CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
V2 = '/home/myuser/notusdt_1d_full_v2.json'
BACKUP = CACHE + '.bak20260718_ohlc'
FAPI = 'https://fapi.binance.com'
T_CUT = 1784160000000  # 2026-07-16 00:00 UTC
FIELDS = ('o', 'h', 'l', 'c', 'v', 'q')
WORKERS = 4


def api_klines(params, tries=4):
    for i in range(tries):
        try:
            r = requests.get(f'{FAPI}/fapi/v1/klines', params=params, timeout=20)
            if r.status_code in (418, 429):
                time.sleep(3 * (i + 1))
                continue
            if r.status_code != 200:
                return []
            return r.json()
        except Exception:
            time.sleep(2)
    return []


def main():
    if not os.path.exists(BACKUP):
        shutil.copy(CACHE, BACKUP)
        print(f'备份: {BACKUP}')

    with open(CACHE) as f:
        cache = json.load(f)
    klines = cache['klines']
    with open(V2) as f:
        v2 = json.load(f)['klines']

    # pass1: v2 覆盖全部字段 (t < T_CUT)
    v2_maps = {s: {int(k['t']): k for k in recs if int(k['t']) < T_CUT}
               for s, recs in v2.items()}
    hit1 = 0
    for sym, recs in klines.items():
        m = v2_maps.get(sym)
        if not m:
            continue
        for k in recs:
            src = m.get(k['t'])
            if src is not None:
                for f_ in FIELDS:
                    k[f_] = float(src[f_])
                k['n'], k['tbq'] = int(src['n']), float(src['tbq'])
                hit1 += 1
    print(f'pass1(v2全字段覆盖): {hit1} 条')

    # pass2: API 近端5天
    hit2 = 0
    def work(sym):
        nonlocal hit2
        for k in api_klines({'symbol': sym, 'interval': '1d', 'limit': 5}):
            t = int(k[0])
            if t < T_CUT:
                continue
            for ck in klines[sym]:
                if ck['t'] == t:
                    ck['o'], ck['h'], ck['l'], ck['c'] = float(k[1]), float(k[2]), float(k[3]), float(k[4])
                    ck['v'], ck['q'] = float(k[5]), float(k[7])
                    ck['n'], ck['tbq'] = int(k[8]), float(k[10])
                    hit2 += 1
        time.sleep(0.15)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(work, sorted(klines.keys())))
    print(f'pass2(API近端): {hit2} 条 ({time.time()-t0:.0f}s)')

    # 验证: BTC 6/1 和 7/10 应与API一致
    btc = {k['t']: k for k in klines['BTCUSDT']}
    for t, name in [(1780272000000, '6/1'), (1782086400000, '7/10')]:
        kk = btc.get(t, {})
        print(f"验证 BTC {name}: c={kk.get('c')}")

    import fcntl
    with open(CACHE, 'r+') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            cur = json.load(f)
            cur['klines'] = klines
            f.seek(0)
            f.truncate()
            json.dump(cur, f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    print(f'已写回 {CACHE} ({os.path.getsize(CACHE)/1024/1024:.1f}MB)')


if __name__ == '__main__':
    main()
