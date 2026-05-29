#!/usr/bin/env python3
"""
V7 混合策略实盘模拟系统
- 现货端: 日线布林带爬坡策略 (p=30, std=2.5, h=4, hlw=5, hlm=3)
- 合约端: 15分钟量surge策略，只在有现货持仓的币种上开仓

一切参数根据 v7 2000轮验证最优结果设定
"""
import os
import io
import json
import time
import requests
import threading
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

# ========== V7 最优参数配置 ==========

# --- 现货参数 (BB Spot) ---
SPOT_INITIAL_CAPITAL = 100          # 初始资金 USDT
SPOT_PER_TRADE = 5                  # 每单金额 USDT
SPOT_MAX_POSITIONS = 20             # 最大持仓币种数
SPOT_FEE = 0.001                    # 手续费 0.1%
SPOT_TP_MULTIPLIER = 2.0            # 止盈: 2倍买入价
SPOT_GAIN_FILTER_PCT = 10.0         # 日涨幅限制 (%)
SPOT_VOL_FILTER = 1_000_000         # 24h最低成交量
SPOT_BB_PERIOD = 30                 # 布林带周期
SPOT_BB_STD_MULT = 2.5              # 标准差倍数
SPOT_MIN_HOURS = 4                  # 量surge最小持续小时数
SPOT_HL_WINDOW = 5                  # 高低点窗口（天）
SPOT_HL_MIN = 3                     # 窗口内最少高低点个数

# --- 合约参数 (VS Futures) ---
FUT_INITIAL_CAPITAL = 100           # 初始资金 USDT
FUT_MARGIN = 20                     # 每单保证金 USDT
FUT_MAX_POSITIONS = 20              # 最大持仓（实际受限于现货）
FUT_LEVERAGE = 10                   # 杠杆倍数
FUT_TP_PCT = 50                     # 止盈比例 (%)
FUT_SL_PCT = 0.02                   # 止损价格比例 2% (10x杠杆下 ≈ 20%保证金损失)
FUT_VOL_FILTER = 1_000_000          # 24h最低成交量
FUT_MIN_RATIO = 4.0                 # 量surge最小倍数
FUT_MAX_DAILY_TP = 4                # 日最大止盈次数

# --- 其他配置 ---
HOST = os.environ.get('MARKET_HOST', 'http://localhost:5003')
API_TIMEOUT = 10

EXCLUDE_SYMBOLS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT',
    'TSLAUSDT', 'NVDAUSDT', 'AMZNUSDT', 'GOOGLUSDT', 'AAPLUSDT',
    'COINUSDT', 'MSTRUSDT', 'METAUSDT', 'TSMUSDT',
    'XAUUSDT', 'XAGUSDT', 'XAUTUSDT', 'NATGASUSDT',
    # 稳定币对
    'USDCUSDT', 'RLUSDUSDT', 'UUSDT', 'XUSDUSDT', 'USD1USDT',
    'FDUSDUSDT', 'TUSDUSDT', 'PAXUSDT', 'BUSDUSDT', 'SUSDUSDT',
    'USDEUSDT',
}

# ========== 全局状态 ==========

# 现货账户
spot_account = {
    "balance": SPOT_INITIAL_CAPITAL,
    "initial_balance": SPOT_INITIAL_CAPITAL,
    "total_pnl": 0,
    "total_trades": 0,
    "win_trades": 0,
    "loss_trades": 0,
    "win_rate": 0,
    "max_drawdown": 0,
    "peak_balance": SPOT_INITIAL_CAPITAL,
}
spot_positions = []  # [{symbol, entry_price, quantity, cost_basis, entry_time, tp_price, sl_price}]
spot_lock = threading.Lock()
futures_lock = threading.Lock()

# 合约账户
futures_account = {
    "balance": FUT_INITIAL_CAPITAL,
    "initial_balance": FUT_INITIAL_CAPITAL,
    "total_pnl": 0,
    "total_trades": 0,
    "win_trades": 0,
    "loss_trades": 0,
    "win_rate": 0,
    "max_drawdown": 0,
    "peak_balance": FUT_INITIAL_CAPITAL,
}
futures_positions = []  # [{symbol, entry_price, quantity, margin, leverage, position_value, entry_time, tp_price, sl_price, signal_type, signal_detail}]

# 联动状态
spot_entry_ts = {}  # {symbol: entry_timestamp} 记录每个币种的现货入场时间

# 过滤/冷却状态
daily_take_profit_count = {}  # {(symbol, date_str): count} 合约日止盈计数
cooldown_symbols = {}  # {symbol: cooldown_end_timestamp} 止损后冷却

# 交易日志
spot_trade_log = []
futures_trade_log = []
signal_log = []

# ========== 工具函数 ==========

def api_get(endpoint: str) -> dict:
    try:
        r = requests.get(f"{HOST}{endpoint}", timeout=API_TIMEOUT)
        return r.json()
    except Exception as e:
        print(f"[API错误] {endpoint}: {e}")
        return {}

def get_current_price(symbol: str) -> float:
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=5
        )
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
    except:
        pass
    return 0

def get_24h_volume(symbol: str) -> float:
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=5
        )
        if resp.status_code == 200:
            return float(resp.json().get("quoteVolume", 0))
    except:
        pass
    return 0

def get_beijing_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def is_in_cooldown(symbol: str) -> bool:
    return time.time() < cooldown_symbols.get(symbol, 0)

def set_cooldown(symbol: str, minutes: int = 30):
    cooldown_symbols[symbol] = time.time() + minutes * 60

def check_daily_tp_filter(symbol: str) -> bool:
    date_str = get_beijing_date_str()
    key = (symbol, date_str)
    count = daily_take_profit_count.get(key, 0)
    if count >= FUT_MAX_DAILY_TP:
        return False
    return True

def record_take_profit(symbol: str):
    date_str = get_beijing_date_str()
    key = (symbol, date_str)
    daily_take_profit_count[key] = daily_take_profit_count.get(key, 0) + 1
    count = daily_take_profit_count[key]
    print(f"[合约日止盈] {symbol} 当日止盈 {count}/{FUT_MAX_DAILY_TP} 次")

def format_price(price: float) -> str:
    if price <= 0:
        return "N/A"
    if price < 0.0001:
        return f"{price:.8f}"
    elif price < 0.001:
        return f"{price:.7f}"
    elif price < 0.01:
        return f"{price:.6f}"
    elif price < 0.1:
        return f"{price:.5f}"
    elif price < 1:
        return f"{price:.4f}"
    elif price < 100:
        return f"{price:.4f}"
    else:
        return f"{price:.2f}"

def check_daily_gain_filter(symbol: str, max_gain_pct: float = SPOT_GAIN_FILTER_PCT) -> tuple[bool, float]:
    """日线涨幅过滤"""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 1},
            timeout=5
        )
        if resp.status_code != 200:
            return True, 0
        klines = resp.json()
        if not klines:
            return True, 0
        k = klines[0]
        open_price = float(k[1])
        close_price = float(k[4])
        if open_price <= 0:
            return True, 0
        gain_pct = (close_price - open_price) / open_price * 100
        if gain_pct > max_gain_pct:
            return False, gain_pct
        return True, gain_pct
    except Exception as e:
        return True, 0

def get_daily_klines(symbol: str, limit: int = 40) -> list:
    """获取日线K线"""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return []

def calculate_bb_lower_band(closes: list, period: int = 30, std_mult: float = 2.5) -> float:
    """计算布林带下轨"""
    if len(closes) < period:
        return None
    recent = closes[-period:]
    sma = sum(recent) / len(recent)
    variance = sum((c - sma) ** 2 for c in recent) / len(recent)
    std = variance ** 0.5
    return sma - std_mult * std

def calculate_pnl(entry_price: float, current_price: float, quantity: float) -> float:
    return (current_price - entry_price) * quantity

# ========== 现货策略 ==========

def get_bb_climb_signals() -> list:
    data = api_get("/api/bollinger_climb")
    signals = data.get("data", [])
    updated_at = data.get("updated_at", 0)
    now = time.time()
    if now - updated_at > 300:
        return []
    return signals

def get_spot_position(symbol: str):
    for p in spot_positions:
        if p["symbol"] == symbol:
            return p
    return None

def get_spot_positions_count():
    return len(spot_positions)

def open_spot_position(symbol: str, entry_price: float, signal_detail: dict = None):
    """开现货仓位"""
    global spot_positions, spot_account
    
    if symbol in EXCLUDE_SYMBOLS:
        return False
    
    if get_spot_position(symbol):
        return False
    
    if get_spot_positions_count() >= SPOT_MAX_POSITIONS:
        return False
    
    if spot_account["balance"] < SPOT_PER_TRADE:
        return False
    
    # 涨幅过滤
    passed, gain_pct = check_daily_gain_filter(symbol)
    if not passed:
        print(f"[现货涨幅过滤] {symbol} 当日涨幅 {gain_pct:.2f}% > {SPOT_GAIN_FILTER_PCT}%，跳过")
        return False
    
    # 计算数量
    quantity = SPOT_PER_TRADE / entry_price
    cost = SPOT_PER_TRADE * (1 + SPOT_FEE)
    
    # 止盈: 2倍买入价
    tp_price = entry_price * SPOT_TP_MULTIPLIER
    
    # 止损: 基于日线布林带下轨（用最近period天数据）
    klines = get_daily_klines(symbol, SPOT_BB_PERIOD + 5)
    sl_price = entry_price * 0.9  # 默认兜底
    if klines and len(klines) >= SPOT_BB_PERIOD:
        closes = [float(k[4]) for k in klines[-SPOT_BB_PERIOD:]]
        bb_lower = calculate_bb_lower_band(closes, SPOT_BB_PERIOD, SPOT_BB_STD_MULT)
        if bb_lower and bb_lower > 0:
            sl_price = bb_lower
    
    pos = {
        "symbol": symbol,
        "entry_price": entry_price,
        "quantity": quantity,
        "cost_basis": cost,
        "entry_time": time.time(),
        "tp_price": tp_price,
        "sl_price": sl_price,
    }
    
    with spot_lock:
        spot_positions.append(pos)
    spot_account["balance"] -= cost
    spot_entry_ts[symbol] = pos["entry_time"]
    
    log = {
        "time": datetime.now().isoformat(),
        "action": "OPEN",
        "symbol": symbol,
        "entry_price": entry_price,
        "quantity": quantity,
        "cost": cost,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "detail": signal_detail or {},
    }
    spot_trade_log.append(log)
    signal_log.append(log)
    
    print(f"[现货开仓] {symbol} @ {format_price(entry_price)} | 数量: {quantity:.4f} | 成本: {cost:.2f} USDT | TP: {format_price(tp_price)} | SL: {format_price(sl_price)}")
    return True

def close_spot_position(pos: dict, reason: str, close_price: float, pnl: float):
    """平现货仓位"""
    global spot_positions, spot_account
    
    symbol = pos["symbol"]
    quantity = pos["quantity"]
    
    # 卖出金额 - 手续费
    sell_value = close_price * quantity * (1 - SPOT_FEE)
    actual_pnl = sell_value - pos["cost_basis"]
    
    spot_account["balance"] += sell_value
    spot_account["total_pnl"] += actual_pnl
    spot_account["total_trades"] += 1
    
    if actual_pnl > 0:
        spot_account["win_trades"] += 1
    else:
        spot_account["loss_trades"] += 1
    
    if spot_account["total_trades"] > 0:
        spot_account["win_rate"] = spot_account["win_trades"] / spot_account["total_trades"] * 100
    
    if spot_account["balance"] > spot_account["peak_balance"]:
        spot_account["peak_balance"] = spot_account["balance"]
    drawdown = (spot_account["peak_balance"] - spot_account["balance"]) / spot_account["peak_balance"] * 100
    if drawdown > spot_account["max_drawdown"]:
        spot_account["max_drawdown"] = drawdown
    
    log = {
        "time": datetime.now().isoformat(),
        "action": "CLOSE",
        "reason": reason,
        "symbol": symbol,
        "entry_price": pos["entry_price"],
        "close_price": close_price,
        "quantity": quantity,
        "pnl": actual_pnl,
        "balance": spot_account["balance"],
    }
    spot_trade_log.append(log)
    
    print(f"[现货平仓] {reason} {symbol} @ {format_price(close_price)} | 盈亏: {actual_pnl:+.2f} USDT | 余额: {spot_account['balance']:.2f}")
    
    with spot_lock:
        spot_positions.remove(pos)
    save_state()

def check_spot_positions():
    """检查所有现货持仓的TP/SL"""
    positions_to_close = []
    
    for pos in spot_positions:
        symbol = pos["symbol"]
        current_price = get_current_price(symbol)
        
        if current_price <= 0:
            continue
        
        entry_price = pos["entry_price"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]
        quantity = pos["quantity"]
        
        # 止盈检测
        if current_price >= tp_price:
            pnl = calculate_pnl(entry_price, current_price, quantity)
            positions_to_close.append((pos, "TAKE_PROFIT", current_price, pnl))
            continue
        
        # 止损检测
        if current_price <= sl_price:
            pnl = calculate_pnl(entry_price, current_price, quantity)
            positions_to_close.append((pos, "STOP_LOSS", current_price, pnl))
            continue
    
    for pos, reason, price, pnl in positions_to_close:
        close_spot_position(pos, reason, price, pnl)

def evaluate_spot_signals():
    """评估BB信号并开现货仓位"""
    signals = get_bb_climb_signals()
    
    for sig in signals:
        symbol = sig.get("symbol", "")
        
        if symbol in EXCLUDE_SYMBOLS:
            continue
        if get_spot_position(symbol):
            continue
        if get_spot_positions_count() >= SPOT_MAX_POSITIONS:
            break
        if spot_account["balance"] < SPOT_PER_TRADE:
            break
        
        consecutive = sig.get("consecutive_hours", 0)
        if consecutive < SPOT_MIN_HOURS:
            continue
        
        # 成交量过滤
        vol_24h = get_24h_volume(symbol)
        if vol_24h < SPOT_VOL_FILTER:
            continue
        
        entry_price = get_current_price(symbol)
        if entry_price <= 0:
            continue
        
        signal_detail = {
            "连续小时": consecutive,
            "24h成交额(亿)": round(vol_24h / 1e8, 2),
        }
        open_spot_position(symbol, entry_price, signal_detail)

# ========== 合约策略 ==========

def get_vol_surge_signals() -> list:
    data = api_get("/api/vol_surge")
    signals = data.get("data", [])
    now = time.time()
    return [s for s in signals if s.get("ratio", 0) >= FUT_MIN_RATIO and now - s.get("start_time", 0) < 300]

def get_futures_position(symbol: str):
    for p in futures_positions:
        if p["symbol"] == symbol:
            return p
    return None

def get_futures_positions_count():
    return len(futures_positions)

def get_futures_available_balance():
    used = sum(p["margin"] for p in futures_positions)
    return futures_account["balance"] - used

def open_futures_position(symbol: str, signal_type: str, entry_price: float, 
                         signal_detail: dict = None):
    """开合约仓位"""
    global futures_positions, futures_account
    
    if symbol in EXCLUDE_SYMBOLS:
        return False
    
    if get_futures_position(symbol):
        return False
    
    if is_in_cooldown(symbol):
        return False
    
    if get_futures_positions_count() >= FUT_MAX_POSITIONS:
        return False
    
    available = get_futures_available_balance()
    if available < FUT_MARGIN:
        return False
    
    # 【V7核心约束】必须在现货持仓中
    if not get_spot_position(symbol):
        return False
    
    # 【V7核心约束】信号时间必须在现货入场之后
    if symbol in spot_entry_ts:
        # 实时场景中，如果信号出现且已有现货持仓，默认满足条件
        pass
    else:
        return False
    
    # 日止盈过滤
    if not check_daily_tp_filter(symbol):
        return False
    
    # 涨幅过滤
    passed, _ = check_daily_gain_filter(symbol, SPOT_GAIN_FILTER_PCT)
    if not passed:
        return False
    
    # 量surge过滤: 检查最近3根1h K线是否有2根收阴
    if check_recent_1h_bearish(symbol):
        return False
    
    margin = FUT_MARGIN
    leverage = FUT_LEVERAGE
    position_value = margin * leverage
    quantity = position_value / entry_price
    
    # 止盈价
    tp_price = entry_price * (1 + FUT_TP_PCT / 100 / leverage)
    # 止损价: 开仓价 * (1 - 2%)
    sl_price = entry_price * (1 - FUT_SL_PCT)
    
    pos = {
        "symbol": symbol,
        "signal_type": signal_type,
        "signal_detail": signal_detail or {},
        "entry_price": entry_price,
        "quantity": quantity,
        "margin": margin,
        "leverage": leverage,
        "position_value": position_value,
        "entry_time": time.time(),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "is_long": True,
    }
    
    with futures_lock:
        futures_positions.append(pos)
    save_state()
    
    log = {
        "time": datetime.now().isoformat(),
        "action": "OPEN",
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "quantity": quantity,
        "margin": margin,
        "leverage": leverage,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "detail": signal_detail or {},
    }
    futures_trade_log.append(log)
    signal_log.append(log)
    
    print(f"[合约开仓] {symbol} @ {format_price(entry_price)} | {signal_type} | 保证金: {margin} USDT | 杠杆: {leverage}x | TP: {format_price(tp_price)} | SL: {format_price(sl_price)}")
    return True

def close_futures_position(pos: dict, reason: str, close_price: float, pnl: float):
    """平合约仓位"""
    global futures_positions, futures_account
    
    margin = pos["margin"]
    leverage = pos["leverage"]
    position_value = pos["position_value"]
    
    # 手续费 0.04% × 2
    total_fee = position_value * 0.0004 * 2
    actual_pnl = pnl - total_fee
    
    futures_account["balance"] += actual_pnl
    futures_account["total_pnl"] += actual_pnl
    futures_account["total_trades"] += 1
    
    if actual_pnl > 0:
        futures_account["win_trades"] += 1
    else:
        futures_account["loss_trades"] += 1
    
    if futures_account["total_trades"] > 0:
        futures_account["win_rate"] = futures_account["win_trades"] / futures_account["total_trades"] * 100
    
    if futures_account["balance"] > futures_account["peak_balance"]:
        futures_account["peak_balance"] = futures_account["balance"]
    drawdown = (futures_account["peak_balance"] - futures_account["balance"]) / futures_account["peak_balance"] * 100
    if drawdown > futures_account["max_drawdown"]:
        futures_account["max_drawdown"] = drawdown
    
    log = {
        "time": datetime.now().isoformat(),
        "action": "CLOSE",
        "reason": reason,
        "symbol": pos["symbol"],
        "entry_price": pos["entry_price"],
        "close_price": close_price,
        "quantity": pos["quantity"],
        "pnl": actual_pnl,
        "fee": total_fee,
        "margin": margin,
    }
    futures_trade_log.append(log)
    
    print(f"[合约平仓] {reason} {pos['symbol']} @ {format_price(close_price)} | 盈亏: {actual_pnl:+.2f} USDT | 余额: {futures_account['balance']:.2f} | 胜率: {futures_account['win_rate']:.0f}%")
    
    with futures_lock:
        futures_positions.remove(pos)
    save_state()
    
    if reason == "TAKE_PROFIT":
        record_take_profit(pos["symbol"])
    
    if reason == "STOP_LOSS":
        set_cooldown(pos["symbol"], 30)

def check_futures_positions():
    """检查合约持仓的TP/SL"""
    positions_to_close = []
    total_unrealized = 0
    
    for pos in futures_positions:
        symbol = pos["symbol"]
        current_price = get_current_price(symbol)
        
        if current_price <= 0:
            continue
        
        entry_price = pos["entry_price"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]
        quantity = pos["quantity"]
        
        pnl = calculate_pnl(entry_price, current_price, quantity)
        total_unrealized += pnl
        
        if current_price >= tp_price:
            positions_to_close.append((pos, "TAKE_PROFIT", current_price, pnl))
            continue
        
        if current_price <= sl_price:
            positions_to_close.append((pos, "STOP_LOSS", current_price, pnl))
            continue
    
    # 逐仓爆仓检测 — Binance逐仓保证金模式
    MAINTENANCE_MARGIN_RATE = 0.005  # 0.5%
    for pos in futures_positions:
        if pos in [pc[0] for pc in positions_to_close]:
            continue
        symbol = pos["symbol"]
        current_price = get_current_price(symbol)
        if current_price <= 0:
            continue
        liq_price = pos["entry_price"] * (1 - 1.0 / pos.get("leverage", 10) + MAINTENANCE_MARGIN_RATE)
        if current_price <= liq_price:
            pnl = calculate_pnl(pos["entry_price"], current_price, pos["quantity"])
            positions_to_close.append((pos, "LIQUIDATED", current_price, pnl))
    
    closed_symbols = set()
    for pos, reason, price, pnl in positions_to_close:
        if pos["symbol"] in closed_symbols:
            continue
        close_futures_position(pos, reason, price, pnl)
        closed_symbols.add(pos["symbol"])

def check_recent_1h_bearish(symbol: str) -> bool:
    """检查最近3根1h K线中是否有至少2根收阴"""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 3},
            timeout=5
        )
        if resp.status_code != 200:
            return False
        klines = resp.json()
        if len(klines) < 2:
            return False
        bearish_count = 0
        for k in klines:
            if float(k[4]) < float(k[1]):
                bearish_count += 1
        return bearish_count >= 2
    except:
        return False

def evaluate_futures_signals():
    """评估VS信号并开合约仓位"""
    available = get_futures_available_balance()
    if available < FUT_MARGIN:
        return
    
    signals = get_vol_surge_signals()
    
    # 按24h成交量降序
    signals_with_vol = []
    for sig in signals:
        symbol = sig.get("symbol", "")
        if symbol in EXCLUDE_SYMBOLS or get_futures_position(symbol):
            continue
        ratio = sig.get("ratio", 0)
        if ratio < FUT_MIN_RATIO:
            continue
        vol_24h = get_24h_volume(symbol)
        if vol_24h < FUT_VOL_FILTER:
            continue
        sig["_vol_24h"] = vol_24h
        signals_with_vol.append(sig)
    
    signals_with_vol.sort(key=lambda s: s.get("_vol_24h", 0), reverse=True)
    
    for sig in signals_with_vol:
        if get_futures_positions_count() >= FUT_MAX_POSITIONS:
            break
        if get_futures_available_balance() < FUT_MARGIN:
            break
        
        symbol = sig.get("symbol", "")
        ratio = sig.get("ratio", 0)
        vol_24h = sig.get("_vol_24h", 0)
        
        entry_price = get_current_price(symbol)
        if entry_price <= 0:
            continue
        
        signal_type = f"VOL_SURGE_{ratio:.1f}x"
        signal_detail = {"突增倍数": round(ratio, 2), "24h成交额(亿)": round(vol_24h / 1e8, 2)}
        open_futures_position(symbol, signal_type, entry_price, signal_detail)

# ========== 状态保存与加载 ==========

SIM_TRADE_STATE_FILE = "/tmp/sim_trade_state.json"

def save_state():
    state = {
        "spot_account": spot_account,
        "spot_positions": spot_positions,
        "futures_account": futures_account,
        "futures_positions": futures_positions,
        "spot_entry_ts": spot_entry_ts,
        "daily_take_profit_count": {f"{k[0]}#{k[1]}": v for k, v in daily_take_profit_count.items()},
        "cooldown_symbols": cooldown_symbols,
        "spot_trade_log": spot_trade_log[-50:],
        "futures_trade_log": futures_trade_log[-50:],
        "saved_at": datetime.now().isoformat(),
    }
    import tempfile
    import fcntl
    tmp_path = "/tmp/sim_trade_state.json.tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(state, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(tmp_path, SIM_TRADE_STATE_FILE)

def load_state():
    global spot_account, spot_positions, futures_account, futures_positions
    global spot_entry_ts, daily_take_profit_count, cooldown_symbols
    
    if not os.path.exists(SIM_TRADE_STATE_FILE):
        return
    
    try:
        with open(SIM_TRADE_STATE_FILE, "r") as f:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            state = json.load(f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        spot_account.update(state.get("spot_account", {}))
        spot_positions[:] = state.get("spot_positions", [])
        futures_account.update(state.get("futures_account", {}))
        futures_positions[:] = state.get("futures_positions", [])
        spot_entry_ts.update(state.get("spot_entry_ts", {}))
        
        for k, v in state.get("daily_take_profit_count", {}).items():
            parts = k.split("#")
            if len(parts) == 2:
                daily_take_profit_count[(parts[0], parts[1])] = v
        
        cooldown_symbols.update(state.get("cooldown_symbols", {}))
        
        print(f"[状态恢复] 现货持仓: {len(spot_positions)} | 合约持仓: {len(futures_positions)}")
    except Exception as e:
        print(f"[状态恢复失败] {e}")

# ========== 状态打印 ==========

def print_status():
    # 现货
    spot_unrealized = 0
    for pos in spot_positions:
        current = get_current_price(pos["symbol"])
        if current > 0:
            spot_unrealized += calculate_pnl(pos["entry_price"], current, pos["quantity"])
    spot_equity = spot_account["balance"] + spot_unrealized
    
    # 合约
    fut_unrealized = 0
    for pos in futures_positions:
        current = get_current_price(pos["symbol"])
        if current > 0:
            fut_unrealized += calculate_pnl(pos["entry_price"], current, pos["quantity"])
    fut_equity = futures_account["balance"] + fut_unrealized
    
    print(f"\n{'='*70}")
    print(f"V7 混合策略 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    print(f"【现货】权益: {spot_equity:.2f} USDT | 余额: {spot_account['balance']:.2f} | 未实现: {spot_unrealized:+.2f} | 持仓: {len(spot_positions)}/{SPOT_MAX_POSITIONS} | 胜率: {spot_account['win_rate']:.0f}%")
    print(f"【合约】权益: {fut_equity:.2f} USDT | 余额: {futures_account['balance']:.2f} | 未实现: {fut_unrealized:+.2f} | 持仓: {len(futures_positions)}/{FUT_MAX_POSITIONS} | 胜率: {futures_account['win_rate']:.0f}%")
    print(f"【综合】权益: {spot_equity + fut_equity:.2f} USDT | 回撤: 现货{spot_account['max_drawdown']:.1f}% 合约{futures_account['max_drawdown']:.1f}%")
    
    if spot_positions:
        print(f"\n现货持仓:")
        for pos in spot_positions:
            current = get_current_price(pos["symbol"])
            pnl_pct = ((current - pos["entry_price"]) / pos["entry_price"] * 100) if current > 0 else 0
            status = "✓止盈" if current >= pos["tp_price"] else ("✗止损" if current <= pos["sl_price"] else "持仓中")
            print(f"  {pos['symbol']:<14} 入:{format_price(pos['entry_price']):<12} 现:{format_price(current):<12} {pnl_pct:>+7.2f}% {status}")
    
    if futures_positions:
        print(f"\n合约持仓:")
        for pos in futures_positions:
            current = get_current_price(pos["symbol"])
            pnl_pct = ((current - pos["entry_price"]) / pos["entry_price"] * 100) if current > 0 else 0
            status = "✓止盈" if current >= pos["tp_price"] else ("✗止损" if current <= pos["sl_price"] else "持仓中")
            print(f"  {pos['symbol']:<14} 入:{format_price(pos['entry_price']):<12} 现:{format_price(current):<12} {pnl_pct:>+7.2f}% {status} | {pos['signal_type']}")
    
    print("=" * 70)

# ========== 主循环 ==========

def main():
    load_state()
    
    print("=" * 70)
    print("V7 混合策略实盘模拟系统")
    print(f"现货: 初始{SPOT_INITIAL_CAPITAL} USDT | 每单{SPOT_PER_TRADE} USDT | 最多{SPOT_MAX_POSITIONS}个")
    print(f"合约: 初始{FUT_INITIAL_CAPITAL} USDT | 每单{FUT_MARGIN} USDT | 杠杆{FUT_LEVERAGE}x")
    print(f"核心约束: 合约只在有现货持仓的币种上开仓")
    print("=" * 70)
    
    last_status_time = 0
    last_signal_check = 0
    
    while True:
        try:
            now = time.time()
            
            # 检查并平仓
            if spot_positions:
                check_spot_positions()
            if futures_positions:
                check_futures_positions()
            
            # 每10秒评估信号
            if now - last_signal_check >= 10:
                last_signal_check = now
                evaluate_spot_signals()
                evaluate_futures_signals()
            
            # 每60秒打印状态
            if now - last_status_time >= 60:
                last_status_time = now
                print_status()
                save_state()
            
            time.sleep(1)
            
        except KeyboardInterrupt:
            print("\n退出...")
            save_state()
            break
        except Exception as e:
            print(f"[错误] {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()
