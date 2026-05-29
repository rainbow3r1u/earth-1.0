#!/usr/bin/env python3
"""2日跌预测器 — 用K线+OI特征选出2天后最可能跌>5%的币种"""
import json, requests, numpy as np, os, time, pickle
from datetime import datetime, timezone
from xgboost import XGBClassifier
import daily_predictor as dp
from utils.feature_builder import assemble_feature_vec

# Kronos 特征提取 (Deep B 方案)
from kronos_features import extract_kronos_features

KRONOS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/kronos_features_cache_short.json")
EMBEDDING_DIM = 832  # Kronos特征维度，与 daily_predictor.py 保持一致

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/daily_short_predictions.json")
MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/xgb_short_model.pkl")
LOG_DIR = "/home/myuser/blockchair_data/predictions"
TRACK_FILE = os.path.join(LOG_DIR, "prediction_tracker_short.json")
SECTOR_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/crypto_sectors.json")
SECTOR_ORDER = [
    'AI', 'AI Agent', 'BTC生态', 'Base生态', 'DEX', 'DeFi', 'DePIN', 'DeSci',
    'ETH生态', 'L1', 'L2', 'Meme', 'RWA', 'Solana', 'TON生态',
    '再质押', '并行EVM', '流动性质押', '游戏', '链抽象', '隐私', '预言机',
]

def _load_sector_map():
    try:
        with open(SECTOR_CACHE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def _precompute_sector_heats(klines_all, sector_map):
    """预计算所有交易日的22个板块TOP15均涨幅
    返回: {timestamp: [22 floats]} 按SECTOR_ORDER顺序
    """
    # 收集每个币的每日收益
    coin_rets = {}  # {sym: [(timestamp, ret), ...]}
    for sym, kls in klines_all.items():
        if len(kls) < 2:
            continue
        closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
        timestamps = [k.get('t', 0)//1000 if isinstance(k, dict) else int(k[0])//1000 for k in kls]
        rets = []
        for j in range(1, len(closes)):
            ret = (closes[j] - closes[j-1]) / closes[j-1] if closes[j-1] > 0 else 0
            rets.append((timestamps[j], ret))
        coin_rets[sym] = rets

    # 收集所有出现的timestamp
    all_ts = set()
    for rets in coin_rets.values():
        for ts, _ in rets:
            all_ts.add(ts)
    print(f"板块热度预计算: {len(coin_rets)}币种, {len(all_ts)}个交易日")

    # 每个timestamp计算22个板块TOP15均值
    heats = {}
    for ts in sorted(all_ts):
        sector_gains = {s: [] for s in SECTOR_ORDER}
        for sym, rets in coin_rets.items():
            for r_ts, ret in rets:
                if r_ts == ts:
                    for tag in sector_map.get(sym, []):
                        if tag in sector_gains:
                            sector_gains[tag].append(ret)
                    break
        vals = []
        for s in SECTOR_ORDER:
            gains = sorted(sector_gains[s], reverse=True)[:15]
            vals.append(round(sum(gains) / len(gains), 6) if gains else 0.0)
        heats[ts] = vals
    return heats

def _get_sector_features(sym, ts, sector_map, sector_heats):
    """返回单个币种在某天的22个板块特征值"""
    coin_sectors = sector_map.get(sym, [])
    heats = sector_heats.get(ts, [0]*len(SECTOR_ORDER))
    return [heats[i] if SECTOR_ORDER[i] in coin_sectors else 0.0
            for i in range(len(SECTOR_ORDER))]

# ─── 宏观特征加载 ───

def _load_etf_features():
    """ETF净流入: {date_str: [btc_flow_m, eth_flow_m]}"""
    try:
        with open('/home/myuser/openclaw-5001-host/config/.openclaw/workspace/etf_data/etf_flow.json') as f:
            data = json.load(f)
        result = {}
        for d in data.get('btc', []):
            result[d['date']] = [d.get('total_flow', 0) or 0, 0]
        for d in data.get('eth', []):
            date = d['date']
            result.setdefault(date, [0, 0])[1] = d.get('total_flow', 0) or 0
        return result
    except Exception: return {}

def _load_chain_features():
    """链上数据日聚合: {date_str: [volume_btc, tx_count, fee_usd, cdd_ratio]}"""
    try:
        import csv
        from collections import defaultdict
        daily = defaultdict(list)
        with open('/home/myuser/blockchair_data/btc_chain.csv') as f:
            for row in csv.DictReader(f):
                daily[row['ts'][:10]].append(row)
        result = {}
        for date, rows in daily.items():
            if len(rows) < 10: continue
            vol = np.mean([float(r['volume_24h_btc']) for r in rows if r.get('volume_24h_btc')])
            tx = np.mean([float(r['tx_count_24h']) for r in rows if r.get('tx_count_24h')])
            fee = np.mean([float(r['avg_fee_usd_24h']) for r in rows if r.get('avg_fee_usd_24h')])
            cdds = [float(r['cdd_24h']) for r in rows if r.get('cdd_24h')]
            cdd = np.mean(cdds) if cdds else 0
            cdd_ratio = cdd / vol if vol > 0 else 0
            result[date] = [round(vol, 2), round(tx, 0), round(fee, 4), round(cdd_ratio, 6)]
        return result
    except Exception: return {}

def _load_sent_features():
    """情绪日聚合: {date_str: [funding_top5_avg, ls_btc, ls_eth, ls_avg10, ls_high, ls_low]}"""
    try:
        import glob
        from collections import defaultdict
        daily = defaultdict(list)
        for fn in sorted(glob.glob('/home/myuser/sentiment_data/sentiment_*.json')):
            with open(fn) as f:
                d = json.load(f)
            date = d.get('datetime', '')[:10]
            fr = d.get('funding_rates', {})
            ls = d.get('long_short_ratios', {})
            daily[date].append((
                fr.get('top5_avg', 0) or 0,
                ls.get('btc', 0) or 0,
                ls.get('eth', 0) or 0,
                ls.get('avg_top10', 0) or 0,
                ls.get('extreme_high', 0) or 0,
                ls.get('extreme_low', 0) or 0,
            ))
        result = {}
        for date, rows in daily.items():
            if len(rows) < 2: continue
            result[date] = [
                round(np.mean([r[0] for r in rows]), 6),
                round(np.mean([r[1] for r in rows]), 4),
                round(np.mean([r[2] for r in rows]), 4),
                round(np.mean([r[3] for r in rows]), 4),
                round(np.mean([r[4] for r in rows]), 2),
                round(np.mean([r[5] for r in rows]), 2),
            ]
        return result
    except Exception: return {}

def _load_fear_greed():
    """恐慌贪婪: {date_str: [fear_greed_normalized]}"""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'data/fear_greed_history.json')) as f:
            return {d['date']: [d['value'] / 100.0] for d in json.load(f)}
    except Exception: return {}

def _load_stablecoin_netflow():
    """稳定币净流入: {date_str: [netflow_100M]}"""
    try:
        with open('/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value'] / 1e8] for d in data if d.get('value') is not None}
    except Exception: return {}

def _load_coinbase_premium():
    """Coinbase溢价: {date_str: [premium_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_coinbase_premium_index.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception: return {}

def _load_cb_gap_features():
    """Coinbase Premium Gap: {date_str: [gap_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_coinbase_premium_gap.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception: return {}

def _load_korea_premium():
    """韩国溢价指数: {date_str: [premium_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_korea_premium_index.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception: return {}

def _load_hashrate_features():
    """BTC算力7日变化率: {date_str: [hashrate_7d_chg]}"""
    try:
        with open('/home/myuser/hashrate_data/hashrate_history.json') as f:
            data = json.load(f)
        hr_map = {d['date']: d['hash_rate_ghs'] for d in data if d.get('hash_rate_ghs') is not None}
        dates = sorted(hr_map.keys())
        result = {}
        for i, date in enumerate(dates):
            if i < 7:
                result[date] = [0.0]
                continue
            prev = dates[i - 7]
            prev_hr = hr_map[prev]
            curr_hr = hr_map[date]
            if prev_hr > 0:
                result[date] = [round((curr_hr - prev_hr) / prev_hr, 6)]
            else:
                result[date] = [0.0]
        return result
    except Exception: return {}

def _load_btc_mcap():
    """BTC市值7日变化率: {date_str: [btc_mcap_7d_chg]}"""
    try:
        with open('/home/myuser/coingecko_data/btc_mcap.json') as f:
            return {d['date']: [d['btc_mcap_7d_chg']] for d in json.load(f)}
    except Exception: return {}

def _load_chain_tvl():
    """链TVL 7日变化率: {date_str: [btc,eth,sol,bsc,arb,base]}"""
    chains = ['btc_chain','ethereum','solana','binance','arbitrum','base']
    try:
        result = {}
        for i, name in enumerate(chains):
            path = f'/home/myuser/defillama_data/{name}_tvl.json'
            with open(path) as f:
                for d in json.load(f):
                    result.setdefault(d['date'], [0]*len(chains))[i] = d['tvl_7d_chg']
        return result
    except Exception: return {}

def _load_liquidation_features():
    """清算热力图日级: {date_str: [total_long, total_short, liq_ratio, long_peak_dist, short_peak_dist, funding, long_ratio]}"""
    try:
        with open('/home/myuser/websocket_new/data/liq_daily.json') as f:
            data = json.load(f)
        return {d['date']: [
            d.get('total_long_liq', 0),
            d.get('total_short_liq', 0),
            d.get('liq_ratio', 1.0),
            d.get('long_peak_dist_pct', 0),
            d.get('short_peak_dist_pct', 0),
            d.get('funding_rate', 0),
            d.get('long_ratio', 0.5),
        ] for d in data}
    except Exception: return {}

def _get_macro_features(ts):
    """根据时间戳获取所有宏观特征 — ETF用前一日数据避免未来函数"""
    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')

    # ETF用前一天 (当天收盘时ETF数据还没公布)
    prev_ts = ts - 86400
    prev_date = datetime.fromtimestamp(prev_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    etf = _etf_features.get(prev_date, [0, 0])

    chain = _chain_features.get(prev_date, [0, 0, 0, 0])
    sent = _sent_features.get(prev_date, [0]*6)
    fg = _fg_features.get(prev_date, [0])
    st = _st_features.get(prev_date, [0])
    cb = _cb_features.get(prev_date, [0])
    cbg = _cbg_features.get(prev_date, [0])
    bd = _bd_features.get(prev_date, [0])
    kg = _kg_features.get(prev_date, [0])
    hr = _hr_features.get(prev_date, [0])
    liq = _liq_features.get(prev_date, [0]*7)
    tvl = _tvl_features.get(prev_date, [0]*6)  # 6链TVL
    ma = _ma_features.get(prev_date, [0]*3)  # SP500/DXY/黄金
    ab = _ab_features.get(int(ts), [0])    # 山寨BTC溢价 (int key, 与dp对齐)
    kr = _kr_features.get(ts, [0.0]*EMBEDDING_DIM)
    if len(kr) > EMBEDDING_DIM:
        kr = kr[:EMBEDDING_DIM]
    return etf + chain + sent + fg + st + cb + cbg + bd + kg + hr + liq + tvl + list(kr) + ma + ab

# 链TVL→币归属映射
CHAIN_TVL_MAP = {'BTC生态': 0, 'ETH生态': 1, 'Solana': 2, 'BSC': 3, 'ARB': 4, 'Base生态': 5}

def _apply_chain_tvl(macro_feats, sym, ts=None):
    """根据币的链归属清零无关链TVL + 填充协议TVL"""
    macro_feats = macro_feats.copy()  # 避免原地修改 (REGRESSION-002 fix)
    coin_tags = _sector_map_cache.get(sym, [])
    tvl_start = len(macro_feats) - 6 - EMBEDDING_DIM - 3 - 1  # tvl(6) + kr(20) + ma(3) + ab(1)

    # 链TVL
    matched = False
    for tag in coin_tags:
        if tag in CHAIN_TVL_MAP:
            matched = True
            break
    if not matched:
        macro_feats[tvl_start + 0] = 0  # BTC
        macro_feats[tvl_start + 1] = 0  # ETH
        macro_feats[tvl_start + 2] = 0  # SOL
        macro_feats[tvl_start + 3] = 0  # BSC
        macro_feats[tvl_start + 4] = 0  # ARB
        macro_feats[tvl_start + 5] = 0  # Base

    return macro_feats
_proto_map_local = {}
# _proto_tvl_data removed (dead code)

_sector_map_cache = {}


def _precompute_kronos_features(timestamps):
    """批量计算 Kronos 特征，带磁盘缓存以避免重复推理"""
    global _kr_features, EMBEDDING_DIM
    if not timestamps:
        return
    unique_ts = sorted(set(int(t) for t in timestamps))

    # 1. 先试 embedding 文件 (20D PCA, 与 daily_predictor 一致) — HIGH-MODEL-001
    emb_data = dp._load_kronos_embeddings() if hasattr(dp, '_load_kronos_embeddings') else None
    if emb_data is not None:
        emb_map = emb_data['embeddings']
        hit = 0
        for ts in unique_ts:
            if str(ts) in emb_map:
                _kr_features[ts] = emb_map[str(ts)]
                hit += 1
        unique_ts = [ts for ts in unique_ts if ts not in _kr_features]
        if hit > 0:
            print(f"[Kronos Embedding] {hit}/{hit+len(unique_ts)} 从预计算命中")

    if not unique_ts:
        return

    # 2. 加载磁盘缓存 (统计特征回退)
    disk_cache = {}
    if os.path.exists(KRONOS_CACHE_FILE):
        try:
            with open(KRONOS_CACHE_FILE) as f:
                disk_cache = json.load(f)
        except Exception:
            pass

    missing = [ts for ts in unique_ts if str(ts) not in disk_cache]
    if missing:
        print(f"[Kronos] 预计算 {len(missing)} 个日期特征 (已缓存 {len(unique_ts)-len(missing)})...")
        for idx, ts in enumerate(missing):
            feats = extract_kronos_features(ts, pred_len=2)
            disk_cache[str(ts)] = feats
            if (idx + 1) % 10 == 0:
                try:
                    tmp = KRONOS_CACHE_FILE + '.tmp'
                    with open(tmp, 'w') as f:
                        json.dump(disk_cache, f)
                    os.rename(tmp, KRONOS_CACHE_FILE)
                except Exception:
                    pass
        try:
            tmp = KRONOS_CACHE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(disk_cache, f)
            os.rename(tmp, KRONOS_CACHE_FILE)
        except Exception:
            pass

    # 回填全局缓存
    for ts in unique_ts:
        feats = disk_cache.get(str(ts), [0.0]*EMBEDDING_DIM)
        if len(feats) < EMBEDDING_DIM:
            feats = list(feats) + [0.0] * (EMBEDDING_DIM - len(feats))
        _kr_features[ts] = feats[:EMBEDDING_DIM]

# winsor截尾bounds — 训练时算好存下来，预测/回测复用
WINSOR_BOUNDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/winsor_bounds_short.json")
_winsor_bounds = None

def _compute_winsor_bounds(X):
    bounds = []
    for j in range(X.shape[1]):
        col = X[:, j]
        bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
    with open(WINSOR_BOUNDS_FILE, 'w') as f:
        json.dump(bounds, f)
    return bounds

def _load_winsor_bounds():
    try:
        with open(WINSOR_BOUNDS_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _apply_winsor(X, bounds):
    if bounds is None:
        return X
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    if len(lo) != X.shape[1]:
        print(f"[WARN] Winsor bounds维度不匹配: bounds={len(lo)}, features={X.shape[1]}, 跳过截尾")
        return X
    return np.clip(X, lo, hi)

def _find_kline_index(kls_data, ts_sec):
    """在K线列表中找到秒级时间戳对应的索引"""
    ts_ms = ts_sec * 1000
    for j, k in enumerate(kls_data):
        ts_k = k.get('t', 0) if isinstance(k, dict) else int(k[0])
        if ts_k == ts_ms:
            return j
    return None

# 全局缓存（每次run/train/backtest时加载）
_etf_features = {}
_chain_features = {}
_sent_features = {}
_fg_features = {}
_st_features = {}
_cb_features = {}
_cbg_features = {}
_bd_features = {}
_kg_features = {}
_hr_features = {}
_liq_features = {}
_tvl_features = {}
_ma_features = {}
_ab_features = {}
# _proto_tvl_data removed (dead code)
_kr_features = {}


def fetch_klines():
    """拉全币种日线（完整缓存优先 + 实时API补最新）"""
    klines = {}
    import concurrent.futures

    # 1. 优先加载完整历史缓存 (541币, 500天BTC, ~240天山寨)
    q4_cache = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    old_cache = '/home/myuser/backtester/data_cache/notusdt_1d.json'
    cache_file = q4_cache if os.path.exists(q4_cache) else old_cache

    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)['klines']
            for sym, kls in cached.items():
                if len(kls) >= 30:
                    klines[sym] = kls
            print(f"K线: 加载{len(klines)}币种 (缓存: {os.path.basename(cache_file)})")
        except Exception as e:
            print(f"缓存加载失败: {e}")

    # 2. 实时API补最新的几天 (确保缓存有最新数据)
    try:
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('contractType')=='PERPETUAL']
    except Exception:
        fut_syms = []

    def _fetch_history(sym):
        """拉全量历史日线（最多500天）"""
        try:
            r = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 500}, timeout=15)
            if r.status_code == 200:
                return sym, [{'t':int(k[0]),'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5]),'q':float(k[7])} for k in r.json()]
        except Exception: pass
        return sym, []

    # 补缓存中没有的币 (主流优先，拉全量历史)
    new_syms = [s for s in fut_syms if s not in klines]
    added = 0
    if new_syms:
        major = ['ETHUSDT','SOLUSDT','POLUSDT','XRPUSDT','TRXUSDT','TONUSDT','NEARUSDT']
        new_syms = sorted(new_syms, key=lambda x: (x not in major, x))
        print(f"补拉{len(new_syms)}新币...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_fetch_history, s): s for s in new_syms[:50]}
            for f in concurrent.futures.as_completed(futures):
                s, kls = f.result()
                if kls and len(kls) >= 30:
                    klines[s] = kls
                    added += 1
        # 新币写入缓存文件，下次不用重拉
        if added > 0:
            try:
                with open(cache_file) as f:
                    cache_data = json.load(f)
                cache_data['klines'] = klines
                with open(cache_file, 'w') as f:
                    json.dump(cache_data, f)
                print(f"  {added}新币已写入缓存")
            except Exception: pass

    print(f"K线: {len(klines)}币种 (缓存{len(klines)-added}+补拉{added})")
    return klines

def fetch_oi(syms, limit=30):
    """拉全币种30天OI — 优先本地缓存，API补缺失"""
    import concurrent.futures
    
    # 加载本地OI历史缓存
    cache_path = "/home/myuser/backtester/data_cache/oi_daily.json"
    local_cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                local_cache = json.load(f)
        except Exception: pass
    
    oi_data = {}
    need_api = []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    
    for sym in syms:
        if sym in local_cache and local_cache[sym]:
            cached = local_cache[sym]
            sorted_ts = sorted(cached.keys(), reverse=True)[:limit]
            result = {int(ts): float(cached[ts]) for ts in sorted_ts}
            if result:
                latest = max(result.keys())
                if now_ts - latest < 86400 * 2:  # 缓存最新数据在2天内，直接可用
                    oi_data[sym] = result
                    continue
        need_api.append(sym)
    
    # API补缺失或陈旧的币种
    def _fetch(sym):
        try:
            r = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
                params={'symbol': sym, 'period': '1d', 'limit': limit}, timeout=10)
            if r.status_code == 200:
                return sym, {int(o['timestamp'])//1000: float(o['sumOpenInterest']) for o in r.json()}
        except Exception: pass
        return sym, {}
    
    if need_api:
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as pool:
            futures = {pool.submit(_fetch, s): s for s in need_api[:400]}
            for f in concurrent.futures.as_completed(futures):
                s, d = f.result()
                if d: oi_data[s] = d
    
    print(f"OI: 缓存{len(oi_data)-len(need_api)}币种 + API补{len(need_api)}币种")
    return oi_data

def _compute_returns(closes):
    """从收盘价序列计算日收益率"""
    return [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0 for j in range(1, len(closes))]

def _compute_rsi(closes, period=14, idx=None):
    """计算RSI — 委托给 dp._compute_rsi (标准Wilder实现)"""
    return dp._compute_rsi(closes, period, idx)

def _regression_features(btc_rets, coin_rets, end_idx, window=20):
    """对单个币种计算 β, α, R², residual（以BTC为基准，滚动窗口OLS）"""
    start = max(0, end_idx - window + 1)
    x = np.array(btc_rets[start:end_idx+1])
    y = np.array(coin_rets[start:end_idx+1])
    if len(x) < 5 or np.std(x) < 1e-8:
        return 1.0, 0.0, 0.0, 0.0  # 默认值
    # OLS
    beta = np.cov(x, y)[0,1] / np.var(x) if np.var(x) > 0 else 1.0
    alpha = np.mean(y) - beta * np.mean(x)
    y_pred = alpha + beta * x
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    residual = y[-1] - y_pred[-1]
    return round(beta, 4), round(alpha, 6), round(r2, 4), round(residual, 6)

def build_features(klines_all, oi_data):
    """构建特征矩阵（含β/α/R²/residual + 22板块热度 + 9宏观特征）"""
    sector_map = _load_sector_map()
    global _sector_map_cache, _proto_map_local; _sector_map_cache = sector_map
    if not _proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                _proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except Exception: pass
    sector_heats = _precompute_sector_heats(klines_all, sector_map) if sector_map else {}

    global _etf_features, _chain_features, _sent_features, _fg_features, _st_features, _cb_features, _cbg_features, _bd_features, _kg_features, _hr_features, _liq_features, _tvl_features, _ma_features, _ab_features
    _etf_features = _load_etf_features()
    _chain_features = _load_chain_features()
    _sent_features = _load_sent_features()
    _fg_features = _load_fear_greed()
    _st_features = _load_stablecoin_netflow()
    _cb_features = _load_coinbase_premium()
    _cbg_features = _load_cb_gap_features()
    _bd_features = _load_btc_mcap()
    _kg_features = _load_korea_premium()
    _hr_features = _load_hashrate_features()
    _liq_features = _load_liquidation_features()
    _tvl_features = _load_chain_tvl()
    _ma_features = dp._load_macro_assets()
    _ab_features = dp._load_btc_dominance_proxy()

    # 收集所有币种最近K线的时间戳，预计算Kronos特征
    all_ts_for_kronos = set()
    for sym, kls in klines_all.items():
        if len(kls) < 30:
            continue
        timestamps = [k.get('t',0)//1000 if isinstance(k,dict) else int(k[0])//1000 for k in kls]
        i = len(kls) - 2
        if i >= 25:
            all_ts_for_kronos.add(timestamps[i])
    if all_ts_for_kronos:
        _precompute_kronos_features(list(all_ts_for_kronos))

    btc_kls = klines_all.get('BTCUSDT', [])
    btc_closes = [k['c'] if isinstance(k,dict) else float(k[4]) for k in btc_kls]
    btc_rets = _compute_returns(btc_closes)

    X, symbols, timestamps_list = [], [], []
    for sym, kls in klines_all.items():
        if len(kls) < 30: continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k,dict) else float(k[4]) for k in kls]
        opens = [k['o'] if isinstance(k,dict) else float(k[1]) for k in kls]
        highs = [k['h'] if isinstance(k,dict) else float(k[2]) for k in kls]
        lows = [k['l'] if isinstance(k,dict) else float(k[3]) for k in kls]
        vols = [k['q'] if isinstance(k,dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t',0)//1000 if isinstance(k,dict) else int(k[0])//1000 for k in kls]
        n = len(kls)
        i = n - 2  # 用最近一根已完成的K线
        if i < 25: continue
        try:
            ret_1d = (closes[i]-closes[i-1])/closes[i-1] if closes[i-1]>0 else 0
            ret_3d = (closes[i]-closes[max(0,i-3)])/closes[max(0,i-3)] if closes[max(0,i-3)]>0 else 0
            ret_5d = (closes[i]-closes[max(0,i-5)])/closes[max(0,i-5)] if closes[max(0,i-5)]>0 else 0
            if i >= 20:
                rets_20 = [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0 for j in range(i-19,i+1)]
                vol_20d = float(np.std(rets_20))
            else:
                vol_20d = 0.02
            vol_floor = max(vol_20d, 0.002)
            ret_1d_norm = round(ret_1d / vol_floor, 4)
            ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
            ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
            if i >= 5:
                daily_rets = [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0 for j in range(i-4,i+1)]
                volatility = np.std(daily_rets)
            else: volatility = 0
            vol_ratio = vols[i]/np.mean(vols[max(0,i-5):i]) if i>=5 and np.mean(vols[max(0,i-5):i])>0 else 1
            if i >= 20:
                c20 = closes[i-20:i+1]
                price_position = (closes[i]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
            else: price_position = 0.5
            amplitude = (highs[i]-lows[i])/opens[i] if opens[i]>0 else 0
            streak = 0
            for j in range(i, max(0,i-7)-1,-1):
                if closes[j]>opens[j]: streak+=1
                else: break
            div_sign = 1 if (closes[i]>closes[i-3] and vols[i]<vols[i-3]*0.7) else 0
            ts = timestamps[i]
            oi_now = oi_map.get(ts, 0); oi_prev = oi_map.get(ts-86400, 0)
            oi_chg = (oi_now-oi_prev)/oi_prev if oi_prev>0 else 0

            # BTC回归特征: β, α, R², residual
            coin_rets = _compute_returns(closes)
            if sym == 'BTCUSDT':
                beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
            else:
                beta, alpha, r2, residual = _regression_features(btc_rets, coin_rets, i-1)

            # 板块热度特征 (22维)
            # 板块热度用前一日，避免当日收益率泄露
            ts_prev = ts - 86400
            sector_feats = _get_sector_features(sym, ts_prev, sector_map, sector_heats)

            # RSI
            rsi7 = _compute_rsi(closes, 7, i)
            rsi14 = _compute_rsi(closes, 14, i)
            rsi30 = _compute_rsi(closes, 30, i)
            rsi14_series = dp._compute_rsi_series(closes, 14)
            rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, i, window=20)
            vol_col = dp._compute_vol_clustering(closes, i)

            # 宏观特征
            macro_feats = _get_macro_features(ts)
            macro_feats = _apply_chain_tvl(macro_feats, sym, ts)

            feat = assemble_feature_vec(
                ret_1d_norm, ret_3d_norm, ret_5d_norm,
                volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg,
                vol_col, beta, alpha, r2, residual, rsi7, rsi14, rsi30,
                rsi_div, sector_feats, macro_feats)
            X.append(feat)
            symbols.append(sym)
            timestamps_list.append(ts)
        except Exception: continue
    # winsor截尾
    global _winsor_bounds
    if _winsor_bounds is None:
        _winsor_bounds = _load_winsor_bounds()
    X_arr = _apply_winsor(np.array(X), _winsor_bounds)
    return X_arr, symbols, timestamps_list

def train(klines_all, oi_data):
    """训练模型 — 用前一天数据预测当天是否跌>5%（含板块热度+宏观特征）"""
    sector_map = _load_sector_map()
    global _sector_map_cache, _proto_map_local; _sector_map_cache = sector_map
    if not _proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                _proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except Exception: pass
    sector_heats = _precompute_sector_heats(klines_all, sector_map) if sector_map else {}

    # 加载宏观特征
    global _etf_features, _chain_features, _sent_features, _fg_features, _st_features, _cb_features, _cbg_features, _bd_features, _kg_features, _hr_features, _liq_features, _tvl_features, _ma_features, _ab_features
    _etf_features = _load_etf_features()
    _chain_features = _load_chain_features()
    _sent_features = _load_sent_features()
    _fg_features = _load_fear_greed()
    _st_features = _load_stablecoin_netflow()
    _cb_features = _load_coinbase_premium()
    _cbg_features = _load_cb_gap_features()
    _bd_features = _load_btc_mcap()
    _kg_features = _load_korea_premium()
    _hr_features = _load_hashrate_features()
    _liq_features = _load_liquidation_features()
    _tvl_features = _load_chain_tvl()
    _ma_features = dp._load_macro_assets()
    _ab_features = dp._load_btc_dominance_proxy()
    print(f"宏观特征: ETF{len(_etf_features)}d 链上{len(_chain_features)}d 情绪{len(_sent_features)}d 恐慌贪婪{len(_fg_features)}d 稳定币{len(_st_features)}d Coinbase{len(_cb_features)}d Gap{len(_cbg_features)}d 韩国{len(_kg_features)}d 算力{len(_hr_features)}d 清算{len(_liq_features)}d 宏观{len(_ma_features) if hasattr(_ma_features, '__len__') else 0}d 山寨溢价{len(_ab_features) if hasattr(_ab_features, '__len__') else 0}d")

    btc_kls = klines_all.get('BTCUSDT', [])
    btc_closes = [k['c'] if isinstance(k,dict) else float(k[4]) for k in btc_kls]
    btc_rets = _compute_returns(btc_closes) if len(btc_closes) > 1 else []

    # 预收集所有需要Kronos特征的时间戳
    all_ts_for_kronos = set()
    for sym, kls in klines_all.items():
        if len(kls) < 30: continue
        timestamps = [k.get('t',0)//1000 if isinstance(k,dict) else int(k[0])//1000 for k in kls]
        for i in range(25, len(kls)-2):
            all_ts_for_kronos.add(timestamps[i])
    if all_ts_for_kronos:
        _precompute_kronos_features(list(all_ts_for_kronos))

    Xall, yall = [], []
    for sym, kls in klines_all.items():
        if len(kls) < 30: continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k,dict) else float(k[4]) for k in kls]
        opens = [k['o'] if isinstance(k,dict) else float(k[1]) for k in kls]
        highs = [k['h'] if isinstance(k,dict) else float(k[2]) for k in kls]
        lows = [k['l'] if isinstance(k,dict) else float(k[3]) for k in kls]
        vols = [k['q'] if isinstance(k,dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t',0)//1000 if isinstance(k,dict) else int(k[0])//1000 for k in kls]
        coin_rets = _compute_returns(closes)
        n = len(kls)

        for i in range(25, n-2):
            try:
                # 特征统一基于 j=i-1 (前日收盘), 与回测/dual_backtest_365d.py对齐
                j = i - 1
                ret_1d = (closes[j]-closes[i-1])/closes[i-1] if closes[i-1]>0 else 0
                ret_3d = (closes[j]-closes[max(0,i-3)])/closes[max(0,i-3)] if closes[max(0,i-3)]>0 else 0
                ret_5d = (closes[j]-closes[max(0,i-5)])/closes[max(0,i-5)] if closes[max(0,i-5)]>0 else 0
                # 20日波动率归一化 — 让不同波动环境下的收益可比较
                if i >= 20:
                    rets_20 = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1]>0 else 0 for k in range(i-19,i+1)]
                    vol_20d = float(np.std(rets_20))
                else:
                    vol_20d = 0.02
                vol_floor = max(vol_20d, 0.002)  # 日波动下限0.2%
                ret_1d_norm = round(ret_1d / vol_floor, 4)
                ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
                ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
                if i >= 5:
                    daily_rets = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1]>0 else 0 for k in range(i-4,i+1)]
                    volatility = np.std(daily_rets)
                else: volatility = 0
                vol_ratio = vols[j]/np.mean(vols[max(0,i-5):i]) if i>=5 and np.mean(vols[max(0,i-5):i])>0 else 1
                if i >= 20:
                    c20 = closes[i-20:i+1]
                    price_position = (closes[j]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
                else: price_position = 0.5
                amplitude = (highs[j]-lows[j])/opens[j] if opens[j]>0 else 0
                streak = 0
                for k in range(i, max(0,i-7)-1,-1):
                    if closes[k]>opens[j]: streak+=1
                    else: break
                div_sign = 1 if (closes[j]>closes[i-3] and vols[j]<vols[i-3]*0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(timestamps[j], 0); oi_prev = oi_map.get(timestamps[j-1], 0)
                oi_chg = (oi_now-oi_prev)/oi_prev if oi_prev>0 else 0

                # BTC回归特征
                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = _regression_features(btc_rets, coin_rets, j)

                # 板块热度特征 (22维)
                # 板块热度用前一日，避免当日收益率泄露
                ts_prev = ts - 86400
                sector_feats = _get_sector_features(sym, ts_prev, sector_map, sector_heats)

                # 宏观特征
                macro_feats = _get_macro_features(ts)
                macro_feats = _apply_chain_tvl(macro_feats, sym, ts)

                rsi7 = _compute_rsi(closes, 7, j)
                rsi14 = _compute_rsi(closes, 14, j)
                rsi30 = _compute_rsi(closes, 30, j)
                rsi14_series = dp._compute_rsi_series(closes, 14)
                rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, j, window=20)
                vol_col = dp._compute_vol_clustering(closes, j)
                feat = [ret_1d_norm,ret_3d_norm,ret_5d_norm,volatility,vol_ratio,price_position,amplitude,streak,div_sign,oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
                next_ret = (closes[i+1]-closes[j])/closes[j] if closes[j]>0 else 0
                # 过滤异常值 (>500%单日可能是代币迁移/合约替换)
                if abs(next_ret) > 5.0: continue
                label = 1 if next_ret < -0.05 else 0
                Xall.append(feat); yall.append(label)
            except Exception: continue

    X=np.array(Xall); y=np.array(yall)
    pos=sum(y)
    print(f"训练样本: {len(y)} 跌>5%: {pos} ({pos/len(y)*100:.1f}%) 特征维度: {X.shape[1]}")
    if pos < 10: return None

    # winsor截尾 — 算P1/P99并保存，供预测/回测复用
    global _winsor_bounds
    _winsor_bounds = _compute_winsor_bounds(X)
    X = _apply_winsor(X, _winsor_bounds)
    print(f"winsor截尾: 已保存 {len(_winsor_bounds)} 维bounds到 {WINSOR_BOUNDS_FILE}")

    model = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                          scale_pos_weight=(len(y)-pos)/pos if pos>0 else 1,
                          random_state=42, eval_metric='logloss')
    model.fit(X, y)
    with open(MODEL_FILE, 'wb') as f: pickle.dump(model, f)

    # 打印特征重要性
    EMBEDDING_DIM = 832
    feat_names = ['ret_1d_norm','ret_3d_norm','ret_5d_norm','volatility','vol_ratio','price_position','amplitude','streak','div_sign','oi_chg',
                  'vol_regime','vol_momentum','vol_persist',
                  'beta','alpha','r2','residual','rsi7','rsi14','rsi30','rsi_div_top','rsi_div_bottom','rsi_overbought_persist','rsi_price_corr_20d'] + SECTOR_ORDER + ['etf_btc','etf_eth','chain_vol','chain_tx','chain_fee','chain_cdd','sent_funding','sent_ls_btc','sent_ls_eth','sent_ls_avg10','sent_ls_high','sent_ls_low','fear_greed','stablecoin','coinbase_prem','coinbase_gap','btc_mcap','korea_prem','hashrate_7d_chg','liq_total_long','liq_total_short','liq_ratio','liq_long_peak_dist','liq_short_peak_dist','liq_funding','liq_long_ratio','chain_tvl_btc','chain_tvl_eth','chain_tvl_sol','chain_tvl_bsc','chain_tvl_arb','chain_tvl_base'] + [f'kronos_emb_{i}' for i in range(EMBEDDING_DIM)] + ['sp500_1d','dxy_1d','gold_1d','alt_btc_spread']
    importances = model.feature_importances_
    top_idx = np.argsort(importances)[-15:][::-1]
    print("特征重要性 TOP15:")
    for idx in top_idx:
        print(f"  {feat_names[idx]:20s} {importances[idx]:.4f}")

    return model

def predict(klines_all, oi_data, model):
    """预测今天哪些币明天可能跌>5%"""
    X, syms, tss = build_features(klines_all, oi_data)
    if len(X) == 0 or model is None:
        # 无模型则用启发式
        results = []
        for sym, kls in list(klines_all.items())[:100]:
            if len(kls) < 10: continue
            c = [k['c'] if isinstance(k,dict) else float(k[4]) for k in kls]
            ret5 = (c[-1]-c[-5])/c[-5] if len(c)>=5 else 0
            results.append({'symbol': sym, 'prob': round(ret5*100, 1)})
        results.sort(key=lambda x:-x['prob'])
        return results[:30]

    probs = model.predict_proba(X)[:, 1]
    results = []
    for i in range(len(syms)):
        results.append({'symbol': syms[i], 'prob': round(probs[i]*100, 1)})
    # 过滤一级市场/新币（数据<60天 或 成交量<50万U）
    filtered = []
    for r in zip(syms, probs):
        sym, prob = r
        kls = klines_all.get(sym, [])
        if len(kls) < 60:
            continue
        vols = [k['q'] if isinstance(k,dict) else float(k[7]) for k in kls[-5:]]
        avg_vol = np.mean(vols) if vols else 0
        if avg_vol < 500000:
            continue
        filtered.append({'symbol': sym, 'prob': round(float(prob)*100, 1)})
    filtered.sort(key=lambda x:-x['prob'])
    return filtered[:50]

def run():
    klines = fetch_klines()
    if not klines:
        print("无K线数据")
        return

    # 拉合约币种
    try:
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('contractType')=='PERPETUAL']
    except Exception:
        fut_syms = list(klines.keys())

    print(f"拉取OI: {len(fut_syms)}个币种...")
    oi_data = fetch_oi(fut_syms)
    print(f"OI数据: {len(oi_data)}币种")

    # 每天重训 (数据更新后模型需要跟进)
    model = None
    if os.path.exists(MODEL_FILE):
        try:
            with open(MODEL_FILE, 'rb') as f: model = pickle.load(f)
            print("加载已有模型(兜底)")
        except Exception:
            model = None

    print("训练新模型...")
    new_model = train(klines, oi_data)
    if new_model:
        model = new_model
        print("训练完成")
    elif model is None:
        print("训练失败, 无兜底模型")

    # 市场状态: BTC近20日波动率 (<1.0% = 震荡市, 区分慢涨)
    btc_kls = klines.get('BTCUSDT', [])
    btc_closes_p = [k['c'] if isinstance(k, dict) else float(k[4]) for k in btc_kls[-21:]]
    btc_recent_rets = [(btc_closes_p[j]-btc_closes_p[j-1])/btc_closes_p[j-1]*100 for j in range(1, len(btc_closes_p))]
    btc_20d_vol = np.std(btc_recent_rets) if len(btc_recent_rets) > 1 else 0
    market_calm = btc_20d_vol < 1.0  # 20天波动率<1.0% = 震荡市

    # 预测
    results = predict(klines, oi_data, model)
    print(f"预测结果: {len(results)}个候选")

    if market_calm:
        print(f"⚠️ BTC近20日波动率 {btc_20d_vol:.2f}% < 1.0%, 震荡市, 建议空仓")

    # 保存
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pred_data = {
        'predictions': results,
        'date': today,
        'updated': time.time(),
        'model_available': model is not None,
        'feature_dims': model.n_features_in_ if model is not None and hasattr(model, 'n_features_in_') else 0,
        'btc_20d_vol': round(btc_20d_vol, 3),
        'market_calm': market_calm,
    }
    with open(CACHE, 'w') as f:
        json.dump(pred_data, f, default=str)

    # 存档预测到本地（每天只存第一版，后续手动重训不覆盖）
    os.makedirs(LOG_DIR, exist_ok=True)
    daily_file = os.path.join(LOG_DIR, f'pred_{today}_short.json')
    if not os.path.exists(daily_file):
        with open(daily_file, 'w') as f:
            json.dump(pred_data, f, default=str)
        print(f"预测存档: {daily_file}")
    else:
        # 手动重训时存副本
        backup_file = os.path.join(LOG_DIR, f'pred_{today}_{int(time.time())}.json')
        with open(backup_file, 'w') as f:
            json.dump(pred_data, f, default=str)
        print(f"预测存档(手动): {backup_file}")

    # 验证2天前预测（2日模型需2天走完）
    pass  # short mode: no verify

    # 复盘错误
    pass  # short mode: no review

    # 打印TOP20
    for i, r in enumerate(results[:20]):
        print(f"  {i+1:2d}. {r['symbol']:<14s} {r['prob']:5.1f}%")

def verify_yesterday(klines_all=None):
    """验证2天前未验的预测 — 2日模型需等2天才能结算"""
    import datetime as _dt
    pred_files = sorted([f for f in os.listdir(LOG_DIR) if f.startswith('pred_') and f.endswith('.json')])
    if not pred_files:
        print("[验证] 无预测文件")
        return
    # 找2天前的预测（今天14号 → 验12号及以前，需2天走完）
    today = _dt.datetime.now(timezone.utc)
    today_str = today.strftime('%Y-%m-%d')
    cutoff = today - _dt.timedelta(days=2)
    cutoff_str = cutoff.strftime('%Y-%m-%d')

    pending = []
    for fn in pred_files:
        date_part = fn.replace('pred_','').replace('.json','')[:10]
        if date_part <= cutoff_str:
            pending.append((date_part, fn))
    if not pending:
        print(f"[验证] 2天前({cutoff_str}及以前)暂无待验证预测")
        return

    date_part, pred_file_name = pending[-1]  # 最近一条到期预测
    pred_file = os.path.join(LOG_DIR, pred_file_name)

    # 检查是否已验证过
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE) as f:
            tracker = json.load(f)
        if any(t['date'] == date_part for t in tracker):
            print(f"[验证] {date_part} 已验证过，跳过")
            return

    with open(pred_file) as f:
        pred = json.load(f)

    print(f"[验证] {date_part}预测 → 2日后结算, 共{len(pred['predictions'])}个币种")
    # 预测日的时间戳
    pred_ts = int(_dt.datetime.strptime(date_part, '%Y-%m-%d')
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)

    results = []
    for p in pred.get('predictions', [])[:50]:
        sym = p['symbol']
        try:
            resp = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 5}, timeout=10)
            if resp.status_code != 200:
                continue
            kls = resp.json()
            if len(kls) < 3: continue

            # 找预测日那根K线，取2日后的close
            entry_close = None
            exit_close = None
            for j, k in enumerate(kls):
                if int(k[0]) == pred_ts:
                    entry_close = float(k[4])
                    if j + 2 < len(kls):
                        exit_close = float(kls[j+2][4])
                    break

            if entry_close is None or exit_close is None or entry_close <= 0:
                continue

            actual_ret = (exit_close - entry_close) / entry_close * 100
            hit = actual_ret < -5
            results.append({
                'symbol': sym, 'prob': p['prob'],
                'actual_ret': round(actual_ret, 2), 'hit': hit,
            })
        except Exception:
            continue

    if not results: return
    hits = sum(1 for r in results if r['hit'])
    top20_hits = sum(1 for r in results[:20] if r['hit'])
    top10_hits = sum(1 for r in results[:10] if r['hit'])

    all_rets = [r['actual_ret'] for r in results]
    top10_ret = round(sum(r['actual_ret'] for r in results[:10]), 2)
    top20_ret = round(sum(r['actual_ret'] for r in results[:20]), 2)
    total_ret = round(sum(all_rets), 2)

    track = {
        'date': date_part, 'total': len(results),
        'hits': hits, 'hit_rate': round(hits/len(results)*100, 1),
        'top10_hits': top10_hits, 'top20_hits': top20_hits,
        'top10_return': top10_ret, 'top20_return': top20_ret, 'total_return': total_ret,
        'details': results[:30],
    }

    # 追加到跟踪文件
    tracker = []
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE) as f:
            tracker = json.load(f)
    tracker.append(track)
    with open(TRACK_FILE, 'w') as f:
        json.dump(tracker, f, indent=2, default=str)

    print(f"\n===== 2日验证: {date_part} → {date_part}+2 =====")
    print(f"TOP10命中(>5%): {top10_hits}/10  TOP20: {top20_hits}/20  总命中: {hits}/{len(results)} ({track['hit_rate']}%)")
    print(f"TOP10 2日收益: {top10_ret:+.1f}%  TOP20: {top20_ret:+.1f}%  总: {total_ret:+.1f}%")

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
        # 上传跟踪文件
        cos.put_object(Bucket=bucket, Key='klines/predictions/prediction_tracker.json',
                       Body=json.dumps(tracker, indent=2).encode('utf-8'), ContentType='application/json')
        # 上传当日预测
        cos.put_object(Bucket=bucket, Key=f'klines/predictions/pred_{date_part}.json',
                       Body=json.dumps(pred, default=str).encode('utf-8'), ContentType='application/json')
        print("[COS] 验证数据已上传")
    except Exception as e:
        print(f"[COS] 上传失败: {e}")

def review_errors():
    """复盘最近一条已验证的预测，分析TOP10错在哪里"""
    if not os.path.exists(TRACK_FILE):
        return
    with open(TRACK_FILE) as f:
        tracker = json.load(f)
    if not tracker:
        return

    last = tracker[-1]
    if 'details' not in last or len(last['details']) < 10:
        return

    details = last['details']
    date = last['date']

    # 分类: 预测准 vs 预测错
    winners = [d for d in details if d['hit']]           # 跌>5%
    positives = [d for d in details if d['actual_ret'] > 0]  # 涨但不到5%
    flat = [d for d in details if -3 <= d['actual_ret'] <= 0]
    losers = [d for d in details if d['actual_ret'] < -3]

    # TOP10错误: 高概率但没跌>5%
    top10 = details[:10]
    top10_wrong = [d for d in top10 if not d['hit']]

    print(f"\n===== 复盘 {date} =====")
    print(f">5%真正牛币: {len(winners)}个  "
          f"涨但不到5%: {len(positives)}个  "
          f"微亏(-3~0%): {len(flat)}个  "
          f"暴跌(<-3%): {len(losers)}个")
    print(f"TOP10预测命中(>5%): {sum(1 for d in top10 if d['hit'])}/10")

    if top10_wrong:
        print(f"\nTOP10预测错 ({len(top10_wrong)}个):")
        top10_wrong.sort(key=lambda x: -x['prob'])
        for d in top10_wrong[:5]:
            ret_str = f"{d['actual_ret']:+.1f}%"
            tag = '暴跌' if d['actual_ret'] < -5 else ('微亏' if d['actual_ret'] > -3 else '小亏')
            print(f"  {d['symbol']:<16s} prob={d['prob']:.0f}%  实际{ret_str}  [{tag}]")

    # 找隐藏的牛币 (低概率却跌>5%)
    low_prob_winners = [d for d in details[10:] if d['hit']]
    if low_prob_winners:
        print(f"\n隐藏牛币 (TOP10外却跌>5%, {len(low_prob_winners)}个):")
        for d in low_prob_winners[:5]:
            print(f"  {d['symbol']:<16s} prob={d['prob']:.0f}%  实际+{d['actual_ret']:.1f}%")

def backtest(stride=5):
    """滚动回测：逐日训练+预测，模拟真实交易"""
    klines = fetch_klines()
    if not klines:
        print("无K线数据")
        return

    try:
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        fut_syms = [s['symbol'] for s in resp.json()['symbols']
                    if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('contractType')=='PERPETUAL']
    except Exception:
        fut_syms = list(klines.keys())

    print(f"拉取OI: {len(fut_syms)}个币种...")
    oi_data = fetch_oi(fut_syms)

    sector_map = _load_sector_map()
    global _sector_map_cache, _proto_map_local; _sector_map_cache = sector_map
    if not _proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                _proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except Exception: pass
    sector_heats_all = _precompute_sector_heats(klines, sector_map) if sector_map else {}

    global _etf_features, _chain_features, _sent_features, _fg_features, _st_features, _cb_features, _cbg_features, _bd_features, _kg_features, _hr_features, _liq_features, _tvl_features, _ma_features, _ab_features
    _etf_features = _load_etf_features()
    _chain_features = _load_chain_features()
    _sent_features = _load_sent_features()
    _fg_features = _load_fear_greed()
    _st_features = _load_stablecoin_netflow()
    _cb_features = _load_coinbase_premium()
    _cbg_features = _load_cb_gap_features()
    _bd_features = _load_btc_mcap()
    _kg_features = _load_korea_premium()
    _hr_features = _load_hashrate_features()
    _liq_features = _load_liquidation_features()
    _tvl_features = _load_chain_tvl()
    _ma_features = dp._load_macro_assets()
    _ab_features = dp._load_btc_dominance_proxy()

    # Kronos必须在样本构建前预计算 (CRITICAL-7-003)
    all_ts_k = set()
    for kls in klines.values():
        if len(kls) < 30: continue
        for k in kls:
            all_ts_k.add(k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000)
    _precompute_kronos_features(list(all_ts_k))

    btc_kls = klines.get('BTCUSDT', [])
    btc_closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in btc_kls]
    btc_rets = _compute_returns(btc_closes) if len(btc_closes) > 1 else []

    # 预构建所有样本
    all_samples = []  # [(ts, sym, feats, label, ret), ...]
    for sym, kls in klines.items():
        if len(kls) < 30:
            continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
        opens = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
        highs = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
        lows = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
        vols = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t', 0)//1000 if isinstance(k, dict) else int(k[0])//1000 for k in kls]
        coin_rets = _compute_returns(closes)
        n = len(kls)

        for i in range(25, n-2):
            try:
                # 特征统一基于 j=i-1 (前日收盘), 与回测/dual_backtest_365d.py对齐
                j = i - 1
                ret_1d = (closes[j]-closes[i-1])/closes[i-1] if closes[i-1] > 0 else 0
                ret_3d = (closes[j]-closes[max(0,i-3)])/closes[max(0,i-3)] if closes[max(0,i-3)] > 0 else 0
                ret_5d = (closes[j]-closes[max(0,i-5)])/closes[max(0,i-5)] if closes[max(0,i-5)] > 0 else 0
                if i >= 20:
                    rets_20 = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1]>0 else 0 for k in range(i-19,i+1)]
                    vol_20d = float(np.std(rets_20))
                else:
                    vol_20d = 0.02
                vol_floor = max(vol_20d, 0.002)
                ret_1d_norm = round(ret_1d / vol_floor, 4)
                ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
                ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
                if i >= 5:
                    daily_rets = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1] > 0 else 0 for k in range(i-4,i+1)]
                    volatility = np.std(daily_rets) if len(daily_rets) > 1 else 0
                else: volatility = 0
                vol_ratio = vols[j]/np.mean(vols[max(0,i-5):i]) if i >= 5 and np.mean(vols[max(0,i-5):i]) > 0 else 1
                if i >= 20:
                    c20 = closes[i-20:i+1]
                    price_position = (closes[j]-min(c20))/(max(c20)-min(c20)) if max(c20) != min(c20) else 0.5
                else: price_position = 0.5
                amplitude = (highs[j]-lows[j])/opens[j] if opens[j] > 0 else 0
                streak = 0
                for k in range(i, max(0, i-7)-1, -1):
                    if closes[j] > opens[j]: streak += 1
                    else: break
                div_sign = 1 if (closes[j] > closes[i-3] and vols[j] < vols[i-3]*0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(timestamps[j], 0); oi_prev = oi_map.get(timestamps[j-1], 0)
                oi_chg = (oi_now-oi_prev)/oi_prev if oi_prev > 0 else 0

                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = _regression_features(btc_rets, coin_rets, j)

                # 板块热度用前一日，避免当日收益率泄露
                ts_prev = ts - 86400
                sector_feats = _get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
                macro_feats = _get_macro_features(ts)
                macro_feats = _apply_chain_tvl(macro_feats, sym, ts)
                rsi7 = _compute_rsi(closes, 7, j)
                rsi14 = _compute_rsi(closes, 14, j)
                rsi30 = _compute_rsi(closes, 30, j)
                rsi14_series = dp._compute_rsi_series(closes, 14)
                rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, j, window=20)
                vol_col = dp._compute_vol_clustering(closes, j)

                feat = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
                next_ret = (closes[i+1]-closes[j])/closes[j] if closes[j] > 0 and i+1 < n else 0
                if abs(next_ret) > 5.0: continue  # 过滤异常值
                label = 1 if next_ret < -0.05 else 0
                all_samples.append((ts, sym, feat, label, next_ret*100))
            except Exception: continue

    # 按timestamp分组
    from collections import defaultdict
    by_day = defaultdict(list)
    for ts, sym, feat, label, ret in all_samples:
        by_day[ts].append((sym, feat, label, ret))

    sorted_days = sorted(by_day.keys())
    print(f"回测: {len(sorted_days)}个交易日, {len(all_samples)}样本")

    # 预计算所有交易日的 Kronos 特征
    _precompute_kronos_features(sorted_days)

    # 滚动回测: 每stride天测一次加速
    START_DAY = max(10, len(sorted_days) // 3)

    daily_results = []
    for d in range(START_DAY, len(sorted_days)-1, stride):
        train_ts = sorted_days[max(0, d-500):d]
        pred_ts = sorted_days[d]

        # 构建训练集
        X_train, y_train = [], []
        for ts in train_ts:
            if ts + 2 * 86400 > pred_ts: continue
            for sym, feat, label, _ in by_day[ts]:
                X_train.append(feat)
                y_train.append(label)

        X_train = np.array(X_train)
        y_train = np.array(y_train)
        # winsor截尾：每个窗口独立计算，不用全局bounds
        bounds = []
        for j in range(X_train.shape[1]):
            col = X_train[:, j]
            bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
        X_train = _apply_winsor(X_train, bounds)
        pos = sum(y_train)
        if pos < 5:
            continue

        # 训练
        model = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                              scale_pos_weight=(len(y_train)-pos)/pos,
                              random_state=42, eval_metric='logloss', verbosity=0)
        model.fit(X_train, y_train)

        # 预测
        pred_samples = by_day[pred_ts]
        X_pred = np.array([s[1] for s in pred_samples])
        X_pred = _apply_winsor(X_pred, bounds)
        probs = model.predict_proba(X_pred)[:, 1]

        # 取TOP10/20
        ranked = sorted(zip(pred_samples, probs), key=lambda x: -x[1])
        # 过滤：使用pred_ts当时的历史成交量，不用全局最新数据
        filtered = []
        for (sym, feat, label, ret), prob in ranked:
            kls_data = klines.get(sym, [])
            if len(kls_data) < 60:
                continue
            idx = _find_kline_index(kls_data, pred_ts)
            if idx is None or idx < 5:
                continue
            v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls_data[idx-5:idx]]
            if np.mean(v) < 500000:
                continue
            filtered.append((sym, prob, ret))

        # filtered里的ret就是实际2日收益（构建样本时存的next_ret*100）
        # 扣除0.2%交易成本（0.1%手续费+0.1%滑点）
        top10_ret = sum(r[2] - 0.2 for r in filtered[:10])
        top20_ret = sum(r[2] - 0.2 for r in filtered[:20])

        day_str = datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%m-%d')
        daily_results.append({
            'day': day_str, 'ts': pred_ts,
            'top10_ret': round(top10_ret, 1),
            'top20_ret': round(top20_ret, 1),
        })
        print(f"  {day_str}: TOP10 {top10_ret:+.1f}%  TOP20 {top20_ret:+.1f}%  (训练{d}天, {len(X_train)}样本)")

    # 汇总
    print(f"\n===== 回测汇总 ({len(daily_results)}天) =====")
    cum10, cum20 = 0, 0
    win10, win20 = 0, 0
    for r in daily_results:
        cum10 += r['top10_ret']
        cum20 += r['top20_ret']
        if r['top10_ret'] > 0: win10 += 1
        if r['top20_ret'] > 0: win20 += 1
        print(f"  {r['day']}: TOP10 {r['top10_ret']:+.1f}%  TOP20 {r['top20_ret']:+.1f}%  | 累计TOP10 {cum10:+.1f}% TOP20 {cum20:+.1f}%")

    n = len(daily_results)
    print(f"\nTOP10: 胜率{win10}/{n} ({win10/n*100:.0f}%)  累计收益{cum10:+.1f}%  日均{cum10/n:+.2f}%")
    print(f"TOP20: 胜率{win20}/{n} ({win20/n*100:.0f}%)  累计收益{cum20:+.1f}%  日均{cum20/n:+.2f}%")
    return daily_results

if __name__ == '__main__':
    import sys
    if '--backtest' in sys.argv:
        backtest()
    else:
        run()
