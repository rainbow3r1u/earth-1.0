#!/usr/bin/env python3
"""
自动多空二选一交易脚本 - 78维完整版
每天运行：检查持仓(止损/2天平仓) → 78维特征训练 → 预测 → 开仓+止损单
"""
import os, sys, json, time, hmac, hashlib, math, warnings, fcntl, pickle
from datetime import datetime, timezone
from urllib.parse import urlencode
import numpy as np
import requests
from xgboost import XGBClassifier
from collections import defaultdict

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(__file__))
import daily_predictor as dp
from utils.feature_builder import assemble_feature_vec

# ============ 加载API ============
with open('.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '') or os.environ.get('BINANCE_SECRET_KEY', '')
BASE_URL = 'https://fapi.binance.com'

DATA_DIR = os.path.expanduser('~/.local/share/auto_trade')
os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
LOG_FILE = os.path.join(DATA_DIR, 'trade.log')
FUTURES_INFO_CACHE = os.path.join(DATA_DIR, 'binance_futures_info.json')

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    import re as _re
    msg = _re.sub(r'signature=[A-Za-z0-9]+', 'signature=***', str(msg))
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_FILE, 'a') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(line + '\n')

# ============ 配置 (优先从 shared_params.json 读取) ============
SHARED_CONFIG = os.path.join(os.path.dirname(__file__), '..', 'backtester', 'config', 'current_params.json')
_DEFAULTS = {
    'STOP_LOSS_PCT': 10.0, 'PROB_THRESHOLD': 60.0, 'LEVERAGE': 2,
    'TOP_N_SYMBOLS': 150, 'MIN_VOLUME_24H': 500000, 'TRAIN_DAYS': 500,
}
try:
    with open(SHARED_CONFIG) as _cf:
        _live = json.load(_cf).get('_live_trading', {})
    for _k, _v in _DEFAULTS.items():
        globals()[_k] = _live.get(_k, _v)
    log(f'配置: 从 {SHARED_CONFIG} 加载')
except Exception:
    for _k, _v in _DEFAULTS.items():
        globals()[_k] = _v
    log('配置: 使用内置默认值')

def safe_float(v, default=0.0):
    try:
        return float(v) if v else default
    except (ValueError, TypeError):
        return default


# NOTE: signed_request() has different implementations in auto_dual_trade.py (with retries),
# monitor_stop_loss.py (no retries), and daily_signal_advisor.py (different signature).
# Consider extracting to shared utils/BinanceAPIClient.
def signed_request(method, endpoint, params=None, max_retries=3):
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    query = urlencode(params)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params['signature'] = signature
    headers = {'X-MBX-APIKEY': API_KEY}
    url = f'{BASE_URL}{endpoint}'

    last_error = None
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                r = requests.get(url, params=params, headers=headers, timeout=15)
            elif method == 'POST':
                r = requests.post(url, params=params, headers=headers, timeout=15)
            elif method == 'DELETE':
                r = requests.delete(url, params=params, headers=headers, timeout=15)
            else:
                return {'error': True, 'msg': f'Unknown method: {method}'}

            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', 5))
                log(f'  429 Rate Limited, 等待{retry_after}s (attempt {attempt+1}/{max_retries})')
                time.sleep(retry_after)
                continue
            if r.status_code == 418:
                retry_after = int(r.headers.get('Retry-After', 300))
                log(f'  418 IP Banned, 等待{retry_after}s')
                time.sleep(retry_after)
                continue
            if r.status_code >= 500:
                wait = min(2 ** attempt, 30)
                log(f'  {r.status_code} Server Error, 等待{wait}s (attempt {attempt+1}/{max_retries})')
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return {'error': True, 'http_code': r.status_code, 'msg': r.text[:200]}

            return r.json()

        except (requests.exceptions.RequestException, ValueError) as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 30)
                log(f'  请求异常: {e}, 等待{wait}s (attempt {attempt+1}/{max_retries})')
                time.sleep(wait)
            continue

    return {'error': True, 'msg': last_error or 'max retries exceeded'}

# ============ 账户操作 ============
def get_account():
    return signed_request('GET', '/fapi/v2/account')

def get_positions():
    return signed_request('GET', '/fapi/v2/positionRisk')

def set_leverage(symbol, lev):
    return signed_request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': lev})

def place_market_order(symbol, side, quantity, reduce_only=False):
    params = {
        'symbol': symbol,
        'side': side,
        'type': 'MARKET',
        'quantity': quantity,
    }
    if reduce_only:
        params['reduceOnly'] = 'true'
    return signed_request('POST', '/fapi/v1/order', params)

def place_stop_loss_order(symbol, side, stop_price, quantity):
    """下止损单 - 币安CONDITIONAL algoOrder接口"""
    return signed_request('POST', '/fapi/v1/algoOrder', {
        'symbol': symbol,
        'side': side,
        'algoType': 'CONDITIONAL',
        'type': 'STOP_MARKET',
        'triggerPrice': stop_price,
        'quantity': quantity,
        'reduceOnly': 'true',
    })

def cancel_algo_order(symbol, order_id):
    """取消指定algoOrder"""
    return signed_request('DELETE', '/fapi/v1/algoOrder', {
        'symbol': symbol,
        'orderId': order_id,
    })

def close_with_retry(symbol, close_side, qty, max_retries=3):
    """平仓市价单, 最多重试3次指数退避, 验证成交数量 (HIGH-TRADE-001)"""
    remaining = qty
    last_result = None
    for attempt in range(max_retries):
        result = place_market_order(symbol, close_side, remaining, reduce_only=True)
        if 'orderId' in result and result.get('status') in ('FILLED', 'PARTIALLY_FILLED'):
            filled = safe_float(result.get('executedQty'), 0)
            if filled >= remaining * 0.95:
                return True, result
            remaining -= filled
            log(f'  部分成交 {filled}/{remaining+filled}, 补单剩余{remaining}')
        else:
            last_result = result
            wait = 2 ** attempt
            log(f'  平仓重试 {attempt+1}/{max_retries}, 等待{wait}s: {result}')
            time.sleep(wait)
    return False, last_result or {'error': True, 'msg': 'max retries'}

def get_symbol_price(symbol):
    r = signed_request('GET', '/fapi/v1/ticker/price', {'symbol': symbol})
    if r.get('error'):
        return None
    return float(r.get('price', 0)) or None

def get_step_size(symbol):
    cache = FUTURES_INFO_CACHE
    try:
        if os.path.exists(cache):
            with open(cache) as f:
                data = json.load(f)
            for s in data.get('symbols', []):
                if s['symbol'] == symbol:
                    for f in s.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            return float(f['stepSize'])
    except Exception:
        pass

    # 缓存缺失/损坏时实时查询 (HIGH-004 fix)
    try:
        r = signed_request('GET', '/fapi/v1/exchangeInfo', {})
        if not r.get('error') and 'symbols' in r:
            for s in r['symbols']:
                if s['symbol'] == symbol:
                    for f in s.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            return float(f['stepSize'])
    except Exception:
        pass
    return None

def get_tick_size(symbol):
    """获取价格最小变动单位"""
    cache = FUTURES_INFO_CACHE
    try:
        if os.path.exists(cache):
            with open(cache) as f:
                data = json.load(f)
            for s in data.get('symbols', []):
                if s['symbol'] == symbol:
                    for f in s.get('filters', []):
                        if f['filterType'] == 'PRICE_FILTER':
                            return float(f['tickSize'])
    except Exception:
        pass
    return None

def round_qty(symbol, qty):
    step = get_step_size(symbol)
    if not step:
        return qty  # 回退到原始数量而非归零
    decimals = len(str(step).split('.')[-1].rstrip('0')) if '.' in str(step) else 0
    if decimals == 0:
        return math.floor(qty / step) * step
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor

# ============ 状态管理 ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return data
    return {'positions': {}, 'history': [], 'daily_pnl': []}

def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(state, f, indent=2, default=str)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.rename(tmp, STATE_FILE)

# ============ 持仓管理 ============
def check_and_close(state):
    positions = get_positions()
    active = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0]
    closed = []
    
    for p in active:
        symbol = p['symbol']
        amt = safe_float(p.get('positionAmt'))
        if amt == 0:
            continue
        side = 'LONG' if amt > 0 else 'SHORT'
        entry = float(p.get('entryPrice', 0) or 0)
        if entry <= 0:
            log(f'[WARN] {p["symbol"]} entryPrice异常={p.get("entryPrice")}, 跳过')
            continue
        mark = safe_float(p.get('markPrice'))

        if side == 'LONG':
            ret_pct = (mark - entry) / entry * 100
        else:
            ret_pct = (entry - mark) / entry * 100
        
        pos_key = f"{symbol}_{side}"
        
        if ret_pct <= -STOP_LOSS_PCT:
            close_side = 'SELL' if side == 'LONG' else 'BUY'
            qty = round_qty(symbol, abs(amt))
            if qty > 0:
                log(f'[STOP LOSS] {symbol} {side} 亏损{ret_pct:.2f}% 平{qty}')
                # 取消本bot的止损单(非地毯式) — HIGH-TRADE-005
                sl_oid = state.get('positions', {}).get(pos_key, {}).get('sl_order_id')
                if sl_oid:
                    cancel_algo_order(symbol, sl_oid)
                else:
                    signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})
                ok, result = close_with_retry(symbol, close_side, qty)
                if ok:
                    log(f'  -> 平仓成功: orderId={result["orderId"]}')
                    closed.append(pos_key)
                else:
                    log(f'  -> 平仓失败(已重试): {result}')
            continue

        if pos_key in state.get('positions', {}):
            open_ts = state['positions'][pos_key].get('open_ts', 0)
            hold_hours = (int(time.time()) - open_ts) / 3600
            if hold_hours >= 48:
                close_side = 'SELL' if side == 'LONG' else 'BUY'
                qty = round_qty(symbol, abs(amt))
                if qty > 0:
                    log(f'[TIME EXIT] {symbol} {side} 持仓{hold_hours:.1f}h 平{qty}')
                    sl_oid = state.get('positions', {}).get(pos_key, {}).get('sl_order_id')
                    if sl_oid:
                        cancel_algo_order(symbol, sl_oid)
                    else:
                        signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})
                    ok, result = close_with_retry(symbol, close_side, qty)
                    if ok:
                        log(f'  -> 平仓成功: orderId={result["orderId"]}')
                        closed.append(pos_key)
                    else:
                        log(f'  -> 平仓失败(已重试): {result}')
    
    # 孤儿仓位: 币安有持仓但状态文件中没有 → 纳入管理
    state_positions = state.get('positions', {})
    for p in active:
        symbol = p['symbol']
        amt = safe_float(p.get('positionAmt'))
        side = 'LONG' if amt > 0 else 'SHORT'
        orphan_key = f"{symbol}_{side}"
        if orphan_key not in state_positions:
            log(f'[ORPHAN] 发现孤儿仓位 {symbol} {side}, 纳入管理')
            state_positions[orphan_key] = {
                'symbol': symbol,
                'direction': side,
                'qty': abs(amt),
                'open_ts': int(time.time()) - 24*3600,  # 保守估计已持24h (避免95h超持)
                'open_price': float(p.get('entryPrice', 0)),
                'notional': abs(amt) * float(p.get('markPrice', 0)),
                'stop_loss_price': 0,
                'source': 'orphan',
            }
    state['positions'] = state_positions

    for key in closed:
        if key in state.get('positions', {}):
            state['positions'][key]['close_ts'] = int(time.time())
            state['history'].append(state['positions'][key])
            del state['positions'][key]
    
    return state

# ============ 仓位计算 ============
# NOTE: Margin steps are hardcoded; consider making them configurable via env vars
# (e.g. MARGIN_STEPS env var or a config dict) for different market regimes.
def get_margin_size(capital):
    if capital < 25:
        return 5.0
    elif capital < 50:
        return 8.0
    elif capital < 100:
        return 10.0
    elif capital < 200:
        return 15.0
    elif capital < 400:
        return 20.0
    else:
        return 30.0

# ============ 数据获取 ============
def fetch_top_symbols(n=80):
    r = signed_request('GET', '/fapi/v1/ticker/24hr')
    if not isinstance(r, list):
        log(f'获取24h数据失败: {r}')
        return []
    syms = [(x['symbol'], float(x['quoteVolume'])) for x in r 
            if x['symbol'].endswith('USDT') and float(x['quoteVolume']) > MIN_VOLUME_24H]
    syms.sort(key=lambda x: -x[1])
    return [s[0] for s in syms[:n]]

def fetch_klines_full(symbols):
    """从本地完整缓存加载K线 + API补最新（最多1005天历史）"""
    klines = {}
    import concurrent.futures

    # 1. 加载本地完整缓存 (595币, XRP 1005天, BTC 505天)
    cache_file = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)['klines']
            for sym in symbols:
                if sym in cached and len(cached[sym]) >= 30:
                    klines[sym] = cached[sym][:]
            log(f"缓存加载: {len(klines)}/{len(symbols)}币种")
        except Exception as e:
            log(f"缓存加载失败: {e}")
    else:
        log(f"警告: 缓存文件不存在 {cache_file}")

    # 2. API补最新K线（公开接口，无需签名）
    def _fetch_latest(sym):
        try:
            r = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 10}, timeout=10)
            if r.status_code == 200:
                return sym, [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                              'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7])}
                             for k in r.json()]
        except:
            pass
        return sym, []

    updated = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_latest, s): s for s in symbols if s in klines}
        for f in concurrent.futures.as_completed(futures):
            s, new_kls = f.result()
            if not new_kls:
                continue
            old_kls = klines[s]
            last_old_ts = old_kls[-1].get('t', 0)
            appended = 0
            for k in new_kls:
                if k['t'] > last_old_ts:
                    old_kls.append(k)
                    appended += 1
            if appended > 0:
                updated += 1

    if updated > 0:
        log(f"API更新: {updated}个币种补充最新K线")
        # 写回缓存（可选，保持缓存最新）
        try:
            with open(cache_file) as f:
                cache_data = json.load(f)
            for s in klines:
                if s in cache_data.get('klines', {}):
                    cache_data['klines'][s] = klines[s]
            tmp = cache_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(cache_data, f)
            os.rename(tmp, cache_file)
        except Exception:
            pass

    return klines

def fetch_oi_for_symbols(symbols):
    """优先本地OI缓存，API补充最新"""
    oi_data = {}
    cache_path = '/home/myuser/backtester/data_cache/oi_daily.json'
    local_cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                local_cache = json.load(f)
        except:
            pass

    for sym in symbols:
        if sym in local_cache and local_cache[sym]:
            cached = local_cache[sym]
            # 取最近90天
            sorted_ts = sorted(cached.keys(), reverse=True)[:90]
            oi_data[sym] = {int(ts): float(cached[ts]) for ts in sorted_ts}

    # API补充缺失的币种
    missing = [s for s in symbols if s not in oi_data]
    for sym in missing[:20]:
        try:
            r = requests.get(f'{BASE_URL}/futures/data/openInterestHist',
                             params={'symbol': sym, 'period': '1d', 'limit': 90}, timeout=10)
            if r.status_code == 200 and isinstance(r.json(), list):
                oi_data[sym] = {int(x['timestamp']) // 1000: float(x['sumOpenInterest']) for x in r.json()}
        except:
            pass
        time.sleep(0.3)
    return oi_data

# ============ 78维特征工程（完整版，与回测一致） ============
def build_features_78d(klines, oi_data, sector_map, sector_heats_all):
    """构建78维特征，与dual_backtest_365d完全一致"""
    all_samples = []
    btc_kls = klines.get('BTCUSDT', [])
    btc_closes = [k['c'] for k in btc_kls]
    btc_rets = dp._compute_returns(btc_closes) if btc_closes else []
    
    for sym, kls in klines.items():
        if len(kls) < 35:
            continue
        oi_map = oi_data.get(sym, {})
        closes = [k['c'] for k in kls]
        opens = [k['o'] for k in kls]
        highs = [k['h'] for k in kls]
        lows = [k['l'] for k in kls]
        vols = [k['q'] for k in kls]
        timestamps = [k['t'] // 1000 for k in kls]
        coin_rets = dp._compute_returns(closes)
        n = len(kls)
        
        for i in range(25, n - 2):
            j = i - 1
            try:
                # CRITICAL-GPU-005: ret_1d 改为正确公式 (j vs j-1, 非 j vs i-1)
                ret_1d = (closes[j] - closes[j-1]) / closes[j-1] if closes[j-1] > 0 else 0
                ret_3d = (closes[j] - closes[max(0, j-3)]) / closes[max(0, j-3)] if closes[max(0, j-3)] > 0 else 0
                ret_5d = (closes[j] - closes[max(0, j-5)]) / closes[max(0, j-5)] if closes[max(0, j-5)] > 0 else 0
                if j >= 20:
                    # CRITICAL-GPU-004: 用 k 避免遮蔽外层 j
                    rets_20 = [(closes[k] - closes[k-1]) / closes[k-1] if closes[k-1] > 0 else 0 for k in range(j-18, j+1)]
                    vol_20d = float(np.std(rets_20))
                else:
                    vol_20d = 0.02
                vol_floor = max(vol_20d, 0.002)
                ret_1d_norm = round(ret_1d / vol_floor, 4)
                ret_3d_norm = round(ret_3d / (vol_floor * 1.732), 4)
                ret_5d_norm = round(ret_5d / (vol_floor * 2.236), 4)
                if j >= 5:
                    daily_rets = [(closes[k] - closes[k-1]) / closes[k-1] if closes[k-1] > 0 else 0 for k in range(j-3, j+1)]
                    volatility = np.std(daily_rets)
                else:
                    volatility = 0
                vol_ratio = vols[j] / np.mean(vols[max(0, j-5):j]) if j >= 5 and np.mean(vols[max(0, j-5):j]) > 0 else 1
                if j >= 20:
                    c20 = closes[j-19:j+1]
                    price_position = (closes[j] - min(c20)) / (max(c20) - min(c20)) if max(c20) != min(c20) else 0.5
                else:
                    price_position = 0.5
                amplitude = (highs[j] - lows[j]) / opens[j] if opens[j] > 0 else 0
                streak = 0
                for k in range(j, max(0, j-7) - 1, -1):
                    if closes[k] > opens[k]:
                        streak += 1
                    else:
                        break
                div_sign = 1 if (closes[j] > closes[j-3] and vols[j] < vols[j-3] * 0.7) else 0
                ts = timestamps[i]
                oi_now = oi_map.get(timestamps[j], 0)
                oi_prev = oi_map.get(timestamps[j-1], 0)
                oi_chg = (oi_now - oi_prev) / oi_prev if oi_prev > 0 else 0

                if sym == 'BTCUSDT':
                    beta, alpha, r2, residual = 1.0, 0.0, 1.0, 0.0
                else:
                    beta, alpha, r2, residual = dp._regression_features(btc_rets, coin_rets, j)

                sector_feats = dp._get_sector_features(sym, ts, sector_map, sector_heats_all)
                macro_feats = dp._get_macro_features(ts)
                macro_feats = dp._apply_chain_tvl(macro_feats, sym, ts)

                rsi7 = dp._compute_rsi(closes, 7, j)
                rsi14 = dp._compute_rsi(closes, 14, j)
                rsi30 = dp._compute_rsi(closes, 30, j)
                rsi14_series = dp._compute_rsi_series(closes, 14)
                rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, j, window=20)
                vol_col = dp._compute_vol_clustering(closes, j)

                feat = assemble_feature_vec(
                    ret_1d_norm, ret_3d_norm, ret_5d_norm,
                    volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg,
                    vol_col, beta, alpha, r2, residual, rsi7, rsi14, rsi30,
                    rsi_div, sector_feats, macro_feats)

                # CRITICAL-7-004: 纯1日预测 (closes[i+1] vs closes[i]), 不含当日收益动量
                next_ret = (closes[i+1] - closes[i]) / closes[i] if closes[i] > 0 and i + 1 < n else 0
                if abs(next_ret) > 5.0:
                    continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                all_samples.append((ts, sym, feat, label_long, label_short, next_ret * 100))
            except:
                continue
    
    by_day = defaultdict(list)
    for ts, sym, feat, ll, ls, ret in all_samples:
        by_day[ts].append((sym, feat, ll, ls, ret))
    
    return by_day

# ============ 训练预测 ============
def train_and_predict(by_day, today_ts, klines):
    sorted_days = sorted(by_day.keys())
    train_days = [ts for ts in sorted_days if ts < today_ts]
    
    if len(train_days) < 15:
        log(f'训练数据不足: {len(train_days)}天')
        return None, None
    
    train_days = train_days[-TRAIN_DAYS:]
    
    X_train, y_long, y_short = [], [], []
    for ts in train_days:
        for sym, feat, ll, ls, ret in by_day[ts]:
            X_train.append(feat)
            y_long.append(ll)
            y_short.append(ls)
    
    X_train = np.array(X_train)
    if len(X_train) < 100:
        log(f'样本不足: {len(X_train)}')
        return None, None
    
    bounds = []
    for j in range(X_train.shape[1]):
        col = X_train[:, j]
        bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
    X_train = dp._apply_winsor(X_train, bounds)
    
    pos_long = sum(y_long)
    pos_short = sum(y_short)
    if pos_long < 5 or pos_short < 5:
        log(f'正样本不足: long={pos_long}, short={pos_short}')
        return None, None
    
    log(f'训练: {len(X_train)}样本, {len(train_days)}天, long={pos_long}, short={pos_short}')

    # 尝试加载已持久化模型(若文件存在且<7天) — CRITICAL-TRADE-002
    models_dir = os.path.join(DATA_DIR, 'models')
    os.makedirs(models_dir, mode=0o700, exist_ok=True)
    model_long_file = os.path.join(models_dir, 'xgb_daily_long.pkl')
    model_short_file = os.path.join(models_dir, 'xgb_daily_short.pkl')
    model_long = None; model_short = None
    try:
        if os.path.exists(model_long_file) and time.time() - os.path.getmtime(model_long_file) < 7*86400:
            with open(model_long_file,'rb') as f: model_long = pickle.load(f)
            log(f'加载预训练多头模型: {model_long_file}')
        if os.path.exists(model_short_file) and time.time() - os.path.getmtime(model_short_file) < 7*86400:
            with open(model_short_file,'rb') as f: model_short = pickle.load(f)
            log(f'加载预训练空头模型: {model_short_file}')
    except: pass

    if model_long is None:
        model_long = XGBClassifier(n_estimators=150, max_depth=5, learning_rate=0.05,
                                   scale_pos_weight=(len(y_long) - pos_long) / pos_long,
                                   random_state=42, eval_metric='logloss', verbosity=0)
        model_long.fit(X_train, y_long)
        try:
            with open(model_long_file,'wb') as f: pickle.dump(model_long, f)
        except Exception as e: log(f'多头模型保存失败: {e}')

    if model_short is None:
        model_short = XGBClassifier(n_estimators=150, max_depth=5, learning_rate=0.05,
                                    scale_pos_weight=(len(y_short) - pos_short) / pos_short,
                                    random_state=43, eval_metric='logloss', verbosity=0)
        model_short.fit(X_train, y_short)
        try:
            with open(model_short_file,'wb') as f: pickle.dump(model_short, f)
        except Exception as e: log(f'空头模型保存失败: {e}')
    
    pred_samples = by_day.get(today_ts, [])
    if not pred_samples:
        log(f'今天无预测样本')
        return None, None
    
    X_pred = np.array([s[1] for s in pred_samples])
    X_pred = dp._apply_winsor(X_pred, bounds)
    probs_long = model_long.predict_proba(X_pred)[:, 1]
    probs_short = model_short.predict_proba(X_pred)[:, 1]
    
    best_long = None
    best_short = None
    
    for idx, ((sym, feat, ll, ls, ret), pl, ps) in enumerate(zip(pred_samples, probs_long, probs_short)):
        kls = klines.get(sym, [])
        if len(kls) < 30:
            continue
        # 取today_ts之前最近5根日K线，避免前视偏差 (HIGH-7-006)
        k_idx = next((i for i, k in enumerate(kls)
                     if (k['t'] if isinstance(k, dict) else int(k[0])) >= today_ts * 1000), len(kls))
        if k_idx < 5:
            continue
        recent_vol = np.mean([k['q'] for k in kls[k_idx-5:k_idx]])
        if recent_vol < MIN_VOLUME_24H:
            continue
        
        if best_long is None or pl > best_long[1]:
            best_long = (sym, pl, ret)
        if best_short is None or ps > best_short[1]:
            best_short = (sym, ps, ret)
    
    return best_long, best_short

# ============ 主流程 ============
def main():
    # 进程锁: 防止cron重叠导致双开 (HIGH-TRADE-002)
    lockfile = os.path.join(DATA_DIR, 'auto_dual.lock')
    _lock_fd = open(lockfile, 'w')
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f'另一个 auto_dual_trade 实例正在运行, 退出')
        _lock_fd.close()
        return

    log('=' * 60)
    log('自动多空二选一交易启动 (78维完整版)')
    log('=' * 60)
    
    # 1. 账户
    account = get_account()
    if 'code' in account:
        log(f'账户读取失败: {account}')
        return
    
    wallet = float(account.get('totalWalletBalance', 0))
    available = float(account.get('availableBalance', 0))
    log(f'钱包: {wallet:.2f}u  可用: {available:.2f}u')
    
    if wallet < 10:
        log('本金不足10u，停止交易')
        return
    
    # 2. 状态
    state = load_state()
    if 'positions' not in state:
        state['positions'] = {}
    
    # 3. 平仓
    state = check_and_close(state)
    
    # 4. 每天开仓, check_and_close已平48h到期, 自然维持0-2个仓位
    positions = get_positions()
    active = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0]
    if active:
        syms = [p['symbol'] for p in active]
        log(f'当前{len(active)}个持仓: {syms} (48h到期自动平)')
    
    # 5. 加载外部数据（完整78维）
    log('加载外部数据...')
    sector_map = dp._load_sector_map()
    dp._sector_map_cache = sector_map
    if not dp._proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                dp._proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except:
            pass
    
    dp._etf_features = dp._load_etf_features()
    dp._chain_features = dp._load_chain_features()
    dp._sent_features = dp._load_sent_features()
    dp._fg_features = dp._load_fear_greed()
    dp._st_features = dp._load_stablecoin_netflow()
    dp._cb_features = dp._load_coinbase_premium()
    dp._cbg_features = dp._load_cb_gap_features()
    dp._bd_features = dp._load_btc_mcap()
    dp._kg_features = dp._load_korea_premium()
    dp._hr_features = dp._load_hashrate_features()
    dp._liq_features = dp._load_liquidation_features()
    dp._tvl_features = dp._load_chain_tvl()
    dp._ma_features = dp._load_macro_assets()
    dp._ab_features = dp._load_btc_dominance_proxy()

    # 6. 获取K线（全量缓存+API补最新）
    log('获取全量K线...')
    top_syms = fetch_top_symbols(TOP_N_SYMBOLS)
    if 'BTCUSDT' not in top_syms:
        top_syms.append('BTCUSDT')
    log(f'目标币种: {len(top_syms)}')
    
    klines = fetch_klines_full(top_syms)
    # 过滤：只保留有足够历史数据的币种
    min_required = TRAIN_DAYS + 35  # 训练天数 + 特征计算buffer
    klines = {sym: kls for sym, kls in klines.items() if len(kls) >= min_required}
    log(f'有效币种(>{min_required}天): {len(klines)}')
    
    oi_data = fetch_oi_for_symbols(list(klines.keys()))
    log(f'OI获取: {len(oi_data)}币种')
    
    # 7. 预计算板块热度 + Kronos
    log('预计算板块热度...')
    sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}
    
    log('预计算Kronos...')
    sorted_days = sorted({k['t'] // 1000 for kls in klines.values() for k in kls})
    dp._precompute_kronos_features(sorted_days)
    
    # 8. 构建78维特征
    log('构建78维特征...')
    by_day = build_features_78d(klines, oi_data, sector_map, sector_heats_all)
    log(f'样本: {len(by_day)}天, 总样本{sum(len(v) for v in by_day.values())}')
    
    # 9. 训练预测
    if len(sorted_days) < 2:
        log('数据不足')
        save_state(state)
        return
    
    # 找到有样本的最后一天（确保能计算next_ret）
    today_ts = None
    for ts in reversed(sorted_days):
        if by_day.get(ts):
            today_ts = ts
            break
    if today_ts is None:
        log('无有效预测日期')
        save_state(state)
        return
    today_str = datetime.fromtimestamp(today_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    log(f'预测日期: {today_str}')
    
    best_long, best_short = train_and_predict(by_day, today_ts, klines)
    
    if best_long is None and best_short is None:
        log('无有效信号')
        save_state(state)
        return
    
    long_prob = best_long[1] * 100 if best_long else 0
    short_prob = best_short[1] * 100 if best_short else 0
    max_prob = max(long_prob, short_prob)
    
    log(f'最佳做多: {best_long[0] if best_long else None} prob={long_prob:.1f}%')
    log(f'最佳做空: {best_short[0] if best_short else None} prob={short_prob:.1f}%')
    
    if max_prob < PROB_THRESHOLD:
        log(f'置信度不足: {max_prob:.1f}% < {PROB_THRESHOLD}%，空仓')
        save_state(state)
        return
    
    # 10. 确定方向并开仓
    if best_long is not None and (best_short is None or long_prob >= short_prob):
        direction = 'LONG'
        symbol = best_long[0]
        side = 'BUY'
        prob = long_prob
    else:
        direction = 'SHORT'
        symbol = best_short[0]
        side = 'SELL'
        prob = short_prob
    
    # 检查同币种已有持仓方向
    existing = next((p for p in active if p['symbol'] == symbol), None)
    if existing:
        existing_amt = float(existing.get('positionAmt', 0))
        existing_dir = 'LONG' if existing_amt > 0 else 'SHORT'
        if existing_dir == direction:
            log(f'{symbol} 已有同向{existing_dir}持仓, 跳过今日')
            save_state(state)
            return
        else:
            log(f'{symbol} 已有反向{existing_dir}持仓, 先平仓再开{direction}')
            close_side = 'SELL' if existing_dir == 'LONG' else 'BUY'
            qty_close = round_qty(symbol, abs(existing_amt))
            if qty_close > 0:
                cancel_result = signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})
                log(f'  取消挂单: {cancel_result.get("code", cancel_result.get("msg", "ok"))}')
                result = place_market_order(symbol, close_side, qty_close, reduce_only=True)
                if 'orderId' not in result:
                    log(f'  平仓失败: {result}, 跳过今日')
                    save_state(state)
                    return
                log(f'  平仓成功: orderId={result.get("orderId")}')
                # 轮询确认持仓已清零
                for poll_i in range(5):
                    time.sleep(1)
                    pos_check = get_positions()
                    if isinstance(pos_check, list):
                        still_open = [p for p in pos_check
                                      if p.get('symbol') == symbol and abs(float(p.get('positionAmt', 0))) > 0]
                        if not still_open:
                            log(f'  持仓确认清零 (poll {poll_i+1})')
                            break
                    log(f'  等待持仓清零... (poll {poll_i+1})')
                else:
                    # 轮询超时后检查是否仍持仓
                    pos_final = get_positions()
                    if isinstance(pos_final, list):
                        still_held = [p for p in pos_final
                                      if p.get('symbol') == symbol and abs(float(p.get('positionAmt', 0))) > 0]
                        if still_held:
                            log(f'[CRITICAL] 平仓5秒后仍持仓 {symbol}, 中止开仓')
                            save_state(state)
                            return
            else:
                time.sleep(0.5)

    margin = get_margin_size(wallet)
    if available < margin:
        log(f'可用不足: {available:.2f}u < {margin}u')
        save_state(state)
        return
    
    price = get_symbol_price(symbol)
    if not price:
        log(f'价格获取失败: {symbol}')
        save_state(state)
        return
    
    notional = margin * LEVERAGE
    qty_raw = notional / price
    qty = round_qty(symbol, qty_raw)
    
    if qty <= 0:
        log(f'数量计算失败: {symbol} qty={qty_raw:.6f} -> {qty}')
        save_state(state)
        return
    
    actual_notional = qty * price
    if actual_notional < 5:
        log(f'名义价值不足5u: {actual_notional:.2f}u')
        save_state(state)
        return
    
    log(f'开仓: {symbol} {direction} 保证金{margin:.1f}u 杠杆{LEVERAGE}x 数量{qty} 名义{actual_notional:.1f}u 置信度{prob:.1f}%')
    
    # 设置杠杆
    lev_result = set_leverage(symbol, LEVERAGE)
    log(f'  杠杆设置: {lev_result.get("leverage", lev_result)}')
    if lev_result.get('code') and lev_result.get('code') != 0:
        log(f'  杠杆设置失败: {lev_result}, 中止开仓')
        save_state(state)
        return

    # 市价单
    order = place_market_order(symbol, side, qty)
    if ('orderId' in order
            and order.get('status') in ('FILLED', 'PARTIALLY_FILLED')
            and float(order.get('executedQty', 0)) > 0):
        log(f'  市价单成功: orderId={order["orderId"]} status={order["status"]}')

        # 用实际成交价计算止损价 (CRITICAL-003 fix)
        actual_price = safe_float(order.get('avgPrice'))
        if actual_price <= 0:
            cum_quote = safe_float(order.get('cumQuote'))
            executed_qty = safe_float(order.get('executedQty'))
            if executed_qty > 0:
                actual_price = cum_quote / executed_qty
            else:
                # 回退: 查positionRisk获取实际entryPrice (MEDIUM-TRADE-001)
                pos_data = signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
                if isinstance(pos_data, list) and pos_data:
                    actual_price = safe_float(pos_data[0].get('entryPrice'), price)
                else:
                    actual_price = price

        # 用实际成交量计算止损量 (CRITICAL-002 fix)
        actual_qty = safe_float(order.get('executedQty', qty), qty)
        if actual_qty <= 0:
            actual_qty = qty

        # 止损单 — 用tickSize动态精度，避免低价币止损伤归零
        tick = get_tick_size(symbol)
        decimals = len(str(tick).split('.')[-1].rstrip('0')) if tick and '.' in str(tick) else 4
        if direction == 'LONG':
            sl_side = 'SELL'
            sl_price = round(actual_price * (1 - STOP_LOSS_PCT / 100), decimals)
        else:
            sl_side = 'BUY'
            sl_price = round(actual_price * (1 + STOP_LOSS_PCT / 100), decimals)

        # 止损单重试最多3次，失败则回滚市价单 (CRITICAL-001 fix)
        sl_order = None
        for sl_attempt in range(3):
            sl_order = place_stop_loss_order(symbol, sl_side, sl_price, actual_qty)
            if 'orderId' in sl_order:
                break
            if sl_attempt < 2:
                wait = 2 ** sl_attempt
                log(f'  止损单重试 {sl_attempt+1}/3, 等待{wait}s: {sl_order}')
                time.sleep(wait)

        if sl_order and 'orderId' in sl_order:
            log(f'  止损单成功: orderId={sl_order["orderId"]} 触发价{sl_price}')
        else:
            log(f'  止损单3次失败，回滚平仓: {sl_order}')
            rollback_side = 'SELL' if side == 'BUY' else 'BUY'
            for rb_attempt in range(3):
                rollback = place_market_order(symbol, rollback_side, actual_qty, reduce_only=True)
                if 'orderId' in rollback:
                    log(f'  回滚成功: orderId={rollback.get("orderId")}')
                    save_state(state)
                    log('止损失败已回滚，交易中止')
                    return
                wait = 2 ** rb_attempt
                log(f'  回滚失败重试 {rb_attempt+1}/3, 等待{wait}s: {rollback}')
                time.sleep(wait)
            # 3次回滚均失败 → 仓位完全裸奔
            log(f'[CRITICAL] {symbol} 止损+回滚均失败！仓位裸奔！请立即人工处理！')
            state['positions'][f'{symbol}_{direction}'] = {
                'symbol': symbol,
                'direction': direction,
                'qty': actual_qty,
                'margin': margin,
                'prob': prob,
                'open_ts': int(time.time()),
                'open_price': actual_price,
                'notional': actual_qty * actual_price,
                'stop_loss_price': 0,
                'naked': True,
            }
            save_state(state)
            log('裸仓状态已记录，中止')
            return

        state['positions'][f'{symbol}_{direction}'] = {
            'symbol': symbol,
            'direction': direction,
            'qty': actual_qty,
            'margin': margin,
            'prob': prob,
            'open_ts': int(time.time()),
            'open_price': actual_price,
            'sl_order_id': sl_order.get('orderId') if sl_order and 'orderId' in sl_order else None,
            'notional': actual_qty * actual_price,
            'stop_loss_price': sl_price,
        }
    else:
        log(f'  下单失败: {order}')
    
    save_state(state)
    log('交易结束')

if __name__ == '__main__':
    main()
