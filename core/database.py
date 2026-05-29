"""
数据库模块 - SQLite存储历史信号
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from configs import config
from utils.logger import get_logger

logger = get_logger('database')


class Database:
    _local = threading.local()
    _db_lock = threading.Lock()

    @classmethod
    def _get_connection(cls) -> sqlite3.Connection:
        if not hasattr(cls._local, 'conn') or cls._local.conn is None:
            db_path = Path(config.DB_PATH)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # 移除 check_same_thread=False，为每个线程创建独立连接，确保线程安全
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cls._local.conn = conn
            
            with cls._db_lock:
                cls._init_tables(conn)
                
        return cls._local.conn

    @classmethod
    def _init_tables(cls, conn: sqlite3.Connection):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                data JSON NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_signals_strategy
            ON signals(strategy, timestamp DESC)
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_signals_symbol
            ON signals(symbol, timestamp DESC)
        ''')
        conn.commit()

    @classmethod
    def save_signal(cls, strategy: str, symbol: str, data: Dict[str, Any],
                    timestamp: Optional[datetime] = None) -> int:
        conn = cls._get_connection()
        ts = timestamp or datetime.now()

        with cls._db_lock:
            cursor = conn.execute('''
                INSERT INTO signals (strategy, symbol, timestamp, data)
                VALUES (?, ?, ?, ?)
            ''', (strategy, symbol, ts.isoformat(), json.dumps(data, ensure_ascii=False)))
            conn.commit()
        return cursor.lastrowid

    @classmethod
    def save_signals_batch(cls, strategy: str, signals: List[Dict[str, Any]],
                           timestamp: Optional[datetime] = None) -> int:
        conn = cls._get_connection()
        ts = timestamp or datetime.now()

        count = 0
        with cls._db_lock:
            for signal in signals:
                symbol = signal.get('symbol', 'UNKNOWN')
                conn.execute('''
                    INSERT INTO signals (strategy, symbol, timestamp, data)
                    VALUES (?, ?, ?, ?)
                ''', (strategy, symbol, ts.isoformat(), json.dumps(signal, ensure_ascii=False)))
                count += 1
            conn.commit()
        logger.info(f"批量保存 {count} 条信号: {strategy}")
        return count

    @classmethod
    def get_latest_signals(cls, strategy: str, limit: int = 100) -> List[Dict[str, Any]]:
        conn = cls._get_connection()
        with cls._db_lock:
            cursor = conn.execute('''
                SELECT * FROM signals
                WHERE strategy = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (strategy, limit))
            rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append({
                'id': row['id'],
                'strategy': row['strategy'],
                'symbol': row['symbol'],
                'timestamp': row['timestamp'],
                'data': json.loads(row['data']),
                'created_at': row['created_at']
            })
        return results

    @classmethod
    def get_signals_by_symbol(cls, symbol: str, days: int = 7) -> List[Dict[str, Any]]:
        conn = cls._get_connection()
        with cls._db_lock:
            cursor = conn.execute('''
                SELECT * FROM signals
                WHERE symbol = ?
                AND timestamp >= datetime('now', 'utc', ?)
                ORDER BY timestamp DESC
            ''', (symbol, f'-{days} days'))
            rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append({
                'id': row['id'],
                'strategy': row['strategy'],
                'symbol': row['symbol'],
                'timestamp': row['timestamp'],
                'data': json.loads(row['data']),
                'created_at': row['created_at']
            })
        return results

    @classmethod
    def cleanup_old_signals(cls, days: int = 30) -> int:
        conn = cls._get_connection()
        with cls._db_lock:
            cursor = conn.execute('''
                DELETE FROM signals
                WHERE created_at < datetime('now', ?)
            ''', (f'-{days} days',))
            conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"清理 {deleted} 条过期信号 (>{days}天)")
        return deleted

    @classmethod
    def close(cls):
        """关闭当前线程的数据库连接"""
        with cls._db_lock:
            if hasattr(cls._local, 'conn') and cls._local.conn:
                cls._local.conn.close()
                cls._local.conn = None
                logger.debug("数据库连接已关闭")
