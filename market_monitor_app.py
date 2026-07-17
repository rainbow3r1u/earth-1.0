#!/usr/bin/env python3
"""
币安全量行情监控 - 独立Flask服务
端口: 5003
功能: REST API拉全量 + WebSocket实时更新 + 分钟K线聚合
新增: 主动买卖估算(delta_q + buy_ratio + 买卖成交额)
"""
import os
import io
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件（与5002端口共享COS凭证）
load_dotenv()
import time
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request, make_response
from flask_socketio import SocketIO
import requests
import pandas as pd

# ========== 配置 ==========
PORT = 5003
BINANCE_API = "https://api.binance.com"
SNAPSHOT_FILE = "/var/www/market_snapshot.json"
WRITE_INTERVAL_SECONDS = 900  # 15分钟写一次快照

# COS 配置（独立于5002端口）
COS_KEY = "klines/minute_klines.parquet"  # 分钟K线（完全独立）
COS_HOURLY_KEY = "klines/hourly_klines_5003.parquet"  # 1h K线缓存（独立于5002）
COS_REGION = os.environ.get('COS_REGION', '')
COS_ENDPOINT = os.environ.get('COS_ENDPOINT', '')
COS_SECRET_ID = os.environ.get('COS_SECRET_ID', '')
COS_SECRET_KEY = os.environ.get('COS_SECRET_KEY', '')
COS_BUCKET = os.environ.get('COS_BUCKET', '')

# 本地文件备份（当COS不可用时使用）
LOCAL_KLINE_FILE = "/tmp/market_minute_klines.parquet"

# K线配置
MAX_MINUTE_KLINES = 6 * 24 * 60  # 6天 = 8640分钟

# delta_q 突增检测配置
DELTA_Q_SURGE_THRESHOLD = 500_000  # 50万USDT以上的delta_q视为突增
SURGE_CACHE_MAX_MINUTES = 15  # 突增记录保留最近15分钟
SURGE_EXCLUDE_SYMBOLS = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XAUUSDT', 'XAGUSDT', 'USDEUSDT'}  # 排除的币种

# ========== 布林爬坡检测配置 (V7 最优参数) ==========
BB_CLIMB_CONFIG = {
    "period": 20,                    # 布林周期(日线)
    "std_mult": 2.0,                 # 标准差倍数 (与 backtester 统一)
    "upper_tolerance_pct": 0.08,    # 收盘价在上轨±8%范围内
    "buy_ratio_threshold": 0.55,    # buy_ratio阈值（仅对真实数据检查）
    "buy_ratio_skip_default": True,
    "volume_ratio": 1.2,
    "hl_tolerance_window": 5,        # 高低点窗口（天）
    "hl_tolerance_min": 3,           # 窗口内最少高低点个数
    "atr_period": 14,
    "atr_enabled": True,
    "exclude_symbols": {
        # 大盘/股票/商品
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT',
        'TSLAUSDT', 'NVDAUSDT', 'AMZNUSDT', 'GOOGLUSDT', 'AAPLUSDT',
        'COINUSDT', 'MSTRUSDT', 'METAUSDT', 'TSMUSDT',
        'XAUUSDT', 'XAGUSDT', 'XAUTUSDT', 'NATGASUSDT',
        # 稳定币对
        'USDCUSDT', 'RLUSDUSDT', 'UUSDT', 'XUSDUSDT', 'USD1USDT',
        'FDUSDUSDT', 'TUSDUSDT', 'PAXUSDT', 'BUSDUSDT', 'SUSDUSDT',
        'USDEUSDT', 'USDPUSDT', 'USDSUSDT', 'AEURUSDT', 'EURIUSDT', 'EURUSDT',
        'BFUSDUSDT',
        # 现货专属（期货无此交易对，对齐回测数据）
        'ACMUSDT', 'ADXUSDT', 'ALCXUSDT', 'AMPUSDT', 'ARDRUSDT',
        'ATMUSDT', 'AUDIOUSDT', 'BARUSDT', 'BNSOLUSDT',
        'BTTCUSDT', 'CITYUSDT', 'DCRUSDT', 'DGBUSDT', 'DODOUSDT',
        'FARMUSDT', 'FTTUSDT', 'GLMRUSDT', 'GNOUSDT', 'GNSUSDT',
        'IQUSDT', 'JUVUSDT', 'KGSTUSDT', 'LAZIOUSDT', 'LUNAUSDT',
        'MBLUSDT', 'NEXOUSDT', 'OSMOUSDT', 'PIVXUSDT', 'PONDUSDT',
        'PORTOUSDT', 'PSGUSDT', 'PYRUSDT', 'QIUSDT', 'QKCUSDT',
        'QUICKUSDT', 'RADUSDT', 'REQUSDT', 'SCUSDT', 'STRAXUSDT',
        'TFUELUSDT', 'TKOUSDT', 'WBETHUSDT', 'WBTCUSDT', 'WINUSDT',
        'XNOUSDT',
    },
    "candidate_enabled": True,
    "candidate_near_hours": 2,
    "candidate_vol_ratio": 0.5,
}

# 内存限制配置（Phase 1）
MAX_MINUTE_KLINES_PER_SYMBOL = 120   # 每个币种只保留最近120条分钟K线（2小时）
MAX_HOURLY_KLINES_PER_SYMBOL = 36    # 每个币种只保留最近36根1h K线（36小时）

# ========== 布林爬坡缓存 ==========

# ========== 全局数据 ==========
market_data = {
    "updated_at": None,
    "symbols": {},                    # {symbol: {q, price, o, h, l, v, updated_at}}
    "last_q": {},                     # {symbol: 上一次q值}
    "second_deltas": {},               # {秒时间戳: {symbol: delta_q}}
    "minute_klines": {},               # {symbol: [kline1, kline2, ...]}
    "current_minute": None,            # 当前分钟开始时间戳
    "minute_state": {},                # {symbol: {open, high, low, close, vol, q, first_price, buy_q, sell_q, last_price}}
    "surge_cache": {},                 # {symbol: [{t, delta_q, buy_ratio, price}, ...]}
    "hourly_kline_cache": {},          # {symbol: [hourly_kline, ...]} 从API拉取+WebSocket聚合的1h K线
    "bb_backfill_done": False,           # API回填是否完成
    "today_open_prices": {},            # {symbol: 北京0点开盘价}
    "today_open_updated": 0,             # 上次更新时间
    # 15分钟成交量统计
    "vol_15m_current": {},              # {symbol: current_15m_volume} 当前15分钟累计
    "vol_15m_last": {},                 # {symbol: last_15m_volume} 上一个15分钟成交额
    "vol_15m_slot": None,               # 当前15分钟区间时间戳
    "vol_15m_history": {},              # {symbol: {slot_ts: vol}} 最近16个15分钟成交额历史(4小时)，从COS加载
    "vol_15m_avg_4h": {},               # {symbol: avg_vol} 前4小时的15分钟均值
    "vol_24h_today": {},                # {symbol: today_volume} 今日累计成交额（北京时间8点起）
    "vol_surge_symbols": {},            # {symbol: {start_time, ratio, vol}} 突增币种记录（5分钟有效期，给交易脚本用）
    "vol_surge_history": {},            # {symbol: {start_time, ratio, vol}} 突增历史记录（1小时，给前端展示用）
    # 追涨追踪器（涨幅5%~10%的币，追踪到10%需要多少成交量）
    "momentum_tracker": {},             # {symbol: {entry_time, entry_price, entry_gain_pct, target_gain_pct, current_price, current_gain_pct, vol_24h_today, vol_needed, elapsed_seconds, status}}
}
data_lock = threading.Lock()

# ========== Flask ==========
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.template_folder = os.path.join(os.path.dirname(__file__), "templates")
socketio = SocketIO(app, async_mode="threading",
                    cors_allowed_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5003").split(","),
                    ping_timeout=60, ping_interval=25)


# ========== 全局 HTTP Session ==========
_requests_session = requests.Session()

# ========== COS 客户端 ==========
_cos_client = None

def get_cos_client():
    global _cos_client
    # 凭证变化时重建客户端
    current_sid = os.environ.get('COS_SECRET_ID', '')
    if _cos_client is None or (COS_SECRET_ID != current_sid and current_sid):
        from qcloud_cos import CosConfig, CosS3Client
        # 优先用环境变量（dotenv加载后可能更新）
        sid = current_sid or COS_SECRET_ID
        skey = os.environ.get('COS_SECRET_KEY', '') or COS_SECRET_KEY
        region = os.environ.get('COS_REGION', COS_REGION)
        endpoint = os.environ.get('COS_ENDPOINT', COS_ENDPOINT)
        if not sid or not skey:
            return None
        cos_config = CosConfig(Region=region, SecretId=sid, SecretKey=skey, Endpoint=endpoint, Timeout=30)
        _cos_client = CosS3Client(cos_config)
    return _cos_client

# ========== 工具函数 ==========
def fetch_24h_snapshot() -> list:
    """REST API拉取全量24h数据"""
    try:
        resp = _requests_session.get(f"{BINANCE_API}/api/v3/ticker/24hr", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"REST API拉取失败: {e}")
        return []

def _v7_load_hourly_cache():
    """V7: 从本地缓存加载1h K线（跳过API回填）
    优先加载 binance API 直拉缓存（720根≈30天），回退到旧缓存
    加载完后强制刷新日线BB缓存
    """
    print("[V7] 开始加载本地1h缓存...", flush=True)
    
    # 优先：binance 直拉缓存（418币种 × 720根 ≈ 30天）
    NEW_CACHE = Path(__file__).parent / "data" / "hourly_backfill.json"
    if NEW_CACHE.exists():
        try:
            with open(NEW_CACHE) as f:
                cache_data = json.load(f)
            raw = cache_data.get("data", {})
            count = 0
            sample_len = 0
            with data_lock:
                for sym, klines in raw.items():
                    for k in klines:
                        if "buy_ratio" not in k:
                            k["buy_ratio"] = 0.5
                    market_data["hourly_kline_cache"][sym] = klines
                    count += 1
                    if not sample_len:
                        sample_len = len(klines)
                market_data["bb_backfill_done"] = True
            print(f"[V7] Binance缓存加载: {count} 币种 ✅（{sample_len}根/K线 ≈ 30天）")
            # 注意：日线BB缓存由 bb_daily_background_loop 负责刷新（使用币安API日线数据）
            return
        except Exception as e:
            print(f"[V7] Binance缓存加载失败: {e}，尝试旧缓存")
            import traceback
            traceback.print_exc()
    
    # 回退：旧缓存
    try:
        cache_path = "/home/myuser/backtester/data_cache/notusdt_1h.json"
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                local_data = json.load(f)
            local_klines = local_data.get("klines", {})
            count = 0
            with data_lock:
                for sym, klines in local_klines.items():
                    for k in klines:
                        if "buy_ratio" not in k:
                            k["buy_ratio"] = 0.5
                    market_data["hourly_kline_cache"][sym] = klines
                    count += 1
                market_data["bb_backfill_done"] = True
            print(f"[V7] 旧缓存加载: {count} 币种1h K线")
        else:
            print(f"[V7] 本地缓存不存在: {cache_path}")
    except Exception as e:
        print(f"[V7] 本地缓存加载失败: {e}")
        import traceback
        traceback.print_exc()


def init_market_data():
    # 从COS加载已有数据
    hourly_cache = _load_hourly_cache_from_cos()
    cos_symbols = set()
    if hourly_cache:
        cos_symbols = set(hourly_cache.keys())
    
    if cos_symbols:
        print(f"[INIT] COS数据加载完成，共 {len(cos_symbols)} 个币种")
        
        # 加载分钟K线
        try:
            historical_klines = load_minute_klines_from_cos()
            with data_lock:
                if historical_klines:
                    market_data["minute_klines"] = historical_klines
                    print(f"从COS加载了 {len(historical_klines)} 个币种的分钟K线")
        except Exception as e:
            print(f"加载分钟K线失败: {e}")

        # V7: 从本地缓存加载1h K线（COS有数据分支）
        _v7_load_hourly_cache()
        return
    
    # ========== COS无数据，回退到币安API加载 ==========
    print("正在从币安API加载全量数据...")
    
    # 从exchangeInfo获取TRADING状态的币种白名单
    trading_symbols = set()
    try:
        resp = _requests_session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=30)
        resp.raise_for_status()
        for s in resp.json().get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                trading_symbols.add(s["symbol"])
        print(f"从exchangeInfo获取 {len(trading_symbols)} 个TRADING币种")
    except Exception as e:
        print(f"exchangeInfo获取失败: {e}")
    
    with data_lock:
        market_data["trading_symbols"] = trading_symbols
    
    # 1. 从币安API加载全部币种（500+个）
    api_symbols = _fetch_all_from_binance_api(trading_symbols)
    print(f"币安API加载: {len(api_symbols)} 个币种")
    
    # 2. 从COS加载已有数据（北京8点开盘价）
    hourly_cache = _load_hourly_cache_from_cos()
    today_open_prices = {}
    cos_symbols = set()
    
    # 计算北京8点时间戳
    now_utc = datetime.now(tz=timezone.utc)
    today_utc0 = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    beijing_8am_ts = int(today_utc0.timestamp())
    
    if hourly_cache:
        print(f"从COS加载了 {len(hourly_cache)} 个币种的历史数据")
        # 提取北京8点开盘价
        for symbol, hklines in hourly_cache.items():
            for h in hklines:
                if h['t'] == beijing_8am_ts:
                    today_open_prices[symbol] = h['o']
                    cos_symbols.add(symbol)
                    # 更新该币种的开盘价和涨跌幅
                    if symbol in api_symbols:
                        api_symbols[symbol]['o'] = h['o']
                        price = api_symbols[symbol]['price']
                        api_symbols[symbol]['priceChangePercent'] = (price - h['o']) / h['o'] * 100 if h['o'] > 0 else 0
                    break
        print(f"从COS更新了 {len(today_open_prices)} 个币种的北京8点开盘价")
    
    # 3. 为剩余币种（没有COS数据的）从币安API获取北京8点K线
    remaining_symbols = [s for s in api_symbols.keys() if s not in cos_symbols]
    print(f"需要为 {len(remaining_symbols)} 个币种获取北京8点开盘价...")
    
    # 批量获取北京8点K线（限制频率避免429）
    beijing_8am_opens = _fetch_beijing_8am_klines(remaining_symbols, beijing_8am_ts)
    
    for symbol, open_price in beijing_8am_opens.items():
        today_open_prices[symbol] = open_price
        if symbol in api_symbols:
            api_symbols[symbol]['o'] = open_price
            price = api_symbols[symbol]['price']
            api_symbols[symbol]['priceChangePercent'] = (price - open_price) / open_price * 100 if open_price > 0 else 0
    
    print(f"总计 {len(today_open_prices)} 个币种有北京8点开盘价")
    
    
    # 检查是否已有完整 hourly_cache（binance直拉缓存已写入）
    with data_lock:
        existing_cache = market_data.get("hourly_kline_cache", {})
        has_full_cache = len(existing_cache) >= 100 and any(len(v) >= 100 for v in existing_cache.values())
    
    if has_full_cache:
        # 已通过 _v7_load_hourly_cache 写入完整720根数据，不再覆盖
        with data_lock:
            market_data["symbols"] = api_symbols
            market_data["today_open_prices"] = today_open_prices
            market_data["updated_at"] = time.time()
            market_data["current_minute"] = get_current_minute_ts()
            market_data["last_q"] = {s: info["q"] for s, info in api_symbols.items()}
        print(f"[INIT] 保留 binance 直拉缓存（418币种×720根），跳过COS hour cache覆盖")
    else:
        with data_lock:
            market_data["symbols"] = api_symbols
            market_data["today_open_prices"] = today_open_prices
            market_data["hourly_kline_cache"] = hourly_cache
            market_data["updated_at"] = time.time()
            market_data["current_minute"] = get_current_minute_ts()
            market_data["last_q"] = {s: info["q"] for s, info in api_symbols.items()}
    
    print(f"总计加载: {len(api_symbols)} 个币种")
    
    # 首次启动后，立即把数据写入COS
    save_symbols_snapshot_to_cos()
    save_vol_24h_today_to_cos()

    # V7: 从本地缓存加载1h K线
    _v7_load_hourly_cache()
    
    # 加载分钟K线（用于15分钟统计）
    try:
        historical_klines = load_minute_klines_from_cos()
        with data_lock:
            if historical_klines:
                market_data["minute_klines"] = historical_klines
                print(f"从COS加载了 {len(historical_klines)} 个币种的分钟K线")
    except Exception as e:
        print(f"加载分钟K线失败: {e}")

def _fetch_all_from_binance_api(trading_symbols: set) -> dict:
    """从币安API获取全部币种数据（24h滚动数据）"""
    raw = fetch_24h_snapshot()
    
    new_symbols = {}
    for item in raw:
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if trading_symbols and symbol not in trading_symbols:
            continue
        
        price_change_pct = float(item.get("priceChangePercent", 0))
        if price_change_pct == 0:
            continue
        
        q = float(item.get("quoteVolume", 0))
        price = float(item.get("lastPrice", 0))
        
        new_symbols[symbol] = {
            "q": q,
            "v": float(item.get("volume", 0)),
            "price": price,
            "o": float(item.get("openPrice", 0)),
            "h": float(item.get("highPrice", 0)),
            "l": float(item.get("lowPrice", 0)),
            "priceChangePercent": price_change_pct,
            "updated_at": time.time()
        }
    
    return new_symbols

def _fetch_beijing_8am_klines(symbols: list, beijing_8am_ts: int) -> dict:
    """批量获取币种的北京8点K线（1h周期）"""
    result = {}
    session = requests.Session()
    
    # 计算北京8点的时间戳对应的ms
    # 减1ms确保获取到正确的K线（避免边界时间戳问题）
    beijing_8am_ms = beijing_8am_ts * 1000 - 1
    
    for i, symbol in enumerate(symbols):
        try:
            # 获取北京8点的那根1h K线
            params = {
                "symbol": symbol,
                "interval": "1h",
                "startTime": beijing_8am_ms,
                "limit": 1
            }
            resp = _requests_session.get(f"{BINANCE_API}/api/v3/klines", params=params, timeout=5)
            
            if resp.status_code == 200:
                klines = resp.json()
                if klines and len(klines) > 0:
                    open_price = float(klines[0][1])  # 开盘价
                    result[symbol] = open_price
            elif resp.status_code == 429:
                # 限流了，等待更长时间
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"[北京8点K线] {symbol} 触发429限流，等待{retry_after}秒")
                time.sleep(retry_after)
                continue
            elif resp.status_code == 418:
                # IP被熔断
                retry_after = int(resp.headers.get("Retry-After", 300))
                print(f"[北京8点K线] {symbol} 触发418熔断，等待{retry_after}秒")
                time.sleep(retry_after)
                continue
                
        except Exception as e:
            print(f"[北京8点K线] {symbol} 获取失败: {e}")
        
        # 每10个币种暂停一下，避免频率限制
        if (i + 1) % 10 == 0:
            print(f"[北京8点K线] 已处理 {i + 1}/{len(symbols)} 个币种")
            time.sleep(1)
    
    return result

def get_current_minute_ts():
    """获取当前分钟的开始时间戳（秒）"""
    return int(time.time()) // 60 * 60

def get_current_15m_slot():
    """获取当前15分钟区间的时间戳"""
    return int(time.time()) // 900 * 900  # 900秒 = 15分钟

# 稳定币对（价格波动极小，排除）
STABLECOIN_PAIRS = {
    'USDCUSDT', 'RLUSDUSDT', 'UUSDT', 'XUSDUSDT', 'USD1USDT',
    'FDUSDUSDT', 'TUSDUSDT', 'PAXUSDT', 'BUSDUSDT', 'SUSDUSDT', 'USDEUSDT',
}

VOL_SURGE_MIN_AVG_VOL = 5_000  # 前4小时均值最低门槛 5000 USDT，避免极小均值产生极端ratio
VOL_SURGE_MIN_GAIN_PCT = 0.8   # 15m涨幅下限（回测最优区间0.8-1.5%）
VOL_SURGE_MAX_GAIN_PCT = 1.5   # 15m涨幅上限（超过就是追涨送死）

def check_volume_surge(symbol: str, current_15m_vol: float, avg_4h_vol: float):
    """检测15分钟成交量突增"""
    # 过滤排除币种（与BB检测一致的完整排除列表）
    if symbol in BB_CLIMB_CONFIG["exclude_symbols"]:
        return False
    # 均值下限，避免极端ratio
    if avg_4h_vol < VOL_SURGE_MIN_AVG_VOL:
        return False
    # 15m K线涨幅过滤 (与回测min_gain_pct对齐)
    minute_klines = market_data.get("minute_klines", {}).get(symbol, [])
    if len(minute_klines) < 15:
        return False
    # 取最近15根分钟K线计算15m涨幅
    last_15 = minute_klines[-15:]
    first_price = last_15[0].get("o", 0)
    last_price = last_15[-1].get("c", 0)
    if first_price <= 0:
        return False
    gain_15m = (last_price - first_price) / first_price * 100
    if gain_15m < VOL_SURGE_MIN_GAIN_PCT or gain_15m > VOL_SURGE_MAX_GAIN_PCT:
        return False
    # ratio带通: 1.0x≤ratio≤2.0x (回测最优区间)
    if avg_4h_vol > 0 and current_15m_vol > avg_4h_vol * 1.0 and current_15m_vol < avg_4h_vol * 2.0:
        surge_info = {
            "start_time": time.time(),
            "ratio": current_15m_vol / avg_4h_vol,
            "vol": current_15m_vol,          # 当前15分钟成交额
            "last_vol": avg_4h_vol,           # 前4小时均值（上期）
            "avg_4h_vol": avg_4h_vol         # 前4小时均值
        }
        market_data["vol_surge_symbols"][symbol] = surge_info
        # 同时写入历史记录（1小时展示用），若已存在则更新为最新
        market_data["vol_surge_history"][symbol] = surge_info.copy()
        print(f"[VOL_SURGE] {symbol} 15分钟成交量突增: 当前{current_15m_vol/1e3:.1f}K vs 前4h均值{avg_4h_vol/1e3:.1f}K ({surge_info['ratio']:.2f}x)")
        return True
    return False

def cleanup_surge_records():
    """清理超过5分钟的突增记录（交易脚本用）"""
    now = time.time()
    expired = []
    for symbol, info in market_data.get("vol_surge_symbols", {}).items():
        if now - info.get("start_time", 0) > 300:  # 5分钟
            expired.append(symbol)
    for symbol in expired:
        del market_data["vol_surge_symbols"][symbol]
        print(f"[VOL_SURGE] {symbol} 突增记录已过期，移除")

def cleanup_vol_surge_history():
    """清理超过1小时的突增历史记录（前端展示用）"""
    now = time.time()
    expired = []
    for symbol, info in market_data.get("vol_surge_history", {}).items():
        if now - info.get("start_time", 0) > 3600:  # 1小时
            expired.append(symbol)
    for symbol in expired:
        del market_data["vol_surge_history"][symbol]
        print(f"[VOL_SURGE] {symbol} 历史展示记录已过期，移除")

def save_snapshot():
    """写入快照文件"""
    with data_lock:
        data = {
            "updated_at": datetime.now().isoformat(),
            "symbols": market_data["symbols"]
        }
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"快照已写入: {SNAPSHOT_FILE}")
    except Exception as e:
        print(f"写入失败: {e}")

def load_minute_klines_from_cos():
    """从COS加载历史分钟K线，失败则用本地文件"""
    # 先尝试COS
    try:
        if COS_SECRET_ID and COS_SECRET_KEY and COS_BUCKET:
            client = get_cos_client()
            if client is None:
                print("COS加载跳过: 凭证无效")
            else:
                resp = client.get_object(Bucket=COS_BUCKET, Key=COS_KEY)
                data = resp['Body'].get_raw_stream().read()
                df = pd.read_parquet(io.BytesIO(data))
                print(f"从COS加载了 {len(df)} 条分钟K线")

                klines_dict = {}
                for symbol in df['symbol'].unique():
                    symbol_df = df[df['symbol'] == symbol].sort_values('timestamp')
                    records = symbol_df.to_dict('records')
                    for r in records:
                        r['t'] = r.pop('timestamp')
                        r['o'] = r.pop('open')
                        r['h'] = r.pop('high')
                        r['l'] = r.pop('low')
                        r['c'] = r.pop('close')
                        r['v'] = r.pop('volume')
                        r['q'] = r.pop('quote_volume')
                    klines_dict[symbol] = records

                # 截断：只保留最近120条，避免加载过多历史数据到内存
                for symbol in klines_dict:
                    if len(klines_dict[symbol]) > MAX_MINUTE_KLINES_PER_SYMBOL:
                        klines_dict[symbol] = klines_dict[symbol][-MAX_MINUTE_KLINES_PER_SYMBOL:]
                print(f"截断后保留每个币种最近 {MAX_MINUTE_KLINES_PER_SYMBOL} 条分钟K线")
                return klines_dict
    except Exception as e:
        print(f"COS加载失败: {e}")
    
    # 回退到本地文件
    try:
        if os.path.exists(LOCAL_KLINE_FILE):
            df = pd.read_parquet(LOCAL_KLINE_FILE)
            print(f"从本地文件加载了 {len(df)} 条分钟K线")
            
            klines_dict = {}
            for symbol in df['symbol'].unique():
                symbol_df = df[df['symbol'] == symbol].sort_values('timestamp')
                records = symbol_df.to_dict('records')
                # Rename columns to match in-memory format
                for r in records:
                    r['t'] = r.pop('timestamp')
                    r['o'] = r.pop('open')
                    r['h'] = r.pop('high')
                    r['l'] = r.pop('low')
                    r['c'] = r.pop('close')
                    r['v'] = r.pop('volume')
                    r['q'] = r.pop('quote_volume')
                klines_dict[symbol] = records
            
            # 截断：只保留最近120条
            for symbol in klines_dict:
                if len(klines_dict[symbol]) > MAX_MINUTE_KLINES_PER_SYMBOL:
                    klines_dict[symbol] = klines_dict[symbol][-MAX_MINUTE_KLINES_PER_SYMBOL:]
            print(f"本地加载截断后保留每个币种最近 {MAX_MINUTE_KLINES_PER_SYMBOL} 条分钟K线")
            return klines_dict
    except Exception as e:
        print(f"本地文件加载失败: {e}")
    
    print("无历史分钟K线数据（新启动或文件丢失）")
    return {}

def save_minute_klines_to_cos(klines_dict):
    """保存分钟K线到COS"""
    try:
        # 转换为 DataFrame
        all_rows = []
        for symbol, klines in klines_dict.items():
            for kline in klines:
                all_rows.append({
                    'symbol': symbol,
                    'timestamp': kline['t'],
                    'open': kline['o'],
                    'high': kline['h'],
                    'low': kline['l'],
                    'close': kline['c'],
                    'volume': kline['v'],
                    'quote_volume': kline['q']
                })
        
        if not all_rows:
            return
        
        df = pd.DataFrame(all_rows)
        
        # 滚动清理超过6天的数据
        cutoff = time.time() - (MAX_MINUTE_KLINES * 60)
        df = df[df['timestamp'] >= cutoff]
        
        # 保存到本地文件（始终）
        try:
            df.to_parquet(LOCAL_KLINE_FILE, index=False)
            print(f"分钟K线已保存本地: {len(df)} 条")
        except Exception as e:
            print(f"本地文件保存失败: {e}")
        
        # 上传到COS（如果配置了）
        if COS_SECRET_ID and COS_SECRET_KEY and COS_BUCKET:
            try:
                client = get_cos_client()
                if client is None:
                    print("COS上传跳过: 凭证无效")
                else:
                    buffer = io.BytesIO()
                    df.to_parquet(buffer, index=False)
                    buffer.seek(0)

                    client.put_object(
                        Bucket=COS_BUCKET,
                        Key=COS_KEY,
                        Body=buffer.read()
                    )
                    print(f"分钟K线已上传COS: {len(df)} 条")
            except Exception as e:
                print(f"COS上传失败: {e}")
    except Exception as e:
        print(f"分钟K线保存失败: {e}")

# ========== 15分钟成交量 COS 持久化 ==========
VOL_15M_PREFIX = "vol_15m/slots_"

def _get_vol_15m_key(slot_ts: int) -> str:
    """根据slot_ts生成COS key（按月份分文件）"""
    dt = datetime.fromtimestamp(slot_ts, tz=timezone.utc)
    return f"{VOL_15M_PREFIX}{dt.year}{dt.month:02d}.parquet"

def save_vol_15m_to_cos(slot_ts: int, vol_data: dict):
    """将15分钟成交量写入COS（按月份分文件）
    vol_data: {symbol: vol}
    """
    try:
        if not COS_SECRET_ID or not COS_SECRET_KEY or not COS_BUCKET:
            return
        
        client = get_cos_client()
        if client is None:
            return
        
        key = _get_vol_15m_key(slot_ts)
        
        # 构建新数据行
        new_rows = [{'slot_ts': slot_ts, 'symbol': s, 'vol': float(v)} for s, v in vol_data.items()]
        new_df = pd.DataFrame(new_rows)
        
        # 尝试读取旧文件
        try:
            resp = client.get_object(Bucket=COS_BUCKET, Key=key)
            old_data = resp['Body'].get_raw_stream().read()
            old_df = pd.read_parquet(io.BytesIO(old_data))
            # 去重：同一slot_ts+symbol只保留新数据
            old_df = old_df[~((old_df['slot_ts'] == slot_ts) & (old_df['symbol'].isin(vol_data.keys())))]
            merged_df = pd.concat([old_df, new_df], ignore_index=True)
        except Exception:
            merged_df = new_df
        
        # 滚动清理：只保留最近7天（672个15分钟区间）
        cutoff = time.time() - 7 * 24 * 3600
        merged_df = merged_df[merged_df['slot_ts'] >= cutoff]
        
        buffer = io.BytesIO()
        merged_df.to_parquet(buffer, index=False)
        buffer.seek(0)
        
        client.put_object(Bucket=COS_BUCKET, Key=key, Body=buffer.read())
        print(f"[VOL_15M_COS] 已上传 {len(new_df)} 条到 {key}")
    except Exception as e:
        print(f"[VOL_15M_COS] 上传失败: {e}")

def load_vol_15m_from_cos(end_slot: int, slots_count: int = 16) -> dict:
    """从COS加载指定时间范围内的15分钟成交量
    返回: {symbol: {slot_ts: vol}}
    """
    result = {}
    try:
        if not COS_SECRET_ID or not COS_SECRET_KEY or not COS_BUCKET:
            return result
        
        client = get_cos_client()
        if client is None:
            return result
        
        # 计算需要加载的月份范围
        start_slot = end_slot - (slots_count - 1) * 900
        start_dt = datetime.fromtimestamp(start_slot, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_slot, tz=timezone.utc)
        
        # 生成需要查询的所有月份key
        keys_to_fetch = set()
        current_dt = datetime(start_dt.year, start_dt.month, 1, tzinfo=timezone.utc)
        end_month_dt = datetime(end_dt.year, end_dt.month, 1, tzinfo=timezone.utc)
        while current_dt <= end_month_dt:
            keys_to_fetch.add(f"{VOL_15M_PREFIX}{current_dt.year}{current_dt.month:02d}.parquet")
            # 下一个月
            if current_dt.month == 12:
                current_dt = datetime(current_dt.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                current_dt = datetime(current_dt.year, current_dt.month + 1, 1, tzinfo=timezone.utc)
        
        all_dfs = []
        for key in keys_to_fetch:
            try:
                resp = client.get_object(Bucket=COS_BUCKET, Key=key)
                data = resp['Body'].get_raw_stream().read()
                df = pd.read_parquet(io.BytesIO(data))
                all_dfs.append(df)
            except Exception:
                pass  # 文件不存在是正常的
        
        if not all_dfs:
            return result
        
        merged_df = pd.concat(all_dfs, ignore_index=True)
        merged_df = merged_df[(merged_df['slot_ts'] >= start_slot) & (merged_df['slot_ts'] <= end_slot)]
        
        for _, row in merged_df.iterrows():
            symbol = row['symbol']
            slot_ts = int(row['slot_ts'])
            vol = float(row['vol'])
            if symbol not in result:
                result[symbol] = {}
            result[symbol][slot_ts] = vol
        
        print(f"[VOL_15M_COS] 从COS加载了 {len(merged_df)} 条15分钟成交量")
    except Exception as e:
        print(f"[VOL_15M_COS] 加载失败: {e}")
    
    return result

def calc_vol_15m_avg_strict(symbol: str, current_slot: int, history: dict) -> float:
    """计算15分钟均值：分母恒为16，缺失slot补0（与回测vol_surge.rs一致）"""
    total = 0.0
    symbol_hist = history.get(symbol, {})
    for i in range(1, 17):
        ts = current_slot - i * 900
        total += symbol_hist.get(ts, 0.0)
    return total / 16.0



# ========== 市场数据快照 COS 持久化 ==========
COS_SNAPSHOT_PREFIX = "market_monitor/"

def save_symbols_snapshot_to_cos():
    """将当前所有币种快照写入COS（价格、成交额、涨跌幅等）"""
    try:
        if not COS_SECRET_ID or not COS_SECRET_KEY or not COS_BUCKET:
            return
        client = get_cos_client()
        if client is None:
            return
        
        with data_lock:
            symbols = dict(market_data.get("symbols", {}))
        
        if not symbols:
            return
        
        rows = []
        for symbol, info in symbols.items():
            rows.append({
                'symbol': symbol,
                'price': float(info.get('price', 0)),
                'q': float(info.get('q', 0)),
                'v': float(info.get('v', 0)),
                'o': float(info.get('o', 0)),
                'h': float(info.get('h', 0)),
                'l': float(info.get('l', 0)),
                'priceChangePercent': float(info.get('priceChangePercent', 0)),
                'updated_at': float(info.get('updated_at', 0)),
            })
        
        df = pd.DataFrame(rows)
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)
        
        key = f"{COS_SNAPSHOT_PREFIX}symbols_snapshot.parquet"
        client.put_object(Bucket=COS_BUCKET, Key=key, Body=buffer.read())
        print(f"[SNAPSHOT_COS] symbols快照已上传: {len(rows)} 个币种")
    except Exception as e:
        print(f"[SNAPSHOT_COS] symbols快照上传失败: {e}")

def save_vol_24h_today_to_cos():
    """将今日累计成交额写入COS"""
    try:
        if not COS_SECRET_ID or not COS_SECRET_KEY or not COS_BUCKET:
            return
        client = get_cos_client()
        if client is None:
            return
        
        with data_lock:
            vol_24h = dict(market_data.get("vol_24h_today", {}))
        
        if not vol_24h:
            return
        
        rows = [{'symbol': s, 'vol': float(v)} for s, v in vol_24h.items()]
        df = pd.DataFrame(rows)
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)
        
        key = f"{COS_SNAPSHOT_PREFIX}vol_24h_today.parquet"
        client.put_object(Bucket=COS_BUCKET, Key=key, Body=buffer.read())
        print(f"[SNAPSHOT_COS] vol_24h_today已上传: {len(rows)} 条")
    except Exception as e:
        print(f"[SNAPSHOT_COS] vol_24h_today上传失败: {e}")

def save_today_open_prices_to_cos():
    """将今日开盘价写入COS"""
    try:
        if not COS_SECRET_ID or not COS_SECRET_KEY or not COS_BUCKET:
            return
        client = get_cos_client()
        if client is None:
            return
        
        with data_lock:
            opens = dict(market_data.get("today_open_prices", {}))
        
        if not opens:
            return
        
        rows = [{'symbol': s, 'open_price': float(v)} for s, v in opens.items()]
        df = pd.DataFrame(rows)
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)
        
        key = f"{COS_SNAPSHOT_PREFIX}today_open_prices.parquet"
        client.put_object(Bucket=COS_BUCKET, Key=key, Body=buffer.read())
        print(f"[SNAPSHOT_COS] today_open_prices已上传: {len(rows)} 条")
    except Exception as e:
        print(f"[SNAPSHOT_COS] today_open_prices上传失败: {e}")

def _update_today_open_from_hourly_cache(hourly_cache: dict):
    """从1h K线缓存中提取北京08:00的开盘价，更新today_open_prices"""
    now_utc = datetime.now(tz=timezone.utc)
    # 最近一个已过去的北京08:00 = 今天UTC 00:00
    today_utc0 = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    today_start_ts = int(today_utc0.timestamp())

    updated = 0
    with data_lock:
        today_open = market_data.get("today_open_prices", {})
        for symbol, hklines in hourly_cache.items():
            for h in hklines:
                if h['t'] == today_start_ts:
                    today_open[symbol] = h['o']
                    updated += 1
                    break
        market_data["today_open_prices"] = today_open
    print(f"[开盘价] 从1h K线更新了 {updated} 个币的今日开盘价 (北京08:00)")


# ========== 币种分类映射 ==========
SECTOR_MAP = {}
SECTOR_CACHE = "/tmp/crypto_sectors.json"

def _load_sectors():
    global SECTOR_MAP
    try:
        with open(SECTOR_CACHE, 'r') as f:
            SECTOR_MAP = json.load(f)
    except:
        pass

_load_sectors()

CATEGORY_MAP = {
    "XAUUSDT": ("黄金", "贵金属"),
    "XAUTUSDT": ("黄金", "贵金属"),
    "XAGUSDT": ("白银", "贵金属"),
    "TSLAUSDT": ("特斯拉", "美股"),
    "NVDAUSDT": ("英伟达", "美股"),
    "AMZNUSDT": ("亚马逊", "美股"),
    "GOOGLUSDT": ("谷歌", "美股"),
    "AAPLUSDT": ("苹果", "美股"),
}

def get_table_data():
    """获取表格数据（排序后）"""
    _load_sectors()  # 每天更新的板块缓存
    acquired = data_lock.acquire(timeout=5.0)
    if not acquired:
        return []
    try:
        # 深拷贝避免遍历期间被其他线程修改
        symbols = dict(market_data["symbols"])
        vol_15m_last = dict(market_data.get("vol_15m_last", {}))
        vol_24h_today = dict(market_data.get("vol_24h_today", {}))
        vol_15m_avg_4h = dict(market_data.get("vol_15m_avg_4h", {}))
        vol_15m_current = dict(market_data.get("vol_15m_current", {}))
        today_open_prices = dict(market_data.get("today_open_prices", {}))
    finally:
        data_lock.release()
    
    current_minute = int(time.time()) // 60 * 60

    rows = []
    for symbol, info in symbols.items():
        price = info["price"]
        o = today_open_prices.get(symbol, 0)
        if o <= 0:
            with _daily_kline_lock:
                dkl = _daily_kline_cache.get(symbol, [])
            if dkl:
                o = dkl[-1].get("o", 0)  # 日线开盘价
        if o <= 0:
            o = info.get("o", 0)
        gain_pct = (price - o) / o * 100 if o > 0 else 0

        sectors = SECTOR_MAP.get(symbol, [])
        cat_name = sectors[0] if sectors else None
        cat_type = sectors[0].lower() if sectors else None
        
        # 使用今日累计成交额
        vol_24h = vol_24h_today.get(symbol, info.get("q", 0))
        
        # 获取前4小时的15分钟均值
        vol_15m_avg = vol_15m_avg_4h.get(symbol, 0)
        
        # 获取上一个完整15分钟成交额；如果缺失，回退到当前正在累加的15分钟值
        current_15m = vol_15m_last.get(symbol, 0)
        if current_15m == 0:
            current_15m = vol_15m_current.get(symbol, 0)
        
        # 检查是否突增：使用与 check_volume_surge 一致的阈值 1.4x（回测最优）
        VOL_SURGE_THRESHOLD = 1.4
        is_surge = False
        surge_ratio = 0
        if (vol_15m_avg >= VOL_SURGE_MIN_AVG_VOL 
            and current_15m > vol_15m_avg * VOL_SURGE_THRESHOLD
            and symbol not in STABLECOIN_PAIRS
            and gain_pct > 0):
            is_surge = True
            surge_ratio = current_15m / vol_15m_avg

        rows.append({
            "symbol": symbol,
            "price": price,
            "q": vol_24h,  # 今日累计成交额
            "v": info["v"],
            "o": o,
            "gain_pct": round(gain_pct, 2),
            "cat_name": cat_name,
            "cat_type": cat_type,
            "vol_15m": vol_15m_avg,  # 前4小时的15分钟均值
            "vol_15m_current": current_15m,  # 当前15分钟成交额
            "is_surge": is_surge,
            "surge_ratio": round(surge_ratio, 2) if surge_ratio else 0
        })

    # 排序：突增币种排第一，其他按涨跌幅排序
    surge_rows = [r for r in rows if r["is_surge"]]
    normal_rows = [r for r in rows if not r["is_surge"]]
    
    # 突增币种按突增倍数降序
    surge_rows.sort(key=lambda x: -x["surge_ratio"])
    # 普通币种按涨跌幅降序
    normal_rows.sort(key=lambda x: -x["gain_pct"])
    
    rows = surge_rows + normal_rows

    for i, row in enumerate(rows, 1):
        row["rank"] = i

    return rows

# ========== 布林爬坡 - API回填历史1h K线 ==========

def _save_hourly_cache_to_cos():
    """将1h K线缓存保存到COS"""
    try:
        client = get_cos_client()
        if client is None:
            print("[HOURLY-COS] 上传跳过: 凭证无效")
            return
        with data_lock:
            hourly_cache = dict(market_data.get("hourly_kline_cache", {}))

        if not hourly_cache:
            print("[HOURLY-COS] 上传跳过: hourly_cache为空")
            return

        print(f"[HOURLY-COS] 开始上传: {len(hourly_cache)} 个币种")

        all_rows = []
        for symbol, hklines in hourly_cache.items():
            for h in hklines:
                all_rows.append({
                    'symbol': symbol,
                    'timestamp': h['t'],
                    'open': h['o'],
                    'high': h['h'],
                    'low': h['l'],
                    'close': h['c'],
                    'volume': h.get('v', 0),
                    'quote_volume': h.get('q', 0),
                    'buy_ratio': h.get('buy_ratio', 0.5),
                })

        if not all_rows:
            return

        df = pd.DataFrame(all_rows)
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)

        client.put_object(
            Bucket=COS_BUCKET,
            Key=COS_HOURLY_KEY,
            Body=buffer.read()
        )
        print(f"[HOURLY-COS] 1h K线缓存已上传: {len(all_rows)} 条 ({len(hourly_cache)} 个币种)")
    except Exception as e:
        print(f"[HOURLY-COS] 上传失败: {e}")


def _load_hourly_cache_from_cos():
    """从COS加载1h K线缓存，返回 {symbol: [hourly_kline, ...]}"""
    try:
        client = get_cos_client()
        if client is None:
            return {}
        resp = client.get_object(Bucket=COS_BUCKET, Key=COS_HOURLY_KEY)
        data = resp['Body'].get_raw_stream().read()
        df = pd.read_parquet(io.BytesIO(data))
        print(f"[HOURLY-COS] 从COS加载了 {len(df)} 条1h K线")

        cache = {}
        for symbol in df['symbol'].unique():
            symbol_df = df[df['symbol'] == symbol].sort_values('timestamp')
            hklines = []
            for _, r in symbol_df.iterrows():
                hklines.append({
                    't': int(r['timestamp']),
                    'o': float(r['open']),
                    'h': float(r['high']),
                    'l': float(r['low']),
                    'c': float(r['close']),
                    'v': float(r.get('volume', 0)),
                    'q': float(r.get('quote_volume', 0)),
                    'buy_ratio': float(r.get('buy_ratio', 0.5)),
                })
            cache[symbol] = hklines
        # 截断：只保留最近36根（36小时）
        for symbol in cache:
            if len(cache[symbol]) > MAX_HOURLY_KLINES_PER_SYMBOL:
                cache[symbol] = cache[symbol][-MAX_HOURLY_KLINES_PER_SYMBOL:]
        print(f"[HOURLY-COS] 截断后保留每个币种最近 {MAX_HOURLY_KLINES_PER_SYMBOL} 根1h K线")
        return cache
    except Exception as e:
        print(f"[HOURLY-COS] 加载失败: {e}")
        return {}


# ========== 布林爬坡检测 ==========

def _calculate_buy_ratio(price_delta: float, last_price: float) -> float:
    """从价格变动估算买卖比率（0=全卖, 0.5=均衡, 1=全买）"""
    if last_price <= 0:
        return 0.5
    pct = abs(price_delta) / last_price
    ratio = 0.5 + (0.5 if price_delta > 0 else -0.5) * min(pct * 50, 1.0)
    return max(0.0, min(1.0, ratio))

def _calculate_atr(hourly_klines: list, period: int = 14) -> float | None:
    """计算ATR（Average True Range）"""
    if len(hourly_klines) < period + 1:
        return None
    
    true_ranges = []
    for i in range(len(hourly_klines) - period, len(hourly_klines)):
        k = hourly_klines[i]
        prev_k = hourly_klines[i - 1]
        tr = max(
            k["h"] - k["l"],              # 当日振幅
            abs(k["h"] - prev_k["c"]),    # 当日最高与昨收的差
            abs(k["l"] - prev_k["c"])     # 当日最低与昨收的差
        )
        true_ranges.append(tr)
    
    return sum(true_ranges) / len(true_ranges) if true_ranges else None


def _check_hl_climb_tolerant(hourly_klines: list, idx: int, cfg: dict) -> bool:
    """检查HL抬高条件（带容忍机制）：最近N根中至少M根HL抬高"""
    window = cfg["hl_tolerance_window"]
    min_count = cfg["hl_tolerance_min"]
    
    # 收集窗口内的HL抬高次数（需要和前一根比较，所以从idx-1开始）
    climb_count = 0
    check_start = max(1, idx - window + 2)  # 确保有前一根可以比较
    for i in range(check_start, idx + 1):
        k = hourly_klines[i]
        prev_k = hourly_klines[i - 1]
        if k["h"] > prev_k["h"] and k["l"] > prev_k["l"]:
            climb_count += 1
    
    return climb_count >= min_count


def _check_hour_climb(k: dict, middle: float, upper: float, avg_vol: float, cfg: dict, check_vol: bool = True) -> bool:
    """检查单根K线的独立条件（不含HL，HL需用容忍机制单独判断）

    check_vol=False 时跳过量能检查，用于连续天数回溯扫描。量能只对最新K线检查，放到最后一步。
    """
    # 1. 收盘价 > 中轨 且 在上轨±8%范围内
    if k["c"] <= middle:
        return False
    tolerance = upper * cfg["upper_tolerance_pct"]
    if not (upper - tolerance <= k["c"] <= upper + tolerance):
        return False

    # 2. buy_ratio > 0.55（仅对真实数据检查，默认0.5跳过）
    if not (cfg.get("buy_ratio_skip_default", True) and abs(k.get("buy_ratio", 0.5) - 0.5) < 0.001):
        if k.get("buy_ratio", 0.5) <= cfg["buy_ratio_threshold"]:
            return False

    # 3. 量能 > 1.2倍均量（check_vol=False时跳过）
    if check_vol and avg_vol > 0 and k.get("q", 0) < avg_vol * cfg["volume_ratio"]:
        return False

    return True


def _compute_rolling_bb(closes: list, period: int, std_mult: float) -> tuple[list, list]:
    """预计算滚动布林带（中轨和上轨数组），避免固定窗口偏差
    
    返回: (mids, uppers) 长度 = len(closes) - period + 1
    mids[0] 对应对 hourly_klines[period-1] 的布林带
    """
    mids = []
    uppers = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        std = (sum((c - mid) ** 2 for c in window) / period) ** 0.5
        mids.append(mid)
        uppers.append(mid + std_mult * std)
    return mids, uppers


def _detect_bollinger_climb(symbol: str, hourly_klines: list) -> dict | None:
    """检测布林爬坡信号：收盘价在中轨附近+HL容忍抬高+量能放大+buy_ratio高+ATR趋势"""
    cfg = BB_CLIMB_CONFIG
    
    if symbol in cfg["exclude_symbols"]:
        return None
    
    # 过滤非TRADING状态的币种
    trading_symbols = market_data.get("trading_symbols", set())
    if trading_symbols and symbol not in trading_symbols:
        return None
    
    n_klines = len(hourly_klines)
    min_required = max(cfg["period"] + 1, cfg["atr_period"] + 1)
    if n_klines < min_required:
        return None
    
    # 预计算滚动布林带（每根K线用其当时可观察到的数据）
    closes = [k["c"] for k in hourly_klines]
    bb_mids, bb_uppers = _compute_rolling_bb(closes, cfg["period"], cfg["std_mult"])
    if not bb_mids:
        return None
    
    # bb_mids[i] 对应对 hourly_klines[cfg["period"] - 1 + i]
    # 即 hourly_klines[n] 对应的布林索引 = n - cfg["period"] + 1
    def _bb_idx(n):
        return n - cfg["period"] + 1
    
    last_idx = n_klines - 1
    bb_last = _bb_idx(last_idx)
    if bb_last < 0 or bb_last >= len(bb_mids):
        return None
    
    middle = bb_mids[bb_last]
    upper = bb_uppers[bb_last]
    
    # 计算ATR（全量滑动）
    atr = _calculate_atr(hourly_klines, cfg["atr_period"]) if cfg["atr_enabled"] else None
    
    # 计算前5个已完成日线的平均成交量（不含今天未完成）
    if len(hourly_klines) >= 6:
        avg_volumes = [k.get("q", 0) for k in hourly_klines[-6:-1]]
    else:
        avg_volumes = [k.get("q", 0) for k in hourly_klines[:-1]] if len(hourly_klines) > 1 else [0]
    avg_vol = sum(avg_volumes) / len(avg_volumes) if avg_volumes else 0
    
    # 检查最后一根K线（不含量能，量能放最后一步）
    last_k = hourly_klines[-1]
    if not _check_hour_climb(last_k, middle, upper, avg_vol, cfg, check_vol=False):
        return None

    # 检查HL容忍抬高（基于最后一根的位置）
    if not _check_hl_climb_tolerant(hourly_klines, last_idx, cfg):
        return None

    # ATR趋势过滤
    if atr is not None and atr > 0:
        current_range = last_k["h"] - last_k["l"]
        if current_range < atr * 0.5:
            return None

    # 往前计算持续了几天（回溯扫描跳过量能）
    consecutive_count = 1
    for i in range(n_klines - 2, -1, -1):
        k = hourly_klines[i]
        bb_i = _bb_idx(i)
        if bb_i < 0:
            break
        mid_i = bb_mids[bb_i]
        upper_i = bb_uppers[bb_i]
        if not _check_hour_climb(k, mid_i, upper_i, avg_vol, cfg, check_vol=False):
            break
        if not _check_hl_climb_tolerant(hourly_klines, i, cfg):
            break
        consecutive_count += 1

    # 10. 现货/合约日线阴阳一致性
    with _daily_kline_lock_futures:
        fut_kl = _daily_kline_cache_futures.get(symbol, [])
    if fut_kl:
        fut_last = fut_kl[-1]
        spot_dir = last_k["c"] - last_k["o"]
        fut_dir = fut_last["c"] - fut_last["o"]
        if (spot_dir > 0) != (fut_dir > 0):
            return None  # 现货合约阴阳不一致

    # 11. 量能检查放最后一步（仅对最新K线）
    # 宽松条件：量>0.3x5日均 且 阳线 → 行情启动初期放过
    # 严格条件：量>1.2x5日均 → 放量确认
    q_today = last_k.get("q", 0)
    is_bullish = last_k["c"] > last_k["o"]
    if avg_vol > 0:
        if q_today < avg_vol * cfg["volume_ratio"] and not (q_today > avg_vol * 0.3 and is_bullish):
            return None

    # 只取最后consecutive_count根K线
    valid_hours = hourly_klines[-consecutive_count:]
    
    return {
        "symbol": symbol,
        "upper": round(upper, 6),
        "middle": round(middle, 6),
        "atr": round(atr, 6) if atr else None,
        "consecutive_hours": consecutive_count,
        "avg_volume_24h": round(avg_vol, 2),
        "valid_hours": [{
            "t": h["t"],
            "o": round(h["o"], 6),
            "h": round(h["h"], 6),
            "l": round(h["l"], 6),
            "c": round(h["c"], 6),
            "v": round(h.get("v", 0), 2),
            "buy_ratio": round(h["buy_ratio"], 3)
        } for h in valid_hours]
    }


def _diagnose_bb(symbol: str, klines: list, cfg: dict) -> tuple:
    """诊断BB检测失败原因，返回 (signal_or_none, reason_str, detail_dict)"""
    # 1. exclude check
    if symbol in cfg["exclude_symbols"]:
        return None, "excluded", {}

    # 2. trading_symbols check
    trading_symbols = market_data.get("trading_symbols", set())
    if trading_symbols and symbol not in trading_symbols:
        return None, "not_trading", {}

    n = len(klines)
    min_required = max(cfg["period"] + 1, cfg["atr_period"] + 1)
    if n < min_required:
        return None, "too_short", {"n_klines": n, "min_required": min_required}

    closes = [k["c"] for k in klines]
    bb_mids, bb_uppers = _compute_rolling_bb(closes, cfg["period"], cfg["std_mult"])
    if not bb_mids:
        return None, "bb_calc_fail", {}

    last_idx = n - 1
    bb_last = last_idx - cfg["period"] + 1
    if bb_last < 0 or bb_last >= len(bb_mids):
        return None, "bb_idx_fail", {}

    middle = bb_mids[bb_last]
    upper = bb_uppers[bb_last]
    last_k = klines[-1]

    # 3. ATR
    atr = _calculate_atr(klines, cfg["atr_period"]) if cfg["atr_enabled"] else None

    # 4. avg volume
    avg_volumes = [k.get("q", 0) for k in klines[-6:-1]] if len(klines) >= 6 else [k.get("q", 0) for k in klines[:-1]] if len(klines) > 1 else [0]
    avg_vol = sum(avg_volumes) / len(avg_volumes) if avg_volumes else 0

    detail = {
        "middle": round(middle, 8),
        "upper": round(upper, 8),
        "close": round(last_k["c"], 8),
        "q": round(last_k.get("q", 0), 0),
        "avg_vol_5d": round(avg_vol, 0),
        "atr": round(atr, 6) if atr else 0,
        "range": round(last_k["h"] - last_k["l"], 8),
    }

    # 5. close vs middle/upper band
    if last_k["c"] <= middle:
        return None, "below_mid", detail
    tolerance = upper * cfg["upper_tolerance_pct"]
    if not (upper - tolerance <= last_k["c"] <= upper + tolerance):
        gap_pct = abs(last_k["c"] - upper) / upper * 100
        detail["gap_pct"] = round(gap_pct, 1)
        return None, "off_upper", detail

    # 6. buy_ratio (skip for default 0.5)
    if not (cfg.get("buy_ratio_skip_default", True) and abs(last_k.get("buy_ratio", 0.5) - 0.5) < 0.001):
        if last_k.get("buy_ratio", 0.5) <= cfg["buy_ratio_threshold"]:
            return None, "buy_ratio_low", detail

    # 7. HL climb
    if not _check_hl_climb_tolerant(klines, last_idx, cfg):
        return None, "hl_fail", detail

    # 8. ATR
    if atr is not None and atr > 0:
        current_range = last_k["h"] - last_k["l"]
        if current_range < atr * 0.5:
            return None, "atr_fail", detail

    # 9. consecutive (回溯扫描跳过量能，量能放在最后第10步)
    consecutive_count = 1
    for i in range(n - 2, -1, -1):
        k = klines[i]
        bb_i = i - cfg["period"] + 1
        if bb_i < 0:
            break
        mid_i = bb_mids[bb_i]
        upper_i = bb_uppers[bb_i]
        if not _check_hour_climb(k, mid_i, upper_i, avg_vol, cfg, check_vol=False):
            break
        if not _check_hl_climb_tolerant(klines, i, cfg):
            break
        consecutive_count += 1

    if consecutive_count < 4:
        return None, "consecutive_short", {**detail, "consecutive": consecutive_count}

    # 10. 现货/合约日线阴阳一致性
    fut_sym = symbol
    with _daily_kline_lock_futures:
        fut_kl = _daily_kline_cache_futures.get(fut_sym, [])
    if fut_kl:
        fut_last = fut_kl[-1]
        spot_dir = last_k["c"] - last_k["o"]  # 现货方向
        fut_dir = fut_last["c"] - fut_last["o"]  # 合约方向
        if (spot_dir > 0) != (fut_dir > 0):
            detail["spot_dir"] = round(spot_dir, 6)
            detail["fut_dir"] = round(fut_dir, 6)
            return None, "spot_fut_mismatch", detail
    # 无合约数据时默认通过（非所有币种都有合约交易对）

    # 11. 量能检查
    #   严格：量 > 1.2x5日均 → 直接通过
    #   宽松：量 > 0.3x5日均 → 进入⑫方向检测
    #   不足：量 < 0.3x → 淘汰
    passed_relaxed = False
    if avg_vol > 0:
        q_today = last_k.get("q", 0)
        vol_ratio = q_today / avg_vol
        detail["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio >= cfg["volume_ratio"]:
            pass  # 严格，直接过
        elif vol_ratio > 0.3:
            passed_relaxed = True  # 宽松，交给⑫
        else:
            return None, "vol_low", detail

    # 12. 反包复活（仅宽松通过 + 前日收阴的进入）
    #   两天结构：[-3]阴被[-2]阳吞掉 → 复活给信号
    #   不收阴的由⑪自行判断，不归⑫管
    if passed_relaxed and len(klines) >= 3:
        cmpl = klines[-2]        # 最近完工日线
        cmpl_prev = klines[-3]   # 再前一根

        if cmpl_prev["c"] < cmpl_prev["o"] and cmpl["c"] >= cmpl["o"]:
            # 前阴+昨阳 → 查反包
            tol = cmpl_prev["c"] * 0.001  # 0.1%容差，避免现货/合约tick差异
            engulfing = (cmpl["o"] <= cmpl_prev["c"] + tol and cmpl["c"] >= cmpl_prev["o"] - tol)
            if engulfing:
                detail["engulfing"] = True
                detail["prev_close"] = round(cmpl_prev["c"], 6)
                detail["prev_open"] = round(cmpl_prev["o"], 6)
            else:
                # 未反包 → 阴线存入队列（未淘汰过的）
                with _resurrection_lock:
                    _resurrection_queue[symbol] = {
                        "bearish_ts": cmpl_prev["t"],
                        "bearish_o": cmpl_prev["o"],
                        "bearish_c": cmpl_prev["c"],
                    }
                return None, "close_bearish_revoked", detail
        elif cmpl["c"] < cmpl["o"]:
            # 最近完工收阴 → 存入队列（未淘汰过的）
            with _resurrection_lock:
                _resurrection_queue[symbol] = {
                    "bearish_ts": cmpl["t"],
                    "bearish_o": cmpl["o"],
                    "bearish_c": cmpl["c"],
                }
            return None, "close_bearish_revoked", detail
        # 两根都阳：不归⑫管，⑪已判过

    # PASSED
    signal = {
        "symbol": symbol,
        "upper": round(upper, 6),
        "middle": round(middle, 6),
        "atr": round(atr, 6) if atr else None,
        "consecutive_hours": consecutive_count,
        "avg_volume_24h": round(avg_vol, 2),
        "valid_hours": [{
            "t": h["t"], "o": round(h["o"], 6), "h": round(h["h"], 6),
            "l": round(h["l"], 6), "c": round(h["c"], 6),
            "v": round(h.get("v", 0), 2), "buy_ratio": round(h["buy_ratio"], 3)
        } for h in klines[-consecutive_count:]],
    }
    return signal, "passed", detail


def _detect_cc(symbol: str, klines: list, cfg: dict, force_pass: bool = False) -> dict | None:
    """CC爆发检测: 连续大阳线+放量+价格在上轨附近/上方。
    抓BB爬坡漏掉的火箭发射型币种（如TAGUSDT）。

    条件:
      - 连续>=2天阳线
      - 其中>=1天涨幅>=15%
      - 价格在上轨95%以上
      - HL爬升通过
      - 量能不低于5日均的50%
    """
    if symbol in cfg.get("exclude_symbols", set()):
        return None
    if len(klines) < cfg["period"] + 2:
        return None

    closes = [k["c"] for k in klines]
    mids, uppers = _compute_rolling_bb(closes, cfg["period"], cfg["std_mult"])
    if not uppers:
        return None

    n = len(klines)
    bi = n - 1 - cfg["period"] + 1
    if bi < 0 or bi >= len(uppers):
        return None

    upper = uppers[bi]
    today = klines[-1]

    # 已锁定的跳过盘中检测，只在日线收阴时解锁
    if force_pass:
        return _detect_cc_force(symbol, klines, cfg)

    if today["c"] <= today["o"]:
        return None
    if today["c"] <= upper * 0.95:
        return None

    consec = 0
    big_days = 0
    valid_hours = []
    for i in range(n - 1, -1, -1):
        k = klines[i]
        if k["c"] <= k["o"]:
            break
        gain = (k["c"] - k["o"]) / k["o"] * 100
        bi_k = i - cfg["period"] + 1
        valid_hours.append({
            "t": k["t"], "o": round(k["o"], 6), "h": round(k["h"], 6),
            "l": round(k["l"], 6), "c": round(k["c"], 6),
            "gain_pct": round(gain, 1),
        })
        if gain >= 15:
            big_days += 1
        consec += 1

    # 通过条件：连阳2天+爆量1天，或反包结构
    if consec >= 2 and big_days >= 1:
        pass  # 正常通过
    elif n >= 3:
        # 反包复活：klines[-2]阳吞klines[-3]阴，且[-2]爆量>15%
        cmpl = klines[-2]; cmpl_prev = klines[-3]
        if (cmpl_prev["c"] < cmpl_prev["o"]  # 前日阴
            and cmpl["c"] > cmpl["o"]         # 昨日阳
            and cmpl["o"] <= cmpl_prev["c"]   # 开盘≤阴收
            and cmpl["c"] >= cmpl_prev["o"]): # 收盘≥阴开
            engulf_gain = (cmpl["c"] - cmpl["o"]) / cmpl["o"] * 100
            if engulf_gain >= 15:
                pass  # 反包通过，连阳从昨日计1天
            else:
                return None
        else:
            return None
    else:
        return None

    if not _check_hl_climb_tolerant(klines, n - 1, cfg):
        return None

    if n >= 6:
        prev5 = [klines[j].get("q", 0) for j in range(n - 6, n - 1)]
        avg5 = sum(prev5) / 5
        if today.get("q", 0) < avg5 * 0.5:
            return None

    today_gain = (today["c"] - today["o"]) / today["o"] * 100
    valid_hours.reverse()
    return {
        "symbol": symbol,
        "upper": round(upper, 6),
        "close": round(today["c"], 6),
        "today_gain_pct": round(today_gain, 1),
        "consecutive_days": consec,
        "big_gain_days": big_days,
        "valid_hours": valid_hours,
    }


def _detect_cc_force(symbol: str, klines: list, cfg: dict) -> dict | None:
    """已锁定的CC信号：只检查日线收阴，收阴则解锁"""
    if len(klines) < 3:
        _cc_locked.pop(symbol, None)
        return None
    cmpl = klines[-2]  # 最近完工日线
    if cmpl["c"] < cmpl["o"]:  # 收阴 → 解锁
        _cc_locked.pop(symbol, None)
        return None
    # 未收阴 → 保持锁定，更新价格但保留原始统计数据
    cached = _cc_locked.get(symbol, {})
    closes = [k["c"] for k in klines]
    mids, uppers = _compute_rolling_bb(closes, cfg["period"], cfg["std_mult"])
    n = len(klines); bi = n - 1 - cfg["period"] + 1
    upper = uppers[bi] if bi < len(uppers) else 0
    gain = (klines[-1]["c"] - klines[-1]["o"]) / klines[-1]["o"] * 100
    return {
        "symbol": symbol, "upper": round(upper, 6),
        "close": round(klines[-1]["c"], 6),
        "today_gain_pct": round(gain, 1),
        "consecutive_days": cached.get("consecutive_days", 0),
        "big_gain_days": cached.get("big_gain_days", 0),
        "locked": True, "valid_hours": [],
    }


def _refresh_cc_cache():
    """后台刷新CC爆发信号"""
    global _cc_cache, _cc_locked

    with _daily_kline_lock:
        spot_cache = dict(_daily_kline_cache)
    with _daily_kline_lock_futures:
        fut_cache = dict(_daily_kline_cache_futures)

    cfg = DAILY_BB_CONFIG
    results = []

    all_symbols = set(spot_cache.keys()) | set(fut_cache.keys())
    for symbol in all_symbols:
        klines = spot_cache.get(symbol) or fut_cache.get(symbol)
        if klines is None:
            continue
        force = symbol in _cc_locked
        signal = _detect_cc(symbol, klines, cfg, force_pass=force)
        if signal:
            results.append(signal)
            if not signal.get("locked"):
                # 首次触发，缓存原始数据
                _cc_locked[symbol] = {
                    "consecutive_days": signal.get("consecutive_days", 0),
                    "big_gain_days": signal.get("big_gain_days", 0),
                }

    results.sort(key=lambda x: -x["today_gain_pct"])
    print(f"[CC] 刷新完成: {len(results)}个CC爆发信号 (基于{len(all_symbols)}币种)")

    with _cc_lock:
        _cc_cache = {
            "results": results[:30],
            "updated_at": time.time(),
        }


@app.route("/api/sector_heatmap")
def api_sector_heatmap():
    try:
        with open("/tmp/sector_heatmap.json", "r") as f:
            return jsonify({"code": 0, "data": json.load(f)})
    except:
        return jsonify({"code": 0, "data": []})

@app.route("/api/liq_heatmap")
def api_liq_heatmap():
    try:
        with open("/tmp/liquidation_heatmap.json", "r") as f:
            return jsonify({"code": 0, "data": json.load(f)})
    except:
        return jsonify({"code": 0, "data": {}})


NOTES_FILE = "/tmp/trading_notes.json"

def _load_notes():
    try:
        with open(NOTES_FILE, 'r') as f:
            return json.load(f).get("notes", [])
    except:
        return []

def _save_notes(notes):
    with open(NOTES_FILE, 'w') as f:
        json.dump({"notes": notes, "updated": time.time()}, f)

@app.route("/api/notes", methods=["GET", "POST", "DELETE"])
def api_notes():
    if request.method == "GET":
        return jsonify({"code": 0, "notes": _load_notes()})
    elif request.method == "DELETE":
        idx = request.args.get("id", "")
        notes = _load_notes()
        notes = [n for n in notes if n.get("id") != idx]
        _save_notes(notes)
        return jsonify({"code": 0})
    else:
        data = request.get_json() or {}
        notes = _load_notes()
        note_id = data.get("id", "")
        title = data.get("title", "").strip()
        content = data.get("content", "").strip()

        if note_id:  # 编辑已有
            for n in notes:
                if n.get("id") == note_id:
                    if title: n["title"] = title
                    n["content"] = content
                    n["updated_at"] = time.time()
                    break
        else:  # 新建
            notes.append({
                "id": str(uuid.uuid4())[:8],
                "title": title or "无标题",
                "content": content,
                "created_at": time.time(),
                "updated_at": time.time(),
            })
        _save_notes(notes)
        return jsonify({"code": 0, "notes": notes})


def _fetch_mcp_context(query: str) -> str:
    """根据用户问题智能预拉相关数据"""
    import re as _re
    parts = []

    # 提取币种
    symbols = _re.findall(r'\b([A-Z]{2,10})USDT\b|\b([A-Z]{2,10})\b', query.upper())
    coins = set()
    for m in symbols:
        coins.add((m[0] or m[1]) + 'USDT')

    # 1. 价格+行情
    price_words = ['价格','行情','涨','跌','突破','支撑','阻力','多少','价位','现在']
    if any(w in query for w in price_words) and coins:
        for sym in list(coins)[:3]:
            try:
                import requests as _req
                r = _req.get(f"https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym}, timeout=5)
                d = r.json()
                parts.append(f"{sym}: 价格={d.get('lastPrice')} 24h涨跌={d.get('priceChangePercent')}% 成交量={float(d.get('quoteVolume',0))/1e6:.1f}M")
            except: pass

    # 2. 吸筹信号
    if any(w in query for w in ['吸筹','量比','天量','放量','缩量','TON模式']):
        try:
            with open("/tmp/accumulation_scan.json") as f:
                data = json.load(f)
            top = data.get("results", [])[:8]
            lines = []
            for r in top:
                lines.append(f"{r['symbol']}: {r['signal_date']}出现吸筹信号(量比{r['vol_ratio']}x,价格{r['signal_price']:.4f})，信号至今涨幅{r['fwd_return']:.1f}%，当前价{r['current_price']:.4f}")
            parts.append("近期吸筹信号(注意:信号日是吸筹出现日,涨幅=信号日到现在的收益,不是当日涨幅):\n" + "\n".join(lines))
        except: pass

    # 3. 资金费率
    if any(w in query for w in ['费率','资金费率','funding']):
        for sym in list(coins)[:2]:
            try:
                r = _req.get(f"https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": sym}, timeout=5)
                d = r.json()
                parts.append(f"{sym} 资金费率: {float(d.get('lastFundingRate',0))*100:.4f}%")
            except: pass

    # 4. 板块数据
    if any(w in query for w in ['板块','赛道','行业','Meme','AI','L1','L2','DeFi','RWA']):
        try:
            with open("/tmp/sector_heatmap.json") as f:
                data = json.load(f)
            parts.append("板块资金流: " + ", ".join(f"{s['name']}{s['mc_change_pct']:+.1f}%" for s in data[:6]))
        except: pass

    # 5. 历史新闻
    if any(w in query for w in ['新闻','消息','公告','事件','为什么']):
        keywords = [c.replace('USDT','') for c in list(coins)[:2]]
        for kw in keywords:
            archive = "/home/myuser/news_monitor/archive"
            results = []
            try:
                for fname in sorted(os.listdir(archive), reverse=True)[:30]:
                    with open(os.path.join(archive, fname)) as f:
                        for line in f:
                            if kw.lower() in line.lower() and len(line.strip())>15:
                                results.append(line.strip()[:120])
                                if len(results)>=3: break
                    if len(results)>=3: break
                if results:
                    parts.append(f"{kw}相关新闻: {'; '.join(results)}")
            except: pass

    # 6. CMC新闻(从日报读取，每天已抓取)
    if any(w in query for w in ['CMC','coinmarketcap','市场热点','最新消息','头条','热点新闻']):
        try:
            report_path = "/home/myuser/news_monitor/daily_report.txt"
            if os.path.exists(report_path):
                with open(report_path) as f:
                    in_cmc = False
                    lines = []
                    for line in f:
                        if '市场热点' in line:
                            in_cmc = True
                            continue
                        if in_cmc and '━━━' in line:
                            break
                        if in_cmc and line.strip().startswith('📰'):
                            lines.append(line.strip()[:200])
                            if len(lines) >= 10:
                                break
                    if lines:
                        parts.append("CMC今日市场热点:\n" + "\n".join(f"  {l}" for l in lines))
        except: pass

    # 7. CMC主动搜索 (当日报没有覆盖时)
    if any(w in query for w in ['搜索CMC','查CMC','搜索一下','帮我查','查查','有没有.*新闻','搜.*CMC']):
        keywords = [w for w in query.split() if len(w) >= 3 and not w.startswith('搜')][:3]
        for kw in keywords[:2]:
            try:
                from playwright.sync_api import sync_playwright
                def _cmc_search(keyword):
                    with sync_playwright() as p:
                        b = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-blink-features=AutomationControlled'])
                        ctx = b.new_context(viewport={'width':1920,'height':1080},user_agent='Mozilla/5.0')
                        page = ctx.new_page()
                        page.goto(f'https://coinmarketcap.com/community/articles/browse/?sort=-publishedOn', wait_until='domcontentloaded',timeout=15000)
                        page.wait_for_timeout(3000)
                        titles = page.evaluate(f'''()=>{{
                            return Array.from(document.querySelectorAll('div[class*="post-item"]'))
                                .map(el=>el.textContent.replace(/\\\\s+/g,' ').trim())
                                .filter(t=>t.toLowerCase().includes("{keyword.lower()}"))
                                .slice(0,5).map(t=>t.slice(0,200));
                        }}''')
                        b.close()
                        return titles
                results = _cmc_search(kw)
                if results:
                    parts.append("CMC搜索[" + kw + "]结果:\n" + "\n".join("  • " + r for r in results))
                break  # 只搜第一个有效关键词
            except:
                pass

    # 8. SoSoValue宏观
    if any(w in query for w in ['宏观','市值','成交量','市场情绪','sosovalue']):
        try:
            report_path = "/home/myuser/news_monitor/daily_report.txt"
            if os.path.exists(report_path):
                with open(report_path) as f:
                    in_sec = False
                    for line in f:
                        if '市场宏观' in line or 'SoSoValue' in line:
                            in_sec = True
                            continue
                        if in_sec and line.strip().startswith('📰'):
                            parts.append("SoSoValue: " + line.strip()[:200])
                        if in_sec and ('━━━' in line or '币安公告' in line):
                            in_sec = False
        except: pass

    return "\n[实时数据]\n" + "\n".join(parts) if parts else ""


_async_tasks = {}  # 异步AI任务缓存

@app.route("/api/ask", methods=["POST"])
def api_ask():
    """AI搜索：异步执行，返回task_id供轮询"""
    data = request.get_json() or {}
    news_title = data.get("title", "")
    question = data.get("question", "")
    history = data.get("history", [])

    if not news_title or not question:
        return jsonify({"code": 1, "msg": "缺少参数"})

    history_text = ""
    if history:
        history_text = "对话历史：\n"
        for h in history[-6:]:
            history_text += f"用户: {h.get('q','')}\n分析师: {h.get('a','')[:200]}\n"

    # MCP预查：智能提取上下文数据
    mcp_context = _fetch_mcp_context(news_title + " " + question)

    prompt = f"""你是一位加密货币市场分析师。根据以下数据回答用户问题（300字以内，中文）。

注意：吸筹信号的"涨幅"是从信号出现日至今的累计收益，不是单日涨跌。
信号日=主力资金异动日，涨幅=从那天到现在总共涨了多少。

新闻/标题：{news_title}
{mcp_context}
{history_text}
用户问题：{question}

请直接回答，不需要问候语。数据中有具体数字的请引用。"""

    # 异步执行：立即返回task_id，后台继续跑
    task_id = str(uuid.uuid4())[:8]
    _async_tasks[task_id] = {"status": "running", "created_at": time.time(), "title": news_title, "question": question}

    def _run_ai():
        try:
            import requests as _req
            resp = _req.post(
                "https://api.deepseek.com/anthropic/v1/messages",
                headers={
                    "x-api-key": os.environ.get("DEEPSEEK_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": "deepseek-v4-pro[1m]", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
                timeout=90
            )
            if resp.status_code == 200:
                result = resp.json()
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        _async_tasks[task_id] = {"status": "done", "answer": block.get("text",""), "created_at": time.time()}
                        return
            _async_tasks[task_id] = {"status": "error", "msg": f"API:{resp.status_code}", "created_at": time.time()}
        except Exception as e:
            _async_tasks[task_id] = {"status": "error", "msg": str(e), "created_at": time.time()}

    threading.Thread(target=_run_ai, daemon=True).start()
    return jsonify({"code": 0, "task_id": task_id, "status": "running"})


@app.route("/api/ask/status")
def api_ask_status():
    task_id = request.args.get("id", "")
    if task_id in _async_tasks:
        return jsonify(_async_tasks[task_id])
    return jsonify({"status": "not_found"})


@app.route("/api/guardian_status")
def api_guardian_status():
    try:
        with open("/tmp/guardian_status.json", "r") as f:
            return jsonify({"code": 0, **json.load(f)})
    except:
        return jsonify({"code": 0, "services": []})


@app.route("/api/predictions")
def api_predictions():
    try:
        with open("/tmp/daily_predictions.json", "r") as f:
            return jsonify({"code": 0, **json.load(f)})
    except:
        return jsonify({"code": 0, "predictions": [], "updated": 0})


@app.route("/api/accumulation")
def api_accumulation():
    if request.args.get("scan"):
        def run_scan():
            try:
                from accumulation_scanner import scan
                scan()
            except Exception as e:
                print(f"Accumulation scan failed: {e}")
        threading.Thread(target=run_scan, daemon=True).start()
        return jsonify({"code": 0, "msg": "扫描已触发"})
    try:
        with open("/tmp/accumulation_scan.json", "r") as f:
            return jsonify({"code": 0, **json.load(f)})
    except:
        return jsonify({"code": 0, "found": 0, "results": []})


@app.route("/accumulation")
def page_accumulation():
    response = make_response(render_template("accumulation.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/api/sector_coins")
def api_sector_coins():
    """返回某个板块的币种列表（实时价格优先，日线兜底）"""
    sector = request.args.get("name", "")
    _load_sectors()
    coins = []
    with data_lock:
        syms = dict(market_data.get("symbols", {}))
    with _daily_kline_lock:
        spot_cache = dict(_daily_kline_cache)
    with _daily_kline_lock_futures:
        fut_cache = dict(_daily_kline_cache_futures)
    for bsym, labels in SECTOR_MAP.items():
        if sector in (labels or []):
            # 实时价格
            info = syms.get(bsym, {})
            price = info.get("price", 0) if info else 0
            # 开盘价：日线当天open（有实时价格）或前一天close（日线兜底）
            dkl = spot_cache.get(bsym) or fut_cache.get(bsym)
            o = 0
            if dkl and len(dkl) >= 2:
                if price > 0:
                    o = dkl[-1].get("o", 0)
                else:
                    # 没有实时价格，全用日线
                    price = dkl[-1].get("c", 0)
                    o = dkl[-2].get("c", 0)
            if price > 0 and o > 0:
                gain = (price - o) / o * 100
                coins.append({
                    "symbol": bsym,
                    "gain_pct": round(gain, 1),
                })
    coins.sort(key=lambda x: -x["gain_pct"])
    return jsonify({"code": 0, "name": sector, "count": len(coins), "data": coins[:30]})


@app.route("/api/sector_heatmap")


@app.route("/api/resurrection_queue")
def api_resurrection_queue():
    """返回复活赛队列"""
    with _resurrection_lock:
        queue = dict(_resurrection_queue)
    return jsonify({"code": 0, "count": len(queue), "data": queue})


@app.route("/api/cc_anchors")
def api_cc_anchors():
    """读取sim_trade的CC锚持仓状态"""
    import json, os
    state_file = "/tmp/sim_trade_state.json"
    try:
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
            anchors = state.get("cc_anchors", [])
            return jsonify({"code": 0, "count": len(anchors), "data": anchors})
    except Exception:
        pass
    return jsonify({"code": 0, "count": 0, "data": []})


@app.route("/news")
def page_news():
    response = make_response(render_template("news.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/api/news/search")
def api_news_search():
    """搜索历史日报，返回匹配的新闻条目"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"code": 1, "msg": "搜索词太短"})

    archive_dir = "/home/myuser/news_monitor/archive"
    results = []
    try:
        for fname in sorted(os.listdir(archive_dir), reverse=True)[:90]:  # 最近90天
            if not fname.endswith('.txt'):
                continue
            filepath = os.path.join(archive_dir, fname)
            date_str = fname.replace('daily_report_','').replace('.txt','')
            with open(filepath, 'r') as f:
                for line in f:
                    if q.lower() in line.lower() and len(line.strip()) > 10:
                        results.append({"date": date_str, "text": line.strip()[:200]})
                        if len(results) >= 30:
                            break
            if len(results) >= 30:
                break
    except:
        pass
    return jsonify({"code": 0, "query": q, "count": len(results), "data": results})


@app.route("/api/news")
def api_news():
    """解析日报txt返回JSON"""
    import re
    report_path = "/home/myuser/news_monitor/daily_report.txt"
    try:
        with open(report_path, 'r') as f:
            text = f.read()
    except:
        return jsonify({"code": 1, "msg": "日报文件不存在"})

    sections = []
    current_section = None
    current_category = None

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # 日期行
        if '日报' in line and '|' in line:
            continue
        # 区块头 ━━━ 📢 币安公告 ━━━
        if '━━━' in line:
            name = line.replace('━━━', '').strip()
            current_section = {"name": name, "categories": [], "items": []}
            sections.append(current_section)
            current_category = None
            continue
        # 分类头 🆕 新币上架
        if line.startswith(('🆕','📌','🎯','🗑️','🎁','🔥')):
            parts = line.split(' ', 1)
            icon = parts[0] if len(parts) > 0 else ''
            title = parts[1] if len(parts) > 1 else line
            if current_section:
                current_category = {"icon": icon, "name": title, "items": []}
                current_section["categories"].append(current_category)
            continue
        # 条目 • xxx → COIN  或  📰 xxx [COIN]
        is_bullet = line.startswith('•')
        is_cmc = line.lstrip().startswith('📰')
        if is_bullet or is_cmc:
            item_text = line.lstrip()[1:].strip() if is_cmc else line[1:].strip()
            coins = []
            # [COIN, COIN] bracket format (CMC)
            bracket = re.search(r'\[([^\]]+)\]', item_text)
            if bracket:
                coins = [c.strip() for c in bracket.group(1).split(',') if c.strip()]
                item_text = item_text[:bracket.start()].strip()
            # → COIN,COIN format (Binance)
            if '→' in item_text:
                parts = item_text.rsplit('→', 1)
                item_text = parts[0].strip()
                coins2 = [c.strip() for c in parts[1].split(',') if c.strip()]
                coins = list(set(coins + coins2))
            item = {"title": item_text, "coins": coins}
            if current_category:
                current_category["items"].append(item)
            elif current_section:
                current_section["items"].append(item)

    return jsonify({"code": 0, "data": sections, "updated": os.path.getmtime(report_path) if os.path.exists(report_path) else 0})


@app.route("/api/cc_signals")
def api_cc_signals():
    """返回CC爆发检测信号"""
    with _cc_lock:
        cache = dict(_cc_cache)
    return jsonify({
        "code": 0,
        "count": len(cache.get("results", [])),
        "data": cache.get("results", []),
        "updated_at": cache.get("updated_at", 0),
    })


@app.route("/api/bollinger_climb")
def api_bollinger_climb():
    """返回日线布林爬坡信号（已统一为日线版本，兼容前端）"""
    with _bb_daily_lock:
        cache = dict(_bb_daily_cache)

    return jsonify({
        "code": 0,
        "count": len(cache.get("results", [])),
        "data": cache.get("results", []),
        "candidate_count": 0,
        "candidates": [],
        "updated_at": cache.get("updated_at", 0),
    })


# ========== 日线BB爬坡检测 (V7 混合策略用) ==========

_bb_daily_cache = {"results": [], "candidates": [], "updated_at": 0}
_bb_daily_lock = threading.Lock()
_bb_diagnostic = {"total": 0, "breakdown": {}, "symbols": {}, "updated_at": 0}
_bb_diagnostic_lock = threading.Lock()
_cc_cache = {"results": [], "updated_at": 0}  # CC爆发检测
_cc_lock = threading.Lock()
_cc_locked = {}  # {symbol: {consecutive_days, big_gain_days, today_gain_pct, ...}} 锁定信号缓存
_resurrection_queue = {}  # {symbol: {"bearish_ts": int, "bearish_o": float, "bearish_c": float}}  复活赛队列
_resurrection_lock = threading.Lock()

DAILY_BB_CONFIG = {
    "period": 20,
    "std_mult": 2.5,
    "upper_tolerance_pct": 0.08,
    "buy_ratio_threshold": 0.55,
    "buy_ratio_skip_default": True,
    "volume_ratio": 1.2,
    "hl_tolerance_window": 5,
    "hl_tolerance_min": 3,
    "atr_period": 14,
    "atr_enabled": True,
    "exclude_symbols": {
        # 大盘/股票/商品
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT',
        'TSLAUSDT', 'NVDAUSDT', 'AMZNUSDT', 'GOOGLUSDT', 'AAPLUSDT',
        'COINUSDT', 'MSTRUSDT', 'METAUSDT', 'TSMUSDT',
        'XAUUSDT', 'XAGUSDT', 'XAUTUSDT', 'NATGASUSDT',
        # 稳定币对
        'USDCUSDT', 'RLUSDUSDT', 'UUSDT', 'XUSDUSDT', 'USD1USDT',
        'FDUSDUSDT', 'TUSDUSDT', 'PAXUSDT', 'BUSDUSDT', 'SUSDUSDT',
        'USDEUSDT', 'USDPUSDT', 'USDSUSDT', 'AEURUSDT', 'EURIUSDT', 'EURUSDT',
        'BFUSDUSDT',
        # 现货专属（期货无此交易对，对齐回测数据）
        'ACMUSDT', 'ADXUSDT', 'ALCXUSDT', 'AMPUSDT', 'ARDRUSDT',
        'ATMUSDT', 'AUDIOUSDT', 'BARUSDT', 'BNSOLUSDT',
        'BTTCUSDT', 'CITYUSDT', 'DCRUSDT', 'DGBUSDT', 'DODOUSDT',
        'FARMUSDT', 'FTTUSDT', 'GLMRUSDT', 'GNOUSDT', 'GNSUSDT',
        'IQUSDT', 'JUVUSDT', 'KGSTUSDT', 'LAZIOUSDT', 'LUNAUSDT',
        'MBLUSDT', 'NEXOUSDT', 'OSMOUSDT', 'PIVXUSDT', 'PONDUSDT',
        'PORTOUSDT', 'PSGUSDT', 'PYRUSDT', 'QIUSDT', 'QKCUSDT',
        'QUICKUSDT', 'RADUSDT', 'REQUSDT', 'SCUSDT', 'STRAXUSDT',
        'TFUELUSDT', 'TKOUSDT', 'WBETHUSDT', 'WBTCUSDT', 'WINUSDT',
        'XNOUSDT',
    },
}



# ========== 日线K线缓存（V7策略核心数据源）==========
_daily_kline_cache = {}
_daily_kline_lock = threading.Lock()
_daily_kline_cache_futures = {}  # 合约日线（用于现货/合约阴阳一致性检查）
_daily_kline_lock_futures = threading.Lock()


def _fetch_daily_klines(symbol: str, limit: int = 40) -> list:
    """从币安API获取日线K线（t/o/h/l/c/v/q格式）"""
    try:
        resp = _requests_session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data:
            return []
        result = []
        for k in data:
            result.append({
                "t": int(k[0]) // 1000,
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
                "q": float(k[7]),
                "buy_ratio": 0.5,
            })
        return result
    except Exception as e:
        return []


def _fetch_futures_daily_klines(symbol: str, limit: int = 40) -> list:
    """从币安合约API获取日线K线"""
    try:
        resp = _requests_session.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data:
            return []
        result = []
        for k in data:
            result.append({
                "t": int(k[0]) // 1000,
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
                "q": float(k[7]),
                "buy_ratio": 0.5,
            })
        return result
    except Exception as e:
        return []


def _load_all_daily_klines():
    """批量加载所有币种的日线K线（现货+合约，并发20线程）"""
    global _daily_kline_cache, _daily_kline_cache_futures

    with data_lock:
        spot_symbols = set(market_data.get("symbols", {}).keys())

    # 合约交易对（含现货没有的币种，如TAGUSDT）
    fut_symbols = set()
    try:
        resp = _requests_session.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        if resp.status_code == 200:
            for s in resp.json().get("symbols", []):
                if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
                    fut_symbols.add(s["symbol"])
    except Exception:
        pass

    all_symbols = list(spot_symbols | fut_symbols)

    if not all_symbols:
        print("[DAILY_KLINES] 无币种列表，跳过加载")
        return

    print(f"[DAILY_KLINES] 开始批量加载 {len(all_symbols)} 个币种的日线K线（现货{len(spot_symbols)}+合约{len(fut_symbols)}）...")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    loaded = 0
    loaded_fut = 0
    failed = 0
    failed_fut = 0
    start_ts = time.time()

    def fetch_one(sym):
        spot = _fetch_daily_klines(sym)
        fut = _fetch_futures_daily_klines(sym)
        return sym, spot, fut

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(fetch_one, sym): sym for sym in all_symbols}
        for future in as_completed(futures):
            sym, spot_kl, fut_kl = future.result()
            if spot_kl and len(spot_kl) >= DAILY_BB_CONFIG["period"] + 1:
                with _daily_kline_lock:
                    _daily_kline_cache[sym] = spot_kl
                loaded += 1
            else:
                failed += 1
            if fut_kl and len(fut_kl) >= DAILY_BB_CONFIG["period"] + 1:
                with _daily_kline_lock_futures:
                    _daily_kline_cache_futures[sym] = fut_kl
                loaded_fut += 1
            else:
                failed_fut += 1

    elapsed = time.time() - start_ts
    print(f"[DAILY_KLINES] 加载完成: 现货{loaded}/{len(all_symbols)} 合约{loaded_fut}/{len(all_symbols)} | 失败现货{failed}合约{failed_fut} | 耗时{elapsed:.1f}s")


def _refresh_bb_daily_cache():
    """后台刷新日线布林爬坡缓存（现货+合约日线）"""
    global _bb_daily_cache, _bb_diagnostic

    with _daily_kline_lock:
        daily_cache = dict(_daily_kline_cache)
    with _daily_kline_lock_futures:
        futures_cache = dict(_daily_kline_cache_futures)

    if not daily_cache and not futures_cache:
        print("[BB_DAILY] 日线K线缓存为空，跳过刷新")
        return

    cfg = DAILY_BB_CONFIG
    results = []
    breakdown = {}
    diag_symbols = {}

    # ==== 复活赛：不走①~⑪，只看反包 ====
    with _resurrection_lock:
        queue = dict(_resurrection_queue)
    resurrected = []
    for symbol, info in queue.items():
        klines = daily_cache.get(symbol) or futures_cache.get(symbol)
        if not klines or len(klines) < 3:
            continue
        cmpl = klines[-2]  # 最近完工日线
        # 反包复活窗口已过（完工日线在阴线之后）
        if cmpl["t"] <= info["bearish_ts"]:
            continue  # 还没到检测时间
        # 查反包：不靠时间戳，直接用存储的阴线数据
        if cmpl["c"] >= cmpl["o"]:
            tol = info["bearish_c"] * 0.001  # 0.1%容差，避免现货/合约tick差异
            engulfing = (cmpl["o"] <= info["bearish_c"] + tol and cmpl["c"] >= info["bearish_o"] - tol)
            if engulfing:
                results.append({
                    "symbol": symbol, "upper": 0, "middle": 0,
                    "consecutive_hours": 4, "resurrected": True, "valid_hours": [],
                })
                resurrected.append(symbol)
                print(f"[BB复活] {symbol} 反包成功，直接给BB信号")
            else:
                print(f"[BB淘汰] {symbol} 阳线但未反包，淘汰")
        else:
            print(f"[BB淘汰] {symbol} 完工阴线，淘汰")
    # 本轮被淘汰的（阳未反包 + 阴线）也收集
    eliminated = []
    for symbol, info in queue.items():
        klines = daily_cache.get(symbol) or futures_cache.get(symbol)
        if klines and len(klines) >= 3 and klines[-2]["t"] > info["bearish_ts"]:
            eliminated.append(symbol)
    # 清理所有已处理的
    for sym in resurrected + eliminated:
        _resurrection_queue.pop(sym, None)

    # ==== 正常检测流程 ====
    all_symbols = set(daily_cache.keys()) | set(futures_cache.keys())
    all_symbols -= set(_resurrection_queue.keys())
    all_symbols -= set(resurrected)      # 已复活的不走
    all_symbols -= set(eliminated)       # 已淘汰的不走

    for symbol in all_symbols:
        klines = daily_cache.get(symbol) or futures_cache.get(symbol)
        if klines is None:
            continue
        signal, reason, detail = _diagnose_bb(symbol, klines, cfg)
        breakdown[reason] = breakdown.get(reason, 0) + 1

        if detail:
            diag_symbols[symbol] = {"reason": reason, **detail}

        if signal and signal.get("consecutive_hours", 0) >= 4:
            results.append(signal)

    results.sort(key=lambda x: -(x.get("consecutive_hours", 0)))
    total = len(all_symbols) + len(_resurrection_queue)
    n_passed = breakdown.get("passed", 0) + len(resurrected)
    spot_count = len(daily_cache)
    fut_count = sum(1 for s in all_symbols if s not in daily_cache)
    queue_count = len(_resurrection_queue)
    msg = f"[BB_DAILY] 刷新完成: {n_passed}个BB信号/{total}币种 (现货{spot_count}+合约{fut_count}"
    if queue_count:
        msg += f" 复活赛{queue_count}"
    msg += ")"
    print(msg)
    # 打印分布
    for reason in sorted(breakdown.keys(), key=lambda r: -breakdown[r]):
        print(f"  {reason}: {breakdown[reason]} ({breakdown[reason]/total*100:.0f}%)")

    with _bb_daily_lock:
        _bb_daily_cache = {
            "results": results[:50],
            "candidates": [],
            "updated_at": time.time(),
        }

    with _bb_diagnostic_lock:
        _bb_diagnostic = {
            "total": total,
            "breakdown": breakdown,
            "symbols": diag_symbols,
            "updated_at": time.time(),
        }


def bb_daily_background_loop():
    """每分钟刷新一次BB日线检测（心跳）"""
    first_run = True
    while True:
        try:
            if first_run:
                wait_count = 0
                while True:
                    with data_lock:
                        symbols = list(market_data.get("symbols", {}).keys())
                    if symbols:
                        break
                    wait_count += 1
                    if wait_count % 6 == 0:
                        print(f"[BB_DAILY] 等待symbols数据就绪... ({wait_count*10}s)")
                    time.sleep(10)
                print(f"[BB_DAILY] symbols就绪 ({len(symbols)}个币种)，开始加载日线K线")
                _load_all_daily_klines()
                _refresh_bb_daily_cache()
                _refresh_cc_cache()
                first_run = False

            time.sleep(60)
            _load_all_daily_klines()
            _refresh_bb_daily_cache()
            _refresh_cc_cache()

        except Exception as e:
            print(f"[BB_DAILY] 刷新失败: {e}")
            time.sleep(60)


@app.route("/api/bollinger_climb_daily")
def api_bollinger_climb_daily():
    """返回日线布林爬坡信号（V7混合策略用）"""
    with _bb_daily_lock:
        cache = dict(_bb_daily_cache)

    return jsonify({
        "code": 0,
        "count": len(cache.get("results", [])),
        "data": cache.get("results", []),
        "candidate_count": 0,
        "candidates": [],
        "updated_at": cache.get("updated_at", 0),
    })


@app.route("/api/bb_diagnostic")
def api_bb_diagnostic():
    """返回BB全量检测诊断数据"""
    with _bb_diagnostic_lock:
        diag = dict(_bb_diagnostic)
    return jsonify({
        "code": 0,
        "total": diag.get("total", 0),
        "breakdown": diag.get("breakdown", {}),
        "symbols": diag.get("symbols", {}),
        "updated_at": diag.get("updated_at", 0),
    })


@app.route("/api/bb_refresh")
def api_bb_refresh():
    """手动触发BB日线刷新（异步）"""
    def do_refresh():
        try:
            _load_all_daily_klines()
            _refresh_bb_daily_cache()
        except Exception as e:
            print(f"[BB_REFRESH] 手动刷新失败: {e}")
    t = threading.Thread(target=do_refresh, daemon=True)
    t.start()
    return jsonify({"code": 0, "msg": "BB刷新已触发（后台执行中）"})


@app.route("/bb_diagnostic")
def page_bb_diagnostic():
    response = make_response(render_template("bb_diagnostic.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response



# ========== 页面 ==========
@app.route("/")
def index():
    response = make_response(render_template("market_monitor.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# 快照缓存：避免API请求线程阻塞在data_lock上
_snapshot_cache = {"rows": [], "count": 0, "updated": 0}
_snapshot_cache_lock = threading.Lock()

def _refresh_snapshot_cache():
    """后台线程：定期刷新快照缓存"""
    global _snapshot_cache
    while True:
        try:
            time.sleep(5)
            rows = get_table_data()
            with _snapshot_cache_lock:
                _snapshot_cache = {"rows": rows, "count": len(rows), "updated": time.time()}
            print(f"[SNAPSHOT_CACHE] refreshed: {len(rows)} rows", flush=True)
        except Exception as e:
            print(f"[SNAPSHOT_CACHE] refresh error: {e}", flush=True)

def _get_cached_snapshot():
    """无锁读取缓存的快照数据"""
    with _snapshot_cache_lock:
        return dict(_snapshot_cache)

@app.route("/api/snapshot")
def api_snapshot():
    """返回全量数据（从缓存读取，无锁）"""
    cache = _get_cached_snapshot()
    return jsonify({
        "code": 0,
        "count": cache.get("count", 0),
        "data": cache.get("rows", [])
    })

@app.route("/api/debug_state")
def api_debug_state():
    """返回内部状态（调试用）"""
    with data_lock:
        current_minute = market_data.get("current_minute")
        second_keys = list(market_data.get("second_deltas", {}).keys())
        minute_klines_counts = {s: len(v) for s, v in market_data.get("minute_klines", {}).items()}
        hourly_cache_counts = {s: len(v) for s, v in market_data.get("hourly_kline_cache", {}).items()}
        bb_backfill_done = market_data.get("bb_backfill_done", False)
        trading_symbols = list(market_data.get("trading_symbols", set()))
        vol_15m_slot = market_data.get("vol_15m_slot")
        vol_15m_current = market_data.get("vol_15m_current", {})
        vol_15m_last = market_data.get("vol_15m_last", {})
        vol_24h_today = market_data.get("vol_24h_today", {})
        vol_surge_symbols = market_data.get("vol_surge_symbols", {})
    
    # Hyperliquid 聚合器统计
    hl_stats = {}
    try:
        from utils.trades_aggregator import get_aggregator
        agg = get_aggregator()
        hl_stats = agg.get_stats()
    except:
        pass
    
    return jsonify({
        "current_minute": current_minute,
        "second_deltas_key_count": len(second_keys),
        "first_5_second_keys": second_keys[:5],
        "minute_klines_counts": dict(list(minute_klines_counts.items())[:10]),
        "hourly_cache_symbol_count": len(hourly_cache_counts),
        "hourly_cache_sample": dict(list(hourly_cache_counts.items())[:5]),
        "bb_backfill_done": bb_backfill_done,
        "server_time": time.time(),
        "server_minute": get_current_minute_ts(),
        "trading_symbols": trading_symbols[:50],
        "hyperliquid_stats": hl_stats,
        "vol_15m_slot": vol_15m_slot,
        "vol_15m_current_count": len(vol_15m_current),
        "vol_15m_current_sample": dict(list(vol_15m_current.items())[:5]),
        "vol_15m_last_count": len(vol_15m_last),
        "vol_24h_today_count": len(vol_24h_today),
        "vol_surge_symbols_count": len(vol_surge_symbols),
        "vol_surge_symbols": vol_surge_symbols
    })

@app.route("/api/minute_buy_ratio/<symbol>")
def api_minute_buy_ratio(symbol: str):
    """返回指定币的分钟级主动买卖比"""
    symbol = symbol.upper()
    with data_lock:
        klines = market_data["minute_klines"].get(symbol, [])
    
    result = [{
        "t": k["t"],
        "buy_ratio": round(k.get("buy_ratio", 0.5), 3),
        "buy_q": round(k.get("buy_q", 0), 2),
        "sell_q": round(k.get("sell_q", 0), 2),
        "q": round(k.get("q", 0), 2),
        "o": k.get("o", 0),
        "c": k.get("c", 0),
        "l": k.get("l", 0),  # 最低价
        "h": k.get("h", 0),  # 最高价
        "gain": round((k.get("c", 0) - k.get("o", 0)) / k.get("o", 1) * 100, 2) if k.get("o", 0) > 0 else 0
    } for k in klines[-60:]]  # 最近60分钟
    
    return jsonify({"code": 0, "symbol": symbol, "count": len(result), "data": result})

@app.route("/api/surge")
def api_surge():
    """返回delta_q突增的币种列表（仅买>=80%）"""
    with data_lock:
        surge_cache = market_data.get("surge_cache", {})
    
    result = []
    now = time.time()
    for symbol, records in surge_cache.items():
        # 过滤30分钟内的记录
        recent = [r for r in records if now - r["t"] < SURGE_CACHE_MAX_MINUTES * 60]
        if recent:
            total_delta_q = sum(r["delta_q"] for r in recent)
            avg_buy_ratio = sum(r["buy_ratio"] for r in recent) / len(recent)
            # 大单追踪：只显示买>=80%（买方主导才显示）
            if avg_buy_ratio >= 0.8:
                result.append({
                    "symbol": symbol,
                    "count": len(recent),
                    "total_delta_q": round(total_delta_q, 2),
                    "avg_buy_ratio": round(avg_buy_ratio, 3),
                    "last_t": recent[-1]["t"],
                    "last_price": recent[-1]["price"]
                })
    
    result.sort(key=lambda x: -x["total_delta_q"])
    return jsonify({"code": 0, "count": len(result), "data": result[:50]})

@app.route("/api/vol_surge")
def api_vol_surge():
    """返回15分钟成交量突增的币种列表（最近1小时，含tradeable标记）"""
    with data_lock:
        vol_surge_history = market_data.get("vol_surge_history", {})
        symbols = market_data.get("symbols", {})
    
    result = []
    now = time.time()
    for symbol, info in vol_surge_history.items():
        elapsed = now - info.get("start_time", 0)
        if elapsed > 3600:  # 超过1小时不展示
            continue
        
        tradeable = elapsed <= 300  # 5分钟内可交易
        symbol_info = symbols.get(symbol, {})
        result.append({
            "symbol": symbol,
            "ratio": round(info.get("ratio", 0), 2),
            "vol_15m": info.get("vol", 0),
            "last_vol": info.get("last_vol", 0),
            "price": symbol_info.get("price", 0),
            "gain_pct": symbol_info.get("priceChangePercent", 0),
            "remaining_seconds": int(300 - elapsed) if tradeable else 0,
            "start_time": info.get("start_time", 0),
            "tradeable": tradeable
        })
    
    # 按突增倍数降序
    result.sort(key=lambda x: -x["ratio"])
    return jsonify({"code": 0, "count": len(result), "data": result})

@app.route("/api/momentum_tracker")
def api_momentum_tracker():
    """返回追涨追踪器列表（涨幅5%~10%的币，追踪到10%需要多少成交量）"""
    with data_lock:
        tracker = dict(market_data.get("momentum_tracker", {}))
        symbols = dict(market_data.get("symbols", {}))
        today_open_prices = dict(market_data.get("today_open_prices", {}))
    
    now = time.time()
    result = []
    for symbol, info in tracker.items():
        symbol_info = symbols.get(symbol, {})
        current_price = symbol_info.get("price", info.get("current_price", 0))
        
        # 使用 today_open_prices 计算涨幅（与 update_momentum_tracker 一致）
        open_price = today_open_prices.get(symbol, symbol_info.get("o", 0))
        if open_price > 0 and current_price > 0:
            current_gain = (current_price - open_price) / open_price * 100
        else:
            current_gain = info.get("current_gain_pct", 0)
        
        vol_24h = info.get("vol_24h_today", 0)
        elapsed = int(now - info.get("entry_time", now))
        remaining = max(0, MOMENTUM_MAX_AGE - elapsed)
        
        # 重新计算还需成交量（基于最新价格）
        vol_needed = 0
        if current_gain > 0 and current_gain < MOMENTUM_TARGET_GAIN:
            vol_needed = (MOMENTUM_TARGET_GAIN - current_gain) / max(current_gain, 0.1) * vol_24h
        
        # 状态：如果涨幅>=10%或status已经是reached，显示为已达标
        status = info.get("status", "tracking")
        if current_gain >= MOMENTUM_TARGET_GAIN:
            status = "reached"
        
        result.append({
            "symbol": symbol,
            "entry_time": info.get("entry_time", 0),
            "entry_price": info.get("entry_price", 0),
            "entry_gain_pct": round(info.get("entry_gain_pct", 0), 2),
            "target_gain_pct": MOMENTUM_TARGET_GAIN,
            "current_price": current_price,
            "current_gain_pct": round(current_gain, 2),
            "vol_24h_today": round(vol_24h, 2),
            "vol_needed": round(vol_needed, 2),
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "status": status,
        })
    
    # 排序：已达标排最后，其他按当前涨幅降序
    result.sort(key=lambda x: (0 if x["status"] == "reached" else 1, -x["current_gain_pct"]))
    return jsonify({"code": 0, "count": len(result), "data": result})

@app.route("/params")
def page_params():
    """参数面板"""
    response = make_response(render_template("params.html"))
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/momentum")
def momentum_page():
    """追涨追踪器页面"""
    response = make_response(render_template("momentum_tracker.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

# ========== WebSocket ==========
def _send_init_data(sid):
    """后台任务：向指定客户端发送初始化数据"""
    print(f"[_send_init_data] started for sid={sid}", flush=True)
    try:
        data = get_table_data()
        print(f"[_send_init_data] got {len(data)} rows, emitting...", flush=True)
        socketio.emit("init_data", {"data": data, "count": len(data)}, to=sid)
        print(f"已发送init_data ({len(data)} 条) to {sid}", flush=True)
    except Exception as e:
        print(f"send init_data error: {e}", flush=True)


@socketio.on("connect", namespace="/")
def on_connect():
    import sys
    from flask import request
    sid = request.sid
    print(f"浏览器已连接 sid={sid}", flush=True)
    sys.stdout.flush()
    # 从缓存读取快照，避免data_lock阻塞
    cache = _get_cached_snapshot()
    data = cache.get("rows", [])
    count = cache.get("count", 0)
    if count == 0:
        # 缓存尚未就绪，fallback到直接获取
        data = get_table_data()
        count = len(data)
    print(f"[init_data] got {count} rows for sid={sid}", flush=True)
    socketio.emit("init_data", {"data": data, "count": count}, to=sid)
    print(f"已发送init_data ({count} 条) to {sid}", flush=True)
    return True

@socketio.on("disconnect")
def on_disconnect():
    print("浏览器断开连接")

def ws_update_loop():
    """WebSocket接收并广播更新，同时记录秒级数据"""
    import websocket

    def on_message(ws, message):
        try:
            frame = json.loads(message)
            if not isinstance(frame, list):
                return

            current_minute = get_current_minute_ts()
            current_second = int(time.time())
            updates = []

            with data_lock:
                # 检查是否进入新的一分钟
                if market_data["current_minute"] is None:
                    market_data["current_minute"] = current_minute
                
                if current_minute > market_data["current_minute"]:
                    # 进入新分钟，聚合上一分钟
                    _aggregate_minute_kline()
                    market_data["current_minute"] = current_minute
                    # 重置分钟状态
                    market_data["minute_state"] = {}

                for item in frame:
                    symbol = item.get("s", "")
                    if not symbol.endswith("USDT"):
                        continue
                    # 过滤非TRADING状态的币种
                    ts = market_data.get("trading_symbols", set())
                    if ts and symbol not in ts:
                        continue

                    q = float(item.get("q", 0))
                    price = float(item.get("c", 0))

                    # 如果币种不存在，自动添加
                    if symbol not in market_data["symbols"]:
                        # 注意：此处已经在 data_lock 保护范围内，不需要再次加锁
                        # 获取北京8点开盘价（如果有）
                        open_price = market_data.get("today_open_prices", {}).get(symbol, price)
                        
                        # 计算北京8点起的涨跌幅
                        if open_price > 0:
                            gain_pct = (price - open_price) / open_price * 100
                        else:
                            gain_pct = 0
                        
                        market_data["symbols"][symbol] = {
                            "price": price,
                            "q": q,
                            "v": float(item.get("v", 0)),
                            "o": open_price,  # 北京8点开盘价
                            "h": float(item.get("h", price)),
                            "l": float(item.get("l", price)),
                            "priceChangePercent": gain_pct,  # 北京8点起的涨跌幅
                            "updated_at": time.time()
                        }
                        market_data["last_q"][symbol] = q
                        print(f"[WebSocket] 新增币种: {symbol}")
                        continue

                    if symbol in market_data["symbols"]:
                        # 记录 delta_q
                        last_q = market_data["last_q"].get(symbol, q)
                        delta_q = q - last_q
                        # 如果 delta_q 为负，说明币安24h滚动统计重置了（每天北京8点）
                        # 此时直接用当前 q 作为这一段的累计值
                        if delta_q < 0:
                            delta_q = q
                        market_data["last_q"][symbol] = q

                        # 存入秒数据
                        if current_second not in market_data["second_deltas"]:
                            market_data["second_deltas"][current_second] = {}
                        market_data["second_deltas"][current_second][symbol] = delta_q

                        # 更新实时价格（只更新价格，不修改累计成交额）
                        old_price = market_data["symbols"][symbol].get("price", 0)
                        open_price = market_data["symbols"][symbol].get("o", 0)
                        
                        # 计算新的涨跌幅（基于北京8点开盘价）
                        if open_price > 0:
                            new_gain_pct = (price - open_price) / open_price * 100
                        else:
                            new_gain_pct = 0
                        
                        market_data["symbols"][symbol].update({
                            "price": price,
                            "q": q,  # 实时更新24h滚动成交额
                            "v": float(item.get("v", 0)),  # 实时更新24h成交量
                            "h": float(item.get("h", price)),  # 实时更新24h最高
                            "l": float(item.get("l", price)),  # 实时更新24h最低
                            "priceChangePercent": new_gain_pct,  # 基于北京8点的涨跌幅
                            "updated_at": time.time()
                        })

                        # 更新当前分钟状态
                        if symbol not in market_data["minute_state"]:
                            market_data["minute_state"][symbol] = {
                                "open": price,
                                "high": price,
                                "low": price,
                                "close": price,
                                "vol": 0,
                                "q": 0,
                                "first_price": price,
                                "buy_q": 0,
                                "sell_q": 0,
                                "last_price": price
                            }
                        
                        ms = market_data["minute_state"][symbol]
                        ms["high"] = max(ms["high"], price)
                        ms["low"] = min(ms["low"], price)
                        ms["close"] = price
                        ms["q"] += delta_q
                        
                        # ===== 新增：计算 buy_ratio 并估算买卖成交额 =====
                        price_delta = price - ms["last_price"]
                        buy_ratio = _calculate_buy_ratio(price_delta, ms["last_price"])
                        
                        # 更新买卖估算
                        if delta_q > 0:
                            ms["buy_q"] += delta_q * buy_ratio
                            ms["sell_q"] += delta_q * (1 - buy_ratio)
                        
                        ms["last_price"] = price
                        
                        # ===== delta_q 突增检测（仅买方主导>=80%）=====
                        if delta_q > DELTA_Q_SURGE_THRESHOLD and symbol not in SURGE_EXCLUDE_SYMBOLS:
                            if buy_ratio >= 0.8:
                                if symbol not in market_data["surge_cache"]:
                                    market_data["surge_cache"][symbol] = []
                                market_data["surge_cache"][symbol].append({
                                    "t": current_second,
                                    "delta_q": delta_q,
                                    "buy_ratio": buy_ratio,
                                    "price": price
                                })
                                import datetime
                                print(f"[SURGE] {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {symbol} delta_q={delta_q:,.0f} buy_ratio={buy_ratio:.3f} price={price}")
                                # 清理超过30分钟的记录，并限制单币种最多50条
                                cache = market_data["surge_cache"][symbol]
                                cache = [r for r in cache if current_second - r["t"] < SURGE_CACHE_MAX_MINUTES * 60]
                                if len(cache) > 50:
                                    cache = cache[-50:]
                                market_data["surge_cache"][symbol] = cache

                        updates.append({
                            "symbol": symbol,
                            "q": q,
                            "price": price
                        })

            if updates:
                socketio.emit("ws_update", {"data": updates}, namespace="/")
        except Exception as e:
            print(f"WebSocket消息处理失败: {e}")

    def on_error(ws, error):
        print(f"WebSocket错误: {error}")

    def on_close(ws, code, reason):
        print(f"WebSocket关闭: {code} {reason}")

    def on_open(ws):
        print("WebSocket连接成功！")

    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/ws/!miniTicker@arr",
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    print("WebSocket连接中...")
    ws.run_forever(ping_interval=20, ping_timeout=10, reconnect=5)


def _aggregate_minute_kline():
    """聚合上一分钟的K线数据"""
    try:
        minute_ts = market_data["current_minute"]
        minute_state = market_data["minute_state"]
        
        # 找出上一分钟内的所有秒级key
        minute_start = minute_ts
        minute_end = minute_ts + 60
        
        # 收集上一分钟的所有秒数据
        minute_seconds_data = {}
        second_deltas_keys_to_delete = []
        
        for sec_ts, sec_data in market_data["second_deltas"].items():
            if minute_start <= sec_ts < minute_end:
                minute_seconds_data.update(sec_data)
                second_deltas_keys_to_delete.append(sec_ts)
        
        if not minute_seconds_data:
            print(f"分钟 {minute_ts} 无秒数据")
            return
        
        # 按symbol聚合
        symbol_totals = {}
        for symbol, delta_q in minute_seconds_data.items():
            if symbol not in symbol_totals:
                symbol_totals[symbol] = 0
            symbol_totals[symbol] += delta_q
        
        # 生成K线
        new_klines = []
        for symbol, total_q in symbol_totals.items():
            if symbol not in minute_state:
                continue
            
            ms = minute_state[symbol]
            
            # 计算分钟级的 buy_ratio
            total_buy_q = ms.get("buy_q", 0)
            total_sell_q = ms.get("sell_q", 0)
            minute_buy_ratio = total_buy_q / (total_buy_q + total_sell_q) if (total_buy_q + total_sell_q) > 0 else 0.5
            
            kline = {
                "symbol": symbol,
                "t": minute_ts,
                "o": ms["first_price"],
                "h": ms["high"],
                "l": ms["low"],
                "c": ms["close"],
                "v": 0,  # 成交量暂时用0
                "q": total_q,  # 成交额
                "buy_q": round(total_buy_q, 2),
                "sell_q": round(total_sell_q, 2),
                "buy_ratio": round(minute_buy_ratio, 3)
            }
            
            if symbol not in market_data["minute_klines"]:
                market_data["minute_klines"][symbol] = []
            market_data["minute_klines"][symbol].append(kline)
            # 截断：只保留最近120条（2小时），降低内存占用
            if len(market_data["minute_klines"][symbol]) > MAX_MINUTE_KLINES_PER_SYMBOL:
                market_data["minute_klines"][symbol] = market_data["minute_klines"][symbol][-MAX_MINUTE_KLINES_PER_SYMBOL:]
            new_klines.append(kline)
        
        # 清理已聚合的秒数据
        for key in second_deltas_keys_to_delete:
            del market_data["second_deltas"][key]

        if new_klines:
            print(f"聚合分钟K线: {len(new_klines)} 条")

        # 15分钟区间统计
        current_15m_slot = get_current_15m_slot()
        last_15m_slot = market_data.get("vol_15m_slot")
        
        # 累加当前分钟的成交额到15分钟统计和今日累计
        for symbol, total_q in symbol_totals.items():
            # 累加到当前15分钟
            if symbol not in market_data["vol_15m_current"]:
                market_data["vol_15m_current"][symbol] = 0
            market_data["vol_15m_current"][symbol] += total_q
            
            # 累加到今日成交额
            if symbol not in market_data["vol_24h_today"]:
                market_data["vol_24h_today"][symbol] = 0
            market_data["vol_24h_today"][symbol] += total_q
        
        # 检查是否进入新的15分钟区间
        if last_15m_slot is None:
            market_data["vol_15m_slot"] = current_15m_slot
        elif current_15m_slot > last_15m_slot:
            # 15分钟区间结束，保存历史并检测突增
            print(f"[15M_SLOT] 区间 {last_15m_slot} 结束，保存历史并检测突增...")
            
            # 收集当前15分钟所有币种的成交额
            all_symbols = set(market_data.get("symbols", {}).keys())
            vol_data = {}
            for symbol in all_symbols:
                current_vol = market_data["vol_15m_current"].get(symbol, 0)
                vol_data[symbol] = current_vol
                market_data["vol_15m_last"][symbol] = current_vol
            
            # 写入COS（持久化）- 后台异步避免阻塞
            threading.Thread(target=save_vol_15m_to_cos, args=(last_15m_slot, vol_data), daemon=True).start()
            
            # 将当前15分钟数据追加到内存历史缓存（不再从COS重新加载）
            for symbol, cur_vol in vol_data.items():
                if symbol not in market_data["vol_15m_history"]:
                    market_data["vol_15m_history"][symbol] = {}
                market_data["vol_15m_history"][symbol][last_15m_slot] = cur_vol
                # 清理超过8小时（32个slot）的旧缓存
                cutoff_slot = last_15m_slot - 32 * 900
                for ts in list(market_data["vol_15m_history"][symbol].keys()):
                    if ts < cutoff_slot:
                        del market_data["vol_15m_history"][symbol][ts]
            
            # 严格4小时时间窗口计算均值（total/16，缺失补0，与回测对齐）
            symbols_info = market_data.get("symbols", {})
            for symbol in all_symbols:
                current_vol = vol_data.get(symbol, 0)
                avg_4h = calc_vol_15m_avg_strict(symbol, last_15m_slot, market_data["vol_15m_history"])
                market_data["vol_15m_avg_4h"][symbol] = avg_4h
                
                # 预筛选（1.0x，减少check_volume_surge调用次数；VS gain≥2.3%做最终过滤）
                if current_vol > 0 and avg_4h > 0 and current_vol > avg_4h * 1.0:
                    check_volume_surge(symbol, current_vol, avg_4h)
            
            # 重置当前15分钟累计
            market_data["vol_15m_current"] = {}
            market_data["vol_15m_slot"] = current_15m_slot
            
            # 清理过期的突增记录
            cleanup_surge_records()
            cleanup_vol_surge_history()

        # 注：hourly_kline_cache 由 bollinger_climb_background_loop 每10秒异步刷新，
        # 不需要在锁内同步聚合，避免长时间持锁阻塞API请求
    except Exception as e:
        print(f"聚合失败: {e}")

# ========== 追涨追踪器 ==========
MOMENTUM_MIN_GAIN = 5.0      # 进入追踪的最小涨幅 5%
MOMENTUM_TARGET_GAIN = 10.0  # 目标涨幅 10%
MOMENTUM_MAX_AGE = 4 * 3600  # 最大追踪时间 4小时
MOMENTUM_MIN_VOL_24H = 3_000_000  # 24h成交量最低门槛 300万

def update_momentum_tracker():
    """更新追涨追踪器
    追踪涨幅5%~10%的币，估算还需多少成交量才能涨到10%
    超过4小时未达10%剔除
    """
    now = time.time()
    with data_lock:
        symbols = dict(market_data.get("symbols", {}))
        today_open_prices = dict(market_data.get("today_open_prices", {}))
        vol_24h_today = dict(market_data.get("vol_24h_today", {}))
        tracker = market_data.get("momentum_tracker", {})
    
    # 如果 today_open_prices 加载不足（少于50个），说明系统刚启动，数据不完整，跳过
    if len(today_open_prices) < 50:
        return
    
    # 复制一份避免遍历期间修改
    tracker = dict(tracker)
    
    added_count = 0
    skipped_vol = 0
    skipped_gain_high = 0
    skipped_gain_low = 0
    
    for symbol, info in symbols.items():
        price = info.get("price", 0)
        open_price = today_open_prices.get(symbol, info.get("o", 0))
        if open_price <= 0 or price <= 0:
            continue
        
        gain_pct = (price - open_price) / open_price * 100
        vol_24h = vol_24h_today.get(symbol, 0)
        
        # 24h成交量门槛
        if vol_24h < MOMENTUM_MIN_VOL_24H:
            skipped_vol += 1
            continue
        
        # 已经达到10%以上的，标记为已完成
        if gain_pct >= MOMENTUM_TARGET_GAIN:
            skipped_gain_high += 1
            if symbol in tracker:
                tracker[symbol]["status"] = "reached"
                tracker[symbol]["current_price"] = price
                tracker[symbol]["current_gain_pct"] = gain_pct
                tracker[symbol]["vol_24h_today"] = vol_24h
                # 记录首次达标时间
                if "reached_time" not in tracker[symbol]:
                    tracker[symbol]["reached_time"] = now
                # 已达到的保留5分钟后剔除
                if now - tracker[symbol]["reached_time"] > 300:
                    del tracker[symbol]
            continue
        
        # 涨幅不足5%的，如果已在列表中则更新，否则不加入
        if gain_pct < MOMENTUM_MIN_GAIN:
            skipped_gain_low += 1
            if symbol in tracker:
                tracker[symbol]["current_price"] = price
                tracker[symbol]["current_gain_pct"] = gain_pct
                tracker[symbol]["vol_24h_today"] = vol_24h
                # 跌回5%以下：如果已追踪超过5分钟且当前涨幅<4%，剔除
                elapsed = now - tracker[symbol]["entry_time"]
                if elapsed > 300 and gain_pct < MOMENTUM_MIN_GAIN - 1.0:
                    del tracker[symbol]
                # 如果跌幅超过3%（低于进入涨幅的一半以下），立即剔除
                elif gain_pct < MOMENTUM_MIN_GAIN * 0.5:
                    del tracker[symbol]
            continue
        
        # 涨幅在5%~10%之间
        if symbol not in tracker:
            # 新进入追踪
            # 估算还需多少成交量到10%
            # 假设：成交额与涨幅大致成比例（非常粗略的估算）
            # vol_needed = (target - current) / current * vol_24h_today
            vol_needed = (MOMENTUM_TARGET_GAIN - gain_pct) / max(gain_pct, 0.1) * vol_24h
            tracker[symbol] = {
                "entry_time": now,
                "entry_price": price,
                "entry_gain_pct": gain_pct,
                "target_gain_pct": MOMENTUM_TARGET_GAIN,
                "current_price": price,
                "current_gain_pct": gain_pct,
                "vol_24h_today": vol_24h,
                "vol_needed": vol_needed,
                "status": "tracking",
            }
            added_count += 1
        else:
            # 更新已有追踪
            entry_time = tracker[symbol]["entry_time"]
            elapsed = now - entry_time
            
            # 超过4小时未达10%，剔除
            if elapsed > MOMENTUM_MAX_AGE:
                del tracker[symbol]
                continue
            
            # 重新估算还需成交量（基于最新数据）
            vol_needed = (MOMENTUM_TARGET_GAIN - gain_pct) / max(gain_pct, 0.1) * vol_24h
            tracker[symbol].update({
                "current_price": price,
                "current_gain_pct": gain_pct,
                "vol_24h_today": vol_24h,
                "vol_needed": vol_needed,
                "elapsed_seconds": int(elapsed),
            })
    
    # 清理已不在symbols中的追踪
    for symbol in list(tracker.keys()):
        if symbol not in symbols:
            del tracker[symbol]
    
    if len(symbols) > 0:
        print(f"[MOMENTUM] 扫描{len(symbols)}个币，新增{added_count}个，跳过(成交量低:{skipped_vol}, 涨幅高:{skipped_gain_high}, 涨幅低:{skipped_gain_low})，当前追踪:{len(tracker)}")
    
    with data_lock:
        market_data["momentum_tracker"] = tracker


# _update_hourly_cache_for_hour 保留供 bollinger 后台线程或其他需要的地方使用

def _update_hourly_cache_for_hour(hour_ts: int, active_symbols: list = None):
    """从minute_klines中聚合指定小时的数据，更新hourly_kline_cache
    
    active_symbols: 本分钟有交易的币种列表，若为None则扫描全量
    """
    hourly_cache = market_data.get("hourly_kline_cache", {})
    minute_klines = market_data.get("minute_klines", {})

    symbols_to_check = active_symbols if active_symbols else list(minute_klines.keys())
    
    for symbol in symbols_to_check:
        klines = minute_klines.get(symbol, [])
        if not klines:
            continue
        # 找出属于该小时的分钟K线（只检查最近60根）
        hour_minutes = [k for k in klines[-60:] if (k["t"] // 3600) * 3600 == hour_ts]
        if len(hour_minutes) < 2:
            continue

        buy_q = sum(k.get("buy_q", 0) for k in hour_minutes)
        sell_q = sum(k.get("sell_q", 0) for k in hour_minutes)
        total_q = buy_q + sell_q

        hourly_kline = {
            "t": hour_ts,
            "o": hour_minutes[0]["o"],
            "h": max(k["h"] for k in hour_minutes),
            "l": min(k["l"] for k in hour_minutes),
            "c": hour_minutes[-1]["c"],
            "v": sum(k.get("v", 0) for k in hour_minutes),
            "q": sum(k.get("q", 0) for k in hour_minutes),
            "buy_q": buy_q,
            "sell_q": sell_q,
            "buy_ratio": buy_q / total_q if total_q > 0 else 0.5,
        }

        if symbol not in hourly_cache:
            hourly_cache[symbol] = []
        existing = {h["t"]: i for i, h in enumerate(hourly_cache[symbol])}
        if hour_ts in existing:
            hourly_cache[symbol][existing[hour_ts]] = hourly_kline
        else:
            hourly_cache[symbol].append(hourly_kline)
            hourly_cache[symbol].sort(key=lambda x: x["t"])
            # 截断：只保留最近36根（36小时），满足BB爬坡25根需求
            if len(hourly_cache[symbol]) > MAX_HOURLY_KLINES_PER_SYMBOL:
                hourly_cache[symbol] = hourly_cache[symbol][-MAX_HOURLY_KLINES_PER_SYMBOL:]

    market_data["hourly_kline_cache"] = hourly_cache

def minute_aggregator_loop():
    """每分钟触发聚合，并上传COS（每次聚合完立即上传）"""
    last_check_minute = None
    cleanup_counter = 0
    momentum_counter = 0
    
    while True:
        time.sleep(1)
        
        # 每10秒更新一次追涨追踪器（放在分钟检查之前）
        momentum_counter += 1
        if momentum_counter >= 10:
            momentum_counter = 0
            try:
                update_momentum_tracker()
            except Exception as e:
                print(f"[MOMENTUM] 更新失败: {e}")
        
        current_minute = get_current_minute_ts()
        
        # 每秒检查是否进入新分钟（只在秒变化时检查）
        if current_minute == last_check_minute:
            continue
        last_check_minute = current_minute
        
        with data_lock:
            if market_data["current_minute"] is not None and current_minute > market_data["current_minute"]:
                # 触发聚合
                _aggregate_minute_kline()
                market_data["current_minute"] = current_minute
                market_data["minute_state"] = {}
                
                cleanup_counter += 1
                if cleanup_counter >= 60:
                    cleanup_counter = 0
                    # 清理超过12天的分钟K线数据
                    cutoff = time.time() - (MAX_MINUTE_KLINES * 60)
                    for symbol in list(market_data["minute_klines"].keys()):
                        market_data["minute_klines"][symbol] = [
                            k for k in market_data["minute_klines"][symbol]
                            if k.get("t", 0) >= cutoff
                        ]
                
                # 清理超过15分钟的大单追踪记录（轻量操作，每分执行）
                surge_cutoff = time.time() - (SURGE_CACHE_MAX_MINUTES * 60)
                for symbol in list(market_data["surge_cache"].keys()):
                    market_data["surge_cache"][symbol] = [
                        r for r in market_data["surge_cache"][symbol]
                        if r["t"] >= surge_cutoff
                    ]
                    if not market_data["surge_cache"][symbol]:
                        del market_data["surge_cache"][symbol]
                
                # 清理超过2分钟的秒级delta数据
                second_cutoff = time.time() - 120
                for sec_ts in list(market_data["second_deltas"].keys()):
                    if sec_ts < second_cutoff:
                        del market_data["second_deltas"][sec_ts]

            if cleanup_counter == 0:
                # 清理 hourly_kline_cache 每小时一次（保留最近36小时）
                hour_cutoff = (int(time.time()) // 3600 - 36) * 3600
                for symbol in list(market_data.get("hourly_kline_cache", {}).keys()):
                    market_data["hourly_kline_cache"][symbol] = [
                        h for h in market_data["hourly_kline_cache"][symbol]
                        if h.get("t", 0) >= hour_cutoff
                    ]

        # 每分钟聚合后立即上传COS分钟K线（必须在锁外，避免死锁）
        klines_copy = dict(market_data["minute_klines"])
        save_minute_klines_to_cos(klines_copy)
        _save_hourly_cache_to_cos()

        # 同时上传最新行情快照到COS（必须在锁外，避免死锁）
        save_symbols_snapshot_to_cos()
        save_vol_24h_today_to_cos()
        save_today_open_prices_to_cos()

# ========== 定时写入 ==========
def write_loop():
    while True:
        time.sleep(WRITE_INTERVAL_SECONDS)
        save_snapshot()
        # 同步写入COS快照（策略数据持久化）
        save_symbols_snapshot_to_cos()
        save_vol_24h_today_to_cos()
        save_today_open_prices_to_cos()

# ========== 模拟交易状态 (V7 双账户: 现货 + 合约) ==========
SIM_TRADE_STATE_FILE = "/tmp/sim_trade_state.json"

def _compute_position_pnl(pos: dict, current_price: float) -> dict:
    """计算单个持仓的实时盈亏和状态"""
    entry_price = pos.get("entry_price", 0)
    quantity = pos.get("quantity", 0)
    
    if current_price > 0 and entry_price > 0:
        pnl = (current_price - entry_price) * quantity
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl = 0
        pnl_pct = 0
    
    pos["current_price"] = current_price
    pos["unrealized_pnl"] = round(pnl, 4)
    pos["unrealized_pnl_pct"] = round(pnl_pct, 2)
    
    # 状态判断
    tp_price = pos.get("tp_price", pos.get("take_profit_price", float('inf')))
    sl_price = pos.get("sl_price", pos.get("stop_loss_price", 0))
    liq_price = pos.get("liquidation_price", 0)
    
    if current_price >= tp_price:
        pos["status"] = "止盈"
        pos["status_color"] = "profit"
    elif liq_price > 0 and current_price <= liq_price:
        pos["status"] = "爆仓"
        pos["status_color"] = "liquidation"
    elif current_price <= sl_price:
        pos["status"] = "止损"
        pos["status_color"] = "loss"
    else:
        pos["status"] = "持仓中"
        pos["status_color"] = "hold"
    
    return pos

def load_sim_trade_state():
    """读取V7模拟交易状态文件，并用当前市场价格计算实时盈亏（带文件锁）"""
    try:
        if not os.path.exists(SIM_TRADE_STATE_FILE):
            return None
        with open(SIM_TRADE_STATE_FILE, "r") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                state = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except ImportError:
                state = json.load(f)
            except Exception:
                state = json.load(f)
        
        # 用当前 market_data 价格计算实时盈亏
        with data_lock:
            symbols = market_data.get("symbols", {})
        
        # --- 兼容旧格式 ---
        if "account" in state and "positions" in state and "spot_account" not in state:
            # 旧格式: 单账户，转为合约账户
            positions = state.get("positions", [])
            total_unrealized = 0
            total_margin = 0
            for pos in positions:
                symbol = pos.get("symbol", "")
                current_price = symbols.get(symbol, {}).get("price", pos.get("entry_price", 0))
                _compute_position_pnl(pos, current_price)
                total_unrealized += pos.get("unrealized_pnl", 0)
                total_margin += pos.get("margin", 0)
            
            account = state.get("account", {})
            effective_balance = account.get("balance", 0) + total_unrealized
            return {
                "spot_account": None,
                "spot_positions": [],
                "futures_account": account,
                "futures_positions": positions,
                "summary": {
                    "positions_count": len(positions),
                    "total_unrealized_pnl": round(total_unrealized, 4),
                    "total_margin": round(total_margin, 2),
                    "effective_balance": round(effective_balance, 2),
                    "max_positions": 5,
                }
            }
        
        # --- V7 新格式: 双账户 ---
        # 现货持仓
        spot_positions = state.get("spot_positions", [])
        spot_unrealized = 0
        for pos in spot_positions:
            symbol = pos.get("symbol", "")
            current_price = symbols.get(symbol, {}).get("price", pos.get("entry_price", 0))
            _compute_position_pnl(pos, current_price)
            spot_unrealized += pos.get("unrealized_pnl", 0)
        
        spot_account = state.get("spot_account", {})
        spot_effective = spot_account.get("balance", 0) + spot_unrealized
        
        # 合约持仓
        futures_positions = state.get("futures_positions", [])
        futures_unrealized = 0
        futures_margin = 0
        for pos in futures_positions:
            symbol = pos.get("symbol", "")
            current_price = symbols.get(symbol, {}).get("price", pos.get("entry_price", 0))
            _compute_position_pnl(pos, current_price)
            futures_unrealized += pos.get("unrealized_pnl", 0)
            futures_margin += pos.get("margin", 0)
        
        futures_account = state.get("futures_account", {})
        futures_effective = futures_account.get("balance", 0) + futures_unrealized
        
        total_effective = spot_effective + futures_effective
        total_unrealized = spot_unrealized + futures_unrealized
        
        # CC锚持仓
        cc_anchors = state.get("cc_anchors", [])
        cc_unrealized = 0
        for a in cc_anchors:
            symbol = a.get("symbol", "")
            current_price = symbols.get(symbol, {}).get("price", a.get("entry_price", 0))
            a["current_price"] = current_price
            a["unrealized_pnl"] = (current_price - a["entry_price"]) * a.get("quantity", 0)
            a["pnl_pct"] = (current_price - a["entry_price"]) / a["entry_price"] * 100 if a["entry_price"] > 0 else 0
            cc_unrealized += a["unrealized_pnl"]

        return {
            "spot_account": spot_account,
            "spot_positions": spot_positions,
            "futures_account": futures_account,
            "futures_positions": futures_positions,
            "cc_anchors": cc_anchors,
            "summary": {
                "spot_positions_count": len(spot_positions),
                "futures_positions_count": len(futures_positions),
                "spot_effective_balance": round(spot_effective, 2),
                "futures_effective_balance": round(futures_effective, 2),
                "total_effective_balance": round(total_effective, 2),
                "spot_unrealized_pnl": round(spot_unrealized, 4),
                "futures_unrealized_pnl": round(futures_unrealized, 4),
                "total_unrealized_pnl": round(total_unrealized, 4),
                "futures_total_margin": round(futures_margin, 2),
                "max_positions": 20,
            }
        }
    except Exception as e:
        print(f"[SIM_TRADE] 读取状态失败: {e}")
        import traceback
        traceback.print_exc()
        return None


@app.route("/api/sim_trade")
def api_sim_trade():
    """返回V7模拟交易实时状态（双账户）"""
    state = load_sim_trade_state()
    if state is None:
        return jsonify({"code": 1, "msg": "无模拟交易数据"})
    return jsonify({"code": 0, "data": state})


def sim_trade_broadcast_loop():
    """每5秒推送模拟交易状态到所有连接的客户端"""
    while True:
        try:
            time.sleep(5)
            state = load_sim_trade_state()
            if state:
                socketio.emit("sim_trade_update", state, namespace="/")
        except Exception as e:
            print(f"[SIM_TRADE] 广播失败: {e}", flush=True)


# ========== 每日北京时间08:00从WebSocket捕获今日开盘价 ==========
def capture_today_open_from_ws():
    """从WebSocket实时数据中捕获当前价格作为今日开盘价（无需API）"""
    with data_lock:
        symbols = market_data.get("symbols", {})
        today_open = {}
        for symbol, info in symbols.items():
            price = info.get("price", 0)
            if price > 0:
                today_open[symbol] = price
    
    with data_lock:
        market_data["today_open_prices"] = today_open
        market_data["today_open_updated"] = time.time()
    
    print(f"今日开盘价已捕获: {len(today_open)} 个币 (直接从WebSocket)")

def daily_open_price_update_loop():
    """每天北京时间08:00（UTC 00:00）从WebSocket捕获今日开盘价"""
    while True:
        now = datetime.now(tz=timezone.utc)
        # 下一个 UTC 00:00 就是北京时间 08:00
        next_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
        seconds_until = (next_utc_midnight - now).total_seconds()
        
        if seconds_until > 0:
            print(f"等待北京08:00... ({seconds_until/3600:.1f}小时后更新今日开盘价)")
            time.sleep(seconds_until)
        
        # 北京时间08:00了，捕获今日开盘价
        print("=== 北京时间08:00，捕获今日开盘价 ===")
        capture_today_open_from_ws()
        
        # 保存今日开盘价到COS
        save_today_open_prices_to_cos()
        
        # 重置今日累计成交额
        print("=== 重置今日累计成交额 ===")
        with data_lock:
            market_data["vol_24h_today"] = {}
            print(f"已重置 {len(market_data['symbols'])} 个币种的今日累计成交额")
        
        # 清空COS中的昨日vol_24h_today（写入空数据）
        save_vol_24h_today_to_cos()


# ========== 回溯API ==========
@app.route("/api/backtest/bollinger_climb", methods=["POST"])
def api_backtest_bollinger_climb():
    """布林爬坡策略回溯API
    
    请求参数:
    {
        "symbol": "HIGHUSDT",
        "start_time": "2026-04-17 00:00:00",
        "end_time": "2026-04-19 00:00:00",
        "config": {}  # 可选
    }
    """
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol", "").upper()
        start_time = data.get("start_time", "")
        end_time = data.get("end_time")
        config = data.get("config", {})
        
        if not symbol:
            return jsonify({"code": 1, "msg": "缺少symbol参数"})
        if not start_time:
            return jsonify({"code": 1, "msg": "缺少start_time参数"})
        
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        
        from core.backtest_bollinger import run_bollinger_climb_backtest
        
        result = run_bollinger_climb_backtest(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            config=config
        )
        
        return jsonify({"code": 0, "data": result})
        
    except Exception as e:
        print(f"布林爬坡回溯失败: {e}")
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/backtest/bollinger_candidate", methods=["POST"])
def api_backtest_bollinger_candidate():
    """候选蓄力策略回溯API
    
    请求参数:
    {
        "symbol": "HIGHUSDT",
        "start_time": "2026-04-17 00:00:00",
        "end_time": "2026-04-19 00:00:00",
        "config": {}  # 可选
    }
    """
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol", "").upper()
        start_time = data.get("start_time", "")
        end_time = data.get("end_time")
        config = data.get("config", {})
        
        if not symbol:
            return jsonify({"code": 1, "msg": "缺少symbol参数"})
        if not start_time:
            return jsonify({"code": 1, "msg": "缺少start_time参数"})
        
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        
        from core.backtest_bollinger import run_bollinger_candidate_backtest
        
        result = run_bollinger_candidate_backtest(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            config=config
        )
        
        return jsonify({"code": 0, "data": result})
        
    except Exception as e:
        print(f"候选蓄力回溯失败: {e}")
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/backtest/batch", methods=["POST"])
def api_backtest_batch():
    """批量回溯多个币种
    
    请求参数:
    {
        "symbols": ["HIGHUSDT", "ORDIUSDT"],
        "start_time": "2026-04-17 00:00:00",
        "end_time": "2026-04-19 00:00:00",
        "strategy": "bollinger_climb"  # 或 "bollinger_candidate"
    }
    """
    try:
        data = request.get_json() or {}
        symbols = data.get("symbols", [])
        start_time = data.get("start_time", "")
        end_time = data.get("end_time")
        strategy = data.get("strategy", "bollinger_climb")
        
        if not symbols:
            return jsonify({"code": 1, "msg": "缺少symbols参数"})
        if not start_time:
            return jsonify({"code": 1, "msg": "缺少start_time参数"})
        
        from core.backtest_bollinger import run_bollinger_climb_backtest, run_bollinger_candidate_backtest
        
        run_func = run_bollinger_climb_backtest if strategy == "bollinger_climb" else run_bollinger_candidate_backtest
        
        results = []
        for symbol in symbols:
            symbol = symbol.upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"
            
            try:
                result = run_func(
                    symbol=symbol,
                    start_time=start_time,
                    end_time=end_time
                )
                results.append({
                    "symbol": symbol,
                    "signals_count": result.get("summary", {}).get("total_signals", 0),
                    "failed_checks_count": result.get("summary", {}).get("total_failed_checks", 0),
                    "signals": result.get("signals", [])[:3]
                })
            except Exception as e:
                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })
        
        return jsonify({
            "code": 0,
            "data": {
                "strategy": strategy,
                "start_time": start_time,
                "end_time": end_time,
                "results": results
            }
        })
        
    except Exception as e:
        print(f"批量回溯失败: {e}")
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/backtest/debug", methods=["POST"])
def api_backtest_debug():
    """调试指定时间点的信号条件
    
    请求参数:
    {
        "symbol": "HIGHUSDT",
        "timestamp": "2026-04-18 05:00:00",
        "strategy": "bollinger_climb"
    }
    
    返回所有条件的检查结果，不只是第一个失败的
    """
    try:
        data = request.get_json() or {}
        symbol = data.get("symbol", "").upper()
        timestamp = data.get("timestamp", "")
        strategy = data.get("strategy", "bollinger_climb")
        
        if not symbol:
            return jsonify({"code": 1, "msg": "缺少symbol参数"})
        if not timestamp:
            return jsonify({"code": 1, "msg": "缺少timestamp参数"})
        
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        
        from core.backtest_bollinger import debug_check_at_timestamp
        
        with data_lock:
            hourly_cache = market_data.get("hourly_kline_cache", {})
        
        result = debug_check_at_timestamp(
            symbol=symbol,
            timestamp=timestamp,
            strategy=strategy,
            hourly_kline_cache=hourly_cache
        )
        
        return jsonify({"code": 0, "data": result})
        
    except Exception as e:
        print(f"调试检查失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"code": 1, "msg": str(e)})


@app.route("/api/backtest/top_symbols", methods=["GET"])
def api_backtest_top_symbols():
    """获取成交额前N的币种列表，用于批量回溯"""
    try:
        n = int(request.args.get("n", 20))
        
        with data_lock:
            symbols = market_data.get("symbols", {})
        
        sorted_symbols = sorted(symbols.items(), key=lambda x: x[1].get("q", 0), reverse=True)
        top_symbols = [s[0] for s in sorted_symbols[:n]]
        
        return jsonify({
            "code": 0,
            "data": {
                "symbols": top_symbols,
                "count": len(top_symbols)
            }
        })
        
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)})


# ========== 回测部署页面 ==========

@app.route("/backtest")
def backtest_page():
    """回溯+参数设置页面"""
    response = make_response(render_template("backtest.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def _import_backtest_runner():
    """延迟导入 backtest_runner（避免触发 core/__init__.py 的 matplotlib 依赖）"""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "backtest_runner", "/home/myuser/websocket_new/core/backtest_runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backtest_runner"] = mod
    spec.loader.exec_module(mod)
    return mod

@app.route("/api/backtest/hybrid", methods=["POST"])
def api_backtest_hybrid():
    """运行Rust hybrid回测搜索 (纯BB绑定模式)"""
    try:
        data = request.get_json(force=True) or {}
        mod = _import_backtest_runner()
        result = mod.run_hybrid_search(
            trials=int(data.get("trials", 100)),
            symbols=int(data.get("symbols", 200)),
            vs_ratio_min=float(data.get("vs_ratio_min", 1.0)),
            vs_ratio_max=float(data.get("vs_ratio_max", 10.0)))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/backtest/current_params", methods=["GET"])
def api_backtest_current_params():
    """返回当前 sim_trade.py 参数"""
    try:
        return jsonify(_import_backtest_runner().get_current_params())
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/backtest/deploy", methods=["POST"])
def api_backtest_deploy_params():
    """部署参数到 sim_trade.py + hybrid.rs 并重启交易服务 (需API Key)"""
    auth = request.headers.get("Authorization", "")
    expected = os.environ.get("DEPLOY_API_KEY", "")
    if not expected or auth != f"Bearer {expected}":
        return jsonify({"error": "unauthorized", "deployed": False}), 401
    try:
        data = request.get_json(force=True) or {}
        result = _import_backtest_runner().deploy_params(**data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "deployed": False})


# ========== 启动 ==========
if __name__ == "__main__":
    # 先启动Web服务，初始化数据放到后台线程
    def _startup():
        init_market_data()
        
        # 尝试从COS加载历史K线
        try:
            historical_klines = load_minute_klines_from_cos()
            with data_lock:
                if historical_klines:
                    market_data["minute_klines"] = historical_klines
        except Exception as e:
            print(f"加载历史K线失败: {e}")
        
        # 从COS加载最近8小时的15分钟成交量历史（扩大窗口减少停机影响）
        try:
            current_slot = get_current_15m_slot()
            vol_15m_hist = load_vol_15m_from_cos(current_slot, slots_count=32)
            actual_slots = {}
            for symbol, slots in vol_15m_hist.items():
                actual_slots[symbol] = slots
                if symbol not in market_data["vol_15m_history"]:
                    market_data["vol_15m_history"][symbol] = {}
                market_data["vol_15m_history"][symbol].update(slots)
            # 统计实际有多少个非零slot（评估数据质量）
            non_zero_counts = [len([v for v in slots.values() if v > 0]) for slots in actual_slots.values()]
            avg_non_zero = sum(non_zero_counts) / len(non_zero_counts) if non_zero_counts else 0
            print(f"[VOL_15M_COS] 启动时加载了 {len(vol_15m_hist)} 个币种的15分钟历史，平均非零slot: {avg_non_zero:.1f}/32")
        except Exception as e:
            print(f"[VOL_15M_COS] 启动加载失败: {e}")
        
        # 启动线程
        threading.Thread(target=ws_update_loop, daemon=True).start()
        threading.Thread(target=write_loop, daemon=True).start()
        threading.Thread(target=minute_aggregator_loop, daemon=True).start()
        threading.Thread(target=daily_open_price_update_loop, daemon=True).start()
        socketio.start_background_task(sim_trade_broadcast_loop)
        threading.Thread(target=bb_daily_background_loop, daemon=True).start()
        threading.Thread(target=_refresh_snapshot_cache, daemon=True).start()

        # 优先从COS加载1h K线缓存，检测数据时效性
        hourly_cache = _load_hourly_cache_from_cos()
        cache_stale = True
        if hourly_cache:
            # 检测缓存中最新的K线时间戳
            latest_ts = 0
            for hklines in hourly_cache.values():
                if hklines:
                    latest_ts = max(latest_ts, max(h.get("t", 0) for h in hklines))
            hours_stale = (time.time() - latest_ts) / 3600 if latest_ts > 0 else 999
            print(f"[HOURLY-COS] COS缓存最新K线时间: {datetime.fromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M') if latest_ts else 'N/A'} (距今{hours_stale:.1f}小时)")
            cache_stale = hours_stale > 2  # 超过2小时未更新视为过期
        
        # 优先从本地binance直拉缓存加载（418币种×720根≈30天，最完整）
        NEW_CACHE = Path(__file__).parent / "data" / "hourly_backfill.json"
        if NEW_CACHE.exists():
            _v7_load_hourly_cache()
        elif hourly_cache and len(hourly_cache) >= 100 and not cache_stale:
            trading_symbols = market_data.get("trading_symbols", set())
            if trading_symbols:
                before = len(hourly_cache)
                hourly_cache = {s: v for s, v in hourly_cache.items() if s in trading_symbols}
                removed = before - len(hourly_cache)
                if removed:
                    print(f"[HOURLY-COS] 清理了 {removed} 个非TRADING币种的1h缓存")
            with data_lock:
                market_data["hourly_kline_cache"] = hourly_cache
                market_data["bb_backfill_done"] = True
            print(f"[HOURLY-COS] 直接从COS加载了 {len(hourly_cache)} 个币种的1h K线，数据新鲜无需API回填")
            _update_today_open_from_hourly_cache(hourly_cache)
        else:
            # V7: 跳过API回填，直接从本地缓存加载1h K线
            _v7_load_hourly_cache()
        print(f"=" * 50)
        print(f"行情数据初始化完成")
        print("=" * 50)

    threading.Thread(target=_startup, daemon=True).start()

    print(f"=" * 50)
    print(f"行情监控服务启动: http://0.0.0.0:{PORT}")
    print(f"分钟K线存储: COS ({COS_KEY})")
    print(f"delta_q突增阈值: {DELTA_Q_SURGE_THRESHOLD/1e6:.1f}M USDT")
    print("=" * 50)
    import sys
    print("=" * 50, file=sys.stderr)
    print(f"行情监控服务启动: http://0.0.0.0:{PORT}", file=sys.stderr)
    print(f"分钟K线存储: COS ({COS_KEY})", file=sys.stderr)
    print(f"delta_q突增阈值: {DELTA_Q_SURGE_THRESHOLD/1e6:.1f}M USDT", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    sys.stderr.flush()
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
