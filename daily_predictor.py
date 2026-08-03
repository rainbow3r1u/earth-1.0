#!/usr/bin/env python3
"""2日涨跌预测器 — 用K线+OI特征选出2天后最可能涨>5%的币种"""
import json, logging, requests, numpy as np, os, time, pickle
from datetime import datetime, timezone
from xgboost import XGBClassifier

# Kronos 特征提取 (Deep B 方案)
from kronos_features import extract_kronos_features
from utils.feature_builder import assemble_feature_vec

# Kronos开关: False=禁用(特征置零, 跳过预计算, 与回溯端KRONOS_MODE=off对齐)
USE_KRONOS = False

KRONOS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/kronos_features_cache.json")
KRONOS_EMBEDDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/kronos_embeddings.json")
_kronos_embedding_data = None
EMBEDDING_DIM = 832

# ===== Kronos Factor Engine 常量 =====
KRONOS_N_CHUNKS = 16
KRONOS_CHUNK_SIZE = 52       # 16 * 52 = 832
KRONOS_TOP_K = 10            # 选 top 10 chunk → 10 * 4 = 40 维因子
KRONOS_MIN_IC = 0.02
KRONOS_ROLLING_IC_WINDOW = 60
KRONOS_FACTOR_DIM = KRONOS_TOP_K * 4  # 40 维
KRONOS_ENGINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/kronos_factor_engine.pkl")
_kronos_engine = None


class KronosFactorEngine:
    """把 832 维 Kronos embedding 转换为可交易的低维因子向量。

    修正点（相对 v1）：
      1. 每个 chunk 提取 4 个 micro-features: mean, std, last, momentum
      2. IC 计算改成 rolling IC（窗口 60 天），不是单点 corr
      3. stability = mean(|rolling_ic|) / std(rolling_ic)
      4. 在固定 discovery 训练集上 fit 一次，冻结 selected_indices，回测期只 transform
    """

    MICRO_FEATURE_NAMES = ['mean', 'std', 'last', 'momentum']
    N_MICRO = len(MICRO_FEATURE_NAMES)

    def __init__(self, n_chunks=KRONOS_N_CHUNKS, top_k=KRONOS_TOP_K,
                 min_ic=KRONOS_MIN_IC, rolling_window=KRONOS_ROLLING_IC_WINDOW):
        self.n_chunks = n_chunks
        self.top_k = min(top_k, n_chunks)
        self.min_ic = min_ic
        self.rolling_window = rolling_window
        self.selected_indices_ = None
        self.selected_micro_ = None
        self.scores_ = None

    @staticmethod
    def _extract_micro_features(chunk):
        """从单个 chunk (N, 52) 提取 4 个 micro-features: (N, 4)"""
        N, C = chunk.shape
        mean_f = chunk.mean(axis=1)
        std_f = chunk.std(axis=1)
        last_f = chunk[:, -1]
        diff = np.diff(chunk, axis=1)
        momentum_f = diff.mean(axis=1)
        return np.stack([mean_f, std_f, last_f, momentum_f], axis=1)

    @staticmethod
    def _rolling_ic(feature, returns, window):
        """计算 rolling window 的 IC 序列。"""
        n = len(feature)
        if n < window + 2:
            return np.array([])
        ics = []
        for end in range(window, n + 1):
            start = end - window
            sub_f = feature[start:end]
            sub_r = returns[start:end]
            if np.std(sub_f) > 1e-12 and np.std(sub_r) > 1e-12:
                ics.append(np.corrcoef(sub_f, sub_r)[0, 1])
        return np.array(ics)

    def fit(self, kronos_832, returns):
        """在 discovery 训练集上 fit 一次，冻结选中 chunk。"""
        N, D = kronos_832.shape
        if D != self.n_chunks * KRONOS_CHUNK_SIZE:
            raise ValueError(f"Kronos dim {D} not match {self.n_chunks}*{KRONOS_CHUNK_SIZE}")

        micro_features = np.zeros((N, self.n_chunks, self.N_MICRO))
        for i in range(self.n_chunks):
            chunk = kronos_832[:, i * KRONOS_CHUNK_SIZE:(i + 1) * KRONOS_CHUNK_SIZE]
            micro_features[:, i, :] = self._extract_micro_features(chunk)

        chunk_scores = np.zeros(self.n_chunks)
        chunk_best_micro = np.zeros(self.n_chunks, dtype=int)

        for i in range(self.n_chunks):
            best_score = -1.0
            best_micro = 0
            for m in range(self.N_MICRO):
                feat = micro_features[:, i, m]
                rolling_ics = self._rolling_ic(feat, returns, self.rolling_window)
                if len(rolling_ics) < 3:
                    continue
                ic_mean = np.mean(rolling_ics)
                ic_abs_mean = np.mean(np.abs(rolling_ics))
                ic_std = np.std(rolling_ics) + 1e-6
                stability = ic_abs_mean / ic_std
                score = abs(ic_mean) * stability
                if abs(ic_mean) < self.min_ic:
                    score = 0.0
                if score > best_score:
                    best_score = score
                    best_micro = m
            chunk_scores[i] = best_score
            chunk_best_micro[i] = best_micro

        self.scores_ = chunk_scores
        top_k = min(self.top_k, self.n_chunks)
        self.selected_indices_ = np.argsort(chunk_scores)[-top_k:]
        self.selected_micro_ = chunk_best_micro[self.selected_indices_]
        return self

    def transform(self, kronos_832):
        """用冻结的 selected_indices_ 把 832 维 Kronos 转成 (N, top_k * 4) 因子矩阵。"""
        if self.selected_indices_ is None:
            raise RuntimeError("Engine not fitted yet")
        N, D = kronos_832.shape
        if D != self.n_chunks * KRONOS_CHUNK_SIZE:
            raise ValueError(f"Kronos dim {D} not match expected")

        out = []
        for idx in self.selected_indices_:
            chunk = kronos_832[:, idx * KRONOS_CHUNK_SIZE:(idx + 1) * KRONOS_CHUNK_SIZE]
            micro = self._extract_micro_features(chunk)
            out.append(micro)
        return np.concatenate(out, axis=1)

    def summary(self):
        if self.selected_indices_ is None:
            return "Engine not fitted"
        lines = ["KronosFactorEngine summary (frozen on discovery set):"]
        for i, idx in enumerate(self.selected_indices_):
            micro_name = self.MICRO_FEATURE_NAMES[self.selected_micro_[i]]
            lines.append(f"  chunk={idx:2d}  best_micro={micro_name:8s}  score={self.scores_[idx]:+.4f}")
        return "\n".join(lines)

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/daily_predictions.json")
MODEL_DIR = os.path.join(os.path.expanduser('~/.local/share/auto_trade'), 'models')
os.makedirs(MODEL_DIR, mode=0o700, exist_ok=True)
MODEL_FILE = os.path.join(MODEL_DIR, 'xgb_daily_model.pkl')
LOG_DIR = "/home/myuser/blockchair_data/predictions"
TRACK_FILE = os.path.join(LOG_DIR, "prediction_tracker.json")
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
    """ETF净流入: {date_str: [btc_flow_m, eth_flow_m]}

    DISABLED 2026-07-12: farside.co.uk只返回最近14天数据, 且57.6%为假0.0值
    (farside对未更新日期显示0而非'-', 连续11个工作日0.0不可能为真实净流入).
    数据质量极差(33天/14天真实), 加入后Sharpe从7.17降至6.03, 净负面影响.
    修复 fetch_etf.py 采集稳定性后可重新启用.
    """
    return {}

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
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_sent_features():
    """情绪日聚合: {date_str: [funding_top5_avg, ls_btc, ls_eth, ls_avg10, ls_high, ls_low]}"""
    import glob
    from collections import defaultdict
    daily = defaultdict(list)
    failed_files = 0
    for fn in sorted(glob.glob('/home/myuser/sentiment_data/sentiment_*.json')):
        try:
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
        except (json.JSONDecodeError, KeyError):
            failed_files += 1
            continue
    if failed_files > 0:
        logging.getLogger(__name__).warning(f"Sentiment: {failed_files} 个文件损坏，已跳过")
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

def _load_fear_greed():
    """恐慌贪婪: {date_str: [fear_greed_normalized]}"""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'data/fear_greed_history.json')) as f:
            return {d['date']: [d['value'] / 100.0] for d in json.load(f)}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_stablecoin_netflow():
    """稳定币净流入: {date_str: [netflow_100M]}"""
    try:
        with open('/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value'] / 1e8] for d in data if d.get('value') is not None}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_coinbase_premium():
    """Coinbase溢价: {date_str: [premium_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_coinbase_premium_index.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_cb_gap_features():
    """Coinbase Premium Gap: {date_str: [gap_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_coinbase_premium_gap.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_korea_premium():
    """韩国溢价指数: {date_str: [premium_pct]}"""
    try:
        with open('/home/myuser/stablecoin_data/btc_korea_premium_index.json') as f:
            data = json.load(f).get('data', [])
        return {datetime.fromtimestamp(d['timestamp']/1000, tz=timezone.utc).strftime('%Y-%m-%d'):
                [d['value']] for d in data if d.get('value') is not None}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

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
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_btc_mcap():
    """BTC市值7日变化率: {date_str: [btc_mcap_7d_chg]}"""
    try:
        with open('/home/myuser/coingecko_data/btc_mcap.json') as f:
            return {d['date']: [d['btc_mcap_7d_chg']] for d in json.load(f)}
    except Exception as exc: logging.getLogger(__name__).warning(f"Failed to load: {exc}"); return {}

def _load_chain_tvl():
    """链TVL 7日变化率: {date_str: [btc,eth,sol,bsc,arb,base,ton,sui,polygon]}"""
    chains = ['btc_chain','ethereum','solana','binance','arbitrum','base','ton','sui','polygon']
    result = {}
    for i, name in enumerate(chains):
        path = f'/home/myuser/defillama_data/{name}_tvl.json'
        try:
            with open(path) as f:
                for d in json.load(f):
                    result.setdefault(d['date'], [0]*len(chains))[i] = d['tvl_7d_chg']
        except FileNotFoundError:
            logging.getLogger(__name__).debug(f"TVL file not found: {path}")
        except (json.JSONDecodeError, KeyError) as e:
            logging.getLogger(__name__).warning(f"TVL file corrupt: {path}: {e}")
    return result

def _extract_level_features(levels):
    """从100层清算分布提取13维基础特征"""
    lvls = sorted(levels, key=lambda x: x['price'])
    n = len(lvls)
    if n < 5:
        return None
    total_l = sum(l['long_liq_usd'] for l in lvls)
    total_s = sum(l['short_liq_usd'] for l in lvls)
    if total_l + total_s == 0:
        return None
    max_l = max(lvls, key=lambda x: x['long_liq_usd'])
    max_s = max(lvls, key=lambda x: x['short_liq_usd'])
    peak_l_pos = lvls.index(max_l) / max(n - 1, 1)
    peak_s_pos = lvls.index(max_s) / max(n - 1, 1)
    ratio_ls = total_l / total_s if total_s > 0 else 1.0
    feats = []
    for q in range(5):
        start = q * n // 5
        end = (q + 1) * n // 5
        chunk = lvls[start:end]
        if not chunk:
            feats += [0.0, 0.0]
            continue
        cl = sum(l['long_liq_usd'] for l in chunk)
        cs = sum(l['short_liq_usd'] for l in chunk)
        feats.append(round(cl / total_l, 6) if total_l > 0 else 0.0)
        feats.append(round(cs / total_s, 6) if total_s > 0 else 0.0)
    feats += [round(ratio_ls, 4), round(peak_l_pos, 4), round(peak_s_pos, 4)]
    return feats


def _load_liquidation_features():
    """清算日级特征: 聚合每小时快照 → 每特征mean+std → 26维
    边界防御: 损坏日期自动跳过，快照数<2的日期降低权重(std=0)，过期日期自动忽略"""
    import numpy as np
    result = {}
    levels_file = '/home/myuser/websocket_new/data/liq_levels_daily.json'
    try:
        if os.path.exists(levels_file):
            with open(levels_file) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logging.getLogger(__name__).warning("liq_levels_daily.json 非dict格式, 跳过")
                raw = {}
            # 只取最近31天的日期 (训练窗口内)
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).strftime('%Y-%m-%d')
            for date_str, snapshots in raw.items():
                if date_str < cutoff:
                    continue
                if not isinstance(snapshots, list) or len(snapshots) == 0:
                    continue
                # 过滤损坏快照
                valid_snaps = []
                for snap in snapshots:
                    if not isinstance(snap, dict):
                        continue
                    lvls = snap.get('levels')
                    if not isinstance(lvls, list) or len(lvls) < 5:
                        continue
                    feats = _extract_level_features(lvls)
                    if feats and len(feats) == 13:
                        # 验证特征值合理 (0.0~1.0占比, >0的ratio)
                        if all(-0.01 <= f <= 1.01 for f in feats[:10]) and feats[10] > 0:
                            valid_snaps.append(feats)
                if len(valid_snaps) < 1:
                    continue
                # 少于2个快照时std无意义→填0
                if len(valid_snaps) == 1:
                    feats = valid_snaps[0]
                    merged = []
                    for f in feats:
                        merged.append(round(float(f), 6))
                        merged.append(0.0)
                else:
                    arr = np.array(valid_snaps, dtype=np.float64)
                    mean = np.mean(arr, axis=0)
                    std = np.std(arr, axis=0)
                    std = np.nan_to_num(std, nan=0.0)
                    merged = []
                    for m, s in zip(mean, std):
                        merged.append(round(float(m), 6))
                        merged.append(round(float(s), 6))
                if len(merged) == 26:
                    result[date_str] = merged
    except (json.JSONDecodeError, Exception) as exc:
        logging.getLogger(__name__).warning(f"Liquidation levels load failed: {exc}")
    # 回退: 对levels文件覆盖不到的日期，用旧liq_daily.json填充基础特征
    try:
        with open('/home/myuser/websocket_new/data/liq_daily.json') as f:
            old = json.load(f)
        for d in old:
            if d['date'] not in result:
                result[d['date']] = [
                    d.get('total_long_liq', 0) / 50, 0.0,
                    d.get('total_short_liq', 0) / 50, 0.0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    d.get('liq_ratio', 1.0) if d.get('total_long_liq', 0) > 0 else 1.0, 0.0,
                    0.5, 0.0, 0.5, 0.0,
                ]
    except Exception:
        pass
    return result

def _load_macro_assets():
    """SP500/DXY/黄金: {date_str: [sp500_7d, dxy_7d, gold_7d]}"""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'data/macro_assets.json')) as f:
            data = json.load(f).get('data', {})
        result = {}
        all_dates = set()
        for asset in ['SP500', 'DXY', 'GOLD']:
            all_dates.update(data.get(asset, {}).keys())
        for date_str in sorted(all_dates):
            sp = data.get('SP500', {}).get(date_str, {})
            dxy = data.get('DXY', {}).get(date_str, {})
            gold = data.get('GOLD', {}).get(date_str, {})
            sp_ret = sp.get('ret_1d', 0) or 0
            dxy_ret = dxy.get('ret_1d', 0) or 0
            gold_ret = gold.get('ret_1d', 0) or 0
            result[date_str] = [sp_ret, dxy_ret, gold_ret]
        return result
    except Exception:
        return {}

def _load_btc_dominance_proxy():
    """山寨vsBTC溢价: {ts_int: [alt_btc_spread]}

    从 coingecko 收集器读取 BTC dominance/mcap 数据,
    转换为 alt-BTC spread proxy (以BTC市值的倒数作为山寨活跃度代理)
    """
    try:
        # 优先读收集器输出 (coingecko_data/btc_dominance.json)
        with open('/home/myuser/coingecko_data/btc_dominance.json') as f:
            data = json.load(f)
        result = {}
        for date_str, v in data.items():
            ts = int(datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
            dom = v.get('btc_dominance', 0)
            # btc_dominance 是百分比 (如56.27→56.27%), 转为小数
            result[ts] = [round(float(dom) / 100, 6) if dom else 0]
        return result
    except Exception:
        pass
    # 回退: 旧格式 /tmp/btc_dominance_proxy.json
    try:
        with open(os.path.join(os.path.dirname(__file__), 'data/btc_dominance_proxy.json')) as f:
            raw = json.load(f)
        return {int(k): [v] for k, v in raw.items()}
    except Exception:
        return {}

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
    kg = _kg_features.get(prev_date, [0.0])
    hr = _hr_features.get(prev_date, [0])
    liq = _liq_features.get(prev_date, [0.0]*26)
    tvl = _tvl_features.get(prev_date, [0]*9)  # 9链TVL
    kr = _kr_features.get(ts, [0.0]*EMBEDDING_DIM)
    # 跨资产宏观
    ma = _ma_features.get(prev_date, [0.0]*3)  # SP500/DXY/黄金
    # 山寨vsBTC溢价 (BTC市占率代理)
    ab = _ab_features.get(int(ts), [0.0])
    return etf + chain + sent + fg + st + cb + cbg + bd + kg + hr + liq + tvl + list(kr) + ma + ab

# 链TVL→币归属映射
CHAIN_TVL_MAP = {'BTC生态': 0, 'ETH生态': 1, 'Solana': 2, 'BSC': 3, 'ARB': 4, 'Base生态': 5, 'TON生态': 6, 'L1': 7, 'L2': 8}

TVL_FEATURE_COUNT = 9

def _apply_chain_tvl(macro_feats, sym, ts=None):
    """根据币的链归属清零无关链TVL + 填充协议TVL"""
    macro_feats = macro_feats.copy()
    coin_tags = _sector_map_cache.get(sym, [])
    # macro_feats = etf(2)+chain(4)+sent(6)+fg(1)+st(1)+cb(1)+cbg(1)+bd(1)+kg(1)+hr(1)+liq(26) + tvl(9) + kr(EMBEDDING_DIM) + ma(3) + ab(1)
    tvl_start = len(macro_feats) - TVL_FEATURE_COUNT - EMBEDDING_DIM - 3 - 1
    # 运行时验证：tvl_start 应在合理范围 (25-30)
    if tvl_start < 20 or tvl_start + TVL_FEATURE_COUNT > len(macro_feats):
        import logging
        logging.warning(f'_apply_chain_tvl: 偏移异常 tvl_start={tvl_start} len={len(macro_feats)} EMBEDDING_DIM={EMBEDDING_DIM}')
        return macro_feats

    # 链TVL: 若币种无对应链标签, 清零无关链的TVL
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
# _proto_tvl_data removed (dead code, never read)

_sector_map_cache = {}


def _precompute_kronos_features(timestamps):
    """批量计算 Kronos 特征 — 优先用预计算embedding, 回退到推理"""
    global _kr_features, EMBEDDING_DIM
    if not USE_KRONOS:
        # Kronos禁用: 跳过预计算, 特征位置将在训练/预测时置零
        return
    if not timestamps:
        return
    # 去重并排序
    unique_ts = sorted(set(int(t) for t in timestamps))

    # 1. 先试 embedding 文件 (Kronos-base 832维→PCA 20维, GPU预计算)
    emb_data = _load_kronos_embeddings()
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
    # remaining_ts for Kronos inference fallback below
    if not unique_ts:
        return
    
    # 加载磁盘缓存
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
                # 每10个保存一次，防止中断丢失
                try:
                    tmp = KRONOS_CACHE_FILE + '.tmp'
                    with open(tmp, 'w') as f:
                        json.dump(disk_cache, f)
                    os.rename(tmp, KRONOS_CACHE_FILE)
                except Exception:
                    pass
        # 最终保存
        try:
            tmp = KRONOS_CACHE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(disk_cache, f)
            os.rename(tmp, KRONOS_CACHE_FILE)
        except Exception:
            pass
    
    # 回填全局缓存 — 统一20D PCA embedding (CRITICAL-GPU-001 FIXED)
    for ts in unique_ts:
        feats = disk_cache.get(str(ts), [0.0]*EMBEDDING_DIM)
        _kr_features[ts] = feats[:EMBEDDING_DIM]

def _load_kronos_embeddings():
    """加载预计算的Kronos-base context embedding (128维)"""
    global _kronos_embedding_data, EMBEDDING_DIM
    if _kronos_embedding_data is not None:
        return _kronos_embedding_data
    try:
        with open(KRONOS_EMBEDDING_FILE, 'r') as f:
            _kronos_embedding_data = json.load(f)
        file_dim = _kronos_embedding_data.get('n_dim', 0)
        # FIX: 忽略旧维度的embedding文件
        if file_dim != EMBEDDING_DIM:
            print(f"[Kronos Embedding] 跳过旧维度文件: {file_dim}D != 当前{EMBEDDING_DIM}D, 回退到Kronos推理")
            _kronos_embedding_data = None
            return None
        print(f"[Kronos Embedding] 加载: {_kronos_embedding_data['total_timestamps']}点 x {EMBEDDING_DIM}维")
        return _kronos_embedding_data
    except Exception as e:
        print(f"[Kronos Embedding] 加载失败: {e}, 回退到Kronos推理")
        return None

# winsor截尾bounds — 训练时算好存下来，预测/回测复用
WINSOR_BOUNDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/winsor_bounds.json")
_winsor_bounds = None
_winsor_bounds_backtest = None  # 回测内复用，避免逐窗口漂移

def _fast_winsor_bounds(X):
    """np.partition (QuickSelect O(n)) 替代 np.percentile (全排序 O(n log n)) — 15-20x 加速
    FIX 2026-08-03 (幽灵bug根因): 原实现 col.partition() 在 X 的列视图上原地分区,
    每列独立重排 → 破坏行的完整性(同行的特征来自不同样本), 训练标签与特征错位!
    修复: 在拷贝上分区, 不再修改 X。
    FIX: 稀疏列(>99%为零)的1%/99%分位都在零值区间, 导致[0,0]截尾抹掉真实信号
    → 回退到列min/max作为截尾边界 (partition不保证极值在端点, 需用min/max)
    CHANGE 2026-07-30 (地球版1.4候选): 截尾分位 1%/99% → 0.1%/99.9%
    原因: GPU实验证实 1%/99% 截尾拍平动量特征右尾(追涨/衰竭信号本体),
    LONG-only Sharpe 41.55→5.38 全部来自该压制; 全系统180d 9.82→35.57,
    换时段(off180 32.82/off360 26.06)与0.1%档(32.30)均复核成立。
    0.1%档保留对1e6级脏数据的防护(7/18冻结bug教训), 不全关。"""
    n, m = X.shape
    k1 = max(0, int(n * 0.001))
    k99 = min(n - 1, int(n * 0.999))
    bounds = []
    for j in range(m):
        col = X[:, j].copy()  # FIX 2026-08-03: 拷贝后再分区, 不修改 X
        col.partition([k1, k99])
        lo = float(col[k1])
        hi = float(col[k99])
        if lo == 0.0 and hi == 0.0:
            # 稀疏列: 分位边界为零, 检查是否存在非零信号
            col_min = float(col.min())
            col_max = float(col.max())
            if col_min < 0.0 or col_max > 0.0:
                lo, hi = col_min, col_max
        bounds.append((lo, hi))
    return bounds

def _compute_winsor_bounds(X):
    bounds = _fast_winsor_bounds(X)
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
# _proto_tvl_data removed (dead code)
_kr_features = {}
_ma_features = {}
_ab_features = {}


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

    # 1. 更新已有币种的最新K线（关键修复）
    updated = 0
    existing_syms = [s for s in fut_syms if s in klines]
    print(f"更新{len(existing_syms)}个已有币种的最新K线...")
    def _fetch_latest(sym):
        try:
            r = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 10}, timeout=10)
            if r.status_code == 200:
                return sym, [{'t':int(k[0]),'o':float(k[1]),'h':float(k[2]),'l':float(k[3]),'c':float(k[4]),'v':float(k[5]),'q':float(k[7])} for k in r.json()]
        except Exception: pass
        return sym, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_latest, s): s for s in existing_syms}
        for f in concurrent.futures.as_completed(futures):
            s, new_kls = f.result()
            if not new_kls: continue
            old_kls = klines[s]
            last_old_ts = old_kls[-1].get('t', 0) if isinstance(old_kls[-1], dict) else old_kls[-1][0]
            appended = 0
            for k in new_kls:
                if k['t'] > last_old_ts:
                    old_kls.append(k)
                    appended += 1
            if appended > 0:
                updated += 1
    if updated > 0:
        try:
            with open(cache_file) as f:
                cache_data = json.load(f)
            cache_data['klines'] = klines
            tmp_path = cache_file + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(cache_data, f)
            os.rename(tmp_path, cache_file)
            print(f"  {updated}个币种已更新最新K线")
        except Exception: pass

    # 2. 补缓存中没有的币
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
        if added > 0:
            try:
                with open(cache_file) as f:
                    cache_data = json.load(f)
                cache_data['klines'] = klines
                tmp_path = cache_file + '.tmp'
                with open(tmp_path, 'w') as f:
                    json.dump(cache_data, f)
                os.rename(tmp_path, cache_file)
                print(f"  {added}新币已写入缓存")
            except Exception: pass

    print(f"K线: {len(klines)}币种 (缓存{len(klines)-added}+补拉{added})")
    return klines

def fetch_oi(syms, limit=500):
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
    
    # FIX: 保存API获取的最新OI到本地缓存, 避免下次仍需API补全
    try:
        merged_cache = dict(local_cache)
        updated_count = 0
        for sym, d in oi_data.items():
            if d:
                if sym not in merged_cache:
                    merged_cache[sym] = {}
                for ts, val in d.items():
                    ts_str = str(ts)
                    if merged_cache[sym].get(ts_str) != val:
                        merged_cache[sym][ts_str] = val
                        updated_count += 1
        if updated_count > 0:
            tmp_path = cache_path + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(merged_cache, f)
            os.rename(tmp_path, cache_path)
            print(f"OI缓存已保存: {updated_count}条更新 → {os.path.basename(cache_path)}")
    except Exception as e:
        print(f"OI缓存保存失败: {e}")
    
    print(f"OI: 缓存{len(oi_data)-len(need_api)}币种 + API补{len(need_api)}币种")
    return oi_data

def _compute_returns(closes):
    """从收盘价序列计算日收益率"""
    return [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0 for j in range(1, len(closes))]

def _compute_rsi(closes, period=14, idx=None):
    """计算RSI (Wilder平滑版, 与币安/TradingView一致)"""
    if idx is None or idx < period + 1:
        return 50.0

    # 初始简单平均 (index 1 到 period)
    gains, losses = 0, 0
    for j in range(1, period + 1):
        diff = closes[j] - closes[j-1]
        if diff > 0: gains += diff
        else: losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period

    # Wilder递归平滑 (从 period+1 到 idx, 与 _compute_rsi_series 一致)
    for j in range(period + 1, idx + 1):
        diff = closes[j] - closes[j-1]
        g = diff if diff > 0 else 0
        l = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _compute_rsi_series(closes, period=14):
    """预计算整个RSI序列，返回与closes等长的列表"""
    n = len(closes)
    rsi = [50.0] * n
    if n < period + 1:
        return rsi

    # 初始简单平均
    gains, losses = 0, 0
    for j in range(1, period + 1):
        diff = closes[j] - closes[j-1]
        if diff > 0: gains += diff
        else: losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = round(100.0 - 100.0 / (1.0 + rs), 2)

    # Wilder递归平滑
    for i in range(period + 1, n):
        diff = closes[i] - closes[i-1]
        g = diff if diff > 0 else 0
        l = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = round(100.0 - 100.0 / (1.0 + rs), 2)
    return rsi


def _compute_rsi_divergence(closes, rsi14_series, idx, window=20):
    """
    计算RSI背离特征，返回4维:
    [rsi_div_top, rsi_div_bottom, rsi_overbought_persist, rsi_price_corr_20d]

    顶背离: 价格创新高但RSI没跟 → 上涨乏力，看跌
    底背离: 价格创新低但RSI没跟 → 下跌衰竭，看涨
    """
    if idx < window + 14:
        return [0.0, 0.0, 0.0, 0.0]

    start = max(0, idx - window + 1)
    price_window = closes[start:idx + 1]
    rsi_window = rsi14_series[start:idx + 1]
    n_win = len(price_window)
    if n_win < 5:
        return [0.0, 0.0, 0.0, 0.0]

    # 1. 顶背离强度: 窗口内价格P80与P20的差距 vs RSI的相对位置
    # 强度 = (价格高位程度) * (RSI不确认程度)
    price_p80 = np.percentile(price_window, 80)
    price_p20 = np.percentile(price_window, 20)
    rsi_p80 = np.percentile(rsi_window, 80)
    rsi_p20 = np.percentile(rsi_window, 20)

    price_range = price_p80 - price_p20
    rsi_range = rsi_p80 - rsi_p20

    if price_range > 0 and rsi_range > 0:
        # 价格位置 (0-1, 越高越在顶部)
        price_pos = (closes[idx] - price_p20) / price_range
        # RSI位置 (0-1)
        rsi_pos = (rsi14_series[idx] - rsi_p20) / rsi_range
        # 顶背离: 价格高位但RSI不跟 → price_pos高, rsi_pos低
        top_div = round(max(0.0, price_pos - rsi_pos), 4)
        # 底背离: 价格低位但RSI不跟 → price_pos低, rsi_pos高
        bottom_div = round(max(0.0, rsi_pos - price_pos), 4)
    else:
        top_div = 0.0
        bottom_div = 0.0

    # 3. RSI超买持续: RSI>70持续天数 (连续值，非01)
    overbought_count = 0
    for j in range(idx, start, -1):
        if j < len(rsi14_series) and rsi14_series[j] > 70:
            overbought_count += 1
        else:
            break
    overbought_persist = min(overbought_count / 20.0, 1.0)  # 归一化到0-1

    # 4. 价格与RSI的20天相关性
    if n_win < 3:
        corr = 0.0
    else:
        p = np.array(price_window)
        r = np.array(rsi_window)
        p_std, r_std = np.std(p), np.std(r)
        if p_std < 1e-8 or r_std < 1e-8:
            corr = 0.0
        else:
            corr = np.corrcoef(p, r)[0, 1]
            corr = 0.0 if np.isnan(corr) else float(np.clip(corr, -1.0, 1.0))

    return [top_div, bottom_div, round(overbought_persist, 4), round(corr, 4)]

def _compute_vol_clustering(closes, i):
    """波动率聚集特征 (Mandelbrot): vol_regime, vol_momentum, vol_persist"""
    if i < 25:
        return [1.0, 0.0, 0.0]
    # 过去20个5日波动率
    rets = [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0 for j in range(i-24, i+1)]
    vols_5d = [float(np.std(rets[max(0,k-4):k+1])) for k in range(4, len(rets))]
    if len(vols_5d) < 5:
        return [1.0, 0.0, 0.0]
    vol_median = np.median(vols_5d)
    vol_20d = float(np.std(rets))
    # regime: 当前vol / 中位数, >1=高波动期
    regime = round(float(np.std(rets[-5:])) / max(vol_median, 0.0001), 4)
    # momentum: vol变化方向
    momentum = round((float(np.std(rets[-5:])) - vols_5d[0]) / max(vols_5d[0], 0.0001), 4)
    # persist: 最近5期高于历史中位数的比例（修复：用全部窗口算中位数，用最近5期做比较）
    above = sum(1 for v in vols_5d[-5:] if v > vol_median)
    persist = round(above / 5, 4)
    return [regime, momentum, persist]

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
    global _kr_features, _sector_map_cache, _proto_map_local
    _kr_features = {}  # 每次调用清空全局缓存，避免跨调用污染
    sector_map = _load_sector_map()
    _sector_map_cache = sector_map
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
    _ma_features = _load_macro_assets()
    _ab_features = _load_btc_dominance_proxy()

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
            else: volatility = 0.02  # 默认波动率，避免归零扭曲归一化
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

            # RSI + 背离特征
            rsi7 = _compute_rsi(closes, 7, i)
            rsi14 = _compute_rsi(closes, 14, i)
            rsi30 = _compute_rsi(closes, 30, i)
            rsi14_series = _compute_rsi_series(closes, 14)
            rsi_div = _compute_rsi_divergence(closes, rsi14_series, i, window=20)

            # 宏观特征
            macro_feats = _get_macro_features(ts)
            macro_feats = _apply_chain_tvl(macro_feats, sym, ts)

            vol_col = _compute_vol_clustering(closes, i)
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
    """训练模型 — 用前一天数据预测当天是否涨>5%（含板块热度+宏观特征）"""
    global _kr_features, _sector_map_cache, _proto_map_local
    _kr_features = {}  # 每次调用清空全局缓存，避免跨调用污染
    sector_map = _load_sector_map()
    _sector_map_cache = sector_map
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
    _ma_features = _load_macro_assets()
    _ab_features = _load_btc_dominance_proxy()
    print(f"宏观特征: ETF{len(_etf_features)}d 链上{len(_chain_features)}d 情绪{len(_sent_features)}d 恐慌贪婪{len(_fg_features)}d 稳定币{len(_st_features)}d Coinbase{len(_cb_features)}d Gap{len(_cbg_features)}d 韩国{len(_kg_features)}d 算力{len(_hr_features)}d 清算{len(_liq_features)}d")

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
                ret_1d = (closes[j]-closes[j-1])/closes[j-1] if closes[j-1]>0 else 0
                ret_3d = (closes[j]-closes[max(0,j-3)])/closes[max(0,j-3)] if closes[max(0,j-3)]>0 else 0
                ret_5d = (closes[j]-closes[max(0,j-5)])/closes[max(0,j-5)] if closes[max(0,j-5)]>0 else 0
                # 20日波动率归一化
                if j >= 20:
                    rets_20 = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1]>0 else 0 for k in range(j-18,j+1)]
                    vol_20d = float(np.std(rets_20))
                else:
                    vol_20d = 0.02
                vol_floor = max(vol_20d, 0.002)
                ret_1d_norm = round(ret_1d / vol_floor, 4)
                ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
                ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
                if j >= 5:
                    daily_rets = [(closes[k]-closes[k-1])/closes[k-1] if closes[k-1]>0 else 0 for k in range(j-3,j+1)]
                    volatility = np.std(daily_rets)
                else: volatility = 0.02
                vol_ratio = vols[j]/np.mean(vols[max(0,j-5):j]) if j>=5 and np.mean(vols[max(0,j-5):j])>0 else 1
                if j >= 20:
                    c20 = closes[j-19:j+1]
                    price_position = (closes[j]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
                else: price_position = 0.5
                amplitude = (highs[j]-lows[j])/opens[j] if opens[j]>0 else 0
                streak = 0
                for k in range(j, max(0,j-7)-1,-1):
                    if closes[k]>opens[k]: streak+=1
                    else: break
                div_sign = 1 if (closes[j]>closes[j-3] and vols[j]<vols[j-3]*0.7) else 0
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
                rsi14_series = _compute_rsi_series(closes, 14)
                rsi_div = _compute_rsi_divergence(closes, rsi14_series, j, window=20)
                vol_col = _compute_vol_clustering(closes, j)
                feat = [ret_1d_norm,ret_3d_norm,ret_5d_norm,volatility,vol_ratio,price_position,amplitude,streak,div_sign,oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
                # 标签: 2日收益 = (day i+1 close - day j close) / day j close, j=i-1
                next_ret = (closes[i+1]-closes[j])/closes[j] if closes[j]>0 else 0
                if abs(next_ret) > 5.0: continue
                label = 1 if next_ret > 0.05 else 0
                Xall.append(feat); yall.append(label)
            except Exception: continue

    X=np.array(Xall); y=np.array(yall)
    pos=sum(y)
    print(f"训练样本: {len(y)} 涨>5%: {pos} ({pos/len(y)*100:.1f}%) 特征维度: {X.shape[1]}")
    if pos < 10: return None

    # 与回溯端对齐: Kronos 832D + liq 19D 置零 (KRONOS_MODE=off)
    if not USE_KRONOS:
        X[:, 100:932] = 0.0
        X[:, 72:91] = 0.0
        print("Kronos 832D + liq 19D 已置零 (non-Kronos: 85D + 19D liq置零)")

    # winsor截尾 — 算P1/P99并保存，供预测/回测复用
    global _winsor_bounds
    _winsor_bounds = _compute_winsor_bounds(X)
    X = _apply_winsor(X, _winsor_bounds)
    print(f"winsor截尾: 已保存 {len(_winsor_bounds)} 维bounds到 {WINSOR_BOUNDS_FILE}")

    # Top1参数 (90d Sharpe=7.98, 180d Sharpe=6.00): d6-w1-L10-A10-s0.8-c0.6
    model = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                          min_child_weight=1, reg_lambda=10, reg_alpha=10,
                          subsample=0.8, colsample_bytree=0.6,
                          scale_pos_weight=(len(y)-pos)/pos if pos>0 else 1,
                          random_state=42, eval_metric='logloss',
                          tree_method='hist', verbosity=0)
    model.fit(X, y)
    with open(MODEL_FILE, 'wb') as f: pickle.dump(model, f)

    # 打印特征重要性
    feat_names = ['ret_1d_norm','ret_3d_norm','ret_5d_norm','volatility','vol_ratio','price_position','amplitude','streak','div_sign','oi_chg',
                  'vol_regime','vol_momentum','vol_persist',
                  'beta','alpha','r2','residual','rsi7','rsi14','rsi30','rsi_div_top','rsi_div_bottom','rsi_overbought_persist','rsi_price_corr_20d'] + SECTOR_ORDER + ['etf_btc','etf_eth','chain_vol','chain_tx','chain_fee','chain_cdd','sent_funding','sent_ls_btc','sent_ls_eth','sent_ls_avg10','sent_ls_high','sent_ls_low','fear_greed','stablecoin','coinbase_prem','coinbase_gap','btc_mcap','korea_prem','hashrate_7d_chg',
                  # 清算特征 26维
                  'liq_q0_long_mean','liq_q0_long_std','liq_q0_short_mean','liq_q0_short_std',
                  'liq_q1_long_mean','liq_q1_long_std','liq_q1_short_mean','liq_q1_short_std',
                  'liq_q2_long_mean','liq_q2_long_std','liq_q2_short_mean','liq_q2_short_std',
                  'liq_q3_long_mean','liq_q3_long_std','liq_q3_short_mean','liq_q3_short_std',
                  'liq_q4_long_mean','liq_q4_long_std','liq_q4_short_mean','liq_q4_short_std',
                  'liq_ratio_mean','liq_ratio_std','liq_long_peak_dist_mean','liq_long_peak_dist_std',
                  'liq_short_peak_dist_mean','liq_short_peak_dist_std',
                  # TVL 9维
                  'chain_tvl_btc','chain_tvl_eth','chain_tvl_sol','chain_tvl_bsc','chain_tvl_arb',
                  'chain_tvl_base','chain_tvl_ton','chain_tvl_sui','chain_tvl_polygon'] + [f'kronos_emb_{i}' for i in range(EMBEDDING_DIM)] + ['sp500_1d','dxy_1d','gold_1d','alt_btc_spread']
    importances = model.feature_importances_
    top_idx = np.argsort(importances)[-15:][::-1]
    print("特征重要性 TOP15:")
    for idx in top_idx:
        name = feat_names[idx] if idx < len(feat_names) else f'unnamed_feat_{idx}'
        print(f"  {name:20s} {importances[idx]:.4f}")

    return model

def predict(klines_all, oi_data, model):
    """预测今天哪些币明天可能涨>5%"""
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

    # 与回溯端对齐: Kronos 832D + liq 19D 置零
    if not USE_KRONOS:
        X[:, 100:932] = 0.0
        X[:, 72:91] = 0.0
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
            with open(MODEL_FILE, 'rb') as f:
                model = pickle.load(f)
            print("加载已有模型(兜底)")
        except Exception:
            print("模型文件损坏，将重新训练")

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
    daily_file = os.path.join(LOG_DIR, f'pred_{today}.json')
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
    verify_yesterday(klines)

    # 复盘错误
    review_errors()

    # 打印TOP20
    for i, r in enumerate(results[:20]):
        print(f"  {i+1:2d}. {r['symbol']:<14s} {r['prob']:5.1f}%")

def _get_live_stop_loss_pct():
    """读生产止损配置 (与auto_dual_trade同源, 保证邮件/回测/生产三口径一致)"""
    try:
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backtester', 'config', 'current_params.json')
        with open(cfg) as f:
            return float(json.load(f).get('_live_trading', {}).get('STOP_LOSS_PCT', 10.0))
    except Exception:
        return 10.0

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

    # 读取预测 (兼容新旧格式)
    if 'predictions' in pred:
        predictions = pred['predictions']
    else:
        predictions = []
        for p in pred.get('top10_long', []):
            prob_val = float(p['prob']) if isinstance(p.get('prob'), str) else p.get('prob', 0)
            predictions.append({'symbol': p['symbol'], 'prob': prob_val, 'direction': 'LONG'})
        for p in pred.get('top10_short', []):
            prob_val = float(p['prob']) if isinstance(p.get('prob'), str) else p.get('prob', 0)
            predictions.append({'symbol': p['symbol'], 'prob': prob_val, 'direction': 'SHORT'})

    print(f"[验证] {date_part}预测 → 2日后结算, 共{len(predictions)}个币种")
    # 预测日的时间戳
    pred_ts = int(_dt.datetime.strptime(date_part, '%Y-%m-%d')
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)

    results = []
    for p in predictions[:50]:
        sym = p['symbol']
        direction = p.get('direction', 'LONG')
        try:
            resp = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 5}, timeout=10)
            if resp.status_code != 200:
                continue
            kls = resp.json()
            if len(kls) < 3: continue

            # 找预测日那根K线，入场取开盘价(aligned: 预测日=入场日)
            # 结算含过程判定 (与回测一致): 后2根日线先触-10%记为止损, 先触+10%记为止盈
            entry_close = None
            exit_close = None
            reason = 'close'
            for j, k in enumerate(kls):
                if int(k[0]) == pred_ts:
                    entry_close = float(k[1])
                    if j + 2 < len(kls):
                        exit_close = float(kls[j+2][4])
                        _sl = _get_live_stop_loss_pct() / 100  # 与生产配置同步 (current_params.json _live_trading)
                        for off in (1, 2):
                            h2, l2 = float(kls[j+off][2]), float(kls[j+off][3])
                            if direction == 'LONG':
                                if l2 <= entry_close * (1 - _sl):
                                    reason = 'stop'; break
                                if h2 >= entry_close * 1.10:
                                    reason = 'take'; break
                            else:
                                if h2 >= entry_close * (1 + _sl):
                                    reason = 'stop'; break
                                if l2 <= entry_close * 0.90:
                                    reason = 'take'; break
                    break

            if entry_close is None or exit_close is None or entry_close <= 0:
                continue

            if reason == 'stop':
                actual_ret = -_sl * 100 if direction == 'LONG' else _sl * 100
            elif reason == 'take':
                actual_ret = 10.0 if direction == 'LONG' else -10.0
            else:
                actual_ret = (exit_close - entry_close) / entry_close * 100
            if direction == 'LONG':
                hit = actual_ret > 5
                pnl = actual_ret
            else:
                hit = actual_ret < -5
                pnl = -actual_ret
            results.append({
                'symbol': sym, 'prob': p['prob'], 'direction': direction,
                'actual_ret': round(actual_ret, 2), 'pnl': round(pnl, 2), 'hit': hit,
                'reason': reason,
            })
        except Exception:
            continue

    if not results: return
    hits = sum(1 for r in results if r['hit'])
    top20_hits = sum(1 for r in results[:20] if r['hit'])
    top10_hits = sum(1 for r in results[:10] if r['hit'])

    all_pnls = [r['pnl'] for r in results]
    top10_ret = round(sum(r['pnl'] for r in results[:10]), 2)
    top20_ret = round(sum(r['pnl'] for r in results[:20]), 2)
    total_ret = round(sum(all_pnls), 2)

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
            Timeout=30
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
    winners = [d for d in details if d['hit']]           # 涨>5%
    positives = [d for d in details if d['actual_ret'] > 0]  # 涨但不到5%
    flat = [d for d in details if -3 <= d['actual_ret'] <= 0]
    losers = [d for d in details if d['actual_ret'] < -3]

    # TOP10错误: 高概率但没涨>5%
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

    # 找隐藏的牛币 (低概率却涨>5%)
    low_prob_winners = [d for d in details[10:] if d['hit']]
    if low_prob_winners:
        print(f"\n隐藏牛币 (TOP10外却涨>5%, {len(low_prob_winners)}个):")
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

    # PERF: Load all macro features ONCE at function start (not per stride iteration).
    # The sample-building loop below inlines macro features into each sample via
    # _get_macro_features() / _apply_chain_tvl(), so once by_day is built, the
    # stride loop (below) only slices pre-computed samples — no re-loading.
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
    _ma_features = _load_macro_assets()
    _ab_features = _load_btc_dominance_proxy()

    # Kronos必须在样本构建前预计算, 否则_kr_features为空 (CRITICAL-7-001/002)
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
                ret_1d = (closes[i]-closes[i-1])/closes[i-1] if closes[i-1] > 0 else 0
                ret_3d = (closes[i]-closes[max(0,i-3)])/closes[max(0,i-3)] if closes[max(0,i-3)] > 0 else 0
                ret_5d = (closes[i]-closes[max(0,i-5)])/closes[max(0,i-5)] if closes[max(0,i-5)] > 0 else 0
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
                    daily_rets = [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1] > 0 else 0 for j in range(i-4,i+1)]
                    volatility = np.std(daily_rets) if len(daily_rets) > 1 else 0.02
                else: volatility = 0.02
                vol_ratio = vols[i]/np.mean(vols[max(0,i-5):i]) if i >= 5 and np.mean(vols[max(0,i-5):i]) > 0 else 1
                if i >= 20:
                    c20 = closes[i-20:i+1]
                    price_position = (closes[i]-min(c20))/(max(c20)-min(c20)) if max(c20) != min(c20) else 0.5
                else: price_position = 0.5
                amplitude = (highs[i]-lows[i])/opens[i] if opens[i] > 0 else 0
                streak = 0
                for j in range(i, max(0, i-7)-1, -1):
                    if closes[j] > opens[j]: streak += 1
                    else: break
                div_sign = 1 if (closes[i] > closes[i-3] and vols[i] < vols[i-3]*0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(ts, 0); oi_prev = oi_map.get(ts-86400, 0)
                oi_chg = (oi_now-oi_prev)/oi_prev if oi_prev > 0 else 0

                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = _regression_features(btc_rets, coin_rets, i-1)

                # 板块热度用前一日，避免当日收益率泄露
                ts_prev = ts - 86400
                sector_feats = _get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
                macro_feats = _get_macro_features(ts)
                macro_feats = _apply_chain_tvl(macro_feats, sym, ts)
                rsi7 = _compute_rsi(closes, 7, i)
                rsi14 = _compute_rsi(closes, 14, i)
                rsi30 = _compute_rsi(closes, 30, i)
                rsi14_series = _compute_rsi_series(closes, 14)
                rsi_div = _compute_rsi_divergence(closes, rsi14_series, i, window=20)

                vol_col = _compute_vol_clustering(closes, i)
                feat = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
                next_ret = (closes[i+2]-closes[i])/closes[i] if closes[i] > 0 and i+2 < n else 0
                if abs(next_ret) > 5.0: continue  # 过滤异常值
                label = 1 if next_ret > 0.05 else 0
                all_samples.append((ts, sym, feat, label, next_ret*100))
            except Exception: continue

    # 按timestamp分组
    from collections import defaultdict
    by_day = defaultdict(list)
    for ts, sym, feat, label, ret in all_samples:
        by_day[ts].append((sym, feat, label, ret))

    sorted_days = sorted(by_day.keys())
    print(f"回测: {len(sorted_days)}个交易日, {len(all_samples)}样本")

    # Kronos已在上方预计算, 此处无需再调用

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
        # winsor截尾：第一个窗口计算bounds并缓存复用 (CRITICAL-MODEL-001)
        if _winsor_bounds_backtest is None:
            _winsor_bounds_backtest = _fast_winsor_bounds(X_train)
        bounds = _winsor_bounds_backtest
        X_train = _apply_winsor(X_train, bounds)
        pos = sum(y_train)
        if pos < 5:
            continue

        # 训练 (Top1参数: d6-w1-L10-A10-s0.8-c0.6)
        model = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                              min_child_weight=1, reg_lambda=10, reg_alpha=10,
                              subsample=0.8, colsample_bytree=0.6,
                              scale_pos_weight=(len(y_train)-pos)/pos,
                              random_state=42, eval_metric='logloss',
                              tree_method='hist', verbosity=0)
        model.fit(X_train, y_train)

        # 预测
        pred_samples = by_day[pred_ts]
        X_pred = np.array([s[1] for s in pred_samples])
        X_pred = _apply_winsor(X_pred, bounds)
        # 与回溯端对齐: Kronos 832D + liq 19D 置零
        X_pred[:, 100:932] = 0.0
        X_pred[:, 72:91] = 0.0
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

# It references functions and globals already defined in that module.

def dual_backtest(days=90, stride=1):
    """多空双边 walk-forward 回测：每天选多空置信度最高的1个币单边开仓，持仓2天"""
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
    global _sector_map_cache, _proto_map_local
    _sector_map_cache = sector_map
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
    _ma_features = _load_macro_assets()
    _ab_features = _load_btc_dominance_proxy()

    # Kronos必须在样本构建前预计算, 否则_kr_features为空 (CRITICAL-7-001/002)
    all_ts_k = set()
    for kls in klines.values():
        if len(kls) < 30: continue
        for k in kls:
            all_ts_k.add(k.get('t', 0) // 1000 if isinstance(k, dict) else int(k[0]) // 1000)
    _precompute_kronos_features(list(all_ts_k))

    btc_kls = klines.get('BTCUSDT', [])
    btc_closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in btc_kls]
    btc_rets = _compute_returns(btc_closes) if len(btc_closes) > 1 else []

    # 预构建所有样本（同时保存多空标签）
    all_samples = []
    for sym, kls in klines.items():
        if len(kls) < 30:
            continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] if isinstance(k, dict) else float(k[4]) for k in kls]
        opens  = [k['o'] if isinstance(k, dict) else float(k[1]) for k in kls]
        highs  = [k['h'] if isinstance(k, dict) else float(k[2]) for k in kls]
        lows   = [k['l'] if isinstance(k, dict) else float(k[3]) for k in kls]
        vols   = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls]
        timestamps = [k.get('t', 0)//1000 if isinstance(k, dict) else int(k[0])//1000 for k in kls]
        coin_rets = _compute_returns(closes)
        n = len(kls)

        for i in range(25, n-2):
            try:
                ret_1d = (closes[i]-closes[i-1])/closes[i-1] if closes[i-1] > 0 else 0
                ret_3d = (closes[i]-closes[max(0,i-3)])/closes[max(0,i-3)] if closes[max(0,i-3)] > 0 else 0
                ret_5d = (closes[i]-closes[max(0,i-5)])/closes[max(0,i-5)] if closes[max(0,i-5)] > 0 else 0
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
                    daily_rets = [(closes[j]-closes[j-1])/closes[j-1] if closes[j-1] > 0 else 0 for j in range(i-4,i+1)]
                    volatility = np.std(daily_rets) if len(daily_rets) > 1 else 0.02
                else:
                    volatility = 0.02
                vol_ratio = vols[i]/np.mean(vols[max(0,i-5):i]) if i >= 5 and np.mean(vols[max(0,i-5):i]) > 0 else 1
                if i >= 20:
                    c20 = closes[i-20:i+1]
                    price_position = (closes[i]-min(c20))/(max(c20)-min(c20)) if max(c20) != min(c20) else 0.5
                else:
                    price_position = 0.5
                amplitude = (highs[i]-lows[i])/opens[i] if opens[i] > 0 else 0
                streak = 0
                for j in range(i, max(0, i-7)-1, -1):
                    if closes[j] > opens[j]:
                        streak += 1
                    else:
                        break
                div_sign = 1 if (closes[i] > closes[i-3] and vols[i] < vols[i-3]*0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(ts, 0)
                oi_prev = oi_map.get(ts-86400, 0)
                oi_chg = (oi_now-oi_prev)/oi_prev if oi_prev > 0 else 0

                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = _regression_features(btc_rets, coin_rets, i-1)

                # 板块热度用前一日，避免当日收益率泄露
                ts_prev = ts - 86400
                sector_feats = _get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
                macro_feats = _get_macro_features(ts)
                macro_feats = _apply_chain_tvl(macro_feats, sym, ts)
                rsi7 = _compute_rsi(closes, 7, i)
                rsi14 = _compute_rsi(closes, 14, i)
                rsi30 = _compute_rsi(closes, 30, i)
                rsi14_series = _compute_rsi_series(closes, 14)
                rsi_div = _compute_rsi_divergence(closes, rsi14_series, i, window=20)

                vol_col = _compute_vol_clustering(closes, i)
                feat = [ret_1d_norm, ret_3d_norm, ret_5d_norm, volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg] + vol_col + [
                        beta, alpha, r2, residual, rsi7, rsi14, rsi30] + rsi_div + sector_feats + macro_feats
                next_ret = (closes[i+2]-closes[i])/closes[i] if closes[i] > 0 and i+2 < n else 0
                if abs(next_ret) > 5.0:
                    continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                all_samples.append((ts, sym, feat, label_long, label_short, next_ret*100))
            except Exception:
                continue

    from collections import defaultdict
    by_day = defaultdict(list)
    for ts, sym, feat, ll, ls, ret in all_samples:
        by_day[ts].append((sym, feat, ll, ls, ret))

    sorted_days = sorted(by_day.keys())
    print(f"回测: {len(sorted_days)}个交易日, {len(all_samples)}样本")

    _precompute_kronos_features(sorted_days)

    START_DAY = max(30, len(sorted_days) - days - 1)

    trades = []
    for d in range(START_DAY, len(sorted_days)-1, stride):
        train_ts = sorted_days[max(0, d-500):d]
        pred_ts = sorted_days[d]

        X_train, y_long, y_short = [], [], []
        for ts in train_ts:
            if ts + 2 * 86400 > pred_ts: continue
            for sym, feat, ll, ls, ret in by_day[ts]:
                X_train.append(feat)
                y_long.append(ll)
                y_short.append(ls)

        X_train = np.array(X_train)
        bounds = _fast_winsor_bounds(X_train)
        X_train = _apply_winsor(X_train, bounds)

        pos_long = sum(y_long)
        pos_short = sum(y_short)
        if pos_long < 5 or pos_short < 5:
            continue

        # Top1参数: d6-w1-L10-A10-s0.8-c0.6
        model_long = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                                   min_child_weight=1, reg_lambda=10, reg_alpha=10,
                                   subsample=0.8, colsample_bytree=0.6,
                                   scale_pos_weight=(len(y_long)-pos_long)/pos_long,
                                   random_state=42, eval_metric='logloss',
                                   tree_method='hist', verbosity=0)
        model_long.fit(X_train, y_long)

        model_short = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                                    min_child_weight=1, reg_lambda=10, reg_alpha=10,
                                    subsample=0.8, colsample_bytree=0.6,
                                    scale_pos_weight=(len(y_short)-pos_short)/pos_short,
                                    random_state=43, eval_metric='logloss',
                                    tree_method='hist', verbosity=0)
        model_short.fit(X_train, y_short)

        pred_samples = by_day[pred_ts]
        X_pred = np.array([s[1] for s in pred_samples])
        X_pred = _apply_winsor(X_pred, bounds)
        # 与回溯端对齐: Kronos 832D + liq 19D 置零
        X_pred[:, 100:932] = 0.0
        X_pred[:, 72:91] = 0.0
        probs_long = model_long.predict_proba(X_pred)[:, 1]
        probs_short = model_short.predict_proba(X_pred)[:, 1]

        best_long = None
        best_short = None

        PROB_THRESHOLD = 60.0  # 全局最低概率阈值，低于此值当天空仓
        for idx, ((sym, feat, ll, ls, ret), pl, ps) in enumerate(zip(pred_samples, probs_long, probs_short)):
            kls_data = klines.get(sym, [])
            if len(kls_data) < 30:  # 放宽：30天即可（与训练集最低要求一致）
                continue
            k_idx = _find_kline_index(kls_data, pred_ts)
            if k_idx is None or k_idx < 5:
                continue
            v = [k['q'] if isinstance(k, dict) else float(k[7]) for k in kls_data[k_idx-5:k_idx]]
            if np.mean(v) < 200000:  # 放宽：20万U成交量
                continue

            if best_long is None or pl > best_long[1]:
                best_long = (sym, pl, ret)
            if best_short is None or ps > best_short[1]:
                best_short = (sym, ps, ret)

        # 确定全局最高概率及方向
        long_prob = best_long[1] * 100 if best_long else 0
        short_prob = best_short[1] * 100 if best_short else 0
        max_prob = max(long_prob, short_prob)

        if max_prob < PROB_THRESHOLD:
            day_str = datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%Y-%m-%d')
            print(f"  {day_str}: SKIP (max_prob={max_prob:.1f}% < {PROB_THRESHOLD}%)")
            continue

        STOP_LOSS = 10.0  # 10%止损
        TRADE_COST = 0.2   # 0.1% fee + 0.1% slippage
        if best_long is not None and (best_short is None or long_prob >= short_prob):
            direction = 'long'
            sym, prob, ret = best_long
            pnl = ret - TRADE_COST
            if pnl < -STOP_LOSS:
                pnl = -STOP_LOSS
        else:
            direction = 'short'
            sym, prob, ret = best_short
            pnl = -ret - TRADE_COST
            if pnl < -STOP_LOSS:
                pnl = -STOP_LOSS

        day_str = datetime.fromtimestamp(pred_ts, tz=timezone.utc).strftime('%Y-%m-%d')
        trades.append({
            'day': day_str, 'ts': pred_ts,
            'direction': direction, 'symbol': sym,
            'prob': round(prob * 100, 1),
            'actual_ret_2d': round(ret, 2),
            'pnl': round(pnl, 2),
        })
        print(f"  {day_str}: {direction:>5s} {sym:<14s} prob={prob*100:5.1f}%  ret2d={ret:+.2f}%  pnl={pnl:+.2f}%")

    if not trades:
        print("无交易记录")
        return

    print(f"\n{'='*60}")
    print(f"90天多空双边回测汇总 ({len(trades)}个交易日)")
    print(f"{'='*60}")

    cum_pnl = 0
    max_cum = 0
    max_dd = 0
    win_count = 0
    long_count = 0
    short_count = 0
    for t in trades:
        cum_pnl += t['pnl']
        max_cum = max(max_cum, cum_pnl)
        max_dd = max(max_dd, max_cum - cum_pnl)
        if t['pnl'] > 0:
            win_count += 1
        if t['direction'] == 'long':
            long_count += 1
        else:
            short_count += 1

    total_days = len(trades)
    print(f"总收益:     {cum_pnl:+.2f}%")
    print(f"日均收益:   {cum_pnl/total_days:+.2f}%")
    print(f"胜率:       {win_count}/{total_days} ({win_count/total_days*100:.1f}%)")
    print(f"最大回撤:   {max_dd:.2f}%")
    print(f"做多天数:   {long_count} ({long_count/total_days*100:.1f}%)")
    print(f"做空天数:   {short_count} ({short_count/total_days*100:.1f}%)")
    print(f"{'='*60}")

    result_file = os.path.join(os.path.dirname(__file__), f'data/dual_backtest_{days}d.json')
    with open(result_file, 'w') as f:
        json.dump({
            'trades': trades,
            'summary': {
                'total_pnl': round(cum_pnl, 2),
                'avg_daily': round(cum_pnl/total_days, 2),
                'win_rate': round(win_count/total_days*100, 1),
                'max_dd': round(max_dd, 2),
                'long_days': long_count,
                'short_days': short_count,
            }
        }, f, indent=2, default=str)
    print(f"结果已保存: {result_file}")

if __name__ == '__main__':
    import sys
    if '--backtest' in sys.argv:
        backtest()
    elif '--dual-backtest' in sys.argv:
        dual_backtest(days=170)
    else:
        run()
