#!/usr/bin/env python3
"""
strict48 labeler (Python 原型, Earth 2.0 strict48-v1 规则卡的逐条实现)

用途:
  1) 在没有 GPU 服务器的本机完成标签口径固化与单元测试;
  2) 待 GPU 服务器开机后, 与其 Rust 标签输出做逐条对照;
  3) 为 MAE 模型实验提供单一真值标签器 (禁止实验脚本各自复制标签逻辑)。

口径 (与 Earth-2.0 strict48-v1-rules.md 对齐):
  entry_ts   : 配置传入; v1 主口径 = UTC 00:02 (北京 08:02)
  expiry_ts  : entry_ts + 172800 秒
  窗口       : [entry_ts, expiry_ts) 半开区间; 到期时刻之后的 bar 不影响结果
  LONG       : SL 若 low  <= entry*(1-SL); TP 若 high >= entry*(1+TP)
  SHORT      : SL 若 high >= entry*(1+SL); TP 若 low  <= entry*(1-TP)
  同分钟双触发: SL_FIRST (保守主口径), ambiguous_same_bar=True
  TIMEOUT    : 未触发时, exit = expiry_ts 时刻可成交价 (bar at expiry_ts 的 open,
               缺该 bar 时用窗口最后一根 close 作为代理并标记 INCOMPLETE_WINDOW)
  费用       : taker 0.05%*2; 入场滑点 0.02%; 平仓滑点 0.02%; 止损滑点 0.05%
  资金费     : 可选传入 [(ts, rate), ...], 只计入持仓期间发生的结算点
  trade_win  : net_pnl > 0; net_pnl == 0 算输

数据状态:
  VALID / MISSING_ENTRY / NO_BARS / INCOMPLETE_WINDOW
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class LabelerConfig:
    tp_pct: float = 10.0
    sl_pct: float = 5.0
    horizon_sec: int = 172800
    taker_fee: float = 0.0005
    slippage_entry: float = 0.0002
    slippage_exit: float = 0.0002
    slippage_stop: float = 0.0005
    funding: Tuple[Tuple[int, float], ...] = ()  # ((timestamp_sec, rate), ...)


def _tolerance(a: float, b: float) -> float:
    return 1e-12 * max(1.0, abs(a), abs(b))


def _le(a: float, b: float) -> bool:
    return a <= b + _tolerance(a, b)


def _ge(a: float, b: float) -> bool:
    return a >= b - _tolerance(a, b)


def _first_bar_at_or_after(bars: Sequence[dict], ts: int) -> Optional[int]:
    """bars 已按 t 升序; 返回第一个 t >= ts 的下标."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid]['t'] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(bars) else None


def _window_gap_ts(bars: Sequence[dict], entry_ts: int, expiry_ts: int) -> Optional[int]:
    """扫描 [entry_ts, expiry_ts) 内的缺口; 返回第一处缺口结束时间戳, 无缺口返回 None."""
    expected = entry_ts
    for b in bars:
        t = b['t']
        if t < entry_ts:
            continue
        if t >= expiry_ts:
            break
        if t > expected:
            return t
        expected = t + 60
    if expected < expiry_ts:
        return expected
    return None


def _funding_cost(entry_ts: int, exit_ts: int, direction: str, cfg: LabelerConfig) -> float:
    """持仓期间资金费对 net_pnl 的百分比贡献; LONG 付, SHORT 收."""
    cost = 0.0
    for ts, rate in cfg.funding:
        if entry_ts < ts < exit_ts:
            cost += -rate if direction == 'LONG' else rate
    return cost * 100.0


def label_trade(
    bars: Sequence[dict],
    symbol: str,
    entry_ts: int,
    direction: str,
    cfg: LabelerConfig = LabelerConfig(),
) -> dict:
    """对单一 (symbol, direction, entry_ts) 生成严格 48h 标签.

    bars 元素: {'t': open_time_sec, 'o': open, 'h': high, 'l': low, 'c': close}
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        raise ValueError(f'direction 必须是 LONG/SHORT, got {direction}')

    expiry_ts = entry_ts + cfg.horizon_sec
    sl = cfg.sl_pct / 100.0
    tp = cfg.tp_pct / 100.0

    idx = _first_bar_at_or_after(bars, entry_ts)
    if idx is None:
        return {
            'symbol': symbol, 'direction': direction, 'entry_ts': entry_ts,
            'expiry_ts': expiry_ts, 'data_status': 'NO_BARS',
        }
    if bars[idx]['t'] > entry_ts + 120:
        return {
            'symbol': symbol, 'direction': direction, 'entry_ts': entry_ts,
            'expiry_ts': expiry_ts, 'data_status': 'MISSING_ENTRY',
        }

    entry_price = float(bars[idx]['o'])
    first_event = None       # 0=SL_FIRST, 1=TP_FIRST, 2=TIMEOUT
    event_ts = None
    ambiguous = False
    gap_ts = None

    lows: List[float] = []
    highs: List[float] = []

    for b in bars[idx:]:
        t = b['t']
        if t < entry_ts:
            continue
        if t >= expiry_ts:
            break
        lows.append(float(b['l']))
        highs.append(float(b['h']))

        sl_hit = _le(b['l'], entry_price * (1 - sl)) if direction == 'LONG' else _ge(b['h'], entry_price * (1 + sl))
        tp_hit = _ge(b['h'], entry_price * (1 + tp)) if direction == 'LONG' else _le(b['l'], entry_price * (1 - tp))

        # Rust 单一真值: 首个事件只决定 first_event; 后续 bar 仍用于 MAE/MFE 全窗口扫描
        if first_event is None:
            if sl_hit and tp_hit:
                first_event = 0
                event_ts = t
                ambiguous = True
            elif sl_hit:
                first_event = 0
                event_ts = t
            elif tp_hit:
                first_event = 1
                event_ts = t

    # 未触发 => TIMEOUT (first_event=2), Rust 单一真值: event_ts=0
    if first_event is None:
        first_event = 2
        event_ts = 0

    # 窗口完整性: Rust 单一真值只检查 TIMEOUT 时到期 bar 是否缺失/窗口不足;
    # 事件已触发则 VALID, 同分钟双触发为 AMBIGUOUS.
    timeout_incomplete = False
    data_status = 'VALID'
    if ambiguous:
        data_status = 'AMBIGUOUS'

    # MAE / MFE 只使用 [entry_ts, expiry_ts) 切片
    if direction == 'LONG':
        mae_pct = max(0.0, (entry_price - min(lows)) / entry_price * 100.0) if lows else None
        mfe_pct = max(0.0, (max(highs) - entry_price) / entry_price * 100.0) if highs else None
    else:
        mae_pct = max(0.0, (max(highs) - entry_price) / entry_price * 100.0) if highs else None
        mfe_pct = max(0.0, (entry_price - min(lows)) / entry_price * 100.0) if lows else None

    # 结算
    if first_event == 0:
        gross_pct = -cfg.sl_pct
        exit_price = entry_price * (1 - sl) if direction == 'LONG' else entry_price * (1 + sl)
        exit_slip = cfg.slippage_stop
        trigger = 'SL_FIRST'
    elif first_event == 1:
        gross_pct = cfg.tp_pct
        exit_price = entry_price * (1 + tp) if direction == 'LONG' else entry_price * (1 - tp)
        exit_slip = cfg.slippage_exit
        trigger = 'TP_FIRST'
    elif first_event == 2:
        trigger = 'TIMEOUT'
        # expiry 时刻可成交价: bar at expiry_ts 的 open; 缺 bar 用窗口最后一根 close 代理
        exp_idx = _first_bar_at_or_after(bars, expiry_ts)
        if exp_idx is not None and bars[exp_idx]['t'] == expiry_ts:
            exit_price = float(bars[exp_idx]['o'])
        elif lows:
            last_idx = idx + len(lows) - 1
            if bars[last_idx]['t'] < expiry_ts - 60:
                timeout_incomplete = True
            exit_price = float(bars[last_idx]['c'])
        else:
            exit_price = None
        exit_slip = cfg.slippage_exit
        if exit_price is not None:
            gross_pct = ((exit_price / entry_price) - 1.0) * 100.0 if direction == 'LONG' else ((1.0 - exit_price / entry_price) * 100.0)

    if first_event == 2 and timeout_incomplete:
        data_status = 'INCOMPLETE_WINDOW'

    if exit_price is None:
        net_pct = None
        trade_win = None
        minutes_to_event = None
    else:
        funding_pct = _funding_cost(entry_ts, expiry_ts, direction, cfg)
        cost_pct = (cfg.taker_fee * 2 + cfg.slippage_entry + exit_slip) * 100.0
        net_pct = gross_pct - cost_pct + funding_pct
        trade_win = net_pct > 0
        minutes_to_event = (event_ts - entry_ts) // 60 if first_event in (0, 1) else None

    result = {
        'symbol': symbol,
        'direction': direction,
        'entry_ts': entry_ts,
        'expiry_ts': expiry_ts,
        'entry_price': entry_price,
        'data_status': data_status,
        'first_event': first_event,
        'trigger': trigger,
        'ambiguous_same_bar': ambiguous,
        'gross_pnl_pct': gross_pct if first_event is not None else None,
        'net_pnl_pct': net_pct,
        'trade_win': trade_win,
        'mae_pct': mae_pct,
        'mfe_pct': mfe_pct,
        'minutes_to_event': minutes_to_event,
        'event_ts': event_ts,
        'exit_price': exit_price,
        'price_source': 'CONTRACT_PROXY',
    }
    return result


def label_many(
    bars_by_symbol: Iterable[Tuple[str, Sequence[dict]]],
    entry_ts: int,
    direction: str,
    cfg: LabelerConfig = LabelerConfig(),
) -> List[dict]:
    return [label_trade(bars, sym, entry_ts, direction, cfg) for sym, bars in bars_by_symbol]


def mae_class(record: dict, threshold_pct: float = 5.0) -> Optional[int]:
    """MAE 风险标签: y=1 if MAE > SL 阈值; 无 MAE 返回 None."""
    if record.get('mae_pct') is None:
        return None
    return 1 if record['mae_pct'] > threshold_pct else 0


if __name__ == '__main__':
    import json, sys
    print('strict48 labeler 原型: 直接运行单元测试请执行 test_strict48_labeler.py')
