#!/usr/bin/env python3
"""
自动多空二选一交易脚本
每天运行：检查持仓(止损/2天平仓) → 训练+预测 → 开仓+止损单+止盈单
特征维度: 104维 (纯手工特征, Kronos已禁用 — Permutation Test验证通过)
"""
import os, sys, json, time, hmac, hashlib, math, warnings, fcntl, pickle, traceback, gc
from datetime import datetime, timezone
from urllib.parse import urlencode
from array import array
from multiprocessing import Pool, cpu_count
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

# Kronos toggle: False = BASELINE 104D (Permutation Test验证通过), True = 936D
USE_KRONOS = False

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
     'TOP_N_SYMBOLS': 150, 'MIN_VOLUME_24H': 500000, 'TRAIN_DAYS': 180,
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
    """FIX: 使用 Algo Order API — /fapi/v1/algoOrder
    closePosition=true平仓全部, workingType=MARK_PRICE按标记价格触发
    """
    return signed_request('POST', '/fapi/v1/algoOrder', {
        'algoType': 'CONDITIONAL',
        'symbol': symbol,
        'side': side,
        'type': 'STOP_MARKET',
        'triggerPrice': stop_price,
        'closePosition': 'true',
        'workingType': 'MARK_PRICE',
        'priceProtect': 'true',
    })

def place_take_profit_order(symbol, side, take_price):
    """FIX: 使用 Algo Order API — /fapi/v1/algoOrder
    closePosition=true平仓全部, workingType=MARK_PRICE按标记价格触发
    """
    return signed_request('POST', '/fapi/v1/algoOrder', {
        'algoType': 'CONDITIONAL',
        'symbol': symbol,
        'side': side,
        'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': take_price,
        'closePosition': 'true',
        'workingType': 'MARK_PRICE',
        'priceProtect': 'true',
    })

def cancel_all_orders(symbol):
    """取消某币种所有挂单 + Algo条件单"""
    signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})
    signed_request('DELETE', '/fapi/v1/algoOpenOrders', {'symbol': symbol})

def get_open_algo_orders(symbol):
    """查询某币种所有未触发的 Algo 条件单"""
    r = signed_request('GET', '/fapi/v1/algoOpenOrders', {'symbol': symbol})
    if isinstance(r, list):
        return r
    return []

def verify_algo_order(symbol, algo_id, max_wait=10, interval=1.5):
    """轮询确认 algo_id 确实在交易所未触发订单列表中。
    返回 (verified, order_info)
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        orders = get_open_algo_orders(symbol)
        for o in orders:
            if o.get('algoId') == algo_id or o.get('orderId') == algo_id:
                return True, o
        time.sleep(interval)
    return False, None

def place_and_verify_algo_order(place_fn, symbol, side, trigger_price, max_attempts=3):
    """下 Algo 条件单并验证它真的挂在交易所。
    place_fn: place_stop_loss_order 或 place_take_profit_order
    返回 (ok, order_dict)
    """
    last_order = None
    for attempt in range(max_attempts):
        order = place_fn(symbol, side, trigger_price)
        last_order = order
        oid = order.get('algoId') or order.get('orderId') if order else None
        if not oid:
            log(f'  Algo单未返回ID: {order}')
            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                time.sleep(wait)
            continue

        verified, info = verify_algo_order(symbol, oid, max_wait=8, interval=1.0)
        if verified:
            log(f'  Algo单已确认挂在交易所: algoId={oid}')
            return True, order
        else:
            log(f'  Algo单未在openOrders中找到，可能未挂成功: algoId={oid}')
            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                log(f'  Algo单重试 {attempt+1}/{max_attempts}, 等待{wait}s')
                time.sleep(wait)
            else:
                # 最后一次也没找到，返回最后一次下单结果（可能交易所延迟）
                return False, last_order
    return False, last_order

def _rearm_stop_loss(symbol, side, amt, entry_price):
    """平仓失败后重新挂Algo止损单，防止仓位裸奔"""
    try:
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        if side == 'LONG':
            stop_price = round(entry_price * (1 - STOP_LOSS_PCT / 100), 2)
        else:
            stop_price = round(entry_price * (1 + STOP_LOSS_PCT / 100), 2)
        if stop_price <= 0:
            log(f'  [REARM] {symbol} 止损价异常={stop_price}, 跳过')
            return
        ok, order = place_and_verify_algo_order(
            place_stop_loss_order, symbol, close_side, stop_price)
        if ok:
            log(f'  [REARM] {symbol} Algo止损已重新挂单 @{stop_price}')
        else:
            log(f'  [REARM] {symbol} Algo止损重挂失败! 仓位裸奔!')
    except Exception as e:
        log(f'  [REARM] {symbol} 止损重挂异常: {e}')

def poll_order_fill(symbol, order_id, max_wait=15, interval=1.5):
    """轮询市价单成交状态，返回最终 order 状态。
    MARKET 单可能先返回 NEW，需要轮询直到 FILLED / PARTIALLY_FILLED / EXPIRED / CANCELED / REJECTED。
    """
    deadline = time.time() + max_wait
    final_order = None
    while time.time() < deadline:
        order = signed_request('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id})
        if order and 'orderId' in order:
            final_order = order
            status = order.get('status')
            if status in ('FILLED', 'PARTIALLY_FILLED', 'EXPIRED', 'CANCELED', 'REJECTED'):
                return order
        time.sleep(interval)
    return final_order


def place_market_order_with_retry(symbol, side, qty, reduce_only=False, max_retries=3):
    """下市价单并轮询成交，支持部分成交后补单。
    返回 (success_bool, last_order_dict, total_filled_qty)
    """
    remaining = max(qty, 0)
    total_filled = 0.0
    last_order = None
    for attempt in range(max_retries):
        if remaining <= 0:
            return True, last_order or {'orderId': 'completed', 'status': 'FILLED'}, total_filled

        order = place_market_order(symbol, side, remaining, reduce_only)
        last_order = order
        if not order or 'orderId' not in order:
            log(f'  下单失败(无orderId): {order}')
            wait = 2 ** attempt
            log(f'  重试 {attempt+1}/{max_retries}, 等待{wait}s')
            time.sleep(wait)
            continue

        # 轮询确认成交
        polled = poll_order_fill(symbol, order['orderId'])
        if polled:
            last_order = polled
            status = polled.get('status')
            if status in ('FILLED', 'PARTIALLY_FILLED'):
                filled = safe_float(polled.get('executedQty'), 0)
                total_filled += filled
                if total_filled >= qty * 0.98:
                    return True, polled, total_filled
                remaining = max(qty - total_filled, 0)
                log(f'  部分成交 {filled:.6f}/{qty:.6f}, 累计{total_filled:.6f}, 补单剩余{remaining:.6f}')
                time.sleep(1)
                continue
            elif status == 'REJECTED':
                log(f'  订单被拒: {polled}')
                return False, polled, total_filled
            else:  # EXPIRED / CANCELED
                log(f'  订单失效: {status} {polled}')
                wait = 2 ** attempt
                log(f'  重试 {attempt+1}/{max_retries}, 等待{wait}s')
                time.sleep(wait)
                continue
        else:
            log(f'  轮询超时未获取最终状态: order={order}')
            wait = 2 ** attempt
            time.sleep(wait)
            continue

    return False, last_order, total_filled


def close_with_retry(symbol, close_side, qty, max_retries=3):
    """平仓市价单, 最多重试3次, 验证成交数量 >= 95%
    FIX: remaining下限保护，防止负数
    """
    success, result, total_filled = place_market_order_with_retry(
        symbol, close_side, qty, reduce_only=True, max_retries=max_retries)
    if success and total_filled >= qty * 0.95:
        return True, result
    log(f'  平仓最终失败/未足量: filled={total_filled:.6f}/{qty:.6f}')
    return False, result or {'error': True, 'msg': 'close failed'}

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
        
        # 孤儿仓位: 纳入管理但不强制平仓 (用户手动管理止损/止盈)
        is_orphan = pos_key not in state.get('positions', {}) or \
                    state['positions'].get(pos_key, {}).get('source') == 'orphan'
        if is_orphan:
            continue
        
        if ret_pct <= -STOP_LOSS_PCT:
            close_side = 'SELL' if side == 'LONG' else 'BUY'
            qty = round_qty(symbol, abs(amt))
            if qty > 0:
                log(f'[STOP LOSS] {symbol} {side} 亏损{ret_pct:.2f}% 平{qty}')
                # 撤旧Algo单 → 市价平 → 失败则重新设止损
                cancel_all_orders(symbol)
                time.sleep(0.5)
                ok, result = close_with_retry(symbol, close_side, qty)
                if ok:
                    log(f'  -> 平仓成功: orderId={result["orderId"]}')
                    closed.append(pos_key)
                else:
                    log(f'  -> 平仓失败(已重试): {result}, 重新设Algo止损')
                    _rearm_stop_loss(symbol, side, amt, entry_price)
            continue

        if pos_key in state.get('positions', {}):
            open_ts = state['positions'][pos_key].get('open_ts') or 0
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
                        log(f'  -> 平仓失败(已重试): {result}, 重新设Algo止损')
                        _rearm_stop_loss(symbol, side, amt, entry_price)
    
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
                # 孤儿仓位: open_ts 从当前开始计算48h, 不回溯猜测
                'open_ts': int(time.time()),
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
# ---- 多进程特征构建支持（模块级函数，供子进程pickle调用） ----
_mp_shared = None  # (btc_rets, sector_map, sector_heats_all) 只读共享数据

def _mp_init_shared(shared):
    """子进程初始化：接收只读共享数据"""
    global _mp_shared
    _mp_shared = shared

def _build_feat_impl(sym, kls, oi_map, btc_rets, sector_map, sector_heats_all):
    """单币种特征计算实现（串行和并行共用）
    返回 [(ts, sym, array('f', feat), label_long, label_short, next_ret_pct), ...]
    """
    results = []
    if len(kls) < 35:
        return results
    closes = [k['c'] for k in kls]
    opens = [k['o'] for k in kls]
    highs = [k['h'] for k in kls]
    lows = [k['l'] for k in kls]
    vols = [k['q'] for k in kls]
    timestamps = [k['t'] // 1000 for k in kls]
    coin_rets = dp._compute_returns(closes)
    n = len(kls)

    # FIX: n-2 → n-1, 包含预测样本 i=n-2 (最新已收盘K线, 无标签)
    for i in range(25, n - 1):
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
            # v3新增: 90天回看特征
            rsi90 = dp._compute_rsi(closes, 90, j) if j >= 90 else 50.0
            r90 = [(closes[k] - closes[k-1]) / closes[k-1] if closes[k-1] > 0 else 0 for k in range(j-88, j+1)] if j >= 90 else [0]
            vol_90d = float(np.std(r90)) if j >= 90 else 0.02
            c90 = closes[j-89:j+1] if j >= 90 else [0, 1]
            pp_90 = (closes[j] - min(c90)) / (max(c90) - min(c90)) if j >= 90 and max(c90) != min(c90) else 0.5
            ret_30d = (closes[j] - closes[max(0, j-30)]) / closes[max(0, j-30)] if closes[max(0, j-30)] > 0 else 0
            ret_60d = (closes[j] - closes[max(0, j-60)]) / closes[max(0, j-60)] if closes[max(0, j-60)] > 0 else 0
            ret_90d = (closes[j] - closes[max(0, j-90)]) / closes[max(0, j-90)] if closes[max(0, j-90)] > 0 else 0
            rsi14_series = dp._compute_rsi_series(closes, 14)
            rsi_div = dp._compute_rsi_divergence(closes, rsi14_series, j, window=20)
            vol_col = dp._compute_vol_clustering(closes, j)

            feat = assemble_feature_vec(
                ret_1d_norm, ret_3d_norm, ret_5d_norm,
                volatility, vol_ratio, price_position, amplitude, streak, div_sign, oi_chg,
                vol_col, beta, alpha, r2, residual, rsi7, rsi14, rsi30,
                rsi_div, sector_feats, macro_feats,
                rsi90, vol_90d, pp_90, ret_30d, ret_60d, ret_90d)

            # FIX: 预测样本(i=n-2)无未来收盘价, 标签设0; 训练样本计算2日收益标签
            if i >= n - 2:
                next_ret = 0  # 预测样本, 无标签 (closes[i+1]未收盘)
            else:
                next_ret = (closes[i+1] - closes[j]) / closes[j] if closes[j] > 0 and i + 1 < n else 0
            if abs(next_ret) > 5.0:
                continue
            label_long = 1 if next_ret > 0.05 else 0
            label_short = 1 if next_ret < -0.05 else 0
            results.append((ts, sym, array('f', feat), label_long, label_short, next_ret * 100))
        except Exception:
            continue
    return results

def _mp_build_features_for_symbol(task):
    """多进程worker：从_mp_shared获取共享数据，调用_build_feat_impl"""
    sym, kls, oi_map = task
    btc_rets, sector_map, sector_heats_all = _mp_shared
    return _build_feat_impl(sym, kls, oi_map, btc_rets, sector_map, sector_heats_all)

def build_features_78d(klines, oi_data, sector_map, sector_heats_all):
    """构建特征，与dual_backtest_365d一致
    优化: feat用array('f') float32, 去掉all_samples中间list, 省~3GB
    多进程: CPU>=8时并行处理各币种（观察机16核），<8时串行（生产端4核）"""
    by_day = defaultdict(list)
    btc_kls = klines.get('BTCUSDT', [])
    btc_closes = [k['c'] for k in btc_kls]
    btc_rets = dp._compute_returns(btc_closes) if btc_closes else []

    n_cpu = cpu_count()
    if n_cpu >= 8:
        # 多进程并行模式（观察机16核: 15 workers, 留1核给系统）
        tasks = [(sym, kls, oi_data.get(sym, {})) for sym, kls in klines.items()]
        shared = (btc_rets, sector_map, sector_heats_all)
        n_workers = min(n_cpu - 1, 15)
        log(f'特征构建: 多进程模式 {n_workers} workers (CPU={n_cpu})')
        with Pool(n_workers, initializer=_mp_init_shared, initargs=(shared,)) as pool:
            all_results = pool.map(_mp_build_features_for_symbol, tasks, chunksize=4)
        for sym_results in all_results:
            for ts, sym, feat, ll, ls, nr in sym_results:
                by_day[ts].append((sym, feat, ll, ls, nr))
        log(f'特征构建完成: {len(by_day)} 天, {sum(len(v) for v in by_day.values())} 样本')
    else:
        # 串行模式（生产端4核）
        log(f'特征构建: 串行模式 (CPU={n_cpu})')
        for sym, kls in klines.items():
            oi_map = oi_data.get(sym, {})
            sym_results = _build_feat_impl(sym, kls, oi_map, btc_rets, sector_map, sector_heats_all)
            for ts, s, feat, ll, ls, nr in sym_results:
                by_day[ts].append((s, feat, ll, ls, nr))
    return by_day

# ============ 训练预测 ============
def train_and_predict(by_day, today_ts, klines):
    sorted_days = sorted(by_day.keys())
    train_days = [ts for ts in sorted_days if ts < today_ts]
    
    if len(train_days) < 15:
        log(f'训练数据不足: {len(train_days)}天')
        return None, None, [], []
    
    train_days = train_days[-TRAIN_DAYS:]

    # Kronos toggle: set False to run BASELINE (104D, no Kronos)
    KRONOS_START = 10 + 3 + 7 + 4 + 22 + (2+4+6+1+1+1+1+1+1+1+26+9)  # = 100
    KRONOS_END = KRONOS_START + dp.EMBEDDING_DIM  # = 932

    X_train, y_long, y_short = [], [], []
    for ts in train_days:
        for sym, feat, ll, ls, ret in by_day[ts]:
            X_train.append(feat)
            y_long.append(ll)
            y_short.append(ls)
        del by_day[ts]
    gc.collect()
    
    X_train = np.array(X_train, dtype=np.float32)
    # 修复NaN (SP500/DXY/黄金列在某些历史日期缺失)
    _nan_count = int(np.isnan(X_train).sum())
    if _nan_count > 0:
        log(f'⚠️ 训练数据有{_nan_count}个NaN, 已填充为0')
    X_train = np.nan_to_num(X_train, nan=0.0, copy=False)
    
    # 训练数据完整性校验
    _validation_issues = []
    # 1. 全零行检查 (整行特征全零的样本)
    _zero_rows = np.where(np.all(X_train == 0, axis=1))[0]
    if len(_zero_rows) > 0:
        _validation_issues.append(f'{len(_zero_rows)}个全零样本行')
        log(f'⚠️ 发现{len(_zero_rows)}个全零样本行, 已剔除')
        _keep = np.ones(len(X_train), dtype=bool)
        _keep[_zero_rows] = False
        X_train = X_train[_keep]
        y_long = [y_long[i] for i in range(len(y_long)) if _keep[i]]
        y_short = [y_short[i] for i in range(len(y_short)) if _keep[i]]
    
    # 2. 异常全零列检查 (排除置零区域)
    if not USE_KRONOS:
        X_train[:, KRONOS_START:KRONOS_END] = 0.0
        X_train[:, 72:91] = 0.0   # liq 19维→回退78D
        log('Kronos 832D + liq 19D 已置零 (non-Kronos: 85D)')
    else:
        log('Kronos 832D 已置零 (BASELINE 104D)')
    
    _expected_zero = set(range(KRONOS_START, KRONOS_END)) | set(range(72, 91)) if not USE_KRONOS else set(range(KRONOS_START, KRONOS_END))
    _zero_cols = np.where(np.all(X_train == 0, axis=0))[0]
    _unexpected_zero = [c for c in _zero_cols if c not in _expected_zero]
    if len(_unexpected_zero) > 5:
        _validation_issues.append(f'{len(_unexpected_zero)}个异常全零列(前10: {_unexpected_zero[:10]})')
        log(f'⚠️ 发现{len(_unexpected_zero)}个异常全零列: {_unexpected_zero[:10]}')
    
    # 3. 极端值检查 (单元素绝对值>1e6)
    _extreme_count = int(np.sum(np.abs(X_train) > 1e6))
    if _extreme_count > 0:
        _validation_issues.append(f'{_extreme_count}个极端值(>1e6)')
        log(f'⚠️ 发现{_extreme_count}个极端值, winsor将处理')
    
    if _validation_issues:
        log(f'训练数据校验: {"; ".join(_validation_issues)}')
    else:
        log('训练数据校验: 全部通过')
    
    if len(X_train) < 100:
        log(f'样本不足: {len(X_train)}')
        return None, None, [], []

    # 运行时维度断言
    n_features = X_train.shape[1]
    EXPECTED_N = 10 + 3 + 7 + 4 + 22 + (2+4+6+1+1+1+1+1+1+1+26+9 + dp.EMBEDDING_DIM + 3 + 1)
    if n_features != EXPECTED_N:
        log(f'[CRITICAL] 特征维度不匹配! 实际={n_features} 期望={EXPECTED_N}')
    else:
        log(f'特征维度验证: {n_features} == {EXPECTED_N} OK')
    
    bounds = dp._fast_winsor_bounds(X_train)
    X_train = dp._apply_winsor(X_train, bounds)
    
    pos_long = sum(y_long)
    pos_short = sum(y_short)
    if pos_long < 5 or pos_short < 5:
        log(f'正样本不足: long={pos_long}, short={pos_short}')
        return None, None, [], []
    
    log(f'训练: {len(X_train)}样本, {len(train_days)}天, long={pos_long}, short={pos_short}')

    # 每次运行都重新训练模型（不使用缓存）
    models_dir = os.path.join(DATA_DIR, 'models')
    os.makedirs(models_dir, mode=0o700, exist_ok=True)
    model_long_file = os.path.join(models_dir, 'xgb_daily_long.pkl')
    model_short_file = os.path.join(models_dir, 'xgb_daily_short.pkl')
    n_features = X_train.shape[1]

    # Top1参数 (90d Sharpe=7.98, 180d Sharpe=6.00): d6-w1-L10-A10-s0.8-c0.6
    log('训练多头模型...')
    model_long = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                               min_child_weight=1, reg_lambda=10, reg_alpha=10,
                               subsample=0.8, colsample_bytree=0.6,
                               scale_pos_weight=(len(y_long) - pos_long) / pos_long,
                               random_state=42, eval_metric='logloss', verbosity=0,
                               tree_method='hist')
    model_long.fit(X_train, y_long)
    try:
        with open(model_long_file,'wb') as f: pickle.dump(model_long, f)
    except Exception as e: log(f'多头模型保存失败: {e}')

    log('训练空头模型...')
    model_short = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                                min_child_weight=1, reg_lambda=10, reg_alpha=10,
                                subsample=0.8, colsample_bytree=0.6,
                                scale_pos_weight=(len(y_short) - pos_short) / pos_short,
                                random_state=43, eval_metric='logloss', verbosity=0,
                                tree_method='hist')
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
         # 清算特征 26维: 5分位 × (long_mean, long_std, short_mean, short_std) + ratio/peak_dist 的 mean/std
         'liq_q0_long_mean','liq_q0_long_std','liq_q0_short_mean','liq_q0_short_std',
         'liq_q1_long_mean','liq_q1_long_std','liq_q1_short_mean','liq_q1_short_std',
         'liq_q2_long_mean','liq_q2_long_std','liq_q2_short_mean','liq_q2_short_std',
         'liq_q3_long_mean','liq_q3_long_std','liq_q3_short_mean','liq_q3_short_std',
         'liq_q4_long_mean','liq_q4_long_std','liq_q4_short_mean','liq_q4_short_std',
         'liq_ratio_mean','liq_ratio_std','liq_long_peak_dist_mean','liq_long_peak_dist_std',
         'liq_short_peak_dist_mean','liq_short_peak_dist_std',
         # TVL 9维
         'chain_tvl_btc','chain_tvl_eth','chain_tvl_sol','chain_tvl_bsc','chain_tvl_arb',
         'chain_tvl_base','chain_tvl_ton','chain_tvl_sui','chain_tvl_polygon'] +
        [f'kronos_emb_{i}' for i in range(dp.EMBEDDING_DIM)] +
        ['sp500_1d','dxy_1d','gold_1d','alt_btc_spread'] +
        # v3新增: 90天回看特征
        ['rsi90','vol_90d','pp_90','ret_30d','ret_60d','ret_90d']
    )

    def _log_importance(model, label):
        imp = model.feature_importances_
        # Top15 overall
        top_idx = np.argsort(imp)[-15:][::-1]
        log(f'[{label}] 特征重要性 TOP15:')
        for idx in top_idx:
            name = FEATURE_NAMES[idx] if idx < len(FEATURE_NAMES) else f'unnamed_feat_{idx}'
            log(f'  {name:25s} {imp[idx]:.4f}')
        # Kronos维度累积 (832D)
        kronos_imp = {}
        for i in range(dp.EMBEDDING_DIM):
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
    
    # FIX: 运行过拟合测试（Permutation Test）— 多空双测，按边阻断
    long_ok, short_ok = _run_permutation_test(X_train, y_long, y_short, model_long, model_short, by_day, today_ts, bounds, klines)
    
    pred_samples = by_day.get(today_ts, [])
    if not pred_samples:
        log(f'今天无预测样本')
        return None, None, [], []
    
    X_pred = np.array([s[1] for s in pred_samples])
    X_pred = np.nan_to_num(X_pred, nan=0.0, copy=False)
    X_pred = dp._apply_winsor(X_pred, bounds)
    # 与回溯端对齐: Kronos 832D + liq 19D 置零
    if not USE_KRONOS:
        KRONOS_START_PRED = 100
        KRONOS_END_PRED = KRONOS_START_PRED + dp.EMBEDDING_DIM  # 932
        X_pred[:, KRONOS_START_PRED:KRONOS_END_PRED] = 0.0
        X_pred[:, 72:91] = 0.0   # liq 19维置零
    probs_long = model_long.predict_proba(X_pred)[:, 1]
    probs_short = model_short.predict_proba(X_pred)[:, 1]

    # 收集所有有效预测结果，输出Top10 (复用过滤函数, 与Perm Test保持同一份样本集)
    valid_samples, valid_indices = _filter_valid_samples(pred_samples, klines, today_ts)
    valid_long = [(s[0], probs_long[i], s[4]) for s, i in zip(valid_samples, valid_indices)]
    valid_short = [(s[0], probs_short[i], s[4]) for s, i in zip(valid_samples, valid_indices)]

    # 排序取Top10
    top10_long = sorted(valid_long, key=lambda x: x[1], reverse=True)[:10]
    top10_short = sorted(valid_short, key=lambda x: x[1], reverse=True)[:10]
    
    # Best用于交易决策
    best_long = top10_long[0] if top10_long else None
    best_short = top10_short[0] if top10_short else None
    
    # Permutation Test 按边阻断：只禁失败侧，不波及对侧
    if not long_ok:
        log('[PERM-TEST] LONG 过拟合，禁止多头交易')
        best_long = None
        top10_long = []
    if not short_ok:
        log('[PERM-TEST] SHORT 过拟合，禁止空头交易')
        best_short = None
        top10_short = []
    
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
    log('自动多空二选一交易启动 (104维 BASELINE, Kronos已禁用)')
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

    # 4.5 数据新鲜度检查: 若OI/K线陈旧(>20h), 自动补采 (冗余机制)
    try:
        import time as _time
        _stale_threshold = 20 * 3600  # 20小时
        _now = _time.time()
        _oi_path = '/home/myuser/backtester/data_cache/oi_daily.json'
        _kline_path = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
        _need_refresh = False
        if not os.path.exists(_oi_path) or (_now - os.path.getmtime(_oi_path)) > _stale_threshold:
            _need_refresh = True
            log(f'OI缓存陈旧, 触发补采: {_oi_path}')
        if not os.path.exists(_kline_path) or (_now - os.path.getmtime(_kline_path)) > _stale_threshold:
            _need_refresh = True
            log(f'K线缓存陈旧, 触发补采: {_kline_path}')
        if _need_refresh:
            try:
                import daily_data_collection as _ddc
                log('开始补采K线+OI...')
                _ddc.update_klines_oi()
                log('补采完成')
            except Exception as _e:
                log(f'补采失败(继续使用现有数据): {_e}')
    except Exception as _e:
        log(f'数据新鲜度检查失败: {_e}')

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
    min_required = 90  # v3: 90天门槛（新币alpha更多，Sharpe 5.80 vs 400天2.30）
    klines_before = len(klines)
    klines = {sym: kls for sym, kls in klines.items() if len(kls) >= min_required}
    log(f'K线加载: {klines_before}币种, 达标(>{min_required}天): {len(klines)}')
    
    oi_data = fetch_oi_for_symbols(list(klines.keys()))
    log(f'OI获取: {len(oi_data)}币种')

    # 持仓检查 + 每日同步: 清除 state.json 中交易所已不存在的持仓记录
    positions = get_positions()
    active = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0] if isinstance(positions, list) else []
    if active:
        syms = [p['symbol'] for p in active]
        log(f'当前{len(active)}个持仓: {syms} (48h到期自动平)')
    
    # 同步: 币安无持仓但state有记录的 → 清理
    binance_keys = {f"{p['symbol']}_{'LONG' if float(p.get('positionAmt',0))>0 else 'SHORT'}"
                    for p in active}
    stale = [k for k in state.get('positions', {}) if k not in binance_keys]
    if stale:
        for k in stale:
            del state['positions'][k]
            log(f'[SYNC] 清理过期持仓记录: {k}')
        save_state(state)
    
    # FIX: 余额不足时仍然训练模型，保持模型最新，只是不执行交易
    # 7. 预计算板块热度 + Kronos
    log('预计算板块热度...')
    try:
        sector_heats_all = dp._precompute_sector_heats(klines, sector_map) if sector_map else {}
        log(f'板块热度完成: {len(sector_heats_all)}天')
    except Exception as e:
        log(f'板块热度失败: {e}')
        import traceback
        log(traceback.format_exc())
        sector_heats_all = {}
    
    # FIX: Kronos只预计算交易日（样本中的日期），而非所有K线时间戳
    if USE_KRONOS:
        log('预计算Kronos...')
        all_ts_for_kronos = set()
        for sym, kls in klines.items():
            if len(kls) < min_required:
                continue
            timestamps = [k['t'] // 1000 for k in kls]
            for i in range(25, len(kls) - 1):  # FIX: n-2 → n-1, 包含预测日
                all_ts_for_kronos.add(timestamps[i])
        dp._precompute_kronos_features(list(all_ts_for_kronos))
        log(f'Kronos预计算: {len(all_ts_for_kronos)}个交易日')
    else:
        log('Kronos已禁用, 跳过预计算')

    # Kronos特征上传COS
    if USE_KRONOS:
        try:
            from qcloud_cos import CosConfig, CosS3Client
            cos_cfg = CosConfig(
                Region=os.environ.get('COS_REGION', 'ap-seoul'),
                SecretId=os.environ['COS_SECRET_ID'],
                SecretKey=os.environ['COS_SECRET_KEY'],
                Endpoint=os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'),
                Timeout=30)
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
    
    # 9. 训练预测（无论余额是否充足，都训练模型保持最新）
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
    
    # 预测存档：保存到 data/pred_YYYY-MM-DD.json (每天只存第一版)
    try:
        pred_archive_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
        os.makedirs(pred_archive_dir, exist_ok=True)
        pred_archive = {
            'date': today_str,
            'updated': time.time(),
            'best_long': {'symbol': best_long[0], 'prob': round(best_long[1]*100, 1)} if best_long else None,
            'best_short': {'symbol': best_short[0], 'prob': round(best_short[1]*100, 1)} if best_short else None,
            'top10_long': [{'symbol': s, 'prob': round(p*100, 1)} for s, p, r in top10_long],
            'top10_short': [{'symbol': s, 'prob': round(p*100, 1)} for s, p, r in top10_short],
        }
        archive_file = os.path.join(pred_archive_dir, f'pred_{today_str}.json')
        if not os.path.exists(archive_file):
            with open(archive_file, 'w') as f:
                json.dump(pred_archive, f, indent=2, default=str)
            log(f'预测存档: {archive_file}')
        # 同步更新 daily_predictions.json (供Web监控读取)
        cache_file = os.path.join(pred_archive_dir, 'daily_predictions.json')
        with open(cache_file, 'w') as f:
            json.dump(pred_archive, f, default=str)
    except Exception as e:
        log(f'预测存档失败: {e}')
    
    # COS备份：模型+训练数据+重要性日志 (跟随每日训练同步上传)
    try:
        from qcloud_cos import CosConfig, CosS3Client
        _cos = CosS3Client(CosConfig(
            Region=os.environ.get('COS_REGION', 'ap-seoul'),
            SecretId=os.environ['COS_SECRET_ID'],
            SecretKey=os.environ['COS_SECRET_KEY'],
            Endpoint=os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com'),
            Timeout=30))
        _bucket = os.environ['COS_BUCKET']
        backups = [
            (os.path.join(DATA_DIR, 'models', 'xgb_daily_long.pkl'), 'klines/cache/xgb_daily_long.pkl', '多头模型'),
            (os.path.join(DATA_DIR, 'models', 'xgb_daily_short.pkl'), 'klines/cache/xgb_daily_short.pkl', '空头模型'),
            (os.path.join(DATA_DIR, 'train_data_latest.npz'), 'klines/cache/train_data_latest.npz', '训练数据'),
            (os.path.join(DATA_DIR, 'kronos_importance_log.json'), 'klines/cache/kronos_importance_log.json', '重要性日志'),
        ]
        for local_path, cos_key, label in backups:
            if os.path.exists(local_path) and time.time() - os.path.getmtime(local_path) < 600:
                with open(local_path, 'rb') as f:
                    _cos.put_object(Bucket=_bucket, Key=cos_key, Body=f.read())
                log(f'{label}已备份COS')
    except Exception as e:
        log(f'COS备份上传失败: {e}')
    
    if best_long is None and best_short is None:
        log('无有效信号')
        save_state(state)
        return
    
    # 余额不足 → 模型已训练更新，跳过交易执行
    if no_trade:
        log('本金不足10u，模型已更新，跳过交易')
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
    
    long_thresh, short_thresh, threshold_reason = get_adaptive_thresholds()
    log(f'多空阈值: LONG={long_thresh:.0f}% SHORT={short_thresh:.0f}% ({threshold_reason})')

    # 概率高的优先, 被阈值挡了则尝试另一个方向, 都不行则空仓
    if long_prob >= short_prob:
        candidates = [('LONG', long_prob, long_thresh, best_long), ('SHORT', short_prob, short_thresh, best_short)]
    else:
        candidates = [('SHORT', short_prob, short_thresh, best_short), ('LONG', long_prob, long_thresh, best_long)]

    direction = symbol = side = prob = None
    for d, p, thresh, best in candidates:
        if best is not None and p >= thresh:
            direction = d
            symbol = best[0]
            side = 'BUY' if d == 'LONG' else 'SELL'
            prob = p
            log(f'{d}信号: {symbol} {p:.1f}% >= {thresh:.0f}%')
            break

    if direction is None:
        log(f'置信度不足: LONG {long_prob:.1f}%<{long_thresh:.0f}% SHORT {short_prob:.1f}%<{short_thresh:.0f}%, 空仓')
        save_state(state)
        return
    
    # 检查同币种已有持仓方向
    existing = next((p for p in active if p['symbol'] == symbol), None)
    if existing:
        existing_amt = float(existing.get('positionAmt', 0))
        existing_dir = 'LONG' if existing_amt > 0 else 'SHORT'
        # orphan仓位: 用户手动管理, 不平仓, 放弃当天交易
        existing_key = f"{symbol}_{existing_dir}"
        if state.get('positions', {}).get(existing_key, {}).get('source') == 'orphan':
            log(f'[ORPHAN] {symbol} 为孤儿仓位(用户手动管理), 跳过平仓, 放弃今日交易')
            save_state(state)
            return
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
                ok, result, filled_qty = place_market_order_with_retry(
                    symbol, close_side, qty_close, reduce_only=True, max_retries=3)
                if not ok or filled_qty <= 0:
                    log(f'  平仓失败: {result}, 跳过今日')
                    save_state(state)
                    return
                log(f'  平仓成功: orderId={result.get("orderId")} filled={filled_qty:.6f}')
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

    # 市价单 (轮询确认成交 — 币安市价单可能立即返回NEW未成交，支持部分成交补单)
    success, order, actual_qty = place_market_order_with_retry(symbol, side, qty, reduce_only=False, max_retries=3)
    if not success or actual_qty <= 0:
        log(f'  下单失败或成交量为0: {order}')
        save_state(state)
        return

    log(f'  市价单成功: orderId={order.get("orderId")} status={order.get("status")} filled={actual_qty:.6f}')

    # 用实际成交数量作为持仓数量
    actual_price = safe_float(order.get('avgPrice'))
    if actual_price <= 0:
        cum_quote = safe_float(order.get('cumQuote'))
        if actual_qty > 0:
            actual_price = cum_quote / actual_qty
        else:
            pos_data = signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
            if isinstance(pos_data, list) and pos_data:
                actual_price = safe_float(pos_data[0].get('entryPrice'), price)
            else:
                actual_price = price

    # 如果 last_order 是补单后的最后一单，avgPrice 可能不代表整体均价，
    # 这里以 positionRisk 的 entryPrice 为准（更准确）
    pos_data = signed_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
    if isinstance(pos_data, list) and pos_data:
        entry_price = safe_float(pos_data[0].get('entryPrice'), 0)
        if entry_price > 0:
            actual_price = entry_price

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

    # 止损单 — Algo Order API，下单后验证真的挂在交易所
    sl_ok, sl_order = place_and_verify_algo_order(
        place_stop_loss_order, symbol, sl_side, sl_price, max_attempts=3)

    # 止盈单 — Algo Order API，下单后验证真的挂在交易所
    tp_ok, tp_order = place_and_verify_algo_order(
        place_take_profit_order, symbol, tp_side, tp_price, max_attempts=3)

    # 兜底: 止损止盈均失败不自动回滚，持仓裸奔 (API挂了也不该自动平仓)

    if not sl_ok and not tp_ok:
        state['positions'][f'{symbol}_{direction}'] = {
            'symbol': symbol, 'direction': direction, 'qty': actual_qty,
            'margin': margin, 'prob': prob, 'open_ts': int(time.time()),
            'open_price': actual_price, 'notional': actual_qty * actual_price,
            'stop_loss_price': 0, 'naked': True,
        }
        save_state(state)
        log('[WARN] 止损+止盈均失败，持仓裸奔，不自动回滚')
        return

    if sl_ok:
        log(f'  止损单成功: algoId={sl_order.get("algoId", sl_order.get("orderId"))} 触发价{sl_price}')
    else:
        log(f'  [WARN] 止损单失败，但止盈单成功')

    if tp_ok:
        log(f'  止盈单成功: algoId={tp_order.get("algoId", tp_order.get("orderId"))} 触发价{tp_price}')
    else:
        log(f'  [WARN] 止盈单失败，但止损单成功')

    state['positions'][f'{symbol}_{direction}'] = {
        'symbol': symbol,
        'direction': direction,
        'qty': actual_qty,
        'margin': margin,
        'prob': prob,
        'open_ts': int(time.time()),
        'open_price': actual_price,
        'notional': actual_qty * actual_price,
        'sl_order_id': sl_order.get('algoId') or sl_order.get('orderId') if sl_ok else None,
        'tp_order_id': tp_order.get('algoId') or tp_order.get('orderId') if tp_ok else None,
        'stop_loss_price': sl_price if sl_ok else 0,
        'take_profit_price': tp_price if tp_ok else 0,
    }

    save_state(state)
    log('交易结束')

# ============ 过拟合测试 ============
def _filter_valid_samples(pred_samples, klines, today_ts):
    """过滤预测样本: 剔除K线不足/位置不足/低成交量的币种
    返回 (valid_samples, valid_indices) — 与train_and_predict过滤逻辑完全一致
    确保Perm Test与交易决策使用同一份样本集, Best结果一致
    """
    valid_samples = []
    valid_indices = []
    for idx, (sym, feat, ll, ls, ret) in enumerate(pred_samples):
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
        valid_samples.append((sym, feat, ll, ls, ret))
        valid_indices.append(idx)
    return valid_samples, valid_indices


def _run_permutation_test(X_train, y_long, y_short, model_long, model_short, by_day, today_ts, bounds, klines):
    """Permutation Test: 打乱标签看概率是否暴跌 (多空双测, 按边返回)

    Returns:
        (long_ok, short_ok): 各边是否通过，任一边失败调用方只禁该侧

    注意: 使用与交易决策相同的过滤后样本(成交量达标)做argmax,
    确保Perm Test Best = 交易Best, 避免日志与存档不一致
    """
    try:
        pred_samples = by_day.get(today_ts, [])
        if not pred_samples:
            log('[PERM-TEST] 今日无预测样本，跳过')
            return True
        
        X_pred = np.array([s[1] for s in pred_samples])
        X_pred = np.nan_to_num(X_pred, nan=0.0, copy=False)
        X_pred = dp._apply_winsor(X_pred, bounds)
        # 与回溯端对齐: Kronos 832D + liq 19D 置零
        if not USE_KRONOS:
            KRONOS_START_PRED = 100
            KRONOS_END_PRED = KRONOS_START_PRED + dp.EMBEDDING_DIM  # 932
            X_pred[:, KRONOS_START_PRED:KRONOS_END_PRED] = 0.0
            X_pred[:, 72:91] = 0.0   # liq 19维置零
        
        # 过滤样本: 与交易决策使用同一份样本集, 确保Best一致
        valid_samples, valid_indices = _filter_valid_samples(pred_samples, klines, today_ts)
        if not valid_indices:
            log('[PERM-TEST] 过滤后无有效样本(成交量均不达标)，跳过')
            return True

        def _test_side(label, y_true, model_normal, random_state):
            """测试单边 (LONG/SHORT), 返回 (passed, best_sym, prob_normal, prob_shuf, drop)
            基于过滤后样本argmax, 与交易Best保持一致
            """
            probs_normal = model_normal.predict_proba(X_pred)[:, 1]
            # 只在过滤后的样本中取argmax
            probs_valid = probs_normal[valid_indices]
            best_local = int(np.argmax(probs_valid))
            best_idx = valid_indices[best_local]
            best_sym = pred_samples[best_idx][0]
            best_prob = probs_normal[best_idx]

            y_shuf = y_true.copy()
            np.random.seed(999)
            np.random.shuffle(y_shuf)
            pos_shuf = sum(y_shuf)
            if pos_shuf == 0 or pos_shuf == len(y_shuf):
                log(f'[PERM-TEST] {label} 打乱后无正/负样本，跳过')
                return True, best_sym, best_prob, 0.0, 1.0

            spw = (len(y_shuf) - pos_shuf) / pos_shuf
            # Top1参数: d6-w1-L10-A10-s0.8-c0.6
            model_shuf = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                                       min_child_weight=1, reg_lambda=10, reg_alpha=10,
                                       subsample=0.8, colsample_bytree=0.6,
                                       scale_pos_weight=spw, random_state=random_state,
                                       eval_metric='logloss', verbosity=0, n_jobs=2,
                                       tree_method='hist')
            model_shuf.fit(X_train, y_shuf)
            best_prob_shuf = model_shuf.predict_proba(X_pred)[best_idx, 1]
            
            drop = best_prob - best_prob_shuf
            if drop > 0.15:
                verdict = 'real'
                passed = True
            elif drop < 0.05:
                verdict = 'overfit'
                passed = False
            else:
                verdict = 'partial'
                passed = True
            
            log(f'[PERM-TEST] [{label}] Best={best_sym} normal={best_prob*100:.1f}% shuf={best_prob_shuf*100:.1f}% drop={drop*100:+.1f}% → {verdict}')
            return passed, best_sym, best_prob, best_prob_shuf, drop
        
        passed_long, sym_l, prob_l, prob_ls, drop_l = _test_side('LONG', y_long, model_long, 42)
        passed_short, sym_s, prob_s, prob_ss, drop_s = _test_side('SHORT', y_short, model_short, 43)
        
        overall = passed_long and passed_short
        
        # 保存到文件
        result = {
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'long': {'sym': sym_l, 'normal': float(prob_l), 'shuffled': float(prob_ls), 'drop': float(drop_l), 'passed': passed_long},
            'short': {'sym': sym_s, 'normal': float(prob_s), 'shuffled': float(prob_ss), 'drop': float(drop_s), 'passed': passed_short},
            'passed': overall
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
        
        if not overall:
            failed_sides = []
            if not passed_long: failed_sides.append('LONG')
            if not passed_short: failed_sides.append('SHORT')
            sides = '+'.join(failed_sides)
            log(f'[PERM-TEST] {sides} 过拟合 (对侧不受影响)')
        else:
            log('[PERM-TEST] 多空信号真实，允许交易')
        
        return passed_long, passed_short
        
    except Exception as e:
        log(f'[PERM-TEST] 测试失败: {e}')
        import traceback
        log(traceback.format_exc())
        return True, True  # 测试失败不阻止交易，避免误杀


CRASH_FILE = '/tmp/auto_dual_trade_crash.json'
CRASH_LOG = '/tmp/auto_dual_trade_crash.log'
SUCCESS_FILE = '/tmp/auto_dual_trade_success.json'


def get_adaptive_thresholds(base_threshold=None):
    """根据最近验证准确率分别调整多空置信度阈值

    返回 (long_threshold, short_threshold, reason)
    从 prediction_tracker.json 的 details 里分别计算多空命中率, 各自独立调阈:
      - 连续3天0%命中 → 100% (禁止该方向)
      - 7天平均命中率<15% → 70% (保守)
      - 7天平均命中率>=35% → 55% (激进)
      - 其他 → 默认阈值
    """
    if base_threshold is None:
        base_threshold = PROB_THRESHOLD

    track_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'prediction_tracker.json')
    if not os.path.exists(track_file):
        return base_threshold, base_threshold, '无验证数据, 用默认阈值'

    try:
        with open(track_file) as f:
            tracker = json.load(f)
    except Exception:
        return base_threshold, base_threshold, '验证数据读取失败, 用默认阈值'

    if not tracker or len(tracker) < 3:
        return base_threshold, base_threshold, f'验证数据不足({len(tracker)}条), 用默认阈值'

    recent = sorted(tracker, key=lambda t: t.get('date', ''))[-7:]

    def calc_side(direction):
        """计算某方向的7天命中率和连续0%天数"""
        rates = []
        for t in recent:
            details = t.get('details', [])
            total = sum(1 for d in details if d.get('direction') == direction)
            hits = sum(1 for d in details if d.get('direction') == direction and d.get('hit'))
            if total > 0:
                rates.append(hits / total * 100)
        if not rates:
            return 0, 0  # 无数据

        avg = sum(rates) / len(rates)

        consecutive_zero = 0
        for r in reversed(rates):
            if r == 0:
                consecutive_zero += 1
            else:
                break
        return avg, consecutive_zero

    avg_long, consec_long_zero = calc_side('LONG')
    avg_short, consec_short_zero = calc_side('SHORT')

    def adjust(avg, consec_zero, label):
        if consec_zero >= 3:
            return 100.0, f'{label}连续{consec_zero}天0%命中, 禁止{label}'
        elif avg < 15:
            return 70.0, f'{label}命中率{avg:.0f}%<15%, 提高'
        elif avg >= 35:
            return 55.0, f'{label}命中率{avg:.0f}%>=35%, 降低'
        else:
            return base_threshold, f'{label}命中率{avg:.0f}%, 正常'

    long_thresh, long_reason = adjust(avg_long, consec_long_zero, 'LONG')
    short_thresh, short_reason = adjust(avg_short, consec_short_zero, 'SHORT')

    reason = f'{long_reason}; {short_reason}'
    return long_thresh, short_thresh, reason


def send_daily_report():
    """每日训练预测完成后发送日报邮件"""
    try:
        from alert_monitor import send_email
    except Exception:
        return

    today_str = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(DATA_DIR, 'trade.log')
    try:
        with open(log_file, 'r') as f:
            today_lines = [l.strip() for l in f if today_str in l]
    except Exception:
        return

    if not today_lines:
        return

    keywords = ['钱包', 'ORPHAN', '当前', '训练:', 'PERM-TEST', '做多概率', '做空概率',
                'prob=', '置信度', '空仓', '开仓:', '跳过交易', '本金不足', '多空信号',
                '预测日期', '样本:', '板块热度', '特征维度', '极端值']
    key_lines = []
    for line in today_lines:
        clean = line.split('] ', 1)[-1] if '] ' in line else line
        if any(kw in clean for kw in keywords):
            key_lines.append(clean)

    if not key_lines:
        key_lines = [l.split('] ', 1)[-1] if '] ' in l else l for l in today_lines[-30:]]

    # 验证2天前预测 + 复盘
    verify_summary = ''
    try:
        # 修正 daily_predictor 的 LOG_DIR 指向实际 pred 文件目录
        dp.LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
        dp.TRACK_FILE = os.path.join(dp.LOG_DIR, 'prediction_tracker.json')
        dp.verify_yesterday()
        if os.path.exists(dp.TRACK_FILE):
            with open(dp.TRACK_FILE) as f:
                tracker = json.load(f)
            if tracker:
                last = sorted(tracker, key=lambda t: t.get('date', ''))[-1]
                recent = tracker[-7:]
                avg_7d = sum(t.get('hit_rate', 0) for t in recent) / len(recent)
                adj_long, adj_short, adj_reason = get_adaptive_thresholds()
                verify_summary = (
                    f"\n\n=== 2日验证 ({last['date']}) ===\n"
                    f"TOP10命中: {last.get('top10_hits',0)}/10\n"
                    f"总命中率: {last.get('hit_rate',0)}%\n"
                    f"TOP10收益: {last.get('top10_return',0):+.1f}%\n"
                    f"\n=== 准确率趋势 ===\n"
                    f"自适应阈值: LONG={adj_long:.0f}% SHORT={adj_short:.0f}%\n"
                    f"  ({adj_reason})\n"
                )
    except Exception as e:
        log(f'验证复盘失败: {e}')

    body = f'=== {today_str} 每日预测日报 ===\n\n' + '\n'.join(key_lines) + verify_summary

    try:
        send_email(f'每日预测日报 {today_str}', body, priority='info')
        log('日报邮件已发送')
    except Exception as e:
        log(f'日报邮件发送失败: {e}')


if __name__ == '__main__':
    try:
        main()
        # 记录成功运行时间 + 清除旧的crash标记
        try:
            with open(SUCCESS_FILE, 'w') as f:
                json.dump({
                    'last_success': datetime.now(timezone.utc).isoformat(),
                    'status': 'ok'
                }, f, indent=2)
            # 清除残留crash文件, 避免guardian误报
            with open(CRASH_FILE, 'w') as f:
                json.dump({'crashed': False, 'timestamp': datetime.now(timezone.utc).isoformat()}, f)
        except Exception:
            pass
        # 发送每日预测日报
        send_daily_report()
    except Exception as e:
        ts = datetime.now(timezone.utc).isoformat()
        tb = traceback.format_exc()
        # 结构化 crash 文件 (供 guardian / 监控读取)
        try:
            with open(CRASH_FILE, 'w') as f:
                json.dump({
                    'crashed': True,
                    'timestamp': ts,
                    'error_type': type(e).__name__,
                    'error_msg': str(e),
                    'traceback': tb,
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # 人类可读 crash 日志
        try:
            with open(CRASH_LOG, 'a') as f:
                f.write(f'[{ts}] CRASH: {type(e).__name__}: {e}\n')
                f.write(tb)
                f.write('\n' + '=' * 60 + '\n')
        except Exception:
            pass
        # 也打印到 stderr，让 cron 邮件/日志能捕获
        print(f'[CRASH] {type(e).__name__}: {e}', file=sys.stderr)
        print(tb, file=sys.stderr)
        sys.exit(1)
