#!/usr/bin/env python3
"""板块热力图 — TOP15均涨幅 = 板块热度，与前端一致"""
import requests, json, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE = "/tmp/sector_heatmap.json"
SECTOR_CACHE = "/tmp/crypto_sectors.json"

def _load_sector_map():
    try:
        with open(SECTOR_CACHE, 'r') as f:
            return json.load(f)
    except:
        return {}

def _fetch_coin_gain(sym):
    """24h涨跌幅 — 合约优先，现货备用（过滤低成交量垃圾币）"""
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/klines',
            params={'symbol': sym, 'interval': '1d', 'limit': 2}, timeout=8)
        if r.status_code == 200 and len(r.json()) >= 2:
            kls = r.json()
            prev_c = float(kls[-2][4]); cur_c = float(kls[-1][4])
            vol = float(kls[-1][5]) * cur_c  # 估算成交额(USDT)
            if prev_c > 0 and vol > 500000:
                return sym, (cur_c - prev_c) / prev_c * 100
    except: pass
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
            params={'symbol': sym, 'interval': '1d', 'limit': 2}, timeout=8)
        if r.status_code == 200 and len(r.json()) >= 2:
            kls = r.json()
            prev_c = float(kls[-2][4]); cur_c = float(kls[-1][4])
            vol = float(kls[-1][5]) * cur_c
            if prev_c > 0 and vol > 500000:
                return sym, (cur_c - prev_c) / prev_c * 100
    except: pass
    return sym, None

def fetch():
    try:
        sector_map = _load_sector_map()
        if not sector_map:
            print("Heatmap: 无SECTOR_MAP数据")
            return

        all_sectors = set()
        for tags in sector_map.values():
            all_sectors.update(tags)

        sector_symbols = {s: [] for s in all_sectors}
        for sym, tags in sector_map.items():
            for tag in tags:
                sector_symbols[tag].append(sym)

        all_syms = set()
        for s in all_sectors:
            all_syms.update(sector_symbols[s][:150])

        print(f"Heatmap: {len(all_sectors)}板块, 去重{len(all_syms)}币种, 拉K线...")

        gains = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_coin_gain, sym): sym for sym in all_syms}
            try:
                for f in as_completed(futures, timeout=90):
                    try:
                        sym, gain = f.result(timeout=8)
                        if gain is not None:
                            gains[sym] = gain
                    except Exception:
                        pass
            except TimeoutError:
                pass  # 超时仍保留已完成的结果

        print(f"Heatmap: {len(gains)}/{len(all_syms)}币种有K线数据")

        # TOP15均涨幅 = 板块热度
        TOPN = 15
        result = []
        for s in sorted(all_sectors):
            syms = sector_symbols[s][:150]
            sector_gains = sorted([gains[sym] for sym in syms if sym in gains], reverse=True)
            top_n = sector_gains[:TOPN]
            if len(top_n) >= 3:
                heat = round(sum(top_n) / len(top_n), 1)
            elif top_n:
                heat = round(sum(top_n) / len(top_n), 1)
            else:
                heat = 0.0
            result.append({
                'name': s,
                'mc_change_pct': heat,
                'coin_count': len(sector_gains),
            })

        result.sort(key=lambda x: -x['mc_change_pct'])

        with open(CACHE, 'w') as f:
            json.dump(result, f, default=str)

        print(f"Heatmap → {CACHE}")
        for s in result:
            print(f"  {s['name']:12s} {s['mc_change_pct']:+.1f}% (总{s['coin_count']}币种, TOP{TOPN}均值)")

        # 上传COS
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
            from qcloud_cos import CosConfig, CosS3Client
            config = CosConfig(
                Region=os.environ.get('COS_REGION', ''),
                SecretId=os.environ.get('COS_SECRET_ID', ''),
                SecretKey=os.environ.get('COS_SECRET_KEY', ''),
                Endpoint=os.environ.get('COS_ENDPOINT', ''),
            )
            cos = CosS3Client(config)
            bucket = os.environ.get('COS_BUCKET', '')
            with open(CACHE) as f:
                cos.put_object(Bucket=bucket, Key='klines/sector_heatmap/sector_heatmap.json',
                               Body=f.read().encode('utf-8'), ContentType='application/json')
        except Exception as e:
            print(f"Heatmap COS: {e}")

    except Exception as e:
        print(f"Heatmap error: {e}")

if __name__ == '__main__':
    fetch()
