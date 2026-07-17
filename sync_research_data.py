#!/usr/bin/env python3
"""
研究服务器数据同步 — 从 COS 下载全部回测所需数据
新服务器: 32GB/12核, pip install qcloud_cos xgboost numpy pandas requests

用法: python3 sync_research_data.py
"""

import os
from qcloud_cos import CosConfig, CosS3Client

# === COS 凭证 (生产服务器 .env 中取) ===
REGION   = 'ap-seoul'
BUCKET   = 'lhsj-1h-1314017643'
ENDPOINT = 'cos.ap-seoul.myqcloud.com'
SECRET_ID  = 'AKID1zrvw22zkOyMZd7QXSHvNfnslU1FYihh'
SECRET_KEY = 'sa8UirHdUEnJdlhRzMWU4cGk150jIk2P'

# === COS路径 → 本地路径（与生产系统对齐） ===
FILES = [

    # ── 核心缓存 ──
    ('klines/cache/notusdt_1d_full.json',       '/home/myuser/backtester/data_cache/notusdt_1d_full.json'),
    ('klines/cache/oi_daily.json',              '/home/myuser/backtester/data_cache/oi_daily.json'),
    ('klines/cache/kronos_features_cache.json', '/home/myuser/websocket_new/data/kronos_features_cache.json'),

    # ── 宏观 ──
    ('klines/macro_assets/macro_assets.json',   '/home/myuser/websocket_new/data/macro_assets.json'),
    ('klines/fear_greed_history.json',          '/home/myuser/websocket_new/data/fear_greed_history.json'),
    ('klines/sector_data/crypto_sectors.json',  '/home/myuser/websocket_new/data/crypto_sectors.json'),

    # ── 链上 ──
    ('klines/blockchair_data/btc_chain.csv',    '/home/myuser/blockchair_data/btc_chain.csv'),

    # ── ETF ──
    ('klines/etf_data/etf_flow.json',           '/home/myuser/etf_data/etf_flow.json'),

    # ── Coingecko ──
    ('klines/coingecko_data/btc_mcap.json',       '/home/myuser/coingecko_data/btc_mcap.json'),
    ('klines/coingecko_data/btc_dominance.json',  '/home/myuser/coingecko_data/btc_dominance.json'),

    # ── 清算 ──
    ('klines/liquidation_heatmap/liq_daily.json',        '/home/myuser/websocket_new/data/liq_daily.json'),
    ('klines/liquidation_heatmap/liq_levels_daily.json', '/home/myuser/websocket_new/data/liq_levels_daily.json'),

    # ── 稳定币 ──
    ('klines/stablecoin_data/stablecoin_exchange_netflow.json',  '/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json'),
    ('klines/stablecoin_data/btc_coinbase_premium_index.json',   '/home/myuser/stablecoin_data/btc_coinbase_premium_index.json'),
    ('klines/stablecoin_data/btc_korea_premium_index.json',      '/home/myuser/stablecoin_data/btc_korea_premium_index.json'),
    ('klines/stablecoin_data/btc_coinbase_premium_gap.json',     '/home/myuser/stablecoin_data/btc_coinbase_premium_gap.json'),

    # ── 算力 ──
    ('klines/hashrate_data/hashrate_history.json',  '/home/myuser/hashrate_data/hashrate_history.json'),

    # ── TVL (9链 + 协议映射) ──
    ('klines/defillama_data/ethereum_tvl.json',    '/home/myuser/defillama_data/ethereum_tvl.json'),
    ('klines/defillama_data/base_tvl.json',        '/home/myuser/defillama_data/base_tvl.json'),
    ('klines/defillama_data/solana_tvl.json',      '/home/myuser/defillama_data/solana_tvl.json'),
    ('klines/defillama_data/binance_tvl.json',     '/home/myuser/defillama_data/binance_tvl.json'),
    ('klines/defillama_data/arbitrum_tvl.json',    '/home/myuser/defillama_data/arbitrum_tvl.json'),
    ('klines/defillama_data/ton_tvl.json',         '/home/myuser/defillama_data/ton_tvl.json'),
    ('klines/defillama_data/sui_tvl.json',         '/home/myuser/defillama_data/sui_tvl.json'),
    ('klines/defillama_data/polygon_tvl.json',     '/home/myuser/defillama_data/polygon_tvl.json'),
    ('klines/defillama_data/btc_chain_tvl.json',   '/home/myuser/defillama_data/btc_chain_tvl.json'),
    ('klines/defillama_data/protocol_map.json',    '/home/myuser/defillama_data/protocol_map.json'),
]


cos = CosS3Client(CosConfig(Region=REGION, SecretId=SECRET_ID, SecretKey=SECRET_KEY, Endpoint=ENDPOINT))

print('=' * 60)
print(f'研究服务器数据同步  Bucket={BUCKET}  Region={REGION}')
print('=' * 60)

ok = fail = 0
for cos_key, local in FILES:
    os.makedirs(os.path.dirname(local), exist_ok=True)
    try:
        cos.download_file(Bucket=BUCKET, Key=cos_key, DestFilePath=local)
        size_kb = os.path.getsize(local) / 1024
        tag = 'MB' if size_kb > 1024 else 'KB'
        size  = size_kb / 1024 if size_kb > 1024 else size_kb
        print(f'  ✅ {cos_key:55s} → {os.path.basename(local):30s} ({size:.1f}{tag})')
        ok += 1
    except Exception as e:
        print(f'  ❌ {cos_key:55s} 失败: {e}')
        fail += 1

print(f'\n同步完成: {ok}成功 / {fail}失败')
print()
print('后续步骤:')
print('  1. scp 生产服务器的 websocket_new/ 代码目录')
print('  2. scp 生产服务器的 kronos_finetune/ 模型权重目录')
print('  3. 手动创建 .env (含币安API密钥)')
print('  4. pip install xgboost numpy pandas requests qcloud_cos')
print('  5. cd websocket_new && python3 dual_backtest_clean.py')
