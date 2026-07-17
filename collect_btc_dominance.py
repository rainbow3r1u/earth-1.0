#!/usr/bin/env python3
"""BTC市占率(BTC.D)采集 — 从CoinGecko拉取"""
import json, os, time, requests
from datetime import datetime, timezone

OUT = '/home/myuser/coingecko_data/btc_dominance.json'

def fetch():
    result = {}

    # 1. 先读已有历史
    if os.path.exists(OUT):
        with open(OUT) as f:
            result = json.load(f)

    # 2. 从CoinGecko global端点获取当前BTC.D
    try:
        r = requests.get('https://api.coingecko.com/api/v3/global', timeout=15)
        if r.status_code == 200:
            data = r.json()
            btc_dom = data['data']['market_cap_percentage']['btc']
            total_mcap = data['data']['total_market_cap']['usd']
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            result[today] = {
                'btc_dominance': round(btc_dom, 2),
                'total_mcap': int(total_mcap)
            }
            print(f"{today}: BTC.D={btc_dom:.1f}% 总市值=${total_mcap/1e12:.2f}T")
    except Exception as e:
        print(f"CoinGecko global失败: {e}")
        return None  # 明确失败，禁止返回旧数据

    # 3. 尝试拉历史BTC市值占率 — 用BTC市值/全球市值的近似
    # CoinGecko免费API限制较多, 这里用BTC market_chart + 估算
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
            params={'vs_currency': 'usd', 'days': 365},
            timeout=30
        )
        if r.status_code == 200:
            mcap_data = r.json().get('market_caps', [])
            # 用今天的数据校准: 已知BTC mcap和BTC.D, 反推总市值
            # 但历史总市值不可得, 用BTC mcap除以今日BTC.D作为近似
            # 实际BTC.D在历史上波动在35-70%之间
            for ts_ms, mcap in mcap_data:
                date_str = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')
                if date_str not in result:
                    # 粗略估算: BTC mcap * 2.5 ≈ 总市值 (BTC.D ~40%)
                    result[date_str] = {
                        'btc_mcap': int(mcap),
                        'btc_dominance_est': round(int(mcap) * 2.5 / total_mcap * btc_dom * 100, 2) if total_mcap > 0 else 0
                    }
    except Exception as e:
        print(f"历史数据失败: {e}")

    # 排序保存
    sorted_result = dict(sorted(result.items()))
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sorted_result, f)
    os.rename(tmp, OUT)
    print(f"保存: {OUT} ({len(result)}天)")

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
        with open(OUT, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/coingecko_data/btc_dominance.json',
                           Body=f.read(), ContentType='application/json')
        print("[BTC.D] COS上传成功")
    except Exception as e:
        print(f"[BTC.D] COS上传失败: {e}")

if __name__ == '__main__':
    fetch()
