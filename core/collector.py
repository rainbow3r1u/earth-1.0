"""
币安K线采集器 - 并发版本
直连币安API采集永续合约K线，上传到COS
"""
import io
import time
import pandas as pd
from datetime import datetime, timezone

_BANNED_UNTIL = 0
_BANNED_RETRY_DELAY = 0.4
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosClientError, CosServiceError
from requests.exceptions import RequestException
import requests

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from configs import config
from utils.logger import get_logger

logger = get_logger('collector')

request_lock = threading.Lock()
ban_lock = threading.Lock()
request_count = 0


class BinanceKlineCollector:
    def __init__(self):
        self.api_base = config.BINANCE_API
        self.max_retries = config.MAX_RETRIES
        self.retry_delays = config.RETRY_DELAY_SECONDS
        self.timeout = config.REQUEST_TIMEOUT
        self.concurrent_workers = 3

    def _api_request(self, path: str, params: Dict = None) -> Any:
        global request_count, _BANNED_UNTIL, _BANNED_RETRY_DELAY
        url = f"{self.api_base}{path}"
        for attempt in range(self.max_retries):
            # 熔断检查 -- protected by ban_lock
            with ban_lock:
                banned = time.time() < _BANNED_UNTIL
                ban_delay = _BANNED_RETRY_DELAY
            if banned:
                time.sleep(ban_delay)

            try:
                resp = requests.get(url, params=params, timeout=self.timeout)

                # 418/429熔断判断
                if resp.status_code in (418, 429):
                    wait_sec = int(resp.headers.get("Retry-After", 300))
                    with ban_lock:
                        _BANNED_UNTIL = time.time() + wait_sec
                        _BANNED_RETRY_DELAY = min(_BANNED_RETRY_DELAY * 2, 3600)
                    logger.error(f"触发{resp.status_code}熔断，封锁至 {datetime.fromtimestamp(_BANNED_UNTIL)}，{wait_sec}秒后重试")
                    break

                resp.raise_for_status()
                with request_lock:
                    request_count += 1
                return resp.json()
            except Exception as e:
                logger.warning(f"请求失败 (尝试 {attempt+1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delays[attempt])
                else:
                    logger.error(f"请求最终失败: {url}")
                    return None
        return None

    def get_perpetual_symbols(self) -> List[str]:
        data = self._api_request("/fapi/v1/exchangeInfo")
        if not data:
            logger.error("获取交易对信息失败")
            return []

        symbols = []
        for s in data.get('symbols', []):
            if (s.get('contractType') == 'PERPETUAL'
                    and s.get('quoteAsset') == 'USDT'
                    and s.get('status') == 'TRADING'
                    and s.get('isInverse') is not True):
                symbol = s['symbol']
                if 'UP' not in symbol and 'DOWN' not in symbol:
                    symbols.append(symbol)

        logger.info(f"获取到 {len(symbols)} 个 USDT 永续合约")
        return symbols

    def fetch_klines(self, symbol: str, interval: str = '1h', limit: int = 520) -> List[Dict]:
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        data = self._api_request("/fapi/v1/klines", params=params)
        if not data:
            return []

        rows = []
        for k in data:
            rows.append({
                'symbol': symbol,
                'timestamp': k[0],
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            })
        return rows

    def _fetch_klines_task(self, symbol: str, idx: int, total: int, delay: float = 0.4):
        rows = self.fetch_klines(symbol)
        if idx % 50 == 0 or idx == total:
            logger.info(f"进度: {idx}/{total}")
        with ban_lock:
            ban_delay = _BANNED_RETRY_DELAY
        time.sleep(ban_delay)
        return symbol, rows

    def _upload_to_cos(self, local_file: str, cos_key: str) -> bool:
        cos_config = CosConfig(
            Region=config.COS_REGION,
            SecretId=config.COS_SECRET_ID,
            SecretKey=config.COS_SECRET_KEY,
            Endpoint=config.COS_ENDPOINT
        )
        client = CosS3Client(cos_config)

        for attempt in range(self.max_retries):
            try:
                resp = client.upload_file(
                    Bucket=config.COS_BUCKET,
                    LocalFilePath=local_file,
                    Key=cos_key,
                    EnableMD5=False
                )
                logger.info(f"上传成功，ETag: {resp['ETag']}")
                return True
            except (RequestException, CosClientError, CosServiceError) as e:
                logger.warning(f"上传失败 (尝试 {attempt+1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delays[attempt])
                else:
                    logger.error(f"上传最终失败: {local_file}")
                    return False
            except Exception as e:
                logger.error(f"上传发生未预期异常: {e}", exc_info=True)
                return False
        return False

    def run(self) -> bool:
        global request_count
        start_time = datetime.now(timezone.utc)
        logger.info("=" * 60)
        logger.info("开始采集币安永续合约K线数据")
        logger.info(f"并发数: {self.concurrent_workers}")
        logger.info("=" * 60)

        symbols = self.get_perpetual_symbols()
        if not symbols:
            logger.error("未获取到任何永续合约，退出")
            return False

        total = len(symbols)
        all_rows = []
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=self.concurrent_workers) as executor:
            futures = {
                executor.submit(self._fetch_klines_task, symbol, idx, total): symbol
                for idx, symbol in enumerate(symbols, 1)
            }

            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    _, rows = future.result()
                    if rows:
                        all_rows.extend(rows)
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    logger.warning(f"{symbol} 获取失败: {e}")
                    fail_count += 1

        if not all_rows:
            logger.error("未采集到任何K线数据，退出")
            return False

        df = pd.DataFrame(all_rows)
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        logger.info(f"共采集到 {len(df)} 条K线记录, {df['symbol'].nunique()} 个币种")
        logger.info(f"成功: {success_count}, 失败: {fail_count}, 总请求: {request_count}")

        local_file = "/tmp/perpetual_klines_latest.parquet"
        try:
            df.to_parquet(local_file, index=False)
        except Exception as e:
            logger.error(f"Parquet写入失败: {e}")
            return False

        success = self._upload_to_cos(local_file, config.COS_KEY)

        import os
        if success:
            if os.path.exists(local_file):
                os.remove(local_file)
        else:
            # Keep local backup if COS upload failed to prevent data loss
            backup_file = local_file + ".backup"
            if os.path.exists(local_file):
                os.rename(local_file, backup_file)
                logger.warning(f"上传失败，本地备份保留在: {backup_file}")

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(f"总耗时: {elapsed:.2f} 秒")

        if success:
            logger.info("=" * 60)
            logger.info("数据采集上传成功")
            logger.info("=" * 60)
        else:
            logger.error("数据上传失败")

        return success


def run_collector() -> bool:
    collector = BinanceKlineCollector()
    return collector.run()

if __name__ == '__main__':
    run_collector()
