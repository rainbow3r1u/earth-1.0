#!/usr/bin/env python3
"""K线缓存回填 n(成交笔数)/tbq(主动买入额) — volfeat 生产接入 (2026-07-18)
策略 (v2, 快速版):
  1. 主干: 本地 notusdt_1d_full_v2.json (524币, 最近1500天含n/tbq), 仅取 t < 2026-07-16 的完整日
  2. 近端: API limit=5 拉最近5天, 补 7/16 至今 (含未收盘蜡烛)
  3. 远端缺口: 历史超过1500天的老币, API endTime 翻一页补齐
已下架币 (MATIC等, 不在v2且非TRADING) → 跳过, 特征构建走中性默认 (tr_ratio=1, tbr=0.5)
只新增 n/tbq 字段, 不改动 t/o/h/l/c/v/q。
"""
import json, os, time, shutil
import concurrent.futures
import requests

CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
V2 = '/home/myuser/notusdt_1d_full_v2.json'
BACKUP = CACHE + '.bak20260718'
FAPI = 'https://fapi.binance.com'
T_CUT = 1784160000000  # 2026-07-16 00:00 UTC — v2最后一天为不完整日, 弃用
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
    print(f'缓存 {len(klines)} 币, v2 {len(v2)} 币')

    # ── pass 1: v2 主干合并 ──
    v2_maps = {}
    for sym, recs in v2.items():
        v2_maps[sym] = {int(k['t']): (int(k['n']), float(k['tbq']))
                        for k in recs if int(k['t']) < T_CUT}
    hit1 = 0
    for sym, recs in klines.items():
        m = v2_maps.get(sym)
        if not m:
            continue
        for k in recs:
            v = m.get(k['t'])
            if v is not None:
                k['n'], k['tbq'] = v
                hit1 += 1
    print(f'pass1(v2主干): 合并 {hit1} 条')

    # ── pass 2+3: API 补近端 + 远端缺口 (下架币 klines 调用失败自动跳过, 无需exchangeInfo) ──
    targets = sorted(klines.keys())
    hit2 = hit3 = 0
    def work(sym):
        nonlocal hit2, hit3
        recs = klines[sym]
        # 近端: 最近5天
        for k in api_klines({'symbol': sym, 'interval': '1d', 'limit': 5}):
            t = int(k[0])
            if t < T_CUT:
                continue
            for ck in recs:
                if ck['t'] == t:
                    ck['n'], ck['tbq'] = int(k[8]), float(k[10])
                    hit2 += 1
        # 远端缺口: 缓存最早日 < v2最早日
        vm = v2_maps.get(sym)
        if vm and recs:
            v2_first = min(vm.keys()) if vm else None
            prod_first = recs[0]['t']
            if v2_first and prod_first < v2_first:
                for k in api_klines({'symbol': sym, 'interval': '1d', 'limit': 1500,
                                     'endTime': v2_first - 1}):
                    t = int(k[0])
                    for ck in recs:
                        if ck['t'] == t:
                            ck['n'], ck['tbq'] = int(k[8]), float(k[10])
                            hit3 += 1
        time.sleep(0.15)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(work, targets))
    print(f'pass2(近端): {hit2} 条, pass3(远端缺口): {hit3} 条 ({time.time()-t0:.0f}s)')

    total = sum(len(v) for v in klines.values())
    covered = sum(1 for v in klines.values() for k in v if 'n' in k and 'tbq' in k)
    print(f'覆盖率: {covered}/{total} ({covered/total*100:.1f}%)')

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
