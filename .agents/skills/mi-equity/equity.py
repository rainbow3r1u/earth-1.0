#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""米账户总权益查询 — 只输出权益金额(U + CNY), 其他什么都不输出。
数据源: HYBRID_BINANCE 凭证 → fapi.binance.com /fapi/v2/account → totalMarginBalance
        (总权益 = 钱包余额 + 未实现盈亏)
汇率:   open.er-api.com (主) / frankfurter.app (备), USD→CNY 实时
"""
import hmac, hashlib, os, time
from urllib.parse import urlencode
import requests

# 仓库根: 脚本位于 <root>/.agents/skills/mi-equity/, 兜底写死本机部署路径
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if not os.path.isfile(os.path.join(ROOT, '.env')):
    ROOT = '/home/myuser/websocket_new'

with open(os.path.join(ROOT, '.env')) as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

KEY = os.environ['HYBRID_BINANCE_API_KEY']
SEC = os.environ['HYBRID_BINANCE_API_SECRET']

# 1) 总权益
params = {'timestamp': int(time.time() * 1000)}
query = urlencode(params)
sig = hmac.new(SEC.encode(), query.encode(), hashlib.sha256).hexdigest()
r = requests.get('https://fapi.binance.com/fapi/v2/account',
                 params={**params, 'signature': sig},
                 headers={'X-MBX-APIKEY': KEY}, timeout=15)
equity = float(r.json()['totalMarginBalance'])

# 2) USD→CNY 汇率 (主备双源)
rate = None
try:
    rate = float(requests.get('https://open.er-api.com/v6/latest/USD', timeout=8).json()['rates']['CNY'])
except Exception:
    try:
        rate = float(requests.get('https://api.frankfurter.app/latest?from=USD&to=CNY', timeout=8).json()['rates']['CNY'])
    except Exception:
        rate = None

if rate:
    print(f"{equity:.2f}U ≈ {equity*rate:,.2f} CNY (rate {rate:.4f})")
else:
    print(f"{equity:.2f}U (CNY汇率源不可用)")
