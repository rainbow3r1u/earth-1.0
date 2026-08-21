#!/usr/bin/env python3
"""
每日数据采集统一脚本

负责更新所有外部数据源，确保XGBoost训练时用的是最新数据。
应该在每天凌晨（交易脚本运行前）执行。

用法:
  python3 daily_data_collection.py
"""
import os, sys, json, subprocess, time, shlex
from datetime import datetime, timezone, timedelta

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
    except Exception as e:
        log(f'[ERROR] Binance exchangeInfo获取失败: {e}, K线/OI更新中止')
        return  # 不行退，不静默跳过

    existing = [s for s in fut_syms if s in klines]

    updated_k = 0

    # 新上市币种: 全量补入缓存 (此前只更新已有币, 新币永远进不来 — 7/18修复)
    new_syms = [s for s in fut_syms if s not in klines]
    if new_syms:
        log(f'  发现 {len(new_syms)} 个新上市币: {new_syms}, 全量补采...')

        def _fetch_full(sym):
            try:
                r = req.get('https://fapi.binance.com/fapi/v1/klines',
                    params={'symbol': sym, 'interval': '1d', 'limit': 1500}, timeout=20)
                if r.status_code == 200:
                    return sym, [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                                  'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7]),
                                  'n': int(k[8]), 'tbq': float(k[10])}
                                 for k in r.json()]
            except Exception:
                pass
            return sym, []

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            for f in concurrent.futures.as_completed([pool.submit(_fetch_full, s) for s in new_syms]):
                s, full = f.result()
                if full:
                    klines[s] = full
                    updated_k += len(full)

    def _fetch_latest(sym):
        try:
            r = req.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 10}, timeout=10)
            if r.status_code == 200:
                return sym, [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                              'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7]),
                              'n': int(k[8]), 'tbq': float(k[10])}  # n=成交笔数, tbq=主动买入额 (volfeat)
                             for k in r.json()]
        except Exception:
            pass
        return sym, []

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
                elif k['t'] == last_old_ts:
                    old_kls[-1] = k  # 刷新未收盘蜡烛, 保证收盘后量/笔数/主动买入额完整

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
            log(f'    最新K线: {datetime.fromtimestamp(latest_ts/1000, tz=timezone.utc).strftime("%Y-%m-%d")} ({age_h:.0f}h前)')

    # ---- 费率更新 (地球版1.2 fund_raw: 8h结算原值, 新币全历史补采) ----
    fund_file = '/home/myuser/backtester/data_cache/funding_hist.json'
    fund_data = {}
    if os.path.exists(fund_file):
        try:
            with open(fund_file) as f:
                fund_data = json.load(f)
        except Exception:
            pass

    def _fetch_funding(sym):
        try:
            limit = 1000 if sym not in fund_data else 10  # 新币全历史, 老币增量
            r = req.get('https://fapi.binance.com/fapi/v1/fundingRate',
                params={'symbol': sym, 'limit': limit}, timeout=15)
            if r.status_code == 200:
                return sym, [(int(x['fundingTime']), float(x['fundingRate'])) for x in r.json()]
        except Exception:
            pass
        return sym, []

    new_f = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_funding, s): s for s in fut_syms}
        for f in concurrent.futures.as_completed(futures):
            s, rows = f.result()
            if not rows:
                continue
            old = fund_data.setdefault(s, [])
            last_t = old[-1][0] if old else 0
            for t_, r_ in rows:
                if t_ > last_t:
                    old.append([t_, r_])
                    new_f += 1
    if new_f > 0:
        try:
            with open(fund_file, 'w') as f:
                json.dump(fund_data, f)
            log(f'  ✅ 费率缓存更新: {new_f} 条新结算 ({len(fund_data)}币)')
        except Exception as e:
            log(f'  ⚠️ 费率缓存写入失败: {e}')
    else:
        log(f'  ℹ️ 费率缓存无需更新')

    # ---- OI 更新 ----
    oi_cache = '/home/myuser/backtester/data_cache/oi_daily.json'
    oi_data = {}
    if os.path.exists(oi_cache):
        try:
            with open(oi_cache) as f:
                oi_data = json.load(f)
        except Exception:
            pass

    # 8/13 修复: OI 新鲜度阈值 = 今天 UTC 00:00 (币安快照每日 UTC 00:00 出,
    # 采集时(UTC 22:00)已可得当日 00:00 快照=昨日收盘持仓; 原 yesterday_ts 阈值
    # 使缓存永远滞后一天 — 8/11 事故根因, 三层自愈同源失效)
    today_ts = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    yesterday_ts = today_ts  # 语义: 需拉到"今天00:00"快照(昨日收盘)才算新鲜
    
    def _oi_needs_update(sym):
        """检查币种OI是否需要更新（最新数据早于昨天）"""
        if sym not in oi_data or not oi_data[sym]:
            return True
        records = oi_data[sym]
        if not isinstance(records, dict) or not records:
            return True
        latest_ts = max(int(k) for k in records.keys())
        return latest_ts < yesterday_ts
    
    need_oi = [s for s in fut_syms if _oi_needs_update(s)]
    oi_updated = 0
    if need_oi:
        log(f'  OI需更新: {len(need_oi)} 币种')
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
                    if s not in oi_data:
                        oi_data[s] = {}
                    oi_data[s].update(d)
                    oi_updated += 1
                time.sleep(0.15)

        try:
            tmp_oi = oi_cache + '.tmp'
            with open(tmp_oi, 'w') as f:
                json.dump(oi_data, f)
            os.rename(tmp_oi, oi_cache)
            log(f'  ✅ OI缓存更新: {oi_updated}/{len(need_oi)} 币种')
        except Exception as e:
            log(f'  ⚠️ OI缓存写入失败: {e}')
    else:
        log(f'  ℹ️ OI缓存无需更新 (全部最新)')

    # ---- COS 上传 ----
    _upload_to_cos(kline_cache, 'klines/cache/notusdt_1d_full.json', 'K线缓存')
    _upload_to_cos(oi_cache, 'klines/cache/oi_daily.json', 'OI缓存')
    # 8/2 审计修复: 费率缓存此前从未上传 COS(本地每日更新, COS 停在 7/30)
    _upload_to_cos(fund_file, 'klines/cache/funding_hist.json', '费率缓存')

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
            Endpoint=cos_vars.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'),
            Timeout=30
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
        # 情绪采集器是常驻守护进程(screen -S sentiment)，不需要在每日脚本中重复调度
        ('BTC市值', 'python3 collect_btc_mcap.py', BASE, 30),
        ('BTC市占率', 'python3 collect_btc_dominance.py', BASE, 30),
        ('宏观资产', 'python3 collect_macro_assets.py', BASE, 30),
        ('TVL数据', 'python3 collect_tvl.py', BASE, 120),
        ('清算热力图', 'python3 liquidation_heatmap.py', BASE, 30),
        ('ETF资金流', 'python3 fetch_etf.py',
         '/home/myuser/websocket_new/data/etf_data', 120),
        ('稳定币+溢价', 'python3 monitor.py', '/home/myuser/stablecoin_data', 30),
        ('BTC算力', 'python3 collector.py', '/home/myuser/hashrate_data', 30),
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
        # FIX: 完整热力图复制到独立文件，liq_daily.json由liquidation_heatmap.py内部维护日级历史
        ('/tmp/liquidation_heatmap.json', '/home/myuser/websocket_new/data/liquidation_heatmap.json'),
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
        ('ETF', '/home/myuser/websocket_new/data/etf_data/etf_flow.json'),
        ('BTC市值', '/home/myuser/coingecko_data/btc_mcap.json'),
        ('BTC市占率', '/home/myuser/coingecko_data/btc_dominance.json'),
        ('TVL', '/home/myuser/defillama_data/ethereum_tvl.json'),
        ('宏观资产', '/home/myuser/websocket_new/data/macro_assets.json'),
        ('清算热力图', '/home/myuser/websocket_new/data/liquidation_heatmap.json'),
        ('清算历史', '/home/myuser/websocket_new/data/liq_daily.json'),
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

# 脚本末尾添加全局异常捕获（已在main中处理，这里增加启动确认）
