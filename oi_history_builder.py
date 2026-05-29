#!/usr/bin/env python3
"""OI日级历史数据库构建器 — 从API拉最近30天，建立本地缓存供回测使用"""
import json, requests, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

CACHE_FILE = "/home/myuser/backtester/data_cache/oi_daily.json"
MAX_WORKERS = 5
DELAY = 0.3  # 每个请求间隔，避免限频

def _atomic_write_json(filepath, data):
    """原子写入JSON: 先写tmp再rename"""
    tmp_path = filepath + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, default=str)
    os.rename(tmp_path, filepath)

def fetch_all_symbols():
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        syms = [s['symbol'] for s in r.json()['symbols']
                if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('contractType')=='PERPETUAL']
        return syms
    except:
        return []

def fetch_oi_history(sym):
    try:
        r = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
            params={'symbol': sym, 'period': '1d', 'limit': 500}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            result = {}
            for item in data:
                ts = int(item['timestamp']) // 1000  # 转秒
                result[ts] = float(item['sumOpenInterest'])
            return sym, result
        elif r.status_code == 429:
            return sym, 'RATE_LIMIT'
    except Exception as e:
        return sym, str(e)
    return sym, {}

def build():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    
    # 加载已有缓存
    existing = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                existing = json.load(f)
            print(f"加载已有缓存: {len(existing)} 币种")
        except:
            pass
    
    syms = fetch_all_symbols()
    print(f"总合约币种: {len(syms)}")
    
    # 分批拉取
    new_data = {}
    rate_limited = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for sym in syms:
            if sym not in existing:
                f = pool.submit(fetch_oi_history, sym)
                futures[f] = sym
                time.sleep(DELAY)  # 控制提交速率
        
        for f in as_completed(futures):
            sym, result = f.result()
            if result == 'RATE_LIMIT':
                rate_limited.append(sym)
            elif isinstance(result, dict) and result:
                new_data[sym] = result
                print(f"  {sym}: {len(result)} 天")
            elif isinstance(result, str):
                print(f"  {sym}: 错误 {result}")
    
    # 合并
    for sym, data in new_data.items():
        existing[sym] = data
    
    # 原子保存
    _atomic_write_json(CACHE_FILE, existing)

    print(f"\n完成: 总计 {len(existing)} 币种")
    if rate_limited:
        print(f"限频待补: {len(rate_limited)} 币种")
        # 重试限频的（指数退避，最多3次）
        print("开始重试（指数退避）...")
        retry_data = {}
        max_retries = 3
        for sym in rate_limited:
            for attempt in range(max_retries):
                wait = 2 ** attempt  # 1, 2, 4 秒
                time.sleep(wait)
                _, result = fetch_oi_history(sym)
                if isinstance(result, dict) and result:
                    retry_data[sym] = result
                    print(f"  {sym}: {len(result)} 天 (retry attempt {attempt+1})")
                    break
                elif result == 'RATE_LIMIT':
                    print(f"  {sym}: 仍限频, 第{attempt+1}次重试, 等待{wait}s")
                else:
                    print(f"  {sym}: 错误 {result} (retry attempt {attempt+1})")
            else:
                print(f"  {sym}: {max_retries}次重试后仍失败，跳过")
        for sym, data in retry_data.items():
            existing[sym] = data
        _atomic_write_json(CACHE_FILE, existing)
        print(f"重试后总计: {len(existing)} 币种")

def incremental_update():
    """增量更新：只拉缓存中缺少最新数据的币种"""
    if not os.path.exists(CACHE_FILE):
        print("无缓存，执行全量构建")
        build()
        return
    
    with open(CACHE_FILE) as f:
        existing = json.load(f)
    
    syms = fetch_all_symbols()
    need_update = []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    
    for sym in syms:
        if sym not in existing or not existing[sym]:
            need_update.append(sym)
            continue
        latest_ts = max(int(k) for k in existing[sym].keys())
        if now_ts - latest_ts > 86400 * 2:  # 超过2天未更新
            need_update.append(sym)
    
    if not need_update:
        print("所有币种已是最新，无需更新")
        return
    
    print(f"需要更新: {len(need_update)} 币种")
    new_data = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for sym in need_update:
            f = pool.submit(fetch_oi_history, sym)
            futures[f] = sym
            time.sleep(DELAY)
        
        for f in as_completed(futures):
            sym, result = f.result()
            if isinstance(result, dict) and result:
                new_data[sym] = result
                print(f"  {sym}: {len(result)} 天")
    
    for sym, data in new_data.items():
        existing[sym] = data

    _atomic_write_json(CACHE_FILE, existing)

    print(f"增量更新完成: 总计 {len(existing)} 币种")

if __name__ == '__main__':
    import sys
    if '--full' in sys.argv:
        build()
    else:
        incremental_update()
