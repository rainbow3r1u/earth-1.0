#!/usr/bin/env python3
"""拉取 SP500/DXY/黄金 日线数据, 存到 /tmp/macro_assets.json"""
import json, os, time
from datetime import datetime, timezone

OUT = '/tmp/macro_assets.json'

def fetch():
    try:
        import yfinance as yf
    except ImportError:
        os.system('pip install yfinance -q')
        import yfinance as yf

    symbols = {
        '^GSPC': 'SP500',       # S&P 500
        'DX-Y.NYB': 'DXY',      # US Dollar Index
        'GC=F': 'GOLD',         # Gold Futures
    }

    result = {}
    for ticker, name in symbols.items():
        print(f"拉取 {name} ({ticker})...")
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5y")
            data = {}
            for idx, row in hist.iterrows():
                date_str = idx.strftime('%Y-%m-%d')
                data[date_str] = {
                    'close': round(float(row['Close']), 4),
                    'ret_1d': round(float(row['Close']) / float(hist.shift(1).loc[idx, 'Close']) - 1, 6) if idx in hist.shift(1).index else 0
                }
            result[name] = data
            print(f"  {name}: {len(data)} days")
        except Exception as e:
            print(f"  {name}: {e}")
        time.sleep(0.5)

    with open(OUT, 'w') as f:
        json.dump({'updated': datetime.now(timezone.utc).isoformat(), 'data': result}, f)
    print(f"\n保存: {OUT} ({os.path.getsize(OUT)/1024:.0f}KB)")

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
        )
        cos = CosS3Client(config)
        bucket = os.environ.get('COS_BUCKET', '')
        with open(OUT, 'rb') as f:
            cos.put_object(Bucket=bucket, Key='klines/macro_assets/macro_assets.json',
                           Body=f.read(), ContentType='application/json')
        print("[Macro] COS上传成功")
    except Exception as e:
        print(f"[Macro] COS上传失败: {e}")

if __name__ == '__main__':
    fetch()
