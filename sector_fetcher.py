#!/usr/bin/env python3
"""CoinGecko板块数据拉取 → 每天一次"""
import requests, json, time, os

CACHE_FILE = "/tmp/crypto_sectors.json"
TARGETS = {
    'meme-token': 'Meme', 'artificial-intelligence': 'AI',
    'layer-1': 'L1', 'layer-2': 'L2',
    'decentralized-finance-defi': 'DeFi', 'real-world-assets-rwa': 'RWA',
    'gaming': '游戏', 'depin': 'DePIN', 'oracle': '预言机',
    'privacy': '隐私', 'solana-ecosystem': 'Solana',
    'ethereum-ecosystem': 'ETH生态', 'decentralized-exchange': 'DEX',
    'ai-agents': 'AI Agent', 'decentralized-science-desci': 'DeSci',
    'chain-abstraction': '链抽象', 'parallel-evm': '并行EVM',
    'bitcoin-ecosystem': 'BTC生态', 'ton-ecosystem': 'TON生态',
    'base-ecosystem': 'Base生态', 'liquid-staking': '流动性质押',
    'restaking': '再质押',
}

def fetch():
    symbol_map = {}
    for cat_id, label in TARGETS.items():
        try:
            resp = requests.get(
                'https://api.coingecko.com/api/v3/coins/markets',
                params={'vs_currency': 'usd', 'category': cat_id,
                        'order': 'market_cap_desc', 'per_page': 250},
                timeout=30
            )
            if resp.status_code == 429:
                print(f'  {label}: 限频，等待60s...')
                time.sleep(60)
                resp = requests.get(
                    'https://api.coingecko.com/api/v3/coins/markets',
                    params={'vs_currency': 'usd', 'category': cat_id,
                            'order': 'market_cap_desc', 'per_page': 250},
                    timeout=30
                )
            if resp.status_code != 200:
                print(f'  {label}: HTTP {resp.status_code}, skip')
                continue
            for c in resp.json():
                sym = c.get('symbol', '').upper()
                bsym = sym + 'USDT'
                if bsym not in symbol_map:
                    symbol_map[bsym] = []
                if label not in symbol_map[bsym]:
                    symbol_map[bsym].append(label)
            print(f'  {label}: {len(resp.json())} coins, mapped {sum(1 for v in symbol_map.values() if label in v)} to USDT pairs')
            time.sleep(3)
        except Exception as e:
            print(f'  {label}: {e}')
    with open(CACHE_FILE, 'w') as f:
        json.dump(symbol_map, f)
    print(f'Done: {len(symbol_map)} symbols cached to {CACHE_FILE}')

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
        with open(CACHE_FILE, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/sector_data/crypto_sectors.json',
                           Body=f.read(), ContentType='application/json')
        print('[Sector] COS上传成功')
    except Exception as e:
        print(f'[Sector] COS上传失败: {e}')

if __name__ == '__main__':
    fetch()
