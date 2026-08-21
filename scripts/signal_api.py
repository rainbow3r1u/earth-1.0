#!/usr/bin/env python3
"""外部信号 API 原型 (FastAPI).
用法:
  SIGNAL_API_KEYS=sk_live_demo uvicorn scripts.signal_api:app --host 127.0.0.1 --port 8080

端点:
  GET /health
  GET /signals/today            (Header: X-API-Key)
  GET /signals/{date}
  GET /pnl/daily
"""
import json, os, glob
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data'
CACHE = DATA / 'top10_forward_cache.json'
API_KEYS = set(k.strip() for k in os.getenv('SIGNAL_API_KEYS', 'dev_key').split(',') if k.strip())

app = FastAPI(title='Signal API', version='0.1.0')

def require_key(x_api_key: str = Header(default=None)):
    if not x_api_key or x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail='invalid api key')

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/signals/today', dependencies=[Depends(require_key)])
def signals_today():
    files = sorted(glob.glob(str(DATA / 'pred_2026-*.json')))
    if not files:
        raise HTTPException(404, 'no prediction files')
    date = Path(files[-1]).stem.replace('pred_', '')
    return _signals(date)

@app.get('/signals/{date}', dependencies=[Depends(require_key)])
def signals_date(date: str):
    return _signals(date)

def _signals(date: str):
    p = DATA / f'pred_{date}.json'
    if not p.exists():
        raise HTTPException(404, f'no prediction for {date}')
    pred = json.load(open(p))
    return {
        'date': date,
        'long': pred.get('top10_long', []),
        'short': pred.get('top10_short', []),
        'disclaimer': '仅供研究，不构成投资建议；模拟口径，非实盘收益。',
    }

@app.get('/pnl/daily')
def pnl_daily():
    if not CACHE.exists():
        raise HTTPException(404, 'no pnl cache')
    cache = json.load(open(CACHE))
    NOTIONAL, COST = 300.0, 0.002
    rows = []
    cum = 0.0
    for ds in sorted(cache):
        r = cache[ds]
        pnls = ([t['pnl'] for t in r.get('long', []) if t.get('pnl') is not None] +
                [t['pnl'] for t in r.get('short', []) if t.get('pnl') is not None])
        if not pnls:
            continue
        day = NOTIONAL * sum(pnls) / 100 - len(pnls) * NOTIONAL * COST
        cum += day
        rows.append({'date': ds, 'day_pnl_usdt': round(day, 2), 'cum_pnl_usdt': round(cum, 2), 'trades': len(pnls)})
    return {'rows': rows, 'disclaimer': '模拟口径，非实盘收益。'}
