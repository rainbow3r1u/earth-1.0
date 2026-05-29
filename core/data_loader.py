"""
数据加载器 - 带缓存的COS数据读取，支持历史数据获取
"""
import io
import time
import threading
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Tuple
from qcloud_cos import CosConfig, CosS3Client

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from configs import config
from utils.logger import get_logger
from utils.timezone_utils import TimezoneUtils

logger = get_logger('data_loader')


class DataLoader:
    _cache: Optional[pd.DataFrame] = None
    _cache_time: Optional[float] = None
    _cache_minutes: int = config.DATA_CACHE_MINUTES

    _cos_client: Optional[CosS3Client] = None
    _cos_lock = threading.Lock()

    @classmethod
    def _get_cos_client(cls) -> CosS3Client:
        if cls._cos_client is None:
            with cls._cos_lock:
                if cls._cos_client is None:
                    cos_config = CosConfig(
                        Region=config.COS_REGION,
                        SecretId=config.COS_SECRET_ID,
                        SecretKey=config.COS_SECRET_KEY,
                        Endpoint=config.COS_ENDPOINT
                    )
                    cls._cos_client = CosS3Client(cos_config)
        return cls._cos_client

    @classmethod
    def _fetch_from_cos(cls) -> pd.DataFrame:
        logger.info("从COS读取数据...")
        client = cls._get_cos_client()

        for attempt in range(config.MAX_RETRIES):
            try:
                resp = client.get_object(Bucket=config.COS_BUCKET, Key=config.COS_KEY)
                data = resp['Body'].get_raw_stream().read()
                df = pd.read_parquet(io.BytesIO(data))

                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    before_nan = df[col].isna().sum()
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    after_nan = df[col].isna().sum()
                    new_nan = after_nan - before_nan
                    if new_nan > 0:
                        logger.warning(f"列 '{col}' 中有 {new_nan} 行无效数值被强制转为NaN")

                df = df.dropna(subset=['symbol', 'close', 'volume'])
                df['quote_volume'] = df['close'] * df['volume']

                logger.info(f"读取完成: {len(df)} 条K线, {df['symbol'].nunique()} 个币种")
                return df

            except Exception as e:
                logger.warning(f"读取失败 (尝试 {attempt+1}/{config.MAX_RETRIES}): {e}")
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(config.RETRY_DELAY_SECONDS[attempt])
                else:
                    logger.error(f"COS读取最终失败: {e}")
                    raise

    @classmethod
    def get_klines(cls, use_cache: bool = True) -> pd.DataFrame:
        if use_cache and cls._cache is not None and cls._cache_time is not None:
            elapsed = (time.time() - cls._cache_time) / 60
            if elapsed < cls._cache_minutes:
                logger.debug(f"使用缓存数据 (已缓存 {elapsed:.1f} 分钟)")
                return cls._cache.copy()

        cls._cache = cls._fetch_from_cos()
        cls._cache_time = time.time()
        return cls._cache.copy()

    @classmethod
    def clear_cache(cls):
        cls._cache = None
        cls._cache_time = None
        logger.info("缓存已清除")

    @classmethod
    def get_symbol_data(cls, symbol: str, use_cache: bool = True) -> Optional[pd.DataFrame]:
        df = cls.get_klines(use_cache=use_cache)
        symbol_data = df[df['symbol'] == symbol].copy()
        if len(symbol_data) == 0:
            return None
        return symbol_data.sort_values('timestamp').reset_index(drop=True)

    @classmethod
    def get_top_symbols(cls, n: int = 100, use_cache: bool = True) -> list:
        df = cls.get_klines(use_cache=use_cache)
        vol_24h = df.groupby('symbol')['quote_volume'].sum().sort_values(ascending=False)
        return vol_24h.head(n).index.tolist()

    @classmethod
    def get_historical_klines(cls,
                             start_time: Union[datetime, str],
                             end_time: Optional[Union[datetime, str]] = None,
                             timeframe: str = '1h',
                             use_cache: bool = True) -> pd.DataFrame:
        """
        获取指定时间范围的历史K线数据

        Args:
            start_time: 开始时间（北京时间字符串或datetime对象）
            end_time: 结束时间（北京时间字符串或datetime对象，默认为当前时间）
            timeframe: K线周期，支持 '1h'（默认）、'4h'、'1d' 等
            use_cache: 是否使用缓存

        Returns:
            DataFrame格式的K线数据
        """
        # 解析时间参数
        if isinstance(start_time, str):
            start_dt = TimezoneUtils.parse_beijing_time_to_utc(start_time)
        else:
            start_dt = TimezoneUtils.beijing_to_utc(start_time)

        if end_time is None:
            end_dt = TimezoneUtils.get_utc_now()
        elif isinstance(end_time, str):
            end_dt = TimezoneUtils.parse_beijing_time_to_utc(end_time)
        else:
            end_dt = TimezoneUtils.beijing_to_utc(end_time)

        logger.info(f"获取历史数据: {TimezoneUtils.format_beijing_time(start_dt)} 到 {TimezoneUtils.format_beijing_time(end_dt)}")

        # 获取完整数据
        df = cls.get_klines(use_cache=use_cache)

        # 过滤时间范围
        mask = (df['timestamp'] >= start_dt) & (df['timestamp'] <= end_dt)
        historical_df = df[mask].copy()

        logger.info(f"历史数据过滤结果: {len(historical_df)} 条K线, {historical_df['symbol'].nunique()} 个币种")

        return historical_df

    @classmethod
    def get_symbol_historical_data(cls,
                                  symbol: str,
                                  start_time: Union[datetime, str],
                                  end_time: Optional[Union[datetime, str]] = None,
                                  timeframe: str = '1h',
                                  use_cache: bool = True) -> Optional[pd.DataFrame]:
        """
        获取指定币种的历史K线数据

        Args:
            symbol: 币种名称
            start_time: 开始时间（北京时间字符串或datetime对象）
            end_time: 结束时间（北京时间字符串或datetime对象，默认为当前时间）
            timeframe: K线周期，支持 '1h'（默认）、'4h'、'1d' 等
            use_cache: 是否使用缓存

        Returns:
            指定币种的K线数据，如果没有数据则返回None
        """
        # 获取历史数据
        historical_df = cls.get_historical_klines(
            start_time=start_time,
            end_time=end_time,
            timeframe=timeframe,
            use_cache=use_cache
        )

        # 过滤指定币种
        symbol_data = historical_df[historical_df['symbol'] == symbol].copy()

        if len(symbol_data) == 0:
            logger.warning(f"币种 {symbol} 在指定时间范围内没有数据")
            return None

        # 按时间排序
        symbol_data = symbol_data.sort_values('timestamp').reset_index(drop=True)

        logger.info(f"获取到 {symbol} 的历史数据: {len(symbol_data)} 条K线")
        return symbol_data

    @classmethod
    def get_available_time_range(cls) -> Tuple[datetime, datetime]:
        """
        获取可用数据的时间范围

        Returns:
            (开始时间, 结束时间) 的元组（UTC时间）
        """
        df = cls.get_klines(use_cache=False)
        if df.empty:
            logger.warning("没有可用数据，返回空时间范围")
            return None

        min_time = df['timestamp'].min()
        max_time = df['timestamp'].max()

        return min_time, max_time


def read_cos_data() -> pd.DataFrame:
    return DataLoader.get_klines()
