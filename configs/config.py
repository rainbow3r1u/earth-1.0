"""
统一配置文件 - 使用环境变量管理敏感信息
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent

env_path = BASE_DIR / '.env'
load_dotenv(env_path)

OUTPUT_DIR = BASE_DIR / 'output'
DATA_DIR = BASE_DIR / 'data'
STATIC_DIR = BASE_DIR / 'static'
CHARTS_DIR = STATIC_DIR / 'charts'

# Nginx 共享目录 (可选配置)
NGINX_WWW_DIR = Path(os.environ.get('NGINX_WWW_DIR', '/var/www/'))

def init_dirs():
    """创建必需目录 — 在应用启动时调用，而非模块导入时"""
    for d in [OUTPUT_DIR, DATA_DIR, STATIC_DIR, CHARTS_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logging.warning("Permission denied creating directory: %s", d)

# init_dirs() 不再在模块导入时调用，避免权限错误在 import 时抛出
# 调用方需显式调用 init_dirs() 来确保目录存在

# 腾讯云COS
COS_SECRET_ID = os.environ.get('COS_SECRET_ID', '')
COS_SECRET_KEY = os.environ.get('COS_SECRET_KEY', '')
COS_REGION = os.environ.get('COS_REGION', 'ap-seoul')
COS_BUCKET = os.environ.get('COS_BUCKET', '')
COS_ENDPOINT = os.environ.get('COS_ENDPOINT', 'cos.ap-seoul.myqcloud.com')
COS_KEY = os.environ.get('COS_KEY', 'klines/futures_latest.parquet')
COS_MONTHLY_KEY_PREFIX = os.environ.get('COS_MONTHLY_KEY_PREFIX', 'klines/monthly')

# 飞书
FEISHU_APP_ID = os.environ.get('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.environ.get('FEISHU_APP_SECRET', '')
FEISHU_USER_OPEN_ID = os.environ.get('FEISHU_USER_OPEN_ID', '')

# DeepSeek
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/chat/completions')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')

# Web
WEB_HOST = os.environ.get('WEB_HOST', '0.0.0.0')
def _parse_int_env(key: str, default: int) -> int:
    """Parse an environment variable as int, falling back to default on invalid input."""
    raw = os.environ.get(key, str(default))
    try:
        return int(raw)
    except (ValueError, TypeError):
        logging.warning("Invalid int for env '%s' (got '%s'), using default %d", key, raw, default)
        return default


WEB_PORT = _parse_int_env('WEB_PORT', 5003)

# 数据库
DB_PATH = os.environ.get('DB_PATH', str(DATA_DIR / 'signals.db'))

# 币安
BINANCE_API = os.environ.get('BINANCE_API', 'https://fapi.binance.com')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', '')

# 通用
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = [5, 15, 30]
REQUEST_TIMEOUT = 10

DATA_CACHE_MINUTES = _parse_int_env('DATA_CACHE_MINUTES', 60)
CHART_CACHE_HOURS = _parse_int_env('CHART_CACHE_HOURS', 1)


# ---------------------------------------------------------------------------
# Startup validation: warn about missing required credentials
# ---------------------------------------------------------------------------
_REQUIRED_CREDENTIALS = {
    # (value, env_var_name, description)
    (COS_SECRET_ID, 'COS_SECRET_ID', 'Tencent COS Secret ID'),
    (COS_SECRET_KEY, 'COS_SECRET_KEY', 'Tencent COS Secret Key'),
    (COS_BUCKET, 'COS_BUCKET', 'Tencent COS Bucket'),
    (FEISHU_APP_ID, 'FEISHU_APP_ID', 'Feishu App ID'),
    (FEISHU_APP_SECRET, 'FEISHU_APP_SECRET', 'Feishu App Secret'),
    (DEEPSEEK_API_KEY, 'DEEPSEEK_API_KEY', 'DeepSeek API Key'),
    (BINANCE_API_KEY, 'BINANCE_API_KEY', 'Binance API Key'),
    (BINANCE_SECRET_KEY, 'BINANCE_SECRET_KEY', 'Binance Secret Key'),
}

for _val, _env_name, _desc in _REQUIRED_CREDENTIALS:
    if not _val:
        logging.warning(
            "%s is not set (env: %s). Related API calls will fail.",
            _desc, _env_name,
        )
