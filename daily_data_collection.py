#!/usr/bin/env python3
"""
每日数据采集统一脚本

负责更新所有外部数据源，确保XGBoost训练时用的是最新数据。
应该在每天凌晨（交易脚本运行前）执行。

用法:
  python3 daily_data_collection.py
"""
import os, sys, json, subprocess, time, shlex
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)

def run(cmd, cwd=None, timeout=60):
    """运行命令，返回成功/失败 (优先用 shlex.split 避免 shell=True)"""
    try:
        cmd_list = shlex.split(cmd) if isinstance(cmd, str) else cmd
        r = subprocess.run(cmd_list, shell=False, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)
        if r.returncode == 0:
            return True, r.stdout.strip()[-200:] if r.stdout.strip() else 'ok'
        else:
            return False, r.stderr.strip()[-200:] if r.stderr.strip() else 'error'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except Exception as e:
        return False, str(e)

def update_klines_oi():
    """更新K线和OI缓存 — 独立于交易，无余额要求"""
    import concurrent.futures, requests as req

    log('-' * 40)
    log('[K线+OI] 开始更新缓存...')

    # 加载 .env
    env_file = os.path.join(BASE, '.env')
    api_vars = {}
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    k, v = line.strip().split('=', 1)
                    api_vars[k] = v

    # ---- K线更新 ----
    kline_cache = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    klines = {}
    if os.path.exists(kline_cache):
        try:
            with open(kline_cache) as f:
                klines = json.load(f).get('klines', {})
            log(f'  K线缓存加载: {len(klines)} 币种')
        except Exception as e:
            log(f'  K线缓存加载失败: {e}')

    # 获取所有交易对
    try:
        resp = req.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT'
                    and s.get('contractType') == 'PERPETUAL']
    except Exception:
        fut_syms = []

    existing = [s for s in fut_syms if s in klines]

    def _fetch_latest(sym):
        try:
            r = req.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 10}, timeout=10)
            if r.status_code == 200:
                return sym, [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                              'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7])}
                             for k in r.json()]
        except Exception:
            pass
        return sym, []

    updated_k = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_latest, s): s for s in existing}
        for f in concurrent.futures.as_completed(futures):
            s, new_kls = f.result()
            if not new_kls:
                continue
            old_kls = klines[s]
            last_old_ts = old_kls[-1].get('t', 0) if old_kls else 0
            for k in new_kls:
                if k['t'] > last_old_ts:
                    old_kls.append(k)
                    updated_k += 1

    if updated_k > 0:
        try:
            import fcntl as _fcntl
            with open(kline_cache, 'r+') as f:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
                try:
                    cache_data = json.load(f)
                    cache_data['klines'] = klines
                    f.seek(0); f.truncate()
                    json.dump(cache_data, f)
                finally:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            log(f'  ✅ K线缓存更新: {updated_k} 条新K线')
        except Exception as e:
            log(f'  ⚠️ K线缓存写入失败: {e}')
    else:
        log(f'  ℹ️ K线缓存无需更新')
        # 检查最新时间
        latest_ts = 0
        for kls in klines.values():
            if kls:
                ts = max(k.get('t', 0) for k in kls)
                if ts > latest_ts: latest_ts = ts
        if latest_ts:
            age_h = (time.time() - latest_ts / 1000) / 3600
            log(f'    最新K线: {datetime.fromtimestamp(latest_ts/1000, tz=timezone).strftime("%Y-%m-%d")} ({age_h:.0f}h前)')

    # ---- OI 更新 ----
    oi_cache = '/home/myuser/backtester/data_cache/oi_daily.json'
    oi_data = {}
    if os.path.exists(oi_cache):
        try:
            with open(oi_cache) as f:
                oi_data = json.load(f)
        except Exception:
            pass

    need_oi = [s for s in fut_syms if s not in oi_data or not oi_data[s]][:50]
    oi_updated = 0
    if need_oi:
        def _fetch_oi(sym):
            try:
                r = req.get('https://fapi.binance.com/futures/data/openInterestHist',
                    params={'symbol': sym, 'period': '1d', 'limit': 90}, timeout=10)
                if r.status_code == 200:
                    return sym, {int(o['timestamp']) // 1000: float(o['sumOpenInterest'])
                                 for o in r.json()}
            except Exception:
                pass
            return sym, {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures_oi = {pool.submit(_fetch_oi, s): s for s in need_oi}
            for f in concurrent.futures.as_completed(futures_oi):
                s, d = f.result()
                if d:
                    oi_data[s] = d
                    oi_updated += 1
                time.sleep(0.15)

        try:
            tmp_oi = oi_cache + '.tmp'
            with open(tmp_oi, 'w') as f:
                json.dump(oi_data, f)
            os.rename(tmp_oi, oi_cache)
            log(f'  ✅ OI缓存更新: {oi_updated} 币种')
        except Exception as e:
            log(f'  ⚠️ OI缓存写入失败: {e}')
    else:
        log(f'  ℹ️ OI缓存无需更新')

    # ---- COS 上传 ----
    _upload_to_cos(kline_cache, 'klines/cache/notusdt_1d_full.json', 'K线缓存')
    _upload_to_cos(oi_cache, 'klines/cache/oi_daily.json', 'OI缓存')

    log('[K线+OI] 缓存更新完成')
    log('-' * 40)


def _upload_to_cos(local_path, cos_key, label):
    """上传单个文件到COS"""
    try:
        from qcloud_cos import CosConfig, CosS3Client
        env_file = os.path.join(BASE, '.env')
        cos_vars = {}
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if '=' in line and not line.startswith('#'):
                        k, v = line.strip().split('=', 1)
                        cos_vars[k] = v
        config = CosConfig(
            Region=cos_vars.get('COS_REGION', 'ap-seoul'),
            SecretId=cos_vars['COS_SECRET_ID'],
            SecretKey=cos_vars['COS_SECRET_KEY'],
            Endpoint=cos_vars.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com')
        )
        client = CosS3Client(config)
        with open(local_path, 'rb') as f:
            client.put_object(Bucket=cos_vars['COS_BUCKET'], Key=cos_key, Body=f.read())
        log(f'  📤 COS上传: {label} → {cos_key}')
    except Exception as e:
        log(f'  ⚠️ COS上传失败 ({label}): {e}')


def main():
    log('=' * 50)
    log('每日数据采集启动')
    log('=' * 50)

    # 0. 先更新 K 线和 OI 缓存（不依赖余额）
    try:
        update_klines_oi()
    except Exception as e:
        log(f'[ERROR] K线/OI更新异常: {e}')

    collectors = [
        # (名称, 命令, 工作目录, 超时秒数)
        ('恐慌贪婪', 'python3 fear_greed_collector.py', BASE, 30),
        ('情绪数据', 'python3 sentiment_collector.py', BASE, 60),
        ('BTC市值', 'python3 collect_btc_mcap.py', BASE, 30),
        ('BTC市占率', 'python3 collect_btc_dominance.py', BASE, 30),
        ('宏观资产', 'python3 collect_macro_assets.py', BASE, 30),
        ('TVL数据', 'python3 collect_tvl.py', BASE, 30),
        ('清算热力图', 'python3 liquidation_heatmap.py', BASE, 30),
        ('ETF资金流', 'python3 fetch_etf.py',
         '/home/myuser/openclaw-5001-host/config/.openclaw/workspace/etf_data', 120),
        ('稳定币+溢价', 'python3 monitor.py', '/home/myuser/stablecoin_data', 30),
    ]
    
    ok_count = 0
    fail_count = 0
    
    for name, cmd, cwd, timeout in collectors:
        log(f'[{name}] 开始...')
        ok, output = run(cmd, cwd=cwd, timeout=timeout)
        if ok:
            log(f'  ✅ {name} 成功: {output[:100]}')
            ok_count += 1
        else:
            log(f'  ❌ {name} 失败: {output[:200]}')
            fail_count += 1
    
    log('=' * 50)
    log(f'采集完成: {ok_count}成功 / {fail_count}失败')
    log('=' * 50)
    
    # FIX: 复制/tmp下的文件到data/目录（收集器写入路径与读取路径不一致）
    import shutil
    copies = [
        ('/tmp/fear_greed_history.json', '/home/myuser/websocket_new/data/fear_greed_history.json'),
        ('/tmp/macro_assets.json', '/home/myuser/websocket_new/data/macro_assets.json'),
        ('/tmp/crypto_sectors.json', '/home/myuser/websocket_new/data/crypto_sectors.json'),
        ('/tmp/liquidation_heatmap.json', '/home/myuser/websocket_new/data/liq_daily.json'),
        ('/tmp/sector_heatmap.json', '/home/myuser/websocket_new/data/sector_heatmap.json'),
    ]
    for src, dst in copies:
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
                log(f'  📁 复制 {os.path.basename(src)} → data/')
            except Exception as e:
                log(f'  ⚠️ 复制失败 {os.path.basename(src)}: {e}')
        else:
            log(f'  ⚠️ 源文件不存在: {src}')
    
    # 检查关键文件时间
    log('数据文件新鲜度检查:')
    key_files = [
        ('恐慌贪婪', '/home/myuser/websocket_new/data/fear_greed_history.json'),
        ('ETF', '/home/myuser/openclaw-5001-host/config/.openclaw/workspace/etf_data/etf_flow.json'),
        ('BTC市值', '/home/myuser/coingecko_data/btc_mcap.json'),
        ('BTC市占率', '/home/myuser/coingecko_data/btc_dominance.json'),
        ('TVL', '/home/myuser/defillama_data/ethereum_tvl.json'),
        ('宏观资产', '/home/myuser/websocket_new/data/macro_assets.json'),
        ('清算', '/home/myuser/websocket_new/data/liq_daily.json'),
        ('稳定币', '/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json'),
        ('算力', '/home/myuser/hashrate_data/hashrate_history.json'),
    ]
    
    now = time.time()
    for name, path in key_files:
        if os.path.exists(path):
            mtime = os.path.getmtime(path)
            age_hours = (now - mtime) / 3600
            status = '✅' if age_hours < 26 else '⚠️' if age_hours < 48 else '🔴'
            log(f'  {status} {name}: {age_hours:.1f}h前 ({os.path.basename(path)})')
        else:
            log(f'  ❌ {name}: 文件不存在 ({path})')
    
    return fail_count == 0

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
