"""
布林爬坡策略回溯器

功能：
1. BollingerClimbBacktest: 布林爬坡策略回溯
2. BollingerCandidateBacktest: 候选蓄力策略回溯
3. 支持滑动窗口检测历史信号
4. 详细记录失败原因
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.backtest_base import BacktestBase, BacktestSignal, BacktestResult, FailedCheck
from utils.timezone_utils import TimezoneUtils
from utils.logger import get_logger


BOLLINGER_CLIMB_CONFIG = {
    "period": 20,  # 与 bb_climb.py 的 detect_bb_climb 默认值保持一致
    "std_mult": 2.5,
    "upper_tolerance_pct": 0.08,
    "buy_ratio_threshold": 0.55,
    "buy_ratio_skip_default": True,
    "volume_ratio": 1.2,
    "hl_tolerance_window": 3,
    "hl_tolerance_min": 2,  # 与 bb_climb.py 对齐
    "atr_period": 14,
    "atr_enabled": True,
    "exclude_symbols": {
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT',
        'TSLAUSDT', 'NVDAUSDT', 'AMZNUSDT', 'GOOGLUSDT', 'AAPLUSDT',
        'COINUSDT', 'MSTRUSDT', 'METAUSDT', 'TSMUSDT',
        'XAUUSDT', 'XAGUSDT', 'XAUTUSDT', 'NATGASUSDT',
        'USDCUSDT', 'RLUSDUSDT', 'UUSDT', 'XUSDUSDT', 'USD1USDT',
        'FDUSDUSDT', 'TUSDUSDT', 'PAXUSDT', 'BUSDUSDT', 'SUSDUSDT',
        'USDEUSDT',
    },
}

CANDIDATE_CONFIG = {
    "candidate_enabled": True,
    "candidate_near_hours": 2,
    "candidate_vol_ratio": 0.5,
}


class BollingerClimbBacktest(BacktestBase):
    """布林爬坡策略回溯器"""

    def __init__(self, strategy_config: Dict[str, Any] = None):
        config = strategy_config or {}
        config['strategy_name'] = 'bollinger_climb'
        super().__init__(config)
        self.bb_config = {**BOLLINGER_CLIMB_CONFIG, **config.get('bb_config', {})}
        self.logger = get_logger('backtest.bollinger_climb')

    def _run_sliding_window_detection(self, historical_data: pd.DataFrame):
        """滑动窗口检测布林爬坡信号"""
        if historical_data is None or len(historical_data) < self.bb_config['period'] + 5:
            self.logger.warning(f"数据不足，需要至少 {self.bb_config['period'] + 5} 条K线")
            return

        df = historical_data.copy()
        df = df.sort_values('timestamp').reset_index(drop=True)

        df['bb_middle'] = df['close'].rolling(window=self.bb_config['period']).mean()
        df['bb_std'] = df['close'].rolling(window=self.bb_config['period']).std()
        df['bb_upper'] = df['bb_middle'] + self.bb_config['std_mult'] * df['bb_std']
        df['bb_lower'] = df['bb_middle'] - self.bb_config['std_mult'] * df['bb_std']

        df['atr'] = self._calculate_atr_series(df)
        df['avg_vol'] = df['quote_volume'].rolling(window=24).mean() if 'quote_volume' in df.columns else df['volume'].rolling(window=24).mean()

        df['hl_higher'] = (df['high'] > df['high'].shift(1)) & (df['low'] > df['low'].shift(1))

        start_idx = max(self.bb_config['period'] + 1, self.bb_config['atr_period'] + 1)

        for i in range(start_idx, len(df)):
            self._check_signal_at_index(df, i)

    def _calculate_atr_series(self, df: pd.DataFrame) -> pd.Series:
        """计算ATR序列"""
        period = self.bb_config['atr_period']
        if len(df) < period + 1:
            return pd.Series([None] * len(df), index=df.index)

        tr = pd.Series(index=df.index, dtype=float)
        tr.iloc[0] = df['high'].iloc[0] - df['low'].iloc[0]

        for i in range(1, len(df)):
            h = df['high'].iloc[i]
            l = df['low'].iloc[i]
            prev_c = df['close'].iloc[i - 1]
            tr.iloc[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

        return tr.rolling(window=period).mean()

    def _check_signal_at_index(self, df: pd.DataFrame, idx: int):
        """检查指定索引位置是否满足布林爬坡条件"""
        row = df.iloc[idx]
        timestamp = row['timestamp']
        price = row['close']
        symbol = self.result.symbol

        if symbol in self.bb_config['exclude_symbols']:
            return

        conditions_met = {}
        conditions_detail = {}
        failed_condition = None

        middle = row['bb_middle']
        upper = row['bb_upper']
        avg_vol = row['avg_vol']
        atr = row['atr']

        if pd.isna(middle) or pd.isna(upper):
            return

        price_above_middle = row['close'] > middle
        conditions_met['price_above_middle'] = price_above_middle
        conditions_detail['price'] = row['close']
        conditions_detail['middle'] = middle

        if not price_above_middle:
            failed_condition = 'price_above_middle'
            self._record_failed_check(timestamp, symbol, failed_condition, row['close'], middle, conditions_detail)
            return

        tolerance = upper * self.bb_config['upper_tolerance_pct']
        price_near_upper = (upper - tolerance) <= row['close'] <= (upper + tolerance)
        conditions_met['price_near_upper'] = price_near_upper
        conditions_detail['upper'] = upper
        conditions_detail['tolerance'] = tolerance

        if not price_near_upper:
            failed_condition = 'price_near_upper'
            self._record_failed_check(timestamp, symbol, failed_condition, row['close'], f"{upper} ± {tolerance:.4f}", conditions_detail)
            return

        volume_col = 'quote_volume' if 'quote_volume' in df.columns else 'volume'
        current_vol = row[volume_col]
        volume_ok = pd.isna(avg_vol) or current_vol >= avg_vol * self.bb_config['volume_ratio']
        conditions_met['volume_ratio'] = volume_ok
        conditions_detail['current_vol'] = current_vol
        conditions_detail['avg_vol'] = avg_vol if not pd.isna(avg_vol) else 0

        if not volume_ok:
            failed_condition = 'volume_ratio'
            self._record_failed_check(timestamp, symbol, failed_condition, current_vol, avg_vol * self.bb_config['volume_ratio'], conditions_detail)
            return

        hl_check = self._check_hl_climb_tolerant(df, idx)
        conditions_met['hl_climb'] = hl_check

        if not hl_check:
            failed_condition = 'hl_climb'
            self._record_failed_check(timestamp, symbol, failed_condition, "HL未抬高", "窗口内至少2根HL抬高", conditions_detail)
            return

        if self.bb_config['atr_enabled'] and not pd.isna(atr):
            current_range = row['high'] - row['low']
            atr_ok = current_range >= atr * 0.5
            conditions_met['atr_filter'] = atr_ok
            conditions_detail['atr'] = atr
            conditions_detail['current_range'] = current_range

            if not atr_ok:
                failed_condition = 'atr_filter'
                self._record_failed_check(timestamp, symbol, failed_condition, current_range, atr * 0.5, conditions_detail)
                return
        else:
            conditions_met['atr_filter'] = True

        consecutive_count = self._count_consecutive_hours(df, idx)

        signal = self._create_signal(
            timestamp=timestamp,
            symbol=symbol,
            price=price,
            signal_type='bollinger_climb',
            data={
                'consecutive_hours': consecutive_count,
                'upper': round(upper, 6),
                'middle': round(middle, 6),
                'lower': round(row['bb_lower'], 6),
                'atr': round(atr, 6) if not pd.isna(atr) else None,
                'avg_volume': round(avg_vol, 2) if not pd.isna(avg_vol) else 0,
            },
            conditions_met=conditions_met,
            conditions_detail=conditions_detail
        )
        self.result.add_signal(signal)

        self._add_timeline_entry(
            timestamp=timestamp,
            event_type='signal_detected',
            data={'signal_type': 'bollinger_climb', 'consecutive_hours': consecutive_count, 'price': price}
        )

    def _check_hl_climb_tolerant(self, df: pd.DataFrame, idx: int) -> bool:
        """检查HL抬高条件（带容忍机制）"""
        window = self.bb_config['hl_tolerance_window']
        min_count = self.bb_config['hl_tolerance_min']

        climb_count = 0
        check_start = max(1, idx - window + 1)

        for i in range(check_start, idx + 1):
            if df['hl_higher'].iloc[i]:
                climb_count += 1

        return climb_count >= min_count

    def _count_consecutive_hours(self, df: pd.DataFrame, start_idx: int) -> int:
        """计算连续满足条件的小时数"""
        count = 1

        for i in range(start_idx - 1, -1, -1):
            row = df.iloc[i]

            if pd.isna(row['bb_middle']) or pd.isna(row['bb_upper']):
                break

            if row['close'] <= row['bb_middle']:
                break

            tolerance = row['bb_upper'] * self.bb_config['upper_tolerance_pct']
            if not ((row['bb_upper'] - tolerance) <= row['close'] <= (row['bb_upper'] + tolerance)):
                break

            if not self._check_hl_climb_tolerant(df, i):
                break

            count += 1

        return count

    def _record_failed_check(self, timestamp, symbol, failed_condition, condition_value, condition_threshold, details):
        """记录失败检查"""
        failed_check = self._create_failed_check(
            timestamp=timestamp,
            symbol=symbol,
            failed_condition=failed_condition,
            condition_value=condition_value,
            condition_threshold=condition_threshold,
            details=details
        )
        self.result.add_failed_check(failed_check)


class BollingerCandidateBacktest(BacktestBase):
    """候选蓄力策略回溯器"""

    def __init__(self, strategy_config: Dict[str, Any] = None):
        config = strategy_config or {}
        config['strategy_name'] = 'bollinger_candidate'
        super().__init__(config)
        self.bb_config = {**BOLLINGER_CLIMB_CONFIG, **config.get('bb_config', {})}
        self.candidate_config = {**CANDIDATE_CONFIG, **config.get('candidate_config', {})}
        self.logger = get_logger('backtest.bollinger_candidate')
        self.climb_backtest = None

    def _run_sliding_window_detection(self, historical_data: pd.DataFrame):
        """检测候选蓄力信号"""
        if historical_data is None or len(historical_data) < self.bb_config['period'] + 5:
            self.logger.warning(f"数据不足")
            return

        df = historical_data.copy()
        df = df.sort_values('timestamp').reset_index(drop=True)

        df['bb_middle'] = df['close'].rolling(window=self.bb_config['period']).mean()
        df['bb_std'] = df['close'].rolling(window=self.bb_config['period']).std()
        df['bb_upper'] = df['bb_middle'] + self.bb_config['std_mult'] * df['bb_std']
        df['bb_lower'] = df['bb_middle'] - self.bb_config['std_mult'] * df['bb_std']

        df['atr'] = self._calculate_atr_series(df)
        volume_col = 'quote_volume' if 'quote_volume' in df.columns else 'volume'
        df['avg_vol'] = df[volume_col].rolling(window=24).mean()

        start_idx = max(self.bb_config['period'] + 1, self.bb_config['atr_period'] + 1)

        for i in range(start_idx, len(df)):
            self._check_candidate_at_index(df, i)

    def _calculate_atr_series(self, df: pd.DataFrame) -> pd.Series:
        """计算ATR序列"""
        period = self.bb_config['atr_period']
        if len(df) < period + 1:
            return pd.Series([None] * len(df), index=df.index)

        tr = pd.Series(index=df.index, dtype=float)
        tr.iloc[0] = df['high'].iloc[0] - df['low'].iloc[0]

        for i in range(1, len(df)):
            h = df['high'].iloc[i]
            l = df['low'].iloc[i]
            prev_c = df['close'].iloc[i - 1]
            tr.iloc[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

        return tr.rolling(window=period).mean()

    def _check_candidate_at_index(self, df: pd.DataFrame, idx: int):
        """检查候选蓄力信号"""
        row = df.iloc[idx]
        timestamp = row['timestamp']
        price = row['close']
        symbol = self.result.symbol

        if symbol in self.bb_config['exclude_symbols']:
            return

        middle = row['bb_middle']
        upper = row['bb_upper']
        avg_vol = row['avg_vol']

        if pd.isna(middle) or pd.isna(upper):
            return

        is_climb_signal = self._is_climb_signal_at(df, idx)
        if is_climb_signal:
            return

        near_hours = self.candidate_config['candidate_near_hours']
        near_count = 0

        for i in range(idx, -1, -1):
            r = df.iloc[i]
            if pd.isna(r['bb_middle']) or pd.isna(r['bb_upper']):
                break

            if r['close'] <= r['bb_middle']:
                break

            tolerance = r['bb_upper'] * self.bb_config['upper_tolerance_pct']
            if not ((r['bb_upper'] - tolerance) <= r['close'] <= (r['bb_upper'] + tolerance)):
                break

            near_count += 1

        if near_count < near_hours:
            return

        volume_col = 'quote_volume' if 'quote_volume' in df.columns else 'volume'
        current_vol = row[volume_col]
        if not pd.isna(avg_vol) and current_vol < avg_vol * self.candidate_config['candidate_vol_ratio']:
            return

        has_hl_climb = False
        for i in range(max(0, idx - near_count), idx + 1):
            if i == 0:
                has_hl_climb = True
                break
            if df['high'].iloc[i] > df['high'].iloc[i - 1] and df['low'].iloc[i] > df['low'].iloc[i - 1]:
                has_hl_climb = True
                break

        if not has_hl_climb:
            return

        conditions_met = {
            'price_above_middle': True,
            'price_near_upper': True,
            'near_hours': near_count >= near_hours,
            'volume_ok': True,
            'has_hl_climb_history': has_hl_climb,
        }

        conditions_detail = {
            'near_count': near_count,
            'upper': upper,
            'middle': middle,
            'current_vol': current_vol,
            'avg_vol': avg_vol if not pd.isna(avg_vol) else 0,
        }

        signal = self._create_signal(
            timestamp=timestamp,
            symbol=symbol,
            price=price,
            signal_type='bollinger_candidate',
            data={
                'consecutive_hours': near_count,
                'upper': round(upper, 6),
                'middle': round(middle, 6),
                'lower': round(row['bb_lower'], 6),
                'avg_volume': round(avg_vol, 2) if not pd.isna(avg_vol) else 0,
            },
            conditions_met=conditions_met,
            conditions_detail=conditions_detail
        )
        self.result.add_signal(signal)

        self._add_timeline_entry(
            timestamp=timestamp,
            event_type='candidate_detected',
            data={'signal_type': 'bollinger_candidate', 'near_hours': near_count, 'price': price}
        )

    def _is_climb_signal_at(self, df: pd.DataFrame, idx: int) -> bool:
        """检查指定位置是否是布林爬坡信号"""
        row = df.iloc[idx]
        middle = row['bb_middle']
        upper = row['bb_upper']

        if pd.isna(middle) or pd.isna(upper):
            return False

        if row['close'] <= middle:
            return False

        tolerance = upper * self.bb_config['upper_tolerance_pct']
        if not ((upper - tolerance) <= row['close'] <= (upper + tolerance)):
            return False

        window = self.bb_config['hl_tolerance_window']
        min_count = self.bb_config['hl_tolerance_min']
        climb_count = 0
        check_start = max(0, idx - window + 1)

        for i in range(check_start, idx + 1):
            if i == 0:
                climb_count += 1
                continue
            if df['high'].iloc[i] > df['high'].iloc[i - 1] and df['low'].iloc[i] > df['low'].iloc[i - 1]:
                climb_count += 1

        return climb_count >= min_count


def run_bollinger_climb_backtest(
    symbol: str,
    start_time: Union[datetime, str],
    end_time: Union[datetime, str] = None,
    config: Dict[str, Any] = None
) -> Dict[str, Any]:
    """运行布林爬坡回溯测试的便捷函数"""
    backtest = BollingerClimbBacktest(config)
    result = backtest.run_backtest(
        symbol=symbol,
        start_time=start_time,
        end_time=end_time,
        timeframe='1h'
    )
    return result.to_dict()


def run_bollinger_candidate_backtest(
    symbol: str,
    start_time: Union[datetime, str],
    end_time: Union[datetime, str] = None,
    config: Dict[str, Any] = None
) -> Dict[str, Any]:
    """运行候选蓄力回溯测试的便捷函数"""
    backtest = BollingerCandidateBacktest(config)
    result = backtest.run_backtest(
        symbol=symbol,
        start_time=start_time,
        end_time=end_time,
        timeframe='1h'
    )
    return result.to_dict()


def debug_check_at_timestamp(
    symbol: str,
    timestamp: Union[datetime, str],
    strategy: str = 'bollinger_climb',
    config: Dict[str, Any] = None,
    hourly_kline_cache: Dict[str, List] = None
) -> Dict[str, Any]:
    """
    调试指定时间点的信号条件
    
    检查所有条件，返回完整结果（不只是第一个失败的）
    
    Args:
        symbol: 币种名称
        timestamp: 目标时间（北京时间字符串）
        strategy: 策略类型 'bollinger_climb' 或 'bollinger_candidate'
        config: 可选配置
        hourly_kline_cache: 内存中的1h K线缓存（优先使用）
    
    Returns:
        包含所有条件检查结果的字典
    """
    from core.data_loader import DataLoader
    
    bb_config = {**BOLLINGER_CLIMB_CONFIG, **(config.get('bb_config', {}) if config else {})}
    required_klines = bb_config['period'] + 5
    
    if isinstance(timestamp, str):
        target_time = TimezoneUtils.parse_beijing_time_to_utc(timestamp)
    else:
        target_time = TimezoneUtils.beijing_to_utc(timestamp)
    
    start_time = target_time - timedelta(hours=30)
    end_time = target_time + timedelta(hours=1)
    
    df = None
    data_source = 'unknown'
    
    if hourly_kline_cache and symbol in hourly_kline_cache:
        klines = hourly_kline_cache[symbol]
        if len(klines) >= required_klines:
            df = pd.DataFrame(klines)
            if 't' in df.columns:
                df = df.rename(columns={'t': 'timestamp'})
            if 'o' in df.columns:
                df = df.rename(columns={'o': 'open'})
            if 'h' in df.columns:
                df = df.rename(columns={'h': 'high'})
            if 'l' in df.columns:
                df = df.rename(columns={'l': 'low'})
            if 'c' in df.columns:
                df = df.rename(columns={'c': 'close'})
            if 'v' in df.columns:
                df = df.rename(columns={'v': 'volume'})
            if 'q' in df.columns:
                df = df.rename(columns={'q': 'quote_volume'})
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            data_source = 'memory'
    
    if df is None:
        df = DataLoader.get_symbol_historical_data(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            timeframe='1h',
            use_cache=True
        )
        data_source = 'cos'
    
    if df is None or len(df) < required_klines:
        available = len(df) if df is not None else 0
        return {
            'symbol': symbol,
            'timestamp': timestamp,
            'error': f'数据不足',
            'data_info': {
                'available_klines': available,
                'required_klines': required_klines,
                'data_source': data_source
            },
            'suggestion': f'该币种当前只有 {available} 条K线，需要至少 {required_klines} 条。可能是新上市币种或数据不完整。'
        }
    
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    df['bb_middle'] = df['close'].rolling(window=bb_config['period']).mean()
    df['bb_std'] = df['close'].rolling(window=bb_config['period']).std()
    df['bb_upper'] = df['bb_middle'] + bb_config['std_mult'] * df['bb_std']
    df['bb_lower'] = df['bb_middle'] - bb_config['std_mult'] * df['bb_std']
    
    atr_series = _calculate_atr_for_debug(df, bb_config['atr_period'])
    df['atr'] = atr_series
    
    volume_col = 'quote_volume' if 'quote_volume' in df.columns else 'volume'
    df['avg_vol'] = df[volume_col].rolling(window=24).mean()
    
    target_ts = target_time.timestamp() if hasattr(target_time, 'timestamp') else target_time
    if hasattr(df['timestamp'].iloc[0], 'timestamp'):
        time_diffs = (df['timestamp'].apply(lambda x: x.timestamp() if hasattr(x, 'timestamp') else x) - target_ts).abs()
    else:
        time_diffs = (df['timestamp'] - target_ts * 1000).abs()
    
    idx = time_diffs.idxmin()
    
    if idx < bb_config['period'] + 1:
        min_time = df['timestamp'].iloc[bb_config['period']].strftime('%Y-%m-%d %H:%M:%S') if len(df) > bb_config['period'] else '未知'
        return {
            'symbol': symbol,
            'timestamp': timestamp,
            'error': '目标时间太早，无法计算布林带',
            'data_info': {
                'target_index': int(idx),
                'required_index': bb_config['period'] + 1,
                'total_klines': len(df),
                'data_source': data_source,
                'earliest_valid_time': min_time
            },
            'suggestion': f'目标时间对应的数据索引为 {idx}，需要至少 {bb_config["period"] + 1} 条历史数据才能计算布林带。最早可调试时间为 {min_time}。'
        }
    
    row = df.iloc[idx]
    price = row['close']
    middle = row['bb_middle']
    upper = row['bb_upper']
    lower = row['bb_lower']
    avg_vol = row['avg_vol']
    atr = row['atr']
    
    conditions = {}
    
    price_above_middle = bool(row['close'] > middle)
    conditions['price_above_middle'] = {
        'passed': price_above_middle,
        'actual': round(row['close'], 6),
        'threshold': round(middle, 6),
        'description': f"价格({row['close']:.4f}) {'>' if price_above_middle else '≤'} 中轨({middle:.4f})"
    }
    
    tolerance = upper * bb_config['upper_tolerance_pct']
    price_near_upper = bool((upper - tolerance) <= row['close'] <= (upper + tolerance))
    conditions['price_near_upper'] = {
        'passed': price_near_upper,
        'actual': round(row['close'], 6),
        'threshold': f"{upper:.4f} ± {tolerance:.4f}",
        'range': f"[{(upper - tolerance):.4f}, {(upper + tolerance):.4f}]",
        'description': f"价格({row['close']:.4f}) {'在' if price_near_upper else '不在'}上轨±{bb_config['upper_tolerance_pct']*100}%范围内"
    }
    
    current_vol = row[volume_col]
    volume_threshold = avg_vol * bb_config['volume_ratio'] if not pd.isna(avg_vol) else 0
    volume_ok = bool(pd.isna(avg_vol) or current_vol >= volume_threshold)
    conditions['volume_ratio'] = {
        'passed': volume_ok,
        'actual': round(current_vol, 2),
        'threshold': round(volume_threshold, 2),
        'description': f"量能({current_vol/1e6:.2f}M) {'≥' if volume_ok else '<'} 1.2倍均量({volume_threshold/1e6:.2f}M)"
    }
    
    hl_result = _check_hl_climb_for_debug(df, idx, bb_config)
    conditions['hl_climb'] = {
        'passed': bool(hl_result['passed']),
        'actual': hl_result['actual'],
        'threshold': hl_result['threshold'],
        'description': hl_result['description']
    }
    
    if bb_config['atr_enabled'] and not pd.isna(atr):
        current_range = row['high'] - row['low']
        atr_threshold = atr * 0.5
        atr_ok = bool(current_range >= atr_threshold)
        conditions['atr_filter'] = {
            'passed': atr_ok,
            'actual': round(current_range, 6),
            'threshold': round(atr_threshold, 6),
            'description': f"振幅({current_range:.4f}) {'≥' if atr_ok else '<'} ATR*0.5({atr_threshold:.4f})"
        }
    else:
        conditions['atr_filter'] = {
            'passed': True,
            'actual': None,
            'threshold': None,
            'description': 'ATR过滤未启用或数据不足'
        }
    
    is_signal = all(c['passed'] for c in conditions.values())
    
    first_failed = None
    for cond_name, cond_result in conditions.items():
        if not cond_result['passed']:
            first_failed = cond_name
            break
    
    if is_signal:
        consecutive_count = _count_consecutive_for_debug(df, idx, bb_config)
        summary = f"✅ 满足所有条件，连续{consecutive_count}小时"
    else:
        failed_desc = conditions[first_failed]['description']
        summary = f"❌ {first_failed}: {failed_desc}"
    
    return {
        'symbol': symbol,
        'timestamp': timestamp,
        'price': round(price, 6),
        'is_signal': is_signal,
        'conditions': conditions,
        'first_failed': first_failed,
        'summary': summary,
        'bb_data': {
            'upper': round(upper, 6),
            'middle': round(middle, 6),
            'lower': round(lower, 6),
            'atr': round(atr, 6) if not pd.isna(atr) else None,
        }
    }


def _calculate_atr_for_debug(df: pd.DataFrame, period: int) -> pd.Series:
    """为调试计算ATR序列"""
    if len(df) < period + 1:
        return pd.Series([None] * len(df), index=df.index)
    
    tr = pd.Series(index=df.index, dtype=float)
    tr.iloc[0] = df['high'].iloc[0] - df['low'].iloc[0]
    
    for i in range(1, len(df)):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        prev_c = df['close'].iloc[i - 1]
        tr.iloc[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))
    
    return tr.rolling(window=period).mean()


def _check_hl_climb_for_debug(df: pd.DataFrame, idx: int, bb_config: Dict) -> Dict:
    """为调试检查HL抬高条件"""
    window = bb_config['hl_tolerance_window']
    min_count = bb_config['hl_tolerance_min']
    
    climb_count = 0
    hl_details = []
    check_start = max(0, idx - window + 1)
    
    for i in range(check_start, idx + 1):
        if i == 0:
            climb_count += 1
            hl_details.append({'idx': i, 'hl_higher': True, 'reason': '第一根K线默认算'})
            continue
        
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        prev_h = df['high'].iloc[i - 1]
        prev_l = df['low'].iloc[i - 1]
        
        is_higher = bool(h > prev_h and l > prev_l)
        if is_higher:
            climb_count += 1
        hl_details.append({
            'idx': i,
            'hl_higher': is_higher,
            'h': round(h, 6),
            'l': round(l, 6),
            'prev_h': round(prev_h, 6),
            'prev_l': round(prev_l, 6)
        })
    
    passed = bool(climb_count >= min_count)
    
    return {
        'passed': passed,
        'actual': f"{climb_count}/{window}根HL抬高",
        'threshold': f"至少{min_count}根",
        'description': f"窗口内{climb_count}根K线HL抬高，{'≥' if passed else '<'} 阈值{min_count}",
        'details': hl_details
    }


def _count_consecutive_for_debug(df: pd.DataFrame, start_idx: int, bb_config: Dict) -> int:
    """计算连续满足条件的小时数"""
    count = 1
    
    for i in range(start_idx - 1, -1, -1):
        row = df.iloc[i]
        
        if pd.isna(row['bb_middle']) or pd.isna(row['bb_upper']):
            break
        
        if row['close'] <= row['bb_middle']:
            break
        
        tolerance = row['bb_upper'] * bb_config['upper_tolerance_pct']
        if not ((row['bb_upper'] - tolerance) <= row['close'] <= (row['bb_upper'] + tolerance)):
            break
        
        window = bb_config['hl_tolerance_window']
        min_count = bb_config['hl_tolerance_min']
        climb_count = 0
        check_start = max(0, i - window + 1)
        
        for j in range(check_start, i + 1):
            if j == 0:
                climb_count += 1
                continue
            if df['high'].iloc[j] > df['high'].iloc[j - 1] and df['low'].iloc[j] > df['low'].iloc[j - 1]:
                climb_count += 1
        
        if climb_count < min_count:
            break
        
        count += 1
    
    return count


if __name__ == '__main__':
    print("=== 布林爬坡策略回溯测试 ===")

    config = {
        'bb_config': {
            'period': 20,
            'std_mult': 2,
            'upper_tolerance_pct': 0.08,
        }
    }

    result = run_bollinger_climb_backtest(
        symbol='HIGHUSDT',
        start_time='2026-04-17 00:00:00',
        end_time='2026-04-19 00:00:00',
        config=config
    )

    print(f"策略: {result['strategy_name']}")
    print(f"币种: {result['symbol']}")
    print(f"时间范围: {result['start_time']} 到 {result['end_time']}")
    print(f"总信号数: {result['summary']['total_signals']}")
    print(f"失败检查数: {result['summary']['total_failed_checks']}")

    if result['signals']:
        print(f"\n信号列表:")
        for i, sig in enumerate(result['signals'][:5]):
            print(f"  {i+1}. {sig['timestamp']}, 价格: {sig['price']:.4f}, 连续: {sig['data'].get('consecutive_hours', 0)}小时")
