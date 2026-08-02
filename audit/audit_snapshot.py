#!/usr/bin/env python3
"""生产全链路审计 · 阶段1: 运行前快照 (08:04:50 cron)
记录关键数据文件 MD5 + 抽查币 K 线副本, 用于事后定位"训练特征偏差"发生在哪个环节。
只读, 不改生产。
"""
import os, json, hashlib, glob
from datetime import datetime, timezone

AUDIT_DIR = '/home/myuser/websocket_new/audit'
os.makedirs(AUDIT_DIR, exist_ok=True)
today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

def md5(p):
    try:
        h = hashlib.md5()
        with open(p, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        return f'ERR:{e}'

# 1. 关键数据文件 MD5 (训练特征的全部依赖)
files = {
    'klines': '/home/myuser/backtester/data_cache/notusdt_1d_full.json',
    'oi': '/home/myuser/backtester/data_cache/oi_daily.json',
    'funding': '/home/myuser/backtester/data_cache/funding_hist.json',
    'chain': '/home/myuser/blockchair_data/btc_chain.csv',
    'macro': '/home/myuser/websocket_new/data/macro_assets.json',
    'fear_greed': '/home/myuser/websocket_new/data/fear_greed_history.json',
    'liq': '/home/myuser/websocket_new/data/liq_levels_daily.json',
    'btc_dominance': '/home/myuser/websocket_new/data/btc_dominance_proxy.json',
    'sector': '/home/myuser/websocket_new/data/crypto_sectors.json',
    'sector_overrides': '/home/myuser/websocket_new/data/sector_overrides.json',
}
md5s = {k: md5(v) for k, v in files.items()}

# 外部目录: 取最新文件 MD5 作为代表
for dname, dpath in [('sentiment', '/home/myuser/sentiment_data'),
                     ('stablecoin', '/home/myuser/stablecoin_data'),
                     ('defillama', '/home/myuser/defillama_data'),
                     ('hashrate', '/home/myuser/hashrate_data'),
                     ('coingecko', '/home/myuser/coingecko_data')]:
    try:
        latest = max(glob.glob(dpath + '/*'), key=os.path.getmtime)
        md5s[f'{dname}_latest'] = os.path.basename(latest) + ':' + md5(latest)
        md5s[f'{dname}_filecount'] = len(glob.glob(dpath + '/*'))
    except Exception as e:
        md5s[dname] = f'ERR:{e}'

# 2. 抽查币 K 线副本 (最近 40 根, 够 20日std窗口) — 用于对比"运行前版本 vs 运行后版本"
with open(files['klines']) as f:
    klines = json.load(f)['klines']
probe_syms = ['0GUSDT', '1000BONKUSDT', 'BTCUSDT']
probe = {}
for sym in probe_syms:
    k = klines.get(sym, [])
    probe[sym] = [{'t': r['t'], 'o': r['o'], 'c': r['c'], 'q': r['q']} for r in k[-40:]]

snapshot = {
    'ts': datetime.now(timezone.utc).isoformat(),
    'md5': md5s,
    'probe_klines': probe,
}
out = f'{AUDIT_DIR}/snapshot_{today}.json'
with open(out, 'w') as f:
    json.dump(snapshot, f, ensure_ascii=False)
print(f'[AUDIT-1] 快照已写: {out}')
print(f'  klines_md5={md5s["klines"]}')
print(f'  0GUSDT 最近6天: {[(datetime.fromtimestamp(r["t"]//1000, tz=timezone.utc).strftime("%m-%d"), r["c"], r["q"]) for r in probe["0GUSDT"]]}')
