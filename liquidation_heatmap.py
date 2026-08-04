#!/usr/bin/env python3
"""清算热力图 — 方案B: 用订单簿+资金费率+OI推算BTC理论清算分布"""
import requests, json, time, os, hashlib, hmac, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

CACHE = "/tmp/liquidation_heatmap.json"
BASE = "https://fapi.binance.com"

def _load_api_keys():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
    return os.environ.get('BINANCE_API_KEY',''), os.environ.get('BINANCE_API_SECRET','')

def fetch_orderbook(sym='BTCUSDT', limit=1000):
    """拉订单簿深度"""
    r = requests.get(f'{BASE}/fapi/v1/depth', params={'symbol': sym, 'limit': limit}, timeout=10)
    return r.json()

OI_CACHE_FILE = "/tmp/btc_oi_cache.json"

def fetch_oi(sym='BTCUSDT'):
    """拉持仓量 — 优先本地缓存，避免直接调币安API"""
    # 1. 读本地缓存 (OI采集器或其他进程写入)
    try:
        if os.path.exists(OI_CACHE_FILE):
            with open(OI_CACHE_FILE) as f:
                cache = json.load(f)
            age = time.time() - cache.get('ts', 0)
            if age < 3600:  # 1小时内有效
                return float(cache['oi'])
    except:
        pass

    # 2. 兜底: 仅调一次币安API (每小时一次不触发限流)
    api_key, api_secret = _load_api_keys()
    if api_key:
        try:
            params = {'symbol': sym, 'timestamp': int(time.time() * 1000)}
            q = urllib.parse.urlencode(params)
            sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f'{BASE}/fapi/v1/openInterest?{q}&signature={sig}',
                             headers={'X-MBX-APIKEY': api_key}, timeout=10)
            if r.status_code == 200:
                oi = float(r.json()['openInterest'])
                # 写缓存
                with open(OI_CACHE_FILE, 'w') as f:
                    json.dump({'oi': oi, 'ts': time.time()}, f)
                return oi
        except:
            pass
    return 100000  # 兜底值

def fetch_funding(sym='BTCUSDT'):
    """拉资金费率"""
    r = requests.get(f'{BASE}/fapi/v1/premiumIndex', params={'symbol': sym}, timeout=10)
    return float(r.json()['lastFundingRate'])

def fetch_atr(sym='BTCUSDT'):
    """拉最近24h波动幅度作为ATR代理"""
    r = requests.get(f'{BASE}/fapi/v1/klines', params={'symbol': sym, 'interval': '1d', 'limit': 1}, timeout=10)
    if r.status_code == 200:
        k = r.json()[-1]
        return (float(k[2]) - float(k[3])) / float(k[1])  # (high-low)/open
    return 0.03  # 默认3%

def compute(dist_pct=15, buckets=100):
    """
    BTC清算分布 — 高斯扩散模型

    逻辑:
    1. 假设仓位开仓价以当前价为中心呈正态分布 (σ ∝ ATR)
    2. 资金费率倾斜多空比例
    3. 不同杠杆的仓位按混合分布分配
    4. 清算价 = 开仓价 × (1 ± 1/杠杆)
    5. 对所有(开仓价, 杠杆)组合采样 → 累积到价格桶 → 热力图
    """
    from math import exp, sqrt, pi
    import numpy as np

    try:
        oi = fetch_oi()
        funding = fetch_funding()
        atr_pct = fetch_atr()
    except Exception as e:
        print(f"数据拉取失败: {e}")
        return

    # 当前价
    r = requests.get(f'{BASE}/fapi/v1/ticker/price', params={'symbol': 'BTCUSDT'}, timeout=10)
    mid_price = float(r.json()['price'])

    # 资金费率 → 多空倾斜
    long_ratio = 0.5 + funding * 50
    long_ratio = max(0.3, min(0.7, long_ratio))

    # 总OI价值
    total_oi_value = oi * mid_price

    # 杠杆分布 (Bitfinex: 实际清算非常接近现价 → 绝大多数用高杠杆)
    leverage_dist = [
        # FIX 2026-08-05 (Bitfinex校准): 真实清算96%在现价±1%内, 高杠杆主导
        # 原10x/20x占35%把清算分布拉宽到±10%, 与真实(±4%)不符
        (25, 0.10),
        (50, 0.30),
        (75, 0.30),
        (100, 0.30),
    ]

    # 开仓价分布参数 — 以Bitfinex实际清算价分布校准
    bfx_prices = fetch_bitfinex_liq()
    if bfx_prices and len(bfx_prices) >= 10:
        # FIX 2026-08-05: 稳健σ = (p90-p10)/2.56 (std对尾部敏感, 76条样本σ失真到11%)
        bfx_std = (np.percentile(bfx_prices, 90) - np.percentile(bfx_prices, 10)) / 2.56
        bfx_dist_pct = bfx_std / mid_price
        # 实际清算价标准差远小于ATR, 用Bitfinex校准
        sigma = bfx_std * 0.6  # 入口价分布比清算价分布更紧
        print(f"  Bitfinex σ={bfx_dist_pct:.2%}, 采用σ={sigma/mid_price:.2%}")
    else:
        sigma = mid_price * atr_pct * 1.5

    # 清算价范围: ±dist_pct%
    lower = mid_price * (1 - dist_pct / 100)
    upper = mid_price * (1 + dist_pct / 100)
    bucket_size = (upper - lower) / buckets

    # 采样开仓价: 对正态分布采样 sample_count 个点
    sample_count = 300
    entry_samples = np.random.normal(mid_price, sigma, sample_count)
    entry_samples = entry_samples[(entry_samples > lower * 0.7) & (entry_samples < upper * 1.3)]

    long_liq = defaultdict(float)
    short_liq = defaultdict(float)

    for entry_price in entry_samples:
        # 正态密度权重
        weight = exp(-0.5 * ((entry_price - mid_price) / sigma) ** 2)
        weight /= (sigma * sqrt(2 * pi))

        for lev, lev_frac in leverage_dist:
            lev_share = total_oi_value * weight * lev_frac / sample_count

            # 多头清算价: 价格跌破这 → 多头爆
            long_liq_price = entry_price * (1 - 1.0 / lev)
            lb = round(long_liq_price / bucket_size) * bucket_size
            if lower <= lb <= upper:
                long_liq[lb] += lev_share * long_ratio * mid_price

            # 空头清算价: 价格涨破这 → 空头爆
            short_liq_price = entry_price * (1 + 1.0 / lev)
            sb = round(short_liq_price / bucket_size) * bucket_size
            if lower <= sb <= upper:
                short_liq[sb] += lev_share * (1 - long_ratio) * mid_price

    # 构建输出
    result = {
        'symbol': 'BTCUSDT',
        'price': round(mid_price, 1),
        'funding_rate': round(funding, 6),
        'long_ratio': round(long_ratio, 2),
        'oi_total': round(oi, 0),
        'oi_usd': round(total_oi_value, 0),
        'range': [round(lower, 1), round(upper, 1)],
        'bucket_size': round(bucket_size, 1),
        'model': 'gaussian_diffusion',
        'atr_pct': round(atr_pct, 4),
        'sigma_pct': round(sigma / mid_price, 4),
        'updated': time.time(),
        'levels': [],
    }

    all_buckets = sorted(set(list(long_liq.keys()) + list(short_liq.keys())))
    for b in all_buckets:
        l_val = long_liq[b]
        s_val = short_liq[b]
        if l_val + s_val > total_oi_value * 0.0005:  # 过滤噪音
            result['levels'].append({
                'price': round(b, 1),
                'long_liq_usd': round(l_val, 0),
                'short_liq_usd': round(s_val, 0),
                'total': round(l_val + s_val, 0),
                'ratio': 'long' if l_val > s_val else 'short',
            })

    result['levels'].sort(key=lambda x: x['price'])

    # 找最大
    top_l = max(result['levels'], key=lambda x: x['long_liq_usd']) if result['levels'] else None
    top_s = max(result['levels'], key=lambda x: x['short_liq_usd']) if result['levels'] else None

    tmp = CACHE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(result, f, default=str)
    os.rename(tmp, CACHE)

    # 保存日级历史数据供XGBoost使用
    try:
        HISTORY_FILE = "/home/myuser/websocket_new/data/liq_daily.json"
        LEVELS_FILE = "/home/myuser/websocket_new/data/liq_levels_daily.json"
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        total_long = sum(l['long_liq_usd'] for l in result['levels'])
        total_short = sum(l['short_liq_usd'] for l in result['levels'])
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # 旧7维汇总 (保持兼容)
        daily_record = {
            'date': today_str,
            'total_long_liq': round(total_long / 1e8, 2),
            'total_short_liq': round(total_short / 1e8, 2),
            'liq_ratio': round(total_long / total_short, 4) if total_short > 0 else 1.0,
            'long_peak_dist_pct': round((top_l['price'] - mid_price) / mid_price * 100, 2) if top_l else 0,
            'short_peak_dist_pct': round((top_s['price'] - mid_price) / mid_price * 100, 2) if top_s else 0,
            'funding_rate': result['funding_rate'],
            'long_ratio': result['long_ratio'],
        }
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        if not isinstance(history, list):
            history = []
        history = [h for h in history if isinstance(h, dict) and h.get('date') != daily_record['date']]
        history.append(daily_record)
        history.sort(key=lambda x: x['date'])
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(history, f, indent=2)
        os.rename(tmp, HISTORY_FILE)

        # 新：按日期累积每小时快照 → 训练时聚合24h信息
        # 边界检查：小时必须在0-23，日期必须合法
        now_hour = datetime.now(timezone.utc).hour
        if not (0 <= now_hour <= 23):
            now_hour = 0
        if not isinstance(today_str, str) or len(today_str) != 10:
            print(f"  ⚠️ 清算历史保存跳过: 日期格式异常 {today_str}")
            return
        # 验证levels结构完整性
        if not isinstance(result.get('levels'), list) or len(result['levels']) < 5:
            print(f"  ⚠️ 清算历史保存跳过: levels数据异常 ({len(result.get('levels', []))}层)")
            return
        # 读历史文件 (防御损坏)
        levels_history = {}
        if os.path.exists(LEVELS_FILE):
            try:
                with open(LEVELS_FILE) as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    levels_history = raw
                else:
                    print(f"  ⚠️ liq_levels_daily.json 格式损坏(type={type(raw).__name__}), 重建")
            except (json.JSONDecodeError, Exception) as e:
                print(f"  ⚠️ liq_levels_daily.json 读取失败: {e}, 重建")
        # 去重同小时 + 追加
        if today_str not in levels_history:
            levels_history[today_str] = []
        # 防御：确保是合法快照列表
        if not isinstance(levels_history[today_str], list):
            levels_history[today_str] = []
        levels_history[today_str] = [
            s for s in levels_history[today_str]
            if isinstance(s, dict) and s.get('h') != now_hour
        ]
        levels_history[today_str].append({'h': now_hour, 'levels': result['levels']})
        levels_history[today_str].sort(key=lambda x: x.get('h', 0))
        # 防御：每个日期最多24个快照
        if len(levels_history[today_str]) > 24:
            levels_history[today_str] = levels_history[today_str][-24:]
        # 清理过期日期 (保留90天) + 清理无效快照
        for d in list(levels_history.keys()):
            if not isinstance(levels_history[d], list):
                del levels_history[d]
                continue
            levels_history[d] = [
                s for s in levels_history[d]
                if isinstance(s, dict) and isinstance(s.get('levels'), list) and len(s['levels']) >= 5
            ]
            if not levels_history[d]:
                del levels_history[d]
        sorted_dates = sorted(levels_history.keys())
        if len(sorted_dates) > 90:
            for old in sorted_dates[:-90]:
                del levels_history[old]
        # 原子写：先.tmp再rename，防止写一半崩溃损坏
        tmp_file = LEVELS_FILE + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(levels_history, f)
        os.rename(tmp_file, LEVELS_FILE)

        print(f"  日级记录已保存: {daily_record['date']} | 清算分布: {len(result['levels'])}层 "
              f"(第{now_hour}时, 今日累计{len(levels_history[today_str])}个快照)")
    except Exception as e:
        print(f"  历史保存失败: {e}")

    print(f"清算热力图: {len(result['levels'])}层, 价${mid_price:,.0f}, OI=${total_oi_value/1e9:.1f}B")
    print(f"  ATR={atr_pct:.1%}, σ={sigma/mid_price:.1%}, 多偏={long_ratio:.0%}")
    if top_l:
        print(f"  多头清算峰: ${top_l['price']:,.0f} ({top_l['long_liq_usd']/1e6:.0f}M)")
    if top_s:
        print(f"  空头清算峰: ${top_s['price']:,.0f} ({top_s['short_liq_usd']/1e6:.0f}M)")
    return result

def upload_cos():
    """上传到COS"""
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
        with open(CACHE, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/liquidation_heatmap/latest.json',
                           Body=f.read(), ContentType='application/json')
        print("COS: 已上传")
    except Exception as e:
        print(f"COS: {e}")

def fetch_bitfinex_liq(sym='BTC'):
    """拉Bitfinex公开清算数据做校准 — 分页拉5000条(原500条样本太小σ失真)"""
    import time as _t
    prices = []
    cursor = None
    try:
        for _ in range(12):
            params = {'limit': 500, 'sort': -1}
            if cursor:
                params['start'] = cursor
            r = requests.get('https://api-pub.bitfinex.com/v2/liquidations/hist',
                             params=params, timeout=10)
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            for item in data:
                inner = item[0]
                if sym in str(inner[4]) and inner[11] is not None:
                    liq_price = float(inner[11])
                    if liq_price > 0:
                        prices.append(liq_price)
            cursor = data[-1][0][2] - 1
            _t.sleep(0.15)
        return sorted(prices)
    except Exception:
        return sorted(prices)

CALIB_LOG = "/tmp/liquidation_calibration.csv"

def compare_with_bitfinex(result):
    """对比自建模型 vs Bitfinex实际清算价"""
    bfx_prices = fetch_bitfinex_liq()
    if not bfx_prices:
        print("  Bitfinex: 无数据")
        return

    # 取Bitfinex清算价分布
    n = len(bfx_prices)
    p50 = bfx_prices[n//2]
    bfx_std = float(__import__('numpy').std(bfx_prices))

    # 我们的模型找峰
    levels = result.get('levels', [])
    if not levels:
        return

    long_peak = max(levels, key=lambda x: x['long_liq_usd'])
    short_peak = max(levels, key=lambda x: x['short_liq_usd'])

    # Bitfinex数据: 用清算价桶聚合
    from collections import Counter
    bucket = result['bucket_size']
    bfx_buckets = Counter(round(p / bucket) * bucket for p in bfx_prices)
    bfx_top_bucket = bfx_buckets.most_common(1)[0][0] if bfx_buckets else 0

    # 偏差 = min(|多头峰-BFX峰|, |空头峰-BFX峰|)
    gap = min(abs(bfx_top_bucket - long_peak['price']),
              abs(bfx_top_bucket - short_peak['price']))

    print(f"\n  === Bitfinex校准 ===")
    print(f"  Bitfinex: {len(bfx_prices)}条, 中位${p50:,.0f}, σ={bfx_std:.0f}")
    print(f"  密集桶: ${bfx_top_bucket:,.0f} | 多头峰${long_peak['price']:,.0f} | 空头峰${short_peak['price']:,.0f}")
    print(f"  偏差: ${gap:,.0f} ({gap/result['price']*100:.1f}%)")

    # 写校准日志
    import csv
    exists = os.path.exists(CALIB_LOG)
    with open(CALIB_LOG, 'a', newline='') as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(['time','price','bfx_count','bfx_median','bfx_std',
                       'bfx_dense','long_peak','short_peak','gap_pct'])
        w.writerow([
            datetime.fromtimestamp(result['updated'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
            result['price'], len(bfx_prices), round(p50, 0), round(bfx_std, 0),
            round(bfx_top_bucket, 0), round(long_peak['price'], 0),
            round(short_peak['price'], 0), round(gap/result['price']*100, 2)
        ])

if __name__ == '__main__':
    result = compute()
    if result:
        compare_with_bitfinex(result)
    upload_cos()
