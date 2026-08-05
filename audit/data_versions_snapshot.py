#!/usr/bin/env python3
"""每日数据版本快照 (2026-08-05 新增, 只读)
记录 K线/费率/OI/外部数据/代码 的 MD5+大小+行数, 写入 data/version_snapshots/YYYY-MM-DD.json。
用途: 事后重放=生产校验时, 知道"运行当天"的输入数据长什么样, 根治 8/3/8/4 无法复现问题。
用法: python3 audit/data_versions_snapshot.py  (建议 cron 8:02 挂载, 在 8:04 审计快照之前)
"""
import os, json, hashlib, csv, glob
from datetime import datetime, timezone

ROOT = '/home/myuser'
OUT_DIR = f'{ROOT}/websocket_new/data/version_snapshots'

def md5(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def stat_file(p):
    if not os.path.exists(p):
        return None
    st = os.stat(p)
    return {'md5': md5(p), 'size': st.st_size, 'mtime': datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}

def stat_csv(p):
    s = stat_file(p)
    if not s:
        return None
    try:
        with open(p) as f:
            rows = list(csv.DictReader(f))
        s['rows'] = len(rows)
        if rows:
            s['first_ts'] = rows[0]['ts']
            s['last_ts'] = rows[-1]['ts']
    except Exception as e:
        s['csv_error'] = str(e)
    return s

def stat_dir(p, pat='*'):
    files = sorted(glob.glob(f'{p}/{pat}'))
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return {'count': 0}
    return {'count': len(files), 'first': stat_file(files[0]), 'last': stat_file(files[-1])}

out = {'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
       'ts': datetime.now(timezone.utc).isoformat(),
       'files': {}, 'dirs': {}}

singles = {
    'klines': f'{ROOT}/backtester/data_cache/notusdt_1d_full.json',
    'oi': f'{ROOT}/backtester/data_cache/oi_daily.json',
    'funding': f'{ROOT}/backtester/data_cache/funding_hist.json',
    'params': f'{ROOT}/backtester/config/current_params.json',
    'sectors': f'{ROOT}/websocket_new/data/crypto_sectors.json',
    'sector_overrides': f'{ROOT}/websocket_new/data/sector_overrides.json',
    'fear_greed': f'{ROOT}/websocket_new/data/fear_greed_history.json',
    'macro': f'{ROOT}/websocket_new/data/macro_assets.json',
    'liq_daily': f'{ROOT}/websocket_new/data/liq_daily.json',
    'liq_levels': f'{ROOT}/websocket_new/data/liq_levels_daily.json',
    'btc_dominance_proxy': f'{ROOT}/websocket_new/data/btc_dominance_proxy.json',
    'etf_flow': f'{ROOT}/websocket_new/data/etf_flow.json',
    'sector_heatmap': f'{ROOT}/websocket_new/data/sector_heatmap.json',
    'code_auto_dual': f'{ROOT}/websocket_new/auto_dual_trade.py',
    'code_daily_predictor': f'{ROOT}/websocket_new/daily_predictor.py',
}
for name, p in singles.items():
    out['files'][name] = stat_file(p)

out['files']['blockchair_btc_chain'] = stat_csv(f'{ROOT}/blockchair_data/btc_chain.csv')

for name, p in [
    ('stablecoin', f'{ROOT}/stablecoin_data'),
    ('coingecko', f'{ROOT}/coingecko_data'),
    ('hashrate', f'{ROOT}/hashrate_data'),
    ('defillama', f'{ROOT}/defillama_data'),
    ('sentiment', f'{ROOT}/sentiment_data'),
]:
    out['dirs'][name] = stat_dir(p)

os.makedirs(OUT_DIR, exist_ok=True)
out_path = f'{OUT_DIR}/{out["date"]}.json'
with open(out_path, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f'数据版本快照已写: {out_path}')
for name, s in out['files'].items():
    if s:
        print(f"  {name}: md5={s['md5'][:12]} size={s['size']}{' rows=' + str(s.get('rows')) if 'rows' in s else ''}")
for name, s in out['dirs'].items():
    print(f"  dir {name}: {s.get('count')} files, last_md5={s.get('last', {}).get('md5', '-')[:12] if s.get('last') else '-'}")
