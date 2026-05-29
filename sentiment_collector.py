#!/usr/bin/env python3
"""市场情绪采集器 — 资金费率+多空比，每小时存COS"""
import requests, json, os, time, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_DIR = "/home/myuser/sentiment_data"
COS_PREFIX = "klines/sentiment_data/"
INTERVAL = 3600  # 每小时拉一次

_cos = None
def get_cos():
    global _cos
    if _cos is None:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=os.environ.get('COS_REGION', ''),
            SecretId=os.environ.get('COS_SECRET_ID', ''),
            SecretKey=os.environ.get('COS_SECRET_KEY', ''),
            Endpoint=os.environ.get('COS_ENDPOINT', ''),
        )
        _cos = CosS3Client(config)
    return _cos

def fetch_funding_rates():
    """拉全合约资金费率"""
    try:
        resp = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=15)
        if resp.status_code != 200: return {}
        return {item['symbol']: float(item['lastFundingRate']) for item in resp.json() if item['symbol'].endswith('USDT')}
    except Exception:
        print(f"[Sentiment] 获取资金费率失败")
        return {}

def fetch_long_short_ratios(syms, limit=20):
    """拉主流币多空比"""
    results = {}
    lock = threading.Lock()
    def _fetch(sym):
        try:
            r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": sym, "period": "15m", "limit": 1}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data:
                    with lock:
                        results[sym] = float(data[0].get("longShortRatio", 0))
        except Exception:
            print(f"[Sentiment] 获取{sym}多空比失败")
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch, s): s for s in syms}
        for f in as_completed(futures): pass
    return results

def run():
    os.makedirs(DATA_DIR, exist_ok=True)
    # 主流币种列表
    major_syms = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT"]

    print(f"[Sentiment] 启动 | 间隔{INTERVAL}s | COS: {COS_PREFIX}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            ts = int(now.timestamp())
            record = {"ts": ts, "datetime": now.isoformat()}

            # 资金费率
            funding = fetch_funding_rates()
            record["funding_rates"] = {
                "top5_avg": round(sum(sorted(funding.values(), reverse=True)[:5])/max(len(sorted(funding.values(), reverse=True)[:5]), 1)*100, 4) if funding else 0,
                "btc": round(funding.get("BTCUSDT", 0)*100, 4),
                "eth": round(funding.get("ETHUSDT", 0)*100, 4),
                "high_count": sum(1 for v in funding.values() if v > 0.0005),
                "neg_count": sum(1 for v in funding.values() if v < -0.0001),
            }

            # 多空比
            ls_ratios = fetch_long_short_ratios(major_syms)
            record["long_short_ratios"] = {
                "btc": ls_ratios.get("BTCUSDT", 0),
                "eth": ls_ratios.get("ETHUSDT", 0),
                "avg_top10": round(sum(ls_ratios.values())/max(len(ls_ratios),1), 2),
                "extreme_high": sum(1 for v in ls_ratios.values() if v > 3),
                "extreme_low": sum(1 for v in ls_ratios.values() if v < 0.5),
            }

            # CoinGecko热搜
            try:
                r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
                if r.status_code == 200:
                    trending = r.json().get("coins", [])
                    record["trending"] = [
                        {"name": c["item"]["name"], "symbol": c["item"]["symbol"],
                         "rank": c["item"].get("market_cap_rank", 0)}
                        for c in trending[:10]
                    ]
            except Exception:
                print("[Sentiment] CoinGecko热搜获取失败")

            # BTC市占率
            try:
                r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
                if r.status_code == 200:
                    g = r.json()["data"]
                    record["btc_dominance"] = g.get("market_cap_percentage", {}).get("btc", 0)
                    record["total_market_cap_usd"] = g.get("total_market_cap", {}).get("usd", 0)
            except Exception:
                print("[Sentiment] CoinGecko全球数据获取失败")

            # 本地存 (merge if same-hour file already exists)
            hour_key = now.strftime("%Y%m%d_%H")
            local_file = os.path.join(DATA_DIR, f"sentiment_{hour_key}.json")
            if os.path.exists(local_file):
                try:
                    with open(local_file) as f:
                        existing_record = json.load(f)
                    existing_record.update(record)
                    record = existing_record
                except Exception:
                    print("[Sentiment] 读取已有本地文件失败，使用新数据")
            with open(local_file, 'w') as f:
                json.dump(record, f)

            # COS存
            try:
                cos_key = f"{COS_PREFIX}{now.strftime('%Y%m%d/%H')}.json"
                bucket = os.environ.get('COS_BUCKET', '')
                get_cos().put_object(Bucket=bucket, Key=cos_key,
                    Body=json.dumps(record).encode('utf-8'), ContentType='application/json')
                print(f"[Sentiment] {now.strftime('%H:%M')} | 费率BTC={record['funding_rates']['btc']:.3f}% | LS_BTC={record['long_short_ratios']['btc']:.2f} | COS ok")
            except Exception as e:
                print(f"[Sentiment] {now.strftime('%H:%M')} | COS失败: {e}")

        except Exception as e:
            print(f"[Sentiment] 错误: {e}")

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
