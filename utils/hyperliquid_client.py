"""
Hyperliquid API 客户端

功能：
1. 获取全币种价格 (allMids)
2. 获取K线数据 (candleSnapshot)
3. 获取元数据 (meta)
4. WebSocket 实时数据订阅
"""
import requests
import websocket
import json
import time
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidClient:
    """Hyperliquid REST API 客户端"""
    
    def __init__(self, api_url: str = HYPERLIQUID_API_URL):
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def __del__(self):
        self.session.close()
    
    def _post(self, data: dict, timeout: int = 10) -> Any:
        """发送POST请求"""
        resp = self.session.post(self.api_url, json=data, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    
    def get_all_mids(self) -> Dict[str, str]:
        """
        获取所有币种的当前价格
        
        Returns:
            {"BTC": "97000", "ETH": "3500", ...}
        """
        data = self._post({"type": "allMids"})
        return data
    
    def get_meta(self) -> Dict[str, Any]:
        """
        获取交易所元数据（交易对列表等）
        
        Returns:
            包含 universe, assets 等信息的字典
        """
        data = self._post({"type": "meta"})
        return data
    
    def get_candle_snapshot(
        self,
        coin: str,
        interval: str = "1h",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            coin: 币种名称（如 "BTC"）
            interval: 时间间隔 (1m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M)
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
        
        Returns:
            K线数据列表
        """
        req = {"coin": coin, "interval": interval}
        if start_time:
            req["startTime"] = start_time
        if end_time:
            req["endTime"] = end_time
        
        data = self._post({"type": "candleSnapshot", "req": req}, timeout=30)
        return data
    
    def get_l2_book(self, coin: str) -> Dict:
        """
        获取订单簿深度
        
        Args:
            coin: 币种名称
        
        Returns:
            订单簿数据
        """
        data = self._post({"type": "l2Book", "coin": coin})
        return data
    
    def get_trading_symbols(self) -> List[str]:
        """
        获取所有交易币种列表
        
        Returns:
            ["BTC", "ETH", ...]
        """
        meta = self.get_meta()
        universe = meta.get("universe", [])
        return [asset["name"] for asset in universe]
    
    def get_all_mids_with_24h_data(self) -> Dict[str, Dict]:
        """
        获取所有币种价格（带格式转换）
        
        转换为类似 Binance 的格式：
        {
            "BTCUSDT": {"price": "97000", "symbol": "BTC"},
            ...
        }
        """
        mids = self.get_all_mids()
        result = {}
        for coin, price in mids.items():
            symbol = f"{coin}USDT"
            result[symbol] = {
                "symbol": symbol,
                "coin": coin,
                "price": float(price),
                "updated_at": time.time()
            }
        return result


class HyperliquidWebSocket:
    """Hyperliquid WebSocket 客户端"""
    
    def __init__(
        self,
        on_message: callable = None,
        on_error: callable = None,
        on_close: callable = None
    ):
        self.ws_url = HYPERLIQUID_WS_URL
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.ws = None
        self.subscriptions = {}
        self._running = False

    def _on_open(self, ws):
        print("[Hyperliquid WS] 已连接")
        self._running = True
        for sub in self.subscriptions.values():
            ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    def _on_message(self, ws, message):
        if self.on_message:
            try:
                self.on_message(json.loads(message))
            except json.JSONDecodeError:
                print(f"[Hyperliquid WS] 非JSON消息: {message[:200]}")
    
    def _on_error(self, ws, error):
        print(f"[Hyperliquid WS] 错误: {error}")
        if self.on_error:
            self.on_error(error)
    
    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[Hyperliquid WS] 关闭: {close_status_code} {close_msg}")
        self._running = False
        if self.on_close:
            self.on_close(close_status_code, close_msg)
    
    def subscribe_trades(self, coin: str):
        """订阅成交数据"""
        sub = {"type": "trades", "coin": coin}
        key = f"trades:{coin}"
        self.subscriptions[key] = sub
        if self.ws and self._running:
            self.ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    def subscribe_l2_book(self, coin: str):
        """订阅订单簿"""
        sub = {"type": "l2Book", "coin": coin}
        key = f"l2Book:{coin}"
        self.subscriptions[key] = sub
        if self.ws and self._running:
            self.ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
    
    def connect(self):
        """建立连接"""
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
    
    def run_forever(self):
        """运行WebSocket（阻塞）"""
        if self.ws:
            self.ws.run_forever()
    
    def start_thread(self):
        """在后台线程运行WebSocket"""
        self.connect()
        thread = threading.Thread(target=self.run_forever, daemon=True)
        thread.start()
        return thread
    
    def close(self):
        """关闭连接"""
        if self.ws:
            self.ws.close()


def convert_candle_to_kline(candle: Dict) -> Dict:
    """
    将 Hyperliquid K线格式转换为标准格式
    
    Args:
        candle: Hyperliquid K线数据
    
    Returns:
        标准K线格式
    """
    return {
        "timestamp": candle["t"],  # 开始时间戳（毫秒）
        "open": float(candle["o"]),
        "high": float(candle["h"]),
        "low": float(candle["l"]),
        "close": float(candle["c"]),
        "volume": float(candle["v"]),
        "coin": candle["s"],
        "interval": candle["i"],
        "trades": candle.get("n", 0)
    }


if __name__ == "__main__":
    print("=== Hyperliquid API 测试 ===\n")
    
    client = HyperliquidClient()
    
    # 测试获取所有价格
    print("1. 获取所有价格...")
    mids = client.get_all_mids()
    print(f"   币种数: {len(mids)}")
    print(f"   BTC: {mids.get('BTC')}")
    print(f"   ETH: {mids.get('ETH')}")
    
    # 测试获取交易对
    print("\n2. 获取交易对列表...")
    symbols = client.get_trading_symbols()
    print(f"   交易对数: {len(symbols)}")
    print(f"   前10个: {symbols[:10]}")
    
    # 测试获取K线
    print("\n3. 获取BTC 1h K线...")
    start_time = int((time.time() - 7 * 24 * 3600) * 1000)  # 7天前
    candles = client.get_candle_snapshot("BTC", "1h", start_time=start_time)
    print(f"   K线数: {len(candles)}")
    if candles:
        print(f"   最新K线: {convert_candle_to_kline(candles[0])}")
    
    print("\n=== 测试完成 ===")
