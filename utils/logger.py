"""
统一日志工具
"""
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Sensitive-data filter: redacts API keys and secrets from log messages
# ---------------------------------------------------------------------------
_SENSITIVE_PATTERNS = [
    # Binance API key header
    (re.compile(r'(X-MBX-APIKEY[:\s=]+)\S+', re.IGNORECASE), r'\1***'),
    # Generic key=value patterns (COS_SECRET, API_KEY, SECRET_KEY, etc.)
    (re.compile(r'((?:api[_-]?key|secret[_-]?key|app[_-]?secret|token)[\s:=]+)\S+', re.IGNORECASE), r'\1***'),
    # Quoted key strings in JSON / dict repr
    (re.compile(r'("(?:api[_-]?key|secret[_-]?key|app[_-]?secret|token)"[\s:]*")[^"]+', re.IGNORECASE), r'\1***'),
    # Binance signature in URL query strings
    (re.compile(r'signature=[A-Za-z0-9]+', re.IGNORECASE), 'signature=***'),
    # WebSocket listenKey (long hex string in URL)
    (re.compile(r'listenKey=[A-Za-z0-9]+', re.IGNORECASE), 'listenKey=***'),
]


class SensitiveDataFilter(logging.Filter):
    """Logging filter that masks sensitive data (API keys, secrets) in messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            msg = record.msg
            for pattern, replacement in _SENSITIVE_PATTERNS:
                msg = pattern.sub(replacement, msg)
            record.msg = msg
        # Also redact args that may contain sensitive strings
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(
                    self._redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern, replacement in _SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


# ---------------------------------------------------------------------------
# Logger cache  (name -> {'logger': Logger, 'level': int, 'log_file': str|None})
# ---------------------------------------------------------------------------
_loggers: dict = {}


def setup_logger(name: str = None, level: int = logging.INFO, log_file: str = None) -> logging.Logger:
    _key = name or 'crypto_scanner'

    # MEDIUM-032: if a cached logger has different settings, reconfigure it
    if _key in _loggers:
        cached = _loggers[_key]
        if cached['level'] != level or cached['log_file'] != log_file:
            logger = cached['logger']
            logger.setLevel(level)
            for handler in logger.handlers:
                handler.setLevel(level)
            # Add a file handler if one was requested and doesn't already exist
            if log_file:
                existing_files = {
                    h.baseFilename for h in logger.handlers
                    if isinstance(h, logging.FileHandler)
                }
                if log_file not in existing_files:
                    _add_file_handler(logger, log_file, level)
            cached['level'] = level
            cached['log_file'] = log_file
        return cached['logger']

    logger = logging.getLogger(_key)
    logger.setLevel(level)

    # HIGH-065: attach sensitive-data filter
    logger.addFilter(SensitiveDataFilter())

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        _add_file_handler(logger, log_file, level)

    _loggers[_key] = {'logger': logger, 'level': level, 'log_file': log_file}
    return logger


def _add_file_handler(logger: logging.Logger, log_file: str, level: int) -> None:
    """Attach a rotating file handler (HIGH-064) to *logger*."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8',
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logger.handlers[0].formatter if logger.handlers else None)
    logger.addHandler(file_handler)


def get_logger(name: str = None) -> logging.Logger:
    _key = name or 'crypto_scanner'
    if _key in _loggers:
        return _loggers[_key]['logger']
    return setup_logger(name)
