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
    ("ton", "ton"),
    ("sui", "sui"),
    ("polygon", "polygon"),
    # v3补全: 以下链之前遗漏，2026-07-12补回
    ("aurora", "aurora"),
    ("avalanche", "avalanche"),
    ("corn", "corn"),
    ("etherlink", "etherlink"),
    ("harmony", "harmony"),
    ("stable", "stable"),
]

def fetch_chain_tvl(chain_name):
    """从DeFiLlama拉单链历史TVL (20s超时, 失败重试1次)"""
    for attempt in range(2):
        try:
            r = requests.get(f"https://api.llama.fi/charts/{chain_name}", timeout=20)
            if r.status_code != 200:
                print(f"[TVL-{chain_name}] HTTP {r.status_code}")
                return []
            return r.json()
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
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

    # all_chains 用特殊API（全行业TVL，不带链名）
    print(f"[TVL] 采集 all_chains -> all_chains_tvl.json")
    try:
        r = requests.get("https://api.llama.fi/charts", timeout=60)
        if r.status_code == 200:
            processed = process_tvl(r.json())
            out_path = os.path.join(DATA_DIR, "all_chains_tvl.json")
            tmp = out_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(processed, f, indent=2)
            os.rename(tmp, out_path)
            print(f"[TVL] all_chains: {len(processed)} 天")
        else:
            print(f"[TVL-all_chains] HTTP {r.status_code}")
    except Exception as e:
        print(f"[TVL-all_chains] 失败: {e}")
    time.sleep(1)

    # 并行采集 (6线程, 避免DeFiLlama限流; 串行曾因外层30s超时被杀)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_and_save(file_name, api_name):
        raw = fetch_chain_tvl(api_name)
        if not raw:
            return file_name, 0
        processed = process_tvl(raw)
        out_path = os.path.join(DATA_DIR, f"{file_name}_tvl.json")
        tmp = f"{out_path}.{os.getpid()}.tmp"  # 并行下避免临时文件冲突
        with open(tmp, 'w') as f:
            json.dump(processed, f, indent=2)
        os.rename(tmp, out_path)
        return file_name, len(processed)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_and_save, fn, an): fn for fn, an in CHAINS}
        for fut in as_completed(futures):
            fn = futures[fut]
            try:
                file_name, days = fut.result()
                if days:
                    print(f"[TVL] {file_name}: {days} 天")
                else:
                    print(f"[TVL] {file_name}: 采集失败")
            except Exception as e:
                print(f"[TVL] {fn}: 异常 {e}")

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
            Timeout=30
        )
        cos = CosS3Client(config)
        bucket = os.environ.get('COS_BUCKET', '')
        # 上传所有 *_tvl.json 文件（包括 all_chains）
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith('_tvl.json'):
                continue
            path = os.path.join(DATA_DIR, fname)
            with open(path, 'rb') as f:
                cos.put_object(Bucket=bucket, Key=f'klines/defillama_data/{fname}',
                               Body=f.read(), ContentType='application/json')
        print("[TVL] COS上传成功")
    except Exception as e:
        print(f"[TVL] COS上传失败: {e}")

if __name__ == '__main__':
    main()
