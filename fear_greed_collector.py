#!/usr/bin/env python3
"""恐慌贪婪指数采集器 — 每天拉一次，存本地+COS"""
import fcntl, requests, json, os, time, tempfile
from datetime import datetime, timezone

CACHE_FILE = "/tmp/fear_greed_history.json"
COS_PREFIX = "klines/fear_greed/"

_cos_client = None

def _get_cos():
    global _cos_client
    if _cos_client is None:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=os.environ.get('COS_REGION', ''),
            SecretId=os.environ.get('COS_SECRET_ID', ''),
            SecretKey=os.environ.get('COS_SECRET_KEY', ''),
            Endpoint=os.environ.get('COS_ENDPOINT', ''),
            Timeout=30
        )
        _cos_client = CosS3Client(config)
    return _cos_client

def fetch_all():
    """首次拉全部历史"""
    try:
        resp = requests.get('https://api.alternative.me/fng/?limit=365', timeout=15)
        data = resp.json()['data']
        records = []
        for d in data:
            records.append({
                'date': datetime.fromtimestamp(int(d['timestamp']), tz=timezone.utc).strftime('%Y-%m-%d'),
                'value': int(d['value']),
                'classification': d['value_classification'],
            })
        records.sort(key=lambda x: x['date'])
        with open(CACHE_FILE, 'w') as f:
            json.dump(records, f, indent=2)
        print(f"FearGreed: 拉取{len(records)}天历史, {records[0]['date']} ~ {records[-1]['date']}")
        return records
    except Exception as e:
        print(f"FearGreed fetch error: {e}")
        return []

def fetch_today():
    """增量更新 — 只拉今天（带文件锁防并发竞态）"""
    try:
        resp = requests.get('https://api.alternative.me/fng/?limit=1', timeout=10)
        d = resp.json()['data'][0]
        today = datetime.fromtimestamp(int(d['timestamp']), tz=timezone.utc).strftime('%Y-%m-%d')
        record = {
            'date': today,
            'value': int(d['value']),
            'classification': d['value_classification'],
        }
        # 合并到缓存（带文件锁防竞态）
        records = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r+') as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        records = json.load(f)
                    except json.JSONDecodeError:
                        print("FearGreed: 缓存文件损坏，从头开始")
                        records = []
                    # 去重
                    if not any(r['date'] == today for r in records):
                        records.append(record)
                    # 原子写入 tempfile + rename
                    f.seek(0)
                    f.truncate()
                    json.dump(records, f, indent=2)
                    fcntl.flock(f, fcntl.LOCK_UN)
            except Exception as e:
                print(f"FearGreed: 文件锁操作失败({e})，使用原子写入")
                records = []
                if os.path.exists(CACHE_FILE):
                    try:
                        with open(CACHE_FILE) as f_ro:
                            records = json.load(f_ro)
                    except (json.JSONDecodeError, ValueError):
                        records = []
                if not any(r['date'] == today for r in records):
                    records.append(record)
                tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CACHE_FILE), suffix='.tmp')
                with os.fdopen(tmp_fd, 'w') as f_tmp:
                    json.dump(records, f_tmp, indent=2)
                os.rename(tmp_path, CACHE_FILE)
        else:
            if not any(r['date'] == today for r in records):
                records.append(record)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CACHE_FILE), suffix='.tmp')
            with os.fdopen(tmp_fd, 'w') as f_tmp:
                json.dump(records, f_tmp, indent=2)
            os.rename(tmp_path, CACHE_FILE)
        print(f"FearGreed: {today} = {record['value']} ({record['classification']}), 共{len(records)}天")
        return records
    except Exception as e:
        print(f"FearGreed fetch error: {e}")
        return []

def upload_cos():
    try:
        if not os.path.exists(CACHE_FILE):
            return
        cos = _get_cos()
        bucket = os.environ.get('COS_BUCKET', '')
        with open(CACHE_FILE) as f:
            body = f.read().encode('utf-8')
        cos.put_object(Bucket=bucket, Key=f'{COS_PREFIX}fear_greed_history.json',
                       Body=body, ContentType='application/json')
        print("FearGreed: COS上传成功")
    except Exception as e:
        print(f"FearGreed COS error: {e}")

if __name__ == '__main__':
    if not os.path.exists(CACHE_FILE):
        fetch_all()
    else:
        fetch_today()
    upload_cos()
