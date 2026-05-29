#!/usr/bin/env python3
"""DeFiLlama链TVL 7日变化率采集器 — 每天跑，更新各链tvl文件"""
import requests, json, os, time
from datetime import datetime, timezone

DATA_DIR = "/home/myuser/defillama_data"
CHAINS = [
    ("btc_chain", "bitcoin"),
    ("ethereum", "ethereum"),
    ("solana", "solana"),
    ("binance", "bsc"),
    ("arbitrum", "arbitrum"),
    ("base", "base"),
]

def fetch_chain_tvl(chain_name):
    """从DeFiLlama拉单链历史TVL"""
    try:
        r = requests.get(f"https://api.llama.fi/charts/{chain_name}", timeout=60)
        if r.status_code != 200:
            print(f"[TVL-{chain_name}] HTTP {r.status_code}")
            return []
        return r.json()
    except Exception as e:
        print(f"[TVL-{chain_name}] 请求失败: {e}")
        return []

def process_tvl(raw_data):
    """按天聚合，计算7日变化率"""
    if not raw_data:
        return []

    # 按天聚合：取每天最后一个TVL值
    daily = {}
    for item in raw_data:
        try:
            ts = int(item["date"])
            tvl = float(item["totalLiquidityUSD"])
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            daily[date] = tvl
        except: continue

    dates = sorted(daily.keys())
    results = []
    for i, date in enumerate(dates):
        if i < 7:
            results.append({"date": date, "tvl_7d_chg": 0.0})
            continue
        prev = dates[i - 7]
        prev_tvl = daily[prev]
        curr_tvl = daily[date]
        if prev_tvl > 0:
            chg = round((curr_tvl - prev_tvl) / prev_tvl, 6)
        else:
            chg = 0.0
        results.append({"date": date, "tvl_7d_chg": chg})

    return results

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    for file_name, api_name in CHAINS:
        print(f"[TVL] 采集 {api_name} -> {file_name}_tvl.json")
        raw = fetch_chain_tvl(api_name)
        if not raw:
            continue
        processed = process_tvl(raw)
        out_path = os.path.join(DATA_DIR, f"{file_name}_tvl.json")
        with open(out_path, 'w') as f:
            json.dump(processed, f, indent=2)
        print(f"[TVL] {file_name}: {len(processed)} 天")
        time.sleep(1)  # 礼貌延迟

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
        for file_name, _ in CHAINS:
            fname = f"{file_name}_tvl.json"
            path = os.path.join(DATA_DIR, fname)
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    cos.put_object(Bucket=bucket, Key=f'klines/defillama_data/{fname}',
                                   Body=f.read(), ContentType='application/json')
        print("[TVL] COS上传成功")
    except Exception as e:
        print(f"[TVL] COS上传失败: {e}")

if __name__ == '__main__':
    main()
