"""
币安账户信息获取模块
获取合约账户余额和持仓盈亏
支持 REST API 和 WebSocket 两种方式
"""
import time
import hmac
import hashlib
import requests
import json
import threading
import websocket
import datetime
from urllib.parse import urlencode
from typing import Dict, List, Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs import config
from utils.logger import get_logger

logger = get_logger('binance_account')


class BinanceAccount:
    _cache: Optional[Dict] = None
    _cache_time: Optional[float] = None
    _cache_ttl: int = 5
    _banned_until: float = 0  # 全局熔断时间戳
    _lock = threading.Lock()
    
    @classmethod
    def _generate_signature(cls, params: dict, timestamp: int) -> str:
        encoded = urlencode({k: v for k, v in sorted(params.items()) if v is not None})
        query_string = f"timestamp={timestamp}&{encoded}" if encoded else f"timestamp={timestamp}"

        signature = hmac.new(
            config.BINANCE_SECRET_KEY.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return signature, query_string
    
    @classmethod
    def _request(cls, endpoint: str, params: dict = None) -> Optional[dict]:
        # 检查是否在熔断期
        if time.time() < cls._banned_until:
            logger.debug(f"IP 处于熔断期，拦截 REST 请求: {endpoint}")
            return None
            
        timestamp = int(time.time() * 1000)
        
        if params is None:
            params = {}
        
        signature, query_string = cls._generate_signature(params, timestamp)
        url = f"{config.BINANCE_API}{endpoint}?{query_string}&signature={signature}"
        
        headers = {
            'X-MBX-APIKEY': config.BINANCE_API_KEY
        }
        
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in [418, 429]:
                retry_after = int(e.response.headers.get('Retry-After', 300))
                cls._banned_until = time.time() + retry_after
                logger.error(f"🚨 [REST API] 触发币安封禁 ({e.response.status_code})，全局熔断 {retry_after} 秒！")
            else:
                logger.error(f"请求失败 {endpoint}: {type(e).__name__}")
            return None
        except Exception as e:
            logger.error(f"请求失败 {endpoint}: {type(e).__name__}")
            return None
    
    @classmethod
    def get_balance(cls, use_cache: bool = True) -> Dict:
        if use_cache:
            with cls._lock:
                if cls._cache is not None and cls._cache_time is not None:
                    if time.time() - cls._cache_time < cls._cache_ttl:
                        return cls._cache.get('balance', {})

        data = cls._request('/fapi/v2/balance')
        if not data:
            return {}

        balance = {}
        for item in data:
            asset = item.get('asset', '')
            if asset == 'USDT':
                balance = {
                    'asset': asset,
                    'balance': float(item.get('balance', 0)),
                    'availableBalance': float(item.get('availableBalance', 0)),
                    'crossWalletBalance': float(item.get('crossWalletBalance', 0)),
                    'crossUnPnl': float(item.get('crossUnPnl', 0))
                }
                break
        
        with cls._lock:
            if cls._cache is None:
                cls._cache = {}
            cls._cache['balance'] = balance
            cls._cache_time = time.time()

        return balance

    @classmethod
    def get_positions(cls, use_cache: bool = True) -> List[Dict]:
        if use_cache:
            with cls._lock:
                if cls._cache is not None and cls._cache_time is not None:
                    if time.time() - cls._cache_time < cls._cache_ttl:
                        if 'positions' in cls._cache:
                            return cls._cache.get('positions', [])
        
        data = cls._request('/fapi/v2/positionRisk')
        if not data:
            return []
        
        positions = []
        for item in data:
            position_amt = float(item.get('positionAmt', 0))
            if position_amt != 0:
                unrealized_profit = float(item.get('unRealizedProfit', 0))
                entry_price = float(item.get('entryPrice', 0))
                mark_price = float(item.get('markPrice', 0))
                liquidation_price = float(item.get('liquidationPrice', 0))
                
                roi = 0
                if entry_price != 0 and position_amt != 0:
                    if position_amt > 0:
                        roi = (mark_price - entry_price) / entry_price * 100
                    else:
                        roi = (entry_price - mark_price) / entry_price * 100
                
                positions.append({
                    'symbol': item.get('symbol', ''),
                    'positionSide': item.get('positionSide', 'BOTH'),
                    'positionAmt': position_amt,
                    'entryPrice': entry_price,
                    'markPrice': mark_price,
                    'unRealizedProfit': unrealized_profit,
                    'liquidationPrice': liquidation_price,
                    'leverage': int(item.get('leverage', 1)),
                    'marginType': item.get('marginType', 'cross'),
                    'positionInitialMargin': float(item.get('positionInitialMargin', 0)),
                    'direction': '多' if position_amt > 0 else '空',
                    'roi': roi
                })
        
        positions.sort(key=lambda x: abs(x['unRealizedProfit']), reverse=True)
        
        with cls._lock:
            if cls._cache is None:
                cls._cache = {}
            cls._cache['positions'] = positions
            cls._cache_time = time.time()

        return positions
    
    @classmethod
    def get_account_info(cls) -> Dict:
        balance = cls.get_balance()
        positions = cls.get_positions()
        
        total_pnl = sum(p['unRealizedProfit'] for p in positions)
        
        return {
            'balance': balance,
            'positions': positions,
            'totalPnl': total_pnl,
            'positionCount': len(positions)
        }
    
    @classmethod
    def clear_cache(cls):
        cls._cache = None
        cls._cache_time = None


def get_account_balance() -> Dict:
    return BinanceAccount.get_balance()


def get_positions() -> List[Dict]:
    return BinanceAccount.get_positions()


def get_account_info() -> Dict:
    return BinanceAccount.get_account_info()


class BinanceAccountWS:
    _instance = None
    _ws = None
    _thread = None
    _running = False
    _listen_key = None
    _account_data = {}
    _last_update = None
    _retry_delay = 30
    
    WS_URL = "wss://fstream.binance.com/ws/"
    REST_API = "https://fapi.binance.com"
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with BinanceAccount._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    @classmethod
    def start(cls):
        if cls._running:
            return
        cls._running = True
        cls._thread = threading.Thread(target=cls._run_ws, daemon=True)
        cls._thread.start()
        logger.info("BinanceAccountWS 已启动")
    
    @classmethod
    def stop(cls):
        cls._running = False
        if cls._ws:
            cls._ws.close()
        logger.info("BinanceAccountWS 已停止")
    
    @classmethod
    def _get_listen_key(cls) -> Optional[str]:
        # 如果还在全局熔断期内，直接拒绝请求
        if time.time() < BinanceAccount._banned_until:
            remaining = int(BinanceAccount._banned_until - time.time())
            logger.warning(f"IP 处于熔断期，拦截 listenKey 请求，还需等待 {remaining} 秒")
            return 'BANNED'
            
        url = f"{cls.REST_API}/fapi/v1/listenKey"
        headers = {'X-MBX-APIKEY': config.BINANCE_API_KEY}
        try:
            resp = requests.post(url, headers=headers, timeout=10)
            
            # 读取 Retry-After 响应头
            if resp.status_code == 418:
                wait_sec = int(resp.headers.get('Retry-After', 300))
                banned_until = time.time() + wait_sec
                BinanceAccount._banned_until = banned_until
                cls._retry_delay = min(cls._retry_delay * 2, 3600)
                logger.warning(f"获取listenKey触发418，封锁至 {datetime.datetime.fromtimestamp(banned_until)}")
                return None
            
            resp.raise_for_status()
            return resp.json().get('listenKey')
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in [418, 429]:
                retry_after = int(e.response.headers.get('Retry-After', 300))
                BinanceAccount._banned_until = time.time() + retry_after
                logger.error(f"🚨 [WS API] 获取 listenKey 触发币安封禁 ({e.response.status_code})，全局熔断 {retry_after} 秒！")
                return 'BANNED'
            logger.error(f"获取 listenKey 失败: {type(e).__name__}")
            return None
        except Exception as e:
            logger.error(f"获取 listenKey 失败: {e}")
            return None
    
    @classmethod
    def _keepalive_listen_key(cls):
        # 如果还在全局熔断期内，直接拒绝请求
        if time.time() < BinanceAccount._banned_until:
            remaining = int(BinanceAccount._banned_until - time.time())
            logger.warning(f"IP 处于熔断期，拦截 keepalive 请求，还需等待 {remaining} 秒")
            return 'BANNED'
            
        url = f"{cls.REST_API}/fapi/v1/listenKey"
        headers = {'X-MBX-APIKEY': config.BINANCE_API_KEY}
        try:
            resp = requests.put(url, headers=headers, timeout=10)
            
            # 418处理
            if resp.status_code == 418:
                logger.warning("Keepalive 触发418，进入熔断休眠")
                return False
            
            if not resp.ok:
                logger.warning(f"Keepalive HTTP错误: {resp.status_code}")
                return False
            
            resp.raise_for_status()
            logger.debug("listenKey keepalive 成功")
            return 'SUCCESS'
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in [418, 429]:
                retry_after = int(e.response.headers.get('Retry-After', 300))
                BinanceAccount._banned_until = time.time() + retry_after
                logger.error(f"🚨 [WS API] keepalive 触发币安封禁 ({e.response.status_code})，全局熔断 {retry_after} 秒！")
                return 'BANNED'
            logger.error(f"listenKey keepalive 失败: {e}")
            return None
        except Exception as e:
            logger.error(f"listenKey keepalive 失败: {e}")
            return None
    
    @classmethod
    def _run_ws(cls):
        while cls._running:
            try:
                listen_key = cls._get_listen_key()
                
                if listen_key == 'BANNED':
                    logger.warning(f"由于 IP 封禁，WebSocket 线程深度休眠 {cls._retry_delay} 秒...")
                    time.sleep(cls._retry_delay)
                    cls._retry_delay = min(cls._retry_delay * 2, 3600)  # 指数退避，最大1小时
                    continue
                elif not listen_key:
                    logger.error("无法获取 listenKey，30秒后重试")
                    time.sleep(30)
                    continue
                
                # 成功获取，重置延迟
                cls._retry_delay = 30
                cls._listen_key = listen_key
                ws_url = cls.WS_URL + listen_key
                
                logger.info("连接 WebSocket: wss://fstream.binance.com/ws/***")
                
                cls._ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=cls._on_message,
                    on_error=cls._on_error,
                    on_close=cls._on_close,
                    on_open=cls._on_open
                )
                
                cls._ws.run_forever(ping_interval=30, ping_timeout=10)
                
                if cls._running:
                    logger.info("WebSocket 断开，5秒后重连...")
                    time.sleep(5)
                    
            except Exception as e:
                logger.error(f"WebSocket 运行错误: {e}")
                if cls._running:
                    time.sleep(5)
    
    @classmethod
    def _on_open(cls, ws):
        logger.info("WebSocket 连接成功")
        def keepalive():
            while cls._running and cls._ws:
                time.sleep(1800)
                if cls._running and cls._ws:
                    cls._keepalive_listen_key()
        # 停止旧keepalive线程防止累积 (MEDIUM-012 fix)
        if hasattr(cls, '_keepalive_thread') and cls._keepalive_thread and cls._keepalive_thread.is_alive():
            cls._keepalive_thread = None  # daemon线程在进程退出时自动清理
        cls._keepalive_thread = threading.Thread(target=keepalive, daemon=True)
        cls._keepalive_thread.start()
    
    @classmethod
    def _on_message(cls, ws, message):
        try:
            data = json.loads(message)
            event_type = data.get('e')
            
            if event_type == 'ACCOUNT_UPDATE':
                cls._handle_account_update(data)
            elif event_type == 'MARGIN_CALL':
                logger.warning(f"保证金预警: {data}")
                
        except Exception as e:
            logger.error(f"处理 WebSocket 消息失败: {e}")
    
    @classmethod
    def _handle_account_update(cls, data):
        try:
            account = data.get('a', {})
            balances = account.get('B', [])
            positions = account.get('P', [])
            
            for bal in balances:
                if bal.get('a') == 'USDT':
                    cls._account_data['balance'] = {
                        'asset': 'USDT',
                        'balance': float(bal.get('wb', 0)),
                        'availableBalance': float(bal.get('cw', 0)),
                        'crossWalletBalance': float(bal.get('cw', 0)),
                        'crossUnPnl': 0
                    }
                    break
            
            pos_list = []
            total_pnl = 0
            for pos in positions:
                pos_amt = float(pos.get('pa', 0))
                if pos_amt != 0:
                    unrealized = float(pos.get('up', 0))
                    total_pnl += unrealized
                    pos_list.append({
                        'symbol': pos.get('s', ''),
                        'positionSide': pos.get('ps', 'BOTH'),
                        'positionAmt': pos_amt,
                        'entryPrice': float(pos.get('ep', 0)),
                        'markPrice': 0,
                        'unRealizedProfit': unrealized,
                        'liquidationPrice': 0,
                        'leverage': int(account.get('l', 1)),
                        'marginType': pos.get('mt', 'cross'),  # 读取仓位级保证金模式
                        'positionInitialMargin': float(pos.get('iw', 0)),
                        'direction': '多' if pos_amt > 0 else '空',
                        'roi': 0
                    })
            
            cls._account_data['positions'] = pos_list
            cls._account_data['totalPnl'] = total_pnl
            cls._account_data['positionCount'] = len(pos_list)
            cls._last_update = time.time()
            
            logger.debug(f"账户更新: 权益={cls._account_data.get('balance', {}).get('balance', 0):.2f}, 持仓={len(pos_list)}")
            
            try:
                from utils.websocket_manager import ws_manager
                if ws_manager.socketio:
                    ws_manager.socketio.emit('account_update', {'code': 0, 'data': cls._account_data})
            except Exception as emit_err:
                logger.error(f"推送账户更新失败: {emit_err}")
                
        except Exception as e:
            logger.error(f"处理账户更新失败: {e}")
    
    @classmethod
    def _on_error(cls, ws, error):
        logger.error(f"WebSocket 错误: {error}")
    
    @classmethod
    def _on_close(cls, ws, close_status_code, close_msg):
        logger.info(f"WebSocket 关闭: {close_status_code} - {close_msg}")
    
    @classmethod
    def get_account_info(cls) -> Dict:
        if cls._account_data and cls._last_update:
            if time.time() - cls._last_update < 60:
                return cls._account_data.copy()
        
        logger.warning("WebSocket 数据不可用，回退到 REST API")
        return BinanceAccount.get_account_info()
    
    @classmethod
    def is_connected(cls) -> bool:
        return cls._running and cls._ws is not None and cls._last_update is not None
