"""
回溯基础模块 - 提供通用的回溯测试框架

功能：
1. BacktestBase: 回溯测试基类，支持时间窗口滑动检测
2. BacktestResult: 回溯结果数据结构，包含信号、指标、时间线等
3. 失败原因详细记录和分析
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

from utils.timezone_utils import TimezoneUtils
from utils.logger import get_logger


@dataclass
class BacktestSignal:
    """回溯信号数据结构"""
    timestamp: datetime  # 信号时间（UTC）
    symbol: str  # 币种名称
    price: float  # 信号触发时的价格
    signal_type: str  # 信号类型，如 'bollinger_climb', 'candidate' 等
    data: Dict[str, Any]  # 信号详细数据
    conditions_met: Dict[str, bool]  # 各个条件的满足情况
    conditions_detail: Dict[str, Any]  # 各个条件的详细数据

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        conditions_met_serialized = {
            k: bool(v) if isinstance(v, (bool, np.bool_)) else v
            for k, v in self.conditions_met.items()
        }
        return {
            'timestamp': TimezoneUtils.format_beijing_time(self.timestamp),
            'symbol': self.symbol,
            'price': self.price,
            'signal_type': self.signal_type,
            'data': self.data,
            'conditions_met': conditions_met_serialized,
            'conditions_detail': self.conditions_detail
        }


@dataclass
class FailedCheck:
    """失败检查记录"""
    timestamp: datetime  # 检查时间（UTC）
    symbol: str  # 币种名称
    failed_condition: str  # 失败的条件名称
    condition_value: Any  # 条件实际值
    condition_threshold: Any  # 条件阈值
    details: Dict[str, Any]  # 详细失败信息

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            'timestamp': TimezoneUtils.format_beijing_time(self.timestamp),
            'symbol': self.symbol,
            'failed_condition': self.failed_condition,
            'condition_value': self.condition_value,
            'condition_threshold': self.condition_threshold,
            'details': self.details
        }


@dataclass
class BacktestResult:
    """回溯结果数据结构"""
    strategy_name: str  # 策略名称
    symbol: str  # 币种名称
    start_time: datetime  # 开始时间（UTC）
    end_time: datetime  # 结束时间（UTC）
    timeframe: str  # 时间周期

    # 结果数据
    signals: List[BacktestSignal] = field(default_factory=list)  # 检测到的信号
    failed_checks: List[FailedCheck] = field(default_factory=list)  # 失败检查记录
    timeline: List[Dict[str, Any]] = field(default_factory=list)  # 时间线数据

    # 性能指标
    metrics: Dict[str, float] = field(default_factory=dict)

    def add_signal(self, signal: BacktestSignal):
        """添加信号"""
        self.signals.append(signal)

    def add_failed_check(self, failed_check: FailedCheck):
        """添加失败检查记录"""
        self.failed_checks.append(failed_check)

    def add_timeline_entry(self, entry: Dict[str, Any]):
        """添加时间线条目"""
        self.timeline.append(entry)

    def calculate_metrics(self):
        """计算性能指标"""
        if not self.signals:
            self.metrics = {
                'total_signals': 0,
                'signal_frequency': 0,
                'avg_holding_period': 0,
                'win_rate': 0,
                'profit_factor': 0
            }
            return

        # 基本指标
        self.metrics['total_signals'] = len(self.signals)

        # 信号频率（信号数/总小时数）
        total_hours = (self.end_time - self.start_time).total_seconds() / 3600
        self.metrics['signal_frequency'] = len(self.signals) / total_hours if total_hours > 0 else 0

        # 条件满足率统计
        condition_stats = self._calculate_condition_stats()
        self.metrics.update(condition_stats)

        # 失败原因统计
        failure_stats = self._calculate_failure_stats()
        self.metrics.update(failure_stats)

        # 交易绩效指标（需要子类提供trade数据）
        trades = getattr(self, 'trades', [])
        if trades:
            closed = [t for t in trades if t.get('pnl') is not None]
            wins = [t for t in closed if t.get('pnl', 0) > 0]
            self.metrics['win_rate'] = len(wins) / len(closed) * 100 if closed else 0
            gross_profit = sum(t['pnl'] for t in wins)
            gross_loss = abs(sum(t['pnl'] for t in closed if t['pnl'] <= 0))
            self.metrics['profit_factor'] = gross_profit / gross_loss if gross_loss > 0 else 0
            self.metrics['total_trades'] = len(closed)
        else:
            self.metrics['win_rate'] = 0
            self.metrics['profit_factor'] = 0
            self.metrics['total_trades'] = 0

        # avg_holding_period: 从trades数据计算 (每根K线=15分钟)
        if closed:
            holding_minutes = [
                (t.get("k_index", 0) - t.get("entry_k_index", 0)) * 15
                for t in closed
                if t.get("entry_k_index") is not None and t.get("k_index") is not None
            ]
            self.metrics['avg_holding_period'] = (sum(holding_minutes) / len(holding_minutes)) if holding_minutes else 0
        else:
            self.metrics['avg_holding_period'] = 0

    def _calculate_condition_stats(self) -> Dict[str, float]:
        """计算条件满足率统计"""
        if not self.signals:
            return {}

        # 收集所有条件
        all_conditions = set()
        for signal in self.signals:
            all_conditions.update(signal.conditions_met.keys())

        condition_stats = {}
        for condition in all_conditions:
            met_count = sum(1 for signal in self.signals if signal.conditions_met.get(condition, False))
            condition_stats[f'condition_{condition}_rate'] = met_count / len(self.signals)

        return condition_stats

    def _calculate_failure_stats(self) -> Dict[str, float]:
        """计算失败原因统计"""
        if not self.failed_checks:
            return {}

        # 按失败条件分组统计
        failure_counts = {}
        for check in self.failed_checks:
            condition = check.failed_condition
            failure_counts[condition] = failure_counts.get(condition, 0) + 1

        total_failures = len(self.failed_checks)
        failure_stats = {}
        for condition, count in failure_counts.items():
            failure_stats[f'failure_{condition}_rate'] = count / total_failures if total_failures > 0 else 0

        return failure_stats

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        self.calculate_metrics()

        return {
            'strategy_name': self.strategy_name,
            'symbol': self.symbol,
            'start_time': TimezoneUtils.format_beijing_time(self.start_time),
            'end_time': TimezoneUtils.format_beijing_time(self.end_time),
            'timeframe': self.timeframe,
            'signals': [signal.to_dict() for signal in self.signals],
            'failed_checks': [check.to_dict() for check in self.failed_checks],
            'timeline': self.timeline,
            'metrics': self.metrics,
            'summary': {
                'total_signals': len(self.signals),
                'total_failed_checks': len(self.failed_checks),
                'signal_frequency': self.metrics.get('signal_frequency', 0),
                'conditions_met_rate': self._calculate_overall_condition_rate()
            }
        }

    def _calculate_overall_condition_rate(self) -> float:
        """计算总体条件满足率"""
        if not self.signals:
            return 0

        total_conditions = 0
        met_conditions = 0

        for signal in self.signals:
            for condition, met in signal.conditions_met.items():
                total_conditions += 1
                if met:
                    met_conditions += 1

        return met_conditions / total_conditions if total_conditions > 0 else 0


class BacktestBase:
    """回溯测试基类"""

    def __init__(self, strategy_config: Dict[str, Any]):
        """
        初始化回溯测试器

        Args:
            strategy_config: 策略配置字典
        """
        self.config = strategy_config
        self.logger = get_logger('backtest')
        self.result: Optional[BacktestResult] = None

    def run_backtest(self,
                     symbol: str,
                     start_time: Union[datetime, str],
                     end_time: Optional[Union[datetime, str]] = None,
                     timeframe: str = '1h') -> BacktestResult:
        """
        运行回溯测试

        Args:
            symbol: 币种名称
            start_time: 开始时间（北京时间字符串或datetime对象）
            end_time: 结束时间（北京时间字符串或datetime对象，默认为当前时间）
            timeframe: K线周期

        Returns:
            回溯测试结果
        """
        # 解析时间参数
        if isinstance(start_time, str):
            start_dt = TimezoneUtils.parse_beijing_time_to_utc(start_time)
        else:
            start_dt = TimezoneUtils.beijing_to_utc(start_time)

        if end_time is None:
            end_dt = datetime.now(timezone.utc)
        elif isinstance(end_time, str):
            end_dt = TimezoneUtils.parse_beijing_time_to_utc(end_time)
        else:
            end_dt = TimezoneUtils.beijing_to_utc(end_time)

        self.logger.info(f"开始回溯测试: {symbol}")
        self.logger.info(f"时间范围: {TimezoneUtils.format_beijing_time(start_dt)} 到 {TimezoneUtils.format_beijing_time(end_dt)}")
        self.logger.info(f"时间周期: {timeframe}")

        # 初始化结果对象
        self.result = BacktestResult(
            strategy_name=self.config.get('strategy_name', 'unknown'),
            symbol=symbol,
            start_time=start_dt,
            end_time=end_dt,
            timeframe=timeframe
        )

        # 获取历史数据
        from core.data_loader import DataLoader
        historical_data = DataLoader.get_symbol_historical_data(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            timeframe=timeframe,
            use_cache=True
        )

        if historical_data is None:
            self.logger.warning(f"币种 {symbol} 在指定时间范围内没有数据")
            return self.result

        self.logger.info(f"获取到 {len(historical_data)} 条历史K线数据")

        # 运行时间窗口滑动检测
        self._run_sliding_window_detection(historical_data)

        self.logger.info(f"回溯测试完成: 找到 {len(self.result.signals)} 个信号")
        self.logger.info(f"失败检查记录: {len(self.result.failed_checks)} 条")

        return self.result

    def _run_sliding_window_detection(self, historical_data: pd.DataFrame):
        """
        运行时间窗口滑动检测

        子类需要实现具体的检测逻辑
        """
        raise NotImplementedError("子类必须实现 _run_sliding_window_detection 方法")

    def _create_signal(self,
                      timestamp: datetime,
                      symbol: str,
                      price: float,
                      signal_type: str,
                      data: Dict[str, Any],
                      conditions_met: Dict[str, bool],
                      conditions_detail: Dict[str, Any]) -> BacktestSignal:
        """
        创建回溯信号对象

        Args:
            timestamp: 信号时间（UTC）
            symbol: 币种名称
            price: 价格
            signal_type: 信号类型
            data: 信号数据
            conditions_met: 条件满足情况
            conditions_detail: 条件详细数据

        Returns:
            回溯信号对象
        """
        return BacktestSignal(
            timestamp=timestamp,
            symbol=symbol,
            price=price,
            signal_type=signal_type,
            data=data,
            conditions_met=conditions_met,
            conditions_detail=conditions_detail
        )

    def _create_failed_check(self,
                            timestamp: datetime,
                            symbol: str,
                            failed_condition: str,
                            condition_value: Any,
                            condition_threshold: Any,
                            details: Dict[str, Any]) -> FailedCheck:
        """
        创建失败检查记录

        Args:
            timestamp: 检查时间（UTC）
            symbol: 币种名称
            failed_condition: 失败的条件名称
            condition_value: 条件实际值
            condition_threshold: 条件阈值
            details: 详细失败信息

        Returns:
            失败检查记录对象
        """
        return FailedCheck(
            timestamp=timestamp,
            symbol=symbol,
            failed_condition=failed_condition,
            condition_value=condition_value,
            condition_threshold=condition_threshold,
            details=details
        )

    def _add_timeline_entry(self,
                           timestamp: datetime,
                           event_type: str,
                           data: Dict[str, Any]):
        """
        添加时间线条目

        Args:
            timestamp: 时间（UTC）
            event_type: 事件类型
            data: 事件数据
        """
        entry = {
            'timestamp': TimezoneUtils.format_beijing_time(timestamp),
            'event_type': event_type,
            'data': data
        }
        self.result.add_timeline_entry(entry)


class SimpleBacktest(BacktestBase):
    """
    简单回溯测试器示例

    用于演示如何继承BacktestBase并实现具体的检测逻辑
    """

    def __init__(self, strategy_config: Dict[str, Any]):
        super().__init__(strategy_config)
        self.window_size = strategy_config.get('window_size', 24)  # 检测窗口大小（小时）

    def _run_sliding_window_detection(self, historical_data: pd.DataFrame):
        """
        实现简单的时间窗口滑动检测

        示例：检测价格突破简单移动平均线
        """
        # 按时间排序
        historical_data = historical_data.sort_values('timestamp').reset_index(drop=True)

        # 计算简单移动平均线
        historical_data['sma'] = historical_data['close'].rolling(window=self.window_size).mean()

        # 滑动窗口检测
        for i in range(self.window_size, len(historical_data)):
            current_row = historical_data.iloc[i]
            prev_row = historical_data.iloc[i-1]

            timestamp = current_row['timestamp']
            price = current_row['close']
            sma = current_row['sma']
            prev_sma = prev_row['sma']

            # 检查条件：价格突破SMA
            price_above_sma = price > sma
            prev_price_below_sma = prev_row['close'] <= prev_sma

            conditions_met = {
                'price_above_sma': price_above_sma,
                'breakout': price_above_sma and prev_price_below_sma
            }

            conditions_detail = {
                'price': price,
                'sma': sma,
                'prev_price': prev_row['close'],
                'prev_sma': prev_sma
            }

            # 如果满足突破条件，记录信号
            if conditions_met['breakout']:
                signal = self._create_signal(
                    timestamp=timestamp,
                    symbol=self.result.symbol,
                    price=price,
                    signal_type='sma_breakout',
                    data={'window_size': self.window_size},
                    conditions_met=conditions_met,
                    conditions_detail=conditions_detail
                )
                self.result.add_signal(signal)

                # 添加时间线条目
                self._add_timeline_entry(
                    timestamp=timestamp,
                    event_type='signal_detected',
                    data={'signal_type': 'sma_breakout', 'price': price}
                )
            else:
                # 记录失败检查（如果价格在SMA下方）
                if not price_above_sma:
                    failed_check = self._create_failed_check(
                        timestamp=timestamp,
                        symbol=self.result.symbol,
                        failed_condition='price_above_sma',
                        condition_value=price,
                        condition_threshold=sma,
                        details={'price': price, 'sma': sma, 'difference': price - sma}
                    )
                    self.result.add_failed_check(failed_check)


if __name__ == '__main__':
    # 示例用法
    print("=== 回溯基础模块测试 ===")

    # 创建简单回溯测试器
    config = {
        'strategy_name': 'simple_sma_breakout',
        'window_size': 20
    }

    backtester = SimpleBacktest(config)

    # 运行回溯测试
    result = backtester.run_backtest(
        symbol='BTCUSDT',
        start_time='2026-04-18 00:00:00',
        end_time='2026-04-19 00:00:00',
        timeframe='1h'
    )

    # 输出结果
    result_dict = result.to_dict()
    print(f"策略名称: {result_dict['strategy_name']}")
    print(f"币种: {result_dict['symbol']}")
    print(f"时间范围: {result_dict['start_time']} 到 {result_dict['end_time']}")
    print(f"总信号数: {result_dict['summary']['total_signals']}")
    print(f"总失败检查: {result_dict['summary']['total_failed_checks']}")
    print(f"信号频率: {result_dict['summary']['signal_frequency']:.4f} 信号/小时")

    if result_dict['signals']:
        print(f"\n前3个信号:")
        for i, signal in enumerate(result_dict['signals'][:3]):
            print(f"  信号 {i+1}: {signal['timestamp']}, 价格: {signal['price']:.2f}")

    if result_dict['failed_checks']:
        print(f"\n前3个失败检查:")
        for i, check in enumerate(result_dict['failed_checks'][:3]):
            print(f"  失败 {i+1}: {check['failed_condition']}, 值: {check['condition_value']:.2f}, 阈值: {check['condition_threshold']:.2f}")

    print(f"\n性能指标:")
    for key, value in result_dict['metrics'].items():
        if isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")