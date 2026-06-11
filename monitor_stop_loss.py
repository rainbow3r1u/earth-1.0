#!/usr/bin/env python3
"""
后台实时监控止损
每30秒检查持仓，浮亏>=10%立即市价平仓
"""
import os, sys, json, time, hmac, hashlib
from datetime import datetime
from urllib.parse import urlencode
import requests

sys.path.insert(0, os.path.dirname(__file__))

with open('.env') as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '') or os.environ.get('BINANCE_SECRET_KEY', '')
BASE_URL = 'https://fapi.binance.com'
STOP_LOSS_PCT = 10.0
CHECK_INTERVAL = 30  # 秒

# Cache step sizes to avoid reading /tmp/binance_futures_info.json on every call
_step_cache = {}
_step_cache_time = 0

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    import re as _re
    msg = _re.sub(r'signature=[A-Za-z0-9]+', 'signature=***', str(msg))
    line = f'[{ts}] {msg}'
    print(line)
    with open('/tmp/monitor_stop_loss.log', 'a') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(line + '\n')

# NOTE: signed_request() now matches auto_dual_trade.py's implementation
# (retries, 429/418/5xx handling, exponential backoff)
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
                log(f'  429 Rate Limited, 等待{retry_after}s')
                time.sleep(retry_after)
                continue
            if r.status_code == 418:
                retry_after = int(r.headers.get('Retry-After', 300))
                log(f'  418 IP Banned, 等待{retry_after}s')
                time.sleep(retry_after)
                continue
            if r.status_code >= 500:
                wait = min(2 ** attempt, 30)
                log(f'  {r.status_code} Server Error, 等待{wait}s')
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return {'error': True, 'http_code': r.status_code, 'msg': r.text[:200]}

            return r.json()

        except (requests.exceptions.RequestException, ValueError) as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 30)
                log(f'  请求异常: {e}, 等待{wait}s')
                time.sleep(wait)
            continue

    return {'error': True, 'msg': last_error or 'max retries exceeded'}

def get_positions():
    return signed_request('GET', '/fapi/v2/positionRisk')

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

def round_qty(symbol, qty):
    global _step_cache, _step_cache_time
    # Reload cache every 3600 seconds or if symbol not found
    if not _step_cache or time.time() - _step_cache_time > 3600 or symbol not in _step_cache:
        cache_file = '/tmp/binance_futures_info.json'
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                data = json.load(f)
            for s in data.get('symbols', []):
                for filt in s.get('filters', []):
                    if filt['filterType'] == 'LOT_SIZE':
                        _step_cache[s['symbol']] = float(filt['stepSize'])
            _step_cache_time = time.time()
    step = _step_cache.get(symbol)
    if step is None:
        return qty
    if step == 0:
        return qty
    decimals = len(str(step).split('.')[-1].rstrip('0')) if '.' in str(step) else 0
    if decimals == 0:
        result = int(qty)
        return max(result, step) if result < step else result
    factor = 10 ** decimals
    return int(qty * factor) / factor

def main():
    log('=' * 60)
    log('后台止损监控启动')
    log('=' * 60)
    
    while True:
        try:
            positions = get_positions()
            if not isinstance(positions, list):
                log(f'API返回异常(非列表): {positions}')
                time.sleep(CHECK_INTERVAL)
                continue
            active = [p for p in positions if abs(float(p.get('positionAmt', 0))) > 0]
            
            if not active:
                log('无持仓，继续监控...')
            else:
                for p in active:
                    symbol = p['symbol']
                    amt = float(p['positionAmt'])
                    entry = float(p.get('entryPrice', 0) or 0)
                    if entry <= 0:
                        log(f'[WARN] {symbol} entryPrice异常={p.get("entryPrice")}, 跳过')
                        continue
                    mark = float(p['markPrice'])
                    pnl = float(p['unRealizedProfit'])
                    side = 'LONG' if amt > 0 else 'SHORT'
                    notional = abs(amt) * mark
                    
                    if side == 'LONG':
                        ret_pct = (mark - entry) / entry * 100
                        close_side = 'SELL'
                    else:
                        ret_pct = (entry - mark) / entry * 100
                        close_side = 'BUY'
                    
                    log(f'{symbol} {side} 名义:{notional:.1f}u 盈亏:{ret_pct:+.2f}% 浮亏:{pnl:+.2f}u')
                    
                    if ret_pct <= -STOP_LOSS_PCT:
                        qty = round_qty(symbol, abs(amt))
                        log(f'>>> 触发止损！{symbol} {side} 亏损{ret_pct:.2f}% 市价平仓 {qty}')
                        result = place_market_order(symbol, close_side, qty, reduce_only=True)
                        # 轮询确认成交 (市价单可能返回NEW)
                        if 'orderId' in result and result.get('status') == 'NEW':
                            oid = result['orderId']
                            for pi in range(5):
                                time.sleep(1)
                                result = signed_request('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': oid})
                                if result and result.get('status') in ('FILLED', 'PARTIALLY_FILLED', 'EXPIRED', 'CANCELED'):
                                    break
                        if 'orderId' in result and result.get('status') in ('FILLED', 'PARTIALLY_FILLED'):
                            log(f'>>> 平仓成功: orderId={result["orderId"]}')
                            # 取消该symbol所有挂单 (残留的止损单)
                            cancel = signed_request('DELETE', '/fapi/v1/allOpenOrders', {'symbol': symbol})
                            log(f'>>> 取消挂单: {cancel.get("code", cancel.get("msg", "ok"))}')
                        else:
                            log(f'>>> 平仓失败: {result}')
        except KeyboardInterrupt:
            log('接收到 Ctrl+C, 退出监控')
            break
        except Exception as e:
            log(f'异常: {e}')
        
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()
