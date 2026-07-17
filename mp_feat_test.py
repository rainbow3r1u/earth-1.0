#!/usr/bin/env python3
"""多进程特征构建性能测试"""
import time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

import auto_dual_trade as adt
import daily_predictor as dp
import json

print('加载数据...')
with open('/home/myuser/backtester/data_cache/notusdt_1d_full.json') as f:
    klines = json.load(f).get('klines', {})
with open('/home/myuser/backtester/data_cache/oi_daily.json') as f:
    oi_data = json.load(f)

with open('/home/myuser/websocket_new/data/crypto_sectors.json') as f:
    sector_data = json.load(f)
sector_map = dp._build_sector_map(sector_data) if hasattr(dp, '_build_sector_map') else {}
sector_heats_all = {}

print(f'数据: {len(klines)} 币种')
from multiprocessing import cpu_count
print(f'CPU: {cpu_count()} 核')

t0 = time.time()
by_day = adt.build_features_78d(klines, oi_data, sector_map, sector_heats_all)
t1 = time.time()

total = sum(len(v) for v in by_day.values())
print(f'完成: {len(by_day)} 天, {total} 样本, 耗时 {t1-t0:.1f}s')
