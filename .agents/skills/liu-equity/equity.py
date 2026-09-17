#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刘账户资金查询 — 输出三行: 钱包余额 / 未实现盈亏 / 总权益(均含U + CNY), 其他什么都不输出。

2026-09-17 用户指令: "两个都出, 再加未实现盈亏" ——
原版只出总权益(总权益 = 钱包余额 + 未实现盈亏), 用户看不出"落袋多少 vs 浮动多少"。

数据源: HYBRID_BINANCE 凭证 → fapi.binance.com /fapi/v2/account
  · totalWalletBalance      = 钱包余额(已实现, 不随行情波动)
  · totalUnrealizedProfit   = 未实现盈亏(全部持仓的浮动盈亏)
  · totalMarginBalance      = 总权益 = 上两者之和
汇率:   open.er-api.com (主) / frankfurter.app (备), USD→CNY 实时
"""
import hmac, hashlib, os, time
from urllib.parse import urlencode
import requests

# 仓库根: 脚本位于 <root>/.agents/skills/liu-equity/, 兜底写死本机部署路径
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

# 1) 账户三个字段
params = {'timestamp': int(time.time() * 1000)}
query = urlencode(params)
sig = hmac.new(SEC.encode(), query.encode(), hashlib.sha256).hexdigest()
r = requests.get('https://fapi.binance.com/fapi/v2/account',
                 params={**params, 'signature': sig},
                 headers={'X-MBX-APIKEY': KEY}, timeout=15)
acct = r.json()
wallet = float(acct['totalWalletBalance'])          # 钱包余额(已实现)
upnl = float(acct['totalUnrealizedProfit'])         # 未实现盈亏
equity = float(acct['totalMarginBalance'])          # 总权益 = wallet + upnl

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
    print(f"钱包余额   {wallet:>10,.2f}U ≈ {wallet*rate:>11,.2f} CNY   (已实现, 不随行情波动)")
    print(f"未实现盈亏 {upnl:>+10,.2f}U")
    print(f"总权益     {equity:>10,.2f}U ≈ {equity*rate:>11,.2f} CNY   (rate {rate:.4f})")
else:
    print(f"钱包余额   {wallet:>10,.2f}U")
    print(f"未实现盈亏 {upnl:>+10,.2f}U")
    print(f"总权益     {equity:>10,.2f}U   (CNY汇率源不可用)")
