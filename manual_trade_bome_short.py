#!/usr/bin/env python3
"""一次性手动交易: BOMEUSDT SHORT 10U 10x, 只挂 SL10 止损 (2026-08-12 08:05 执行)
用户 8/11 指令: 明天 08:05 开 BOMEUSDT SHORT, 10U 保证金, 10x, SL10 不挂止盈
说明: 绕过 ALLOW_SHORT=false (用户手动指令); 系统不管理此 SHORT (仅交易所 SL 单兜底)
"""
import sys, os, json, time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import auto_dual_trade as adt

SYMBOL = 'BOMEUSDT'
DIRECTION = 'SHORT'
MARGIN = 10.0
LEVERAGE = 10
SL_PCT = 10.0  # 只挂止损, 不挂止盈

def log(msg):
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(os.path.join(BASE, 'logs', 'manual_trade_bome.log'), 'a') as f:
        f.write(line + '\n')

def main():
    # 1. 设置杠杆
    try:
        adt.signed_request('POST', '/fapi/v1/leverage', {'symbol': SYMBOL, 'leverage': LEVERAGE})
        log(f'杠杆已设 {LEVERAGE}x')
    except Exception as e:
        log(f'杠杆设置失败(继续): {e}')

    # 2. 计算数量: 名义 = MARGIN × LEVERAGE = 100U
    price = adt.get_symbol_price(SYMBOL)
    if not price or price <= 0:
        log(f'❌ 获取价格失败: {price}')
        return
    notional = MARGIN * LEVERAGE
    qty = notional / price
    # 按数量精度取整 (quantityPrecision, 币安合约数量多为整数) — 8/12 修复
    info = adt.get_exchange_info() if hasattr(adt, 'get_exchange_info') else None
    qty_prec = 0
    try:
        qinfo = json.load(open('/home/myuser/.local/share/auto_trade/binance_futures_info.json'))
        syms = qinfo.get('symbols', qinfo) if isinstance(qinfo, dict) else qinfo
        for s in syms:
            if s.get('symbol') == SYMBOL:
                qty_prec = int(s.get('quantityPrecision', 0))
                break
    except Exception:
        pass
    qty = round(qty, qty_prec)
    if qty_prec == 0:
        qty = int(qty)  # 整数步进, 必须转 int 避免 125722.0 浮点残留
    log(f'BOMEUSDT 价格={price}, 名义={notional}U, 数量={qty}')

    # 3. 市价开空
    ok, result, filled = adt.place_market_order_with_retry(SYMBOL, 'SELL', qty, reduce_only=False, max_retries=3)
    if not ok or filled <= 0:
        log(f'❌ 开仓失败: {result}')
        return
    log(f'✅ 开仓成功: SHORT {filled} 枚, orderId={result.get("orderId")}')

    # 4. 获取实际成交价
    time.sleep(1)
    try:
        pos = adt.signed_request('GET', '/fapi/v2/positionRisk', {'symbol': SYMBOL})
        if isinstance(pos, list) and pos:
            entry = float(pos[0].get('entryPrice', 0) or 0)
            if entry > 0:
                price = entry
    except Exception:
        pass

    # 5. 挂 SL10 止损 (SHORT: 价格涨 10% 触发 BUY 平仓)
    tick = adt.get_tick_size(SYMBOL)
    decimals = len(str(tick).split('.')[-1].rstrip('0')) if tick and '.' in str(tick) else 4
    sl_price = round(price * (1 + SL_PCT / 100), decimals)
    sl_ok, sl_order = adt.place_and_verify_algo_order(
        adt.place_stop_loss_order, SYMBOL, 'BUY', sl_price, max_attempts=3)
    if sl_ok:
        log(f'✅ SL10 止损已挂: {sl_price} (orderId={sl_order.get("orderId") if isinstance(sl_order, dict) else sl_order})')
    else:
        log(f'❌ SL10 挂单失败: {sl_order} — 仓位裸奔! 需手动补挂')
    log(f'完成: BOMEUSDT SHORT {filled}枚 @~{price}, SL={sl_price}, 无TP')

if __name__ == '__main__':
    main()
