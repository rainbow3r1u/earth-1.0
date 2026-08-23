#!/usr/bin/env python3
"""拉取 SP500/DXY/黄金 日线数据, 存到 /tmp/macro_assets.json

2026-08-06 修复(漂移监控首次ALERT的根因):
  旧版任一 ticker 拉取失败时静默丢弃该资产 → 8/5 夜 ^GSPC 被限流,
  SP500 整段从文件消失, 今早生产训练 sp500 特征全零。
  现: 每 ticker 重试3次; 最终失败保留旧文件中的陈旧序列(宁陈旧不缺失);
  资产缺失时 exit 1 大声告警; 写入前校验3资产齐全。
"""
import json, os, time, sys, subprocess, shutil
from datetime import datetime, timezone

OUT = '/tmp/macro_assets.json'
# 训练与健康检查读取的是 data/macro_assets.json；采集成功后同步过去，避免手动补跑后仍显示 STALE
DATA_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'macro_assets.json')
SYMBOLS = {
    '^GSPC': 'SP500',       # S&P 500
    'DX-Y.NYB': 'DXY',      # US Dollar Index
    'GC=F': 'GOLD',         # Gold Futures
}

def fetch_one(ticker, name, yf):
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5y")
            if len(hist) < 500:
                raise RuntimeError(f'仅{len(hist)}行, 异常')
            data = {}
            for idx, row in hist.iterrows():
                date_str = idx.strftime('%Y-%m-%d')
                data[date_str] = {
                    'close': round(float(row['Close']), 4),
                    'ret_1d': round(float(row['Close']) / float(hist.shift(1).loc[idx, 'Close']) - 1, 6) if idx in hist.shift(1).index else 0
                }
            print(f"  {name}: {len(data)} days")
            return data
        except Exception as e:
            print(f"  {name} 第{attempt+1}次失败: {e}")
            time.sleep(20 * (attempt + 1))
    return None

def fetch():
    try:
        import yfinance as yf
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'yfinance', '-q'], check=False)
        import yfinance as yf

    # 旧数据兜底: 拉取失败时保留陈旧序列, 绝不静默丢资产
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT)).get('data', {})
        except Exception:
            prev = {}

    result, failed = {}, []
    for ticker, name in SYMBOLS.items():
        print(f"拉取 {name} ({ticker})...")
        data = fetch_one(ticker, name, yf)
        if data is not None:
            result[name] = data
        elif name in prev and len(prev[name]) >= 500:
            print(f"  ❌❌ {name} 拉取失败, 保留上一版陈旧数据({len(prev[name])}天) — 该序列滞后, 下次运行补!")
            result[name] = prev[name]
            failed.append(name)
        else:
            print(f"  ❌❌❌ {name} 拉取失败且无旧数据可保留 — 资产将缺失!")
            failed.append(name)
        time.sleep(0.5)

    missing = [n for n in SYMBOLS.values() if n not in result]
    if missing:
        print(f"❌[Macro] FATAL: 资产缺失 {missing}, 不覆盖现有文件!")
        sys.exit(2)

    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'updated': datetime.now(timezone.utc).isoformat(), 'data': result}, f)
    os.rename(tmp, OUT)
    print(f"\n保存: {OUT} ({os.path.getsize(OUT)/1024:.0f}KB)")

    # 同步到 data/（训练与健康检查实际读取路径）
    try:
        shutil.copy2(OUT, DATA_OUT)
        print(f"[Macro] 已同步到 {DATA_OUT}")
    except Exception as e:
        print(f"[Macro] 同步到 data/ 失败: {e}")

    # 上传COS
    try:
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
        cos = CosS3Client(config)
        bucket = os.environ.get('COS_BUCKET', '')
        with open(OUT, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/macro_assets/macro_assets.json',
                           Body=f.read(), ContentType='application/json')
        print("[Macro] COS上传成功")
    except Exception as e:
        print(f"[Macro] COS上传失败: {e}")

    if failed:
        print(f"❌[Macro] ALERT: 本次降级运行, 陈旧序列={failed}")
        sys.exit(1)

if __name__ == '__main__':
    fetch()
