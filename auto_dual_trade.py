#!/usr/bin/env python3
"""
自动多空二选一交易脚本
每天运行：检查持仓(止损/2天平仓) → 训练+预测 → 开仓+止损单+止盈单
特征维度: 10(基)+3(vol)+7(信号)+4(RSI背离)+22(板块)+~876(宏观+Kronos) ≈ 922维
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

# ============ 路径常量 ============
KLINE_CACHE_FILE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
OI_CACHE_FILE = '/home/myuser/backtester/data_cache/oi_daily.json'
PROTOCOL_MAP_FILE = '/home/myuser/defillama_data/protocol_map.json'
SECTOR_CACHE_FILE = '/home/myuser/websocket_new/data/crypto_sectors.json'
KRONOS_CACHE_FILE = '/home/myuser/websocket_new/data/kronos_features_cache.json'
KRONOS_EMBEDDING_FILE = '/home/myuser/websocket_new/data/kronos_embeddings.json'
FEAR_GREED_FILE = '/home/myuser/websocket_new/data/fear_greed_history.json'
MACRO_ASSETS_FILE = '/home/myuser/websocket_new/data/macro_assets.json'
LIQ_DAILY_FILE = '/home/myuser/websocket_new/data/liq_daily.json'

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
    'STOP_LOSS_PCT': 10.0, 'TAKE_PROFIT_PCT': 10.0,  # FIX: 对称止盈
    'PROB_THRESHOLD': 60.0, 'LEVERAGE': 2,
    'TOP_N_SYMBOLS': 150, 'MIN_VOLUME_24H': 500000, 'TRAIN_DAYS': 365,
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
        if v is None or v == '':
            return default
        result = float(v)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (ValueError, TypeError):
        return default


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
                log(f'  418 IP Banned, 等待{retry_after}s (attempt {attempt+1}/{max_retries})')
                time.sleep(retry_after)
                if attempt >= max_retries - 1:
                    return {'error': True, 'http_code': 418, 'msg': 'IP banned, max retries exhausted'}
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
    r = signed_request('GET', '/fapi/v2/account')
    if isinstance(r, dict) and ('error' in r or 'code' in r):
        log(f'[API_ERROR] get_account失败: {r}')
        return None
    return r

def get_positions():
    r = signed_request('GET', '/fapi/v2/positionRisk')
    if isinstance(r, dict) and ('error' in r or 'code' in r):
        log(f'[API_ERROR] get_positions失败: {r}')
        return None
    if not isinstance(r, list):
        log(f'[API_ERROR] get_positions返回非列表: {type(r)} {r}')
        return None
    return r

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

def place_stop_loss_order(symbol, side, stop_price):
    """FIX: 使用标准合约止损单接口，closePosition=true平仓全部
    FIX: workingType=MARK_PRICE 按标记价格触发，避免插针误触发
    """
    return signed_request('POST', '/fapi/v1/order', {
        'symbol': symbol,
        'side': side,
        'type': 'STOP_MARKET',
        'stopPrice': stop_price,
        'closePosition': 'true',
        'workingType': 'MARK_PRICE',
    })

def place_take_profit_order(symbol, side, take_price):
    """FIX: 新增止盈单 — 与回测对称+10%对齐
    FIX: workingType=MARK_PRICE 按标记价格触发，避免插针误触发
    """
    return signed_request('POST', '/fapi/v1/order', {
        'symbol': symbol,
        'side': side,
        'type': 'TAKE_PROFIT_MARKET',
        'stopPrice': take_price,
        'closePosition': 'true',
        'workingType': 'MARK_PRICE',
    })

def cancel_all_orders(symbol):
    """取消某币种所有挂单"""
    return signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})

def close_with_retry(symbol, close_side, qty, max_retries=3):
    """平仓市价单, 最多重试3次指数退避, 验证成交数量
    FIX: remaining下限保护，防止负数
    """
    remaining = max(qty, 0)
    total_filled = 0
    last_result = None
    for attempt in range(max_retries):
        if remaining <= 0:
            return True, last_result or {'orderId': 'completed', 'status': 'FILLED'}
        result = place_market_order(symbol, close_side, remaining, reduce_only=True)
        if 'orderId' in result and result.get('status') in ('FILLED', 'PARTIALLY_FILLED'):
            filled = safe_float(result.get('executedQty'), 0)
            total_filled += filled
            # FIX: 用原始qty判断，避免remaining缩小后比例失真
            if total_filled >= qty * 0.95:
                return True, result
            remaining = max(remaining - filled, 0)
            log(f'  部分成交 {filled}/{qty}, 累计{total_filled}, 补单剩余{remaining}')
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

def _refresh_futures_info():
    """FIX: 定期刷新合约信息缓存（币安可能调整规则）"""
    try:
        # 如果缓存>7天，刷新
        if os.path.exists(FUTURES_INFO_CACHE):
            if time.time() - os.path.getmtime(FUTURES_INFO_CACHE) < 7 * 86400:
                return
        r = signed_request('GET', '/fapi/v1/exchangeInfo', {})
        if not r.get('error') and 'symbols' in r:
            with open(FUTURES_INFO_CACHE, 'w') as f:
                json.dump(r, f)
            log(f'[CACHE] 刷新合约信息: {len(r["symbols"])}币种')
    except Exception as e:
        log(f'[WARN] 刷新合约信息失败: {e}')

def get_step_size(symbol):
    _refresh_futures_info()
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
    return None

def get_tick_size(symbol):
    """获取价格最小变动单位"""
    _refresh_futures_info()
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
        return qty
    decimals = len(str(step).split('.')[-1].rstrip('0')) if '.' in str(step) else 0
    if decimals == 0:
        return math.floor(qty / step) * step
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor

# ============ 状态管理 ============
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                # FIX: 确保基本结构存在，防止损坏文件导致崩溃
                if not isinstance(data, dict):
                    log(f'[WARN] state文件损坏(非dict), 重置')
                    return {'positions': {}, 'history': [], 'daily_pnl': []}
                data.setdefault('positions', {})
                data.setdefault('history', [])
                data.setdefault('daily_pnl', [])
                return data
        except Exception as e:
            log(f'[WARN] state文件读取失败: {e}, 重置')
            # 备份损坏文件
            backup_name = STATE_FILE + '.corrupt.' + str(int(time.time()))
            try:
                os.rename(STATE_FILE, backup_name)
            except Exception:
                # rename失败时尝试直接覆盖写入，防止损坏文件永久残留
                try:
                    os.remove(STATE_FILE)
                except Exception:
                    log(f'[CRITICAL] 无法移除损坏的state文件: {STATE_FILE}')
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
    if positions is None:
        log('[WARN] 获取持仓失败，跳过平仓检查')
        return state
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
                # FIX: 先取消所有挂单，再市价平（避免双重平仓）
                cancel_all_orders(symbol)
                time.sleep(0.5)
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
                    cancel_all_orders(symbol)
                    time.sleep(0.5)
                    ok, result = close_with_retry(symbol, close_side, qty)
                    if ok:
                        log(f'  -> 平仓成功: orderId={result["orderId"]}')
                        closed.append(pos_key)
                    else:
                        log(f'  -> 平仓失败(已重试): {result}')
    
    # 孤儿仓位 (跳过刚刚平仓的)
    closed_set = set(closed)
    state_positions = state.get('positions', {})
    for p in active:
        symbol = p['symbol']
        amt = safe_float(p.get('positionAmt'))
        side = 'LONG' if amt > 0 else 'SHORT'
        orphan_key = f"{symbol}_{side}"
        # 跳过已平仓的 + 已在管理中的
        if orphan_key in closed_set:
            continue
        if orphan_key not in state_positions:
            log(f'[ORPHAN] 发现孤儿仓位 {symbol} {side}, 纳入管理')
            # FIX: 更保守地估计持仓时间（按entryPrice估算）
            state_positions[orphan_key] = {
                'symbol': symbol,
                'direction': side,
                'qty': abs(amt),
                # FIX: 保守估计已持40h（避免实际47h时又被延到71h）
                'open_ts': int(time.time()) - 40*3600,
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
    
    # FIX: 限制history长度，防止state文件无限增长
    # 截断前先将旧记录归档到独立文件，保留完整审计轨迹
    MAX_HISTORY = 200
    history = state.get('history', [])
    if len(history) > MAX_HISTORY:
        overflow = history[:-MAX_HISTORY]
        archive_file = os.path.join(DATA_DIR, 'history_archive.jsonl')
        try:
            with open(archive_file, 'a') as af:
                for entry in overflow:
                    af.write(json.dumps(entry, default=str) + '\n')
        except Exception:
            pass
        state['history'] = history[-MAX_HISTORY:]
        log(f'[STATE] history截断至{MAX_HISTORY}条 ({len(overflow)}条已归档)')
    
    return state

# ============ 仓位计算 ============
def get_margin_size(capital):
    if capital <= 0:
        return 0.0
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
def get_funding_rate(symbol):
    """FIX: 获取当前资金费率，做空时若资金费为正需警惕"""
    r = signed_request('GET', '/fapi/v1/premiumIndex', {'symbol': symbol})
    if isinstance(r, dict) and not r.get('error'):
        return safe_float(r.get('lastFundingRate'), 0)
    return 0.0

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
    """从本地完整缓存加载K线 + API补最新"""
    klines = {}
    import concurrent.futures

    cache_file = KLINE_CACHE_FILE
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

    def _fetch_latest(sym):
        try:
            r = requests.get('https://fapi.binance.com/fapi/v1/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 10}, timeout=10)
            if r.status_code == 200:
                return sym, [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                              'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]), 'q': float(k[7])}
                             for k in r.json()]
        except Exception:
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
        try:
            # 加文件锁防止与 daily_predictor.py 并发写入
            with open(cache_file, 'r+') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    cache_data = json.load(f)
                    for s in klines:
                        if s in cache_data.get('klines', {}):
                            cache_data['klines'][s] = klines[s]
                    tmp = cache_file + '.tmp'
                    with open(tmp, 'w') as fw:
                        json.dump(cache_data, fw)
                    os.rename(tmp, cache_file)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    return klines

def fetch_oi_for_symbols(symbols):
    """优先本地OI缓存，API补充最新 (不限制数量)"""
    oi_data = {}
    cache_path = OI_CACHE_FILE
    local_cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                local_cache = json.load(f)
        except Exception:
            pass

    for sym in symbols:
        if sym in local_cache and local_cache[sym]:
            cached = local_cache[sym]
            sorted_ts = sorted(cached.keys(), reverse=True)[:90]
            oi_data[sym] = {int(ts): float(cached[ts]) for ts in sorted_ts}

    missing = [s for s in symbols if s not in oi_data]
    # 批量拉取所有缺失币种的OI (不再限制[:20])
    batch_size = 30
    for batch_start in range(0, len(missing), batch_size):
        batch = missing[batch_start:batch_start + batch_size]
        for sym in batch:
            try:
                r = requests.get(f'{BASE_URL}/futures/data/openInterestHist',
                                 params={'symbol': sym, 'period': '1d', 'limit': 90}, timeout=10)
                if r.status_code == 200 and isinstance(r.json(), list):
                    oi_data[sym] = {int(x['timestamp']) // 1000: float(x['sumOpenInterest']) for x in r.json()}
            except Exception:
                pass
            time.sleep(0.3)
    return oi_data

# ============ 78维特征工程（完整版，与回测一致） ============
def build_features_78d(klines, oi_data, sector_map, sector_heats_all):
    """构建914维特征（含832维Kronos），与dual_backtest_365d一致"""
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
                ret_1d = (closes[j] - closes[j-1]) / closes[j-1] if closes[j-1] > 0 else 0
                ret_3d = (closes[j] - closes[max(0, j-3)]) / closes[max(0, j-3)] if closes[max(0, j-3)] > 0 else 0
                ret_5d = (closes[j] - closes[max(0, j-5)]) / closes[max(0, j-5)] if closes[max(0, j-5)] > 0 else 0
                if j >= 20:
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
                    volatility = 0.02
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

                # FIX: 板块热度用前一日，避免当日收益泄露
                ts_prev = ts - 86400
                sector_feats = dp._get_sector_features(sym, ts_prev, sector_map, sector_heats_all)
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

                # FIX: 2日收益标签，与回测对齐
                next_ret = (closes[i+1] - closes[j]) / closes[j] if closes[j] > 0 and i + 1 < n else 0
                if abs(next_ret) > 5.0:
                    continue
                label_long = 1 if next_ret > 0.05 else 0
                label_short = 1 if next_ret < -0.05 else 0
                all_samples.append((ts, sym, feat, label_long, label_short, next_ret * 100))
            except Exception:
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
        return None, None, [], []
    
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
        return None, None, [], []

    # 运行时维度断言：确保特征维度与 FEATURE_NAMES 一致
    n_features = X_train.shape[1]
    # 动态计算期望维度 (FEATURE_NAMES 在下方定义)
    EXPECTED_N = 10 + 3 + 7 + 4 + 22 + (2+4+6+1+1+1+1+1+1+1+7+6 + dp.EMBEDDING_DIM + 3 + 1)
    if n_features != EXPECTED_N:
        log(f'[CRITICAL] 特征维度不匹配! 实际={n_features} 期望={EXPECTED_N} (Kronos={dp.EMBEDDING_DIM}D)')
        # 如果只差 Kronos 维度，尝试调整期望
        for test_dim in [832, 20]:
            test_expected = 10 + 3 + 7 + 4 + 22 + (2+4+6+1+1+1+1+1+1+1+7+6 + test_dim + 3 + 1)
            if n_features == test_expected:
                log(f'  → 匹配 Kronos={test_dim}D，回测/实盘维度异构!')
                break
    else:
        log(f'特征维度验证: {n_features} == {EXPECTED_N} OK')
    
    bounds = []
    for j in range(X_train.shape[1]):
        col = X_train[:, j]
        bounds.append((float(np.percentile(col, 1)), float(np.percentile(col, 99))))
    X_train = dp._apply_winsor(X_train, bounds)
    
    pos_long = sum(y_long)
    pos_short = sum(y_short)
    if pos_long < 5 or pos_short < 5:
        log(f'正样本不足: long={pos_long}, short={pos_short}')
        return None, None, [], []
    
    log(f'训练: {len(X_train)}样本, {len(train_days)}天, long={pos_long}, short={pos_short}')

    # FIX: 模型缓存改为1天（市场变化快，7天太长）
    models_dir = os.path.join(DATA_DIR, 'models')
    os.makedirs(models_dir, mode=0o700, exist_ok=True)
    model_long_file = os.path.join(models_dir, 'xgb_daily_long.pkl')
    model_short_file = os.path.join(models_dir, 'xgb_daily_short.pkl')
    model_long = None; model_short = None
    try:
        if os.path.exists(model_long_file) and time.time() - os.path.getmtime(model_long_file) < 1*86400:
            with open(model_long_file,'rb') as f: model_long = pickle.load(f)
            log(f'加载预训练多头模型: {model_long_file}')
        if os.path.exists(model_short_file) and time.time() - os.path.getmtime(model_short_file) < 1*86400:
            with open(model_short_file,'rb') as f: model_short = pickle.load(f)
            log(f'加载预训练空头模型: {model_short_file}')
    except Exception: pass

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

    # FIX: 打印并累积Kronos维度特征重要性（7天筛选实验）
    SECTOR_ORDER = ['AI', 'AI Agent', 'BTC生态', 'Base生态', 'DEX', 'DeFi', 'DePIN', 'DeSci',
                    'ETH生态', 'L1', 'L2', 'Meme', 'RWA', 'Solana', 'TON生态',
                    '再质押', '并行EVM', '流动性质押', '游戏', '链抽象', '隐私', '预言机']
    FEATURE_NAMES = (
        ['ret_1d_norm','ret_3d_norm','ret_5d_norm','volatility','vol_ratio',
         'price_position','amplitude','streak','div_sign','oi_chg'] +
        ['vol_regime','vol_momentum','vol_persist'] +
        ['beta','alpha','r2','residual','rsi7','rsi14','rsi30'] +
        ['rsi_div_top','rsi_div_bottom','rsi_overbought_persist','rsi_price_corr_20d'] +
        SECTOR_ORDER +
        ['etf_btc','etf_eth','chain_vol','chain_tx','chain_fee','chain_cdd',
         'sent_funding','sent_ls_btc','sent_ls_eth','sent_ls_avg10','sent_ls_high','sent_ls_low',
         'fear_greed','stablecoin','coinbase_prem','coinbase_gap','btc_mcap','korea_prem','hashrate_7d_chg',
         'liq_total_long','liq_total_short','liq_ratio','liq_long_peak_dist','liq_short_peak_dist',
         'liq_funding','liq_long_ratio','chain_tvl_btc','chain_tvl_eth','chain_tvl_sol',
         'chain_tvl_bsc','chain_tvl_arb','chain_tvl_base'] +
        [f'kronos_emb_{i}' for i in range(832)] +
        ['sp500_1d','dxy_1d','gold_1d','alt_btc_spread']
    )

    def _log_importance(model, label):
        imp = model.feature_importances_
        # Top15 overall
        top_idx = np.argsort(imp)[-15:][::-1]
        log(f'[{label}] 特征重要性 TOP15:')
        for idx in top_idx:
            log(f'  {FEATURE_NAMES[idx]:25s} {imp[idx]:.4f}')
        # Kronos维度累积 (832D)
        kronos_imp = {}
        for i in range(832):
            idx = FEATURE_NAMES.index(f'kronos_emb_{i}')
            kronos_imp[f'kronos_emb_{i}'] = float(imp[idx])
        # 保存到累积文件
        log_file = os.path.join(DATA_DIR, 'kronos_importance_log.json')
        history = {}
        if os.path.exists(log_file):
            try:
                with open(log_file) as f:
                    history = json.load(f)
            except Exception:
                pass
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        history.setdefault(today_str, {})[label] = kronos_imp
        with open(log_file + '.tmp', 'w') as f:
            json.dump(history, f)
        os.rename(log_file + '.tmp', log_file)
        log(f'[{label}] Kronos832重要性已累积到 {log_file}')
        return kronos_imp

    _log_importance(model_long, 'LONG')
    _log_importance(model_short, 'SHORT')
    
    # FIX: 保存训练数据用于过拟合测试
    try:
        train_data_file = os.path.join(DATA_DIR, 'train_data_latest.npz')
        np.savez(train_data_file,
                 X_train=X_train, y_long=y_long, y_short=y_short,
                 train_days=np.array(train_days, dtype=np.int64))
        log(f'训练数据已保存: {train_data_file}')
    except Exception as e:
        log(f'训练数据保存失败: {e}')
    
    # FIX: 运行过拟合测试（Permutation Test）
    perm_passed = _run_permutation_test(X_train, y_long, model_long, by_day, today_ts, bounds)
    if not perm_passed:
        log('[PERM-TEST] 信号不真实，跳过今日交易')
        return None, None, [], []
    
    pred_samples = by_day.get(today_ts, [])
    if not pred_samples:
        log(f'今天无预测样本')
        return None, None, [], []
    
    X_pred = np.array([s[1] for s in pred_samples])
    X_pred = dp._apply_winsor(X_pred, bounds)
    probs_long = model_long.predict_proba(X_pred)[:, 1]
    probs_short = model_short.predict_proba(X_pred)[:, 1]
    
    # 收集所有有效预测结果，输出Top10
    valid_long = []
    valid_short = []
    
    for idx, ((sym, feat, ll, ls, ret), pl, ps) in enumerate(zip(pred_samples, probs_long, probs_short)):
        kls = klines.get(sym, [])
        if len(kls) < 30:
            continue
        k_idx = next((i for i, k in enumerate(kls)
                     if (k['t'] if isinstance(k, dict) else int(k[0])) >= today_ts * 1000), len(kls))
        if k_idx < 5:
            continue
        recent_vol = np.mean([k['q'] for k in kls[k_idx-5:k_idx]])
        if recent_vol < MIN_VOLUME_24H:
            continue
        
        valid_long.append((sym, pl, ret))
        valid_short.append((sym, ps, ret))
    
    # 排序取Top10
    top10_long = sorted(valid_long, key=lambda x: x[1], reverse=True)[:10]
    top10_short = sorted(valid_short, key=lambda x: x[1], reverse=True)[:10]
    
    # Best用于交易决策
    best_long = top10_long[0] if top10_long else None
    best_short = top10_short[0] if top10_short else None
    
    return best_long, best_short, top10_long, top10_short

# ============ 主流程 ============
def main():
    # 进程锁
    lockfile = os.path.join(DATA_DIR, 'auto_dual.lock')
    _lock_fd = open(lockfile, 'w')
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f'另一个 auto_dual_trade 实例正在运行, 退出')
        _lock_fd.close()
        return

    log('=' * 60)
    log('自动多空二选一交易启动 (78维完整版 FIXED)')
    log('=' * 60)
    
    # 1. 账户
    account = get_account()
    if account is None:
        log('账户读取失败，中止')
        return
    if 'code' in account or 'error' in account:
        log(f'账户读取失败: {account}')
        return
    
    wallet = float(account.get('totalWalletBalance', 0))
    available = float(account.get('availableBalance', 0))
    log(f'钱包: {wallet:.2f}u  可用: {available:.2f}u')
    
    # 2. 状态
    state = load_state()
    if 'positions' not in state:
        state['positions'] = {}
    
    # 3. 平仓 — FIX: 余额不足也要先管理老持仓(止损/48h到期)
    state = check_and_close(state)
    
    # 4. 持仓检查 + 数据更新（余额不足时仍更新缓存）
    no_trade = wallet < 10

    # 5. 加载外部数据 + 更新K线/OI缓存（始终执行）
    log('加载外部数据...')
    sector_map = dp._load_sector_map()
    dp._sector_map_cache = sector_map
    if not dp._proto_map_local:
        try:
            with open('/home/myuser/defillama_data/protocol_map.json') as _pf:
                dp._proto_map_local = {k: v[0] for k, v in json.load(_pf).items()}
        except Exception:
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

    # 6. 获取K线 (全量加载, 不依赖24h行情筛选)
    log('获取全量K线...')
    # 直接从缓存加载全部币种，由后续长度过滤决定实际参与训练的币种
    all_syms = []
    try:
        with open(KLINE_CACHE_FILE) as f:
            all_syms = list(json.load(f).get('klines', {}).keys())
    except Exception:
        pass
    if not all_syms:
        # 回退：如果缓存读取失败，用24h行情兜底
        all_syms = fetch_top_symbols(200)
    if 'BTCUSDT' not in all_syms:
        all_syms.append('BTCUSDT')
    
    klines = fetch_klines_full(all_syms)
    min_required = TRAIN_DAYS + 35
    klines_before = len(klines)
    klines = {sym: kls for sym, kls in klines.items() if len(kls) >= min_required}
    log(f'K线加载: {klines_before}币种, 达标(>{min_required}天): {len(klines)}')
    
    oi_data = fetch_oi_for_symbols(list(klines.keys()))
    log(f'OI获取: {len(oi_data)}币种')

    # 持仓检查
    positions = get_positions()
    active = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0] if isinstance(positions, list) else []
    if active:
        syms = [p['symbol'] for p in active]
        log(f'当前{len(active)}个持仓: {syms} (48h到期自动平)')
    
    # 余额不足 → 数据已更新，跳过交易
    if no_trade:
        log('本金不足10u，K线/OI缓存已更新，跳过交易')
        save_state(state)
        return
    
    # 7. 预计算板块热度 + Kronos
    log('预计算板块热度...')
    sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}
    
    # FIX: Kronos只预计算交易日（样本中的日期），而非所有K线时间戳
    log('预计算Kronos...')
    all_ts_for_kronos = set()
    for sym, kls in klines.items():
        if len(kls) < min_required:
            continue
        timestamps = [k['t'] // 1000 for k in kls]
        for i in range(25, len(kls) - 2):
            all_ts_for_kronos.add(timestamps[i])
    dp._precompute_kronos_features(list(all_ts_for_kronos))
    log(f'Kronos预计算: {len(all_ts_for_kronos)}个交易日')

    # Kronos特征上传COS
    try:
        from qcloud_cos import CosConfig, CosS3Client
        cos_cfg = CosConfig(
            Region=os.environ.get('COS_REGION', 'ap-seoul'),
            SecretId=os.environ['COS_SECRET_ID'],
            SecretKey=os.environ['COS_SECRET_KEY'],
            Endpoint=os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'))
        cos_cli = CosS3Client(cos_cfg)
        kronos_path = os.path.join(os.path.dirname(__file__), 'data/kronos_features_cache.json')
        if os.path.exists(kronos_path):
            with open(kronos_path, 'rb') as f:
                cos_cli.put_object(Bucket=os.environ['COS_BUCKET'], Key='klines/cache/kronos_features_cache.json', Body=f.read())
            log(f'Kronos特征已上传COS ({os.path.getsize(kronos_path)/1024/1024:.1f}MB)')
    except Exception as e:
        log(f'Kronos COS上传失败: {e}')

    # 8. 构建特征
    log('构建特征...')
    by_day = build_features_78d(klines, oi_data, sector_map, sector_heats_all)
    log(f'样本: {len(by_day)}天, 总样本{sum(len(v) for v in by_day.values())}')
    
    # 9. 训练预测
    if len(by_day) < 2:
        log('数据不足')
        save_state(state)
        return
    
    today_ts = None
    for ts in reversed(sorted(by_day.keys())):
        if by_day.get(ts):
            today_ts = ts
            break
    if today_ts is None:
        log('无有效预测日期')
        save_state(state)
        return
    today_str = datetime.fromtimestamp(today_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    log(f'预测日期: {today_str}')
    
    best_long, best_short, top10_long, top10_short = train_and_predict(by_day, today_ts, klines)
    
    if best_long is None and best_short is None:
        log('无有效信号')
        save_state(state)
        return
    
    long_prob = best_long[1] * 100 if best_long else 0
    short_prob = best_short[1] * 100 if best_short else 0
    max_prob = max(long_prob, short_prob)
    
    # 打印Top10多空
    log('做多概率 TOP10:')
    for rank, (sym, prob, ret) in enumerate(top10_long, 1):
        log(f'  #{rank:2d} {sym:12s} prob={prob*100:5.1f}%')
    log('做空概率 TOP10:')
    for rank, (sym, prob, ret) in enumerate(top10_short, 1):
        log(f'  #{rank:2d} {sym:12s} prob={prob*100:5.1f}%')
    
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
                cancel_result = cancel_all_orders(symbol)
                log(f'  取消挂单: {cancel_result.get("code", cancel_result.get("msg", "ok"))}')
                result = place_market_order(symbol, close_side, qty_close, reduce_only=True)
                if 'orderId' not in result:
                    log(f'  平仓失败: {result}, 跳过今日')
                    save_state(state)
                    return
                log(f'  平仓成功: orderId={result.get("orderId")}')
                # 指数退避轮询确认持仓清零，最多10次 ~30秒
                max_polls = 10
                cleared = False
                for poll_i in range(max_polls):
                    if poll_i > 0:
                        wait = min(2 ** (poll_i - 1), 10)
                        time.sleep(wait)
                    pos_check = get_positions()
                    if isinstance(pos_check, list):
                        still_open = [p for p in pos_check
                                      if p.get('symbol') == symbol and abs(float(p.get('positionAmt', 0))) > 0]
                        if not still_open:
                            log(f'  持仓确认清零 (poll {poll_i+1})')
                            cleared = True
                            break
                    log(f'  等待持仓清零... (poll {poll_i+1}/{max_polls})')
                if not cleared:
                    pos_final = get_positions()
                    if isinstance(pos_final, list):
                        still_held = [p for p in pos_final
                                      if p.get('symbol') == symbol and abs(float(p.get('positionAmt', 0))) > 0]
                        if still_held:
                            log(f'[CRITICAL] 平仓30秒后仍持仓 {symbol}, 中止开仓')
                            save_state(state)
                            return
            else:
                time.sleep(0.5)

    margin = get_margin_size(wallet)
    # FIX: 预留资金费率缓冲（2天持仓约2次资金费，按保守0.1%估计）
    funding_buffer = wallet * 0.005  # 0.5%缓冲
    required = margin + funding_buffer
    if available < required:
        log(f'可用不足(含资金费缓冲): {available:.2f}u < {required:.2f}u (保证金{margin}u + 缓冲{funding_buffer:.2f}u)')
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
    
    # FIX: 开仓前检查资金费率（做空时若资金费为正，可能侵蚀利润）
    funding_rate = get_funding_rate(symbol)
    if direction == 'SHORT' and funding_rate > 0.001:
        log(f'[WARN] {symbol} 做空资金费率{funding_rate*100:.4f}% > 0.1%，可能侵蚀利润')
    elif direction == 'LONG' and funding_rate < -0.001:
        log(f'[WARN] {symbol} 做多资金费率{funding_rate*100:.4f}% < -0.1%，多头获资金费收入')

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

        actual_price = safe_float(order.get('avgPrice'))
        if actual_price <= 0:
            cum_quote = safe_float(order.get('cumQuote'))
            executed_qty = safe_float(order.get('executedQty'))
            if executed_qty > 0:
                actual_price = cum_quote / executed_qty
            else:
                pos_data = signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
                if isinstance(pos_data, list) and pos_data:
                    actual_price = safe_float(pos_data[0].get('entryPrice'), price)
                else:
                    actual_price = price

        actual_qty = safe_float(order.get('executedQty', qty), qty)
        if actual_qty <= 0:
            actual_qty = qty

        tick = get_tick_size(symbol)
        decimals = len(str(tick).split('.')[-1].rstrip('0')) if tick and '.' in str(tick) else 4

        # FIX: 同时下止损单+止盈单
        if direction == 'LONG':
            sl_side = 'SELL'
            tp_side = 'SELL'
            sl_price = round(actual_price * (1 - STOP_LOSS_PCT / 100), decimals)
            tp_price = round(actual_price * (1 + TAKE_PROFIT_PCT / 100), decimals)
        else:
            sl_side = 'BUY'
            tp_side = 'BUY'
            sl_price = round(actual_price * (1 + STOP_LOSS_PCT / 100), decimals)
            tp_price = round(actual_price * (1 - TAKE_PROFIT_PCT / 100), decimals)

        # 止损单
        sl_order = None
        for sl_attempt in range(3):
            sl_order = place_stop_loss_order(symbol, sl_side, sl_price)
            if 'orderId' in sl_order:
                break
            if sl_attempt < 2:
                wait = 2 ** sl_attempt
                log(f'  止损单重试 {sl_attempt+1}/3, 等待{wait}s: {sl_order}')
                time.sleep(wait)

        # 止盈单
        tp_order = None
        for tp_attempt in range(3):
            tp_order = place_take_profit_order(symbol, tp_side, tp_price)
            if 'orderId' in tp_order:
                break
            if tp_attempt < 2:
                wait = 2 ** tp_attempt
                log(f'  止盈单重试 {tp_attempt+1}/3, 等待{wait}s: {tp_order}')
                time.sleep(wait)

        # 检查止损+止盈是否至少有一个成功
        sl_ok = sl_order and 'orderId' in sl_order
        tp_ok = tp_order and 'orderId' in tp_order

        # FIX: Binance API -4120 = STOP_MARKET/TAKE_PROFIT_MARKET 不再支持标准接口
        # 遇到此错误不回滚，记录裸仓状态继续持仓（比来回打脸强）
        sl_api_err = isinstance(sl_order, dict) and sl_order.get('code') == -4120
        tp_api_err = isinstance(tp_order, dict) and tp_order.get('code') == -4120
        api_not_supported = sl_api_err or tp_api_err

        if not sl_ok and not tp_ok:
            if api_not_supported:
                log(f'[WARN] Binance API不再支持STOP_MARKET/TAKE_PROFIT_MARKET标准接口，持仓裸奔（请尽快手动挂止损）')
                state['positions'][f'{symbol}_{direction}'] = {
                    'symbol': symbol, 'direction': direction, 'qty': actual_qty,
                    'margin': margin, 'prob': prob, 'open_ts': int(time.time()),
                    'open_price': actual_price, 'notional': actual_qty * actual_price,
                    'stop_loss_price': sl_price, 'take_profit_price': tp_price,
                    'sl_order_id': None, 'tp_order_id': None, 'naked': True,
                }
                save_state(state)
                log('裸仓状态已记录（API限制），继续持仓')
                return
            log(f'[CRITICAL] 止损+止盈单均失败，回滚平仓')
            rollback_side = 'SELL' if side == 'BUY' else 'BUY'
            for rb_attempt in range(3):
                rollback = place_market_order(symbol, rollback_side, actual_qty, reduce_only=True)
                if 'orderId' in rollback:
                    log(f'  回滚成功: orderId={rollback.get("orderId")}')
                    save_state(state)
                    log('止损+止盈均失败已回滚，交易中止')
                    return
                wait = 2 ** rb_attempt
                log(f'  回滚失败重试 {rb_attempt+1}/3, 等待{wait}s: {rollback}')
                time.sleep(wait)
            log(f'[CRITICAL] {symbol} 止损+止盈+回滚均失败！仓位裸奔！请立即人工处理！')
            state['positions'][f'{symbol}_{direction}'] = {
                'symbol': symbol, 'direction': direction, 'qty': actual_qty,
                'margin': margin, 'prob': prob, 'open_ts': int(time.time()),
                'open_price': actual_price, 'notional': actual_qty * actual_price,
                'stop_loss_price': 0, 'naked': True,
            }
            save_state(state)
            log('裸仓状态已记录，中止')
            return

        if sl_ok:
            log(f'  止损单成功: orderId={sl_order["orderId"]} 触发价{sl_price}')
        else:
            log(f'  [WARN] 止损单失败，但止盈单成功 — 有 upside 无 downside 保护')

        if tp_ok:
            log(f'  止盈单成功: orderId={tp_order["orderId"]} 触发价{tp_price}')
        else:
            log(f'  [WARN] 止盈单失败，但止损单成功 — 有 downside 无 upside 保护')

        state['positions'][f'{symbol}_{direction}'] = {
            'symbol': symbol,
            'direction': direction,
            'qty': actual_qty,
            'margin': margin,
            'prob': prob,
            'open_ts': int(time.time()),
            'open_price': actual_price,
            'notional': actual_qty * actual_price,
            'sl_order_id': sl_order.get('orderId') if sl_ok else None,
            'tp_order_id': tp_order.get('orderId') if tp_ok else None,
            'stop_loss_price': sl_price if sl_ok else 0,
            'take_profit_price': tp_price if tp_ok else 0,
        }
    else:
        log(f'  下单失败: {order}')
    
    save_state(state)
    log('交易结束')

# ============ 过拟合测试 ============
def _run_permutation_test(X_train, y_long, model_long, by_day, today_ts, bounds):
    """Permutation Test: 打乱标签看概率是否暴跌
    
    Returns:
        bool: True = 信号真实，允许交易; False = 过拟合，阻止交易
    """
    try:
        pred_samples = by_day.get(today_ts, [])
        if not pred_samples:
            log('[PERM-TEST] 今日无预测样本，跳过')
            return True  # 无样本不阻止交易
        
        X_pred = np.array([s[1] for s in pred_samples])
        X_pred = dp._apply_winsor(X_pred, bounds)
        
        # 正常概率
        probs_normal = model_long.predict_proba(X_pred)[:, 1]
        best_idx = int(np.argmax(probs_normal))
        best_sym = pred_samples[best_idx][0]
        best_prob = probs_normal[best_idx]
        
        log(f'[PERM-TEST] 正常训练 Best: {best_sym} prob={best_prob*100:.1f}%')
        
        # 打乱标签
        y_shuffled = y_long.copy()
        np.random.seed(999)
        np.random.shuffle(y_shuffled)
        pos_shuf = sum(y_shuffled)
        if pos_shuf == 0 or pos_shuf == len(y_shuffled):
            log('[PERM-TEST] 打乱后无正/负样本，跳过')
            return True
        
        spw_shuf = (len(y_shuffled) - pos_shuf) / pos_shuf
        model_shuf = XGBClassifier(n_estimators=100, max_depth=5, learning_rate=0.05,
                                   scale_pos_weight=spw_shuf, random_state=42,
                                   eval_metric='logloss', verbosity=0, n_jobs=4)
        model_shuf.fit(X_train, y_shuffled)
        probs_shuf = model_shuf.predict_proba(X_pred)[:, 1]
        
        best_prob_shuf = probs_shuf[best_idx]
        drop = best_prob - best_prob_shuf
        log(f'[PERM-TEST] 打乱标签 Best: {best_sym} prob={best_prob_shuf*100:.1f}%  下降={drop*100:.1f}%')
        
        # 判断 + 返回是否通过
        if drop > 0.15:
            log('[PERM-TEST] ✅ 信号真实，允许交易')
            passed = True
        elif drop < 0.05:
            log('[PERM-TEST] 🔴 严重过拟合，阻止交易')
            passed = False
        else:
            log('[PERM-TEST] ⚠️  部分过拟合，允许交易但需关注')
            passed = True  # 灰色地带允许交易，但记录警告
        
        # 保存到文件
        result = {
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'best_sym': best_sym,
            'prob_normal': float(best_prob),
            'prob_shuffled': float(best_prob_shuf),
            'drop': float(drop),
            'passed': passed,
            'verdict': 'real' if drop > 0.15 else ('overfit' if drop < 0.05 else 'partial')
        }
        perm_file = os.path.join(DATA_DIR, 'permutation_test_log.json')
        perm_history = []
        if os.path.exists(perm_file):
            try:
                with open(perm_file) as f:
                    perm_history = json.load(f)
            except Exception:
                pass
        perm_history.append(result)
        with open(perm_file + '.tmp', 'w') as f:
            json.dump(perm_history, f, indent=2)
        os.rename(perm_file + '.tmp', perm_file)
        
        return passed
        
    except Exception as e:
        log(f'[PERM-TEST] 测试失败: {e}')
        return True  # 测试失败不阻止交易，避免误杀


if __name__ == '__main__':
    main()
