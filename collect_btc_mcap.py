#!/usr/bin/env python3
"""BTC市值7日变化率采集器 — 每天跑，追加到 btc_mcap.json"""
import requests, json, os
from datetime import datetime, timezone

DATA_FILE = "/home/myuser/coingecko_data/btc_mcap.json"

def fetch():
    """从CoinGecko拉BTC最近30天市值，计算7日变化率"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "30"},
            timeout=30
        )
        if r.status_code != 200:
            print(f"[BTC MCap] HTTP {r.status_code}")
            return {}
        data = r.json()
        mcaps = data.get("market_caps", [])
        if not mcaps:
            return {}
    except Exception as e:
        print(f"[BTC MCap] 请求失败: {e}")
        return {}

    # 按天聚合：取每天最后一个市值值
    daily = {}
    for ts_ms, mcap in mcaps:
        date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[date] = mcap  # 后面的覆盖前面的，最终是当天最后一个值

    # 计算7日变化率
    dates = sorted(daily.keys())
    results = {}
    for i, date in enumerate(dates):
        if i < 7:
            results[date] = 0.0
            continue
        prev = dates[i - 7]
        prev_mcap = daily[prev]
        curr_mcap = daily[date]
        if prev_mcap > 0:
            results[date] = round((curr_mcap - prev_mcap) / prev_mcap, 6)
        else:
            results[date] = 0.0

    return results

def main():
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    # 加载已有数据
    existing = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                for item in json.load(f):
                    existing[item["date"]] = item["btc_mcap_7d_chg"]
        except Exception as e:
            print(f"[BTC MCap] 读取旧数据失败: {e}")

    # 拉新数据
    new_data = fetch()
    if not new_data:
        print("[BTC MCap] 无新数据")
        return

    # 合并
    existing.update(new_data)

    # 写回
    output = [{"date": d, "btc_mcap_7d_chg": v} for d, v in sorted(existing.items())]
    with open(DATA_FILE, 'w') as f:
        json.dump(output, f, indent=2)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[BTC MCap] 已更新 {len(output)} 天 | 今日 {today}: {new_data.get(today, 'N/A')}")

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
        with open(DATA_FILE, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/coingecko_data/btc_mcap.json',
                           Body=f.read(), ContentType='application/json')
        print("[BTC MCap] COS上传成功")
    except Exception as e:
        print(f"[BTC MCap] COS上传失败: {e}")

if __name__ == '__main__':
    main()
