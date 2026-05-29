#!/usr/bin/env python3
"""TON式吸筹模式扫描器 — 量比>6x + EMA50下 + 日涨幅<3%"""
import requests, json, time, sys
from datetime import datetime, timezone

CACHE = "/tmp/accumulation_scan.json"

def ema(series, period):
    k = 2/(period+1)
    result = [series[0]]
    for i in range(1, len(series)):
        result.append(series[i]*k + result[-1]*(1-k))
    return result

def scan():
    # 币种列表
    try:
        resp = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=15)
        symbols = [s['symbol'] for s in resp.json()['symbols']
                   if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('contractType')=='PERPETUAL']
    except Exception:
        symbols = []

    results = []
    for sym in symbols:
        try:
            resp = requests.get('https://api.binance.com/api/v3/klines',
                params={'symbol': sym, 'interval': '1d', 'limit': 90}, timeout=10)
            if resp.status_code != 200: continue
            kls = resp.json()
            if len(kls) < 50: continue

            closes = [float(k[4]) for k in kls]
            vols = [float(k[7]) for k in kls]
            highs = [float(k[2]) for k in kls]
            lows = [float(k[3]) for k in kls]
            n = len(closes)
            ema50 = ema(closes, 50)

            vol_ma20 = []
            for i in range(20, n):
                vol_ma20.append(sum(vols[i-20:i])/20)

            signals = []
            for i in range(max(20, n-90), n):
                if vol_ma20[i-20] <= 0: continue
                vr = vols[i] / vol_ma20[i-20]
                chg = abs(closes[i]-closes[i-1])/closes[i-1]*100
                if vr > 5 and closes[i] < ema50[i]*1.05 and chg < 3:
                    dt = datetime.fromtimestamp(int(kls[i][0])//1000, tz=timezone.utc).strftime('%m-%d')
                    signals.append({'date': dt, 'price': closes[i], 'vol_ratio': round(vr,1), 'chg': round(chg,1)})

            if signals:
                last = signals[-1]
                sig_idx = next((i for i in range(n-1,-1,-1)
                    if i > 0 and closes[i-1] > 0
                    and abs((closes[i]-closes[i-1])/closes[i-1]*100)<3
                    and sum(vols[max(0,i-20):i]) > 0
                    and vols[i]/(sum(vols[max(0,i-20):i])/20)>5
                    and closes[i]<ema50[i]*1.05), None)
                if sig_idx is None:
                    continue
                fwd = (closes[-1]-closes[sig_idx])/closes[sig_idx]*100 if closes[sig_idx] > 0 else 0
                results.append({
                    'symbol': sym,
                    'signal_date': last['date'],
                    'signal_price': last['price'],
                    'vol_ratio': last['vol_ratio'],
                    'current_price': closes[-1],
                    'fwd_return': round(fwd, 1),
                    'signals_count': len(signals),
                    'vs_ema50': round(closes[-1]/ema50[-1], 2),
                })
        except Exception:
            pass

    results.sort(key=lambda x: -x['fwd_return'])
    with open(CACHE, 'w') as f:
        json.dump({'scanned': len(symbols), 'found': len(results), 'results': results,
                    'updated': time.time()}, f, default=str)
    print(f'Scan done: {len(results)} signals from {len(symbols)} coins')
    return results

if __name__ == '__main__':
    scan()
