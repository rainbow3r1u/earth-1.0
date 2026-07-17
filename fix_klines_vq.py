#!/usr/bin/env python3
"""修复K线缓存近月蜡烛 v/q 残量问题 (2026-07-18)
背景: 日更管道"只追加不刷新", 每根蜡烛在开盘后~5分钟被写入后冻结,
      近月蜡烛的 v/q 只有全天值的零头 (BTC 0.06B vs 真实8.59B)。
      n/tbq 已按完整值回填 → tbr=tbq/q 分母残缺导致比率爆炸。
修复: v2文件(完整数据)覆盖 t<2026-07-16 的 v/q; API limit=5 覆盖近端 v/q/n/tbq。
只动 v/q (价格字段 o/h/l/c 不受影响也不改动)。
"""
import json, os, time, shutil
import concurrent.futures
import requests

CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
V2 = '/home/myuser/notusdt_1d_full_v2.json'
BACKUP = CACHE + '.bak20260718_vq'
FAPI = 'https://fapi.binance.com'
T_CUT = 1784160000000  # 2026-07-16 00:00 UTC
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

    # pass1: v2 覆盖 v/q (t < T_CUT)
    v2_maps = {s: {int(k['t']): (float(k['v']), float(k['q']))
                   for k in recs if int(k['t']) < T_CUT}
               for s, recs in v2.items()}
    hit1 = 0
    for sym, recs in klines.items():
        m = v2_maps.get(sym)
        if not m:
            continue
        for k in recs:
            vv = m.get(k['t'])
            if vv is not None:
                k['v'], k['q'] = vv
                hit1 += 1
    print(f'pass1(v2覆盖v/q): {hit1} 条')

    # pass2: API 近端5天 覆盖 v/q/n/tbq
    hit2 = 0
    def work(sym):
        nonlocal hit2
        recs = klines[sym]
        for k in api_klines({'symbol': sym, 'interval': '1d', 'limit': 5}):
            t = int(k[0])
            if t < T_CUT:
                continue
            for ck in recs:
                if ck['t'] == t:
                    ck['v'], ck['q'] = float(k[5]), float(k[7])
                    ck['n'], ck['tbq'] = int(k[8]), float(k[10])
                    hit2 += 1
        time.sleep(0.15)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(work, sorted(klines.keys())))
    print(f'pass2(API近端): {hit2} 条 ({time.time()-t0:.0f}s)')

    # 验证: BTC 7/15 应为完整值
    btc = {k['t']: k for k in klines['BTCUSDT']}
    k715 = btc.get(1784073600000, {})
    print(f"验证 BTC 7/15: q={k715.get('q',0)/1e9:.2f}B (期望~8.6B), tbq/q={k715.get('tbq',0)/max(k715.get('q',1),1):.3f}")

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
