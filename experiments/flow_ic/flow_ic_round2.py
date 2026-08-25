#!/usr/bin/env python3
"""第二轮 IC: 200币, 180天, 重点 basis/oi_chg, 并算超额收益(减BTC)."""
import requests, time, json, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DAYS=180
TOP_N=200
START=int((datetime.now(timezone.utc)-timedelta(days=DAYS+5)).timestamp()*1000)
END=int(datetime.now(timezone.utc).timestamp()*1000)

def get(url, params=None, retries=4):
    for i in range(retries):
        try:
            r=requests.get(url, params=params, timeout=25)
            if r.status_code==200: return r.json()
            if r.status_code in (429,418):
                time.sleep(min(30, 2**i*2)); continue
        except Exception:
            time.sleep(1)
    return None

def day_str(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')

kl=json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
syms=[]
for sym,bars in kl.items():
    if not sym.endswith('USDT') or len(bars)<20: continue
    q=sum(b['q'] for b in bars[-5:])/5
    syms.append((sym,q))
syms.sort(key=lambda x:-x[1])
syms=[s for s,_ in syms[:TOP_N]]
print('symbols', len(syms), syms[:10])

# BTC 收益用于超额
btc_sp=get('https://api.binance.com/api/v3/klines', {'symbol':'BTCUSDT','interval':'1d','startTime':START,'endTime':END,'limit':1000}) or []
btc_d={day_str(int(k[0])): float(k[4]) for k in btc_sp}

rows=[]
for idx,sym in enumerate(syms):
    sp=get('https://api.binance.com/api/v3/klines', {'symbol':sym,'interval':'1d','startTime':START,'endTime':END,'limit':1000})
    fu=get('https://fapi.binance.com/fapi/v1/klines', {'symbol':sym,'interval':'1d','startTime':START,'endTime':END,'limit':1000})
    oi=get('https://fapi.binance.com/futures/data/openInterestHist', {'symbol':sym,'period':'1d','limit':DAYS+5})
    fr=get('https://fapi.binance.com/fapi/v1/fundingRate', {'symbol':sym,'startTime':START,'endTime':END,'limit':1000})
    if not sp or not fu: continue
    spd={day_str(int(k[0])): {'q':float(k[7]),'tbq':float(k[10]),'c':float(k[4])} for k in sp}
    fud={day_str(int(k[0])): {'q':float(k[7]),'tbq':float(k[10]),'c':float(k[4])} for k in fu}
    oid={}
    if oi:
        for x in oi: oid[day_str(x['timestamp'])] = float(x['sumOpenInterest'])
    frd={}
    if fr:
        for x in fr: frd[day_str(x['fundingTime'])] = float(x['fundingRate'])
    dates=sorted(set(spd)&set(fud))
    for i,d in enumerate(dates):
        s=spd[d]; f=fud[d]
        if s['q']<=0 or f['q']<=0: continue
        basis=(f['c']/s['c']-1)*100 if s['c']>0 else None
        prev_oi=oid.get(dates[i-1]) if i>0 else None
        oi_chg=((oid[d]/prev_oi-1)*100) if (d in oid and prev_oi and prev_oi>0) else None
        funding=frd.get(d)
        ret1=ret2=None
        if i+1 < len(dates):
            ret1=(spd[dates[i+1]]['c']/s['c']-1)*100
        if i+2 < len(dates):
            ret2=(spd[dates[i+2]]['c']/s['c']-1)*100
        btc_cur = btc_d.get(d)
        btc_c1 = btc_d.get(dates[i+1]) if i+1 < len(dates) else None
        btc_c2 = btc_d.get(dates[i+2]) if i+2 < len(dates) else None
        if btc_cur and btc_cur > 0:
            ret1_ex = (ret1 - (btc_c1/btc_cur-1)*100) if (ret1 is not None and btc_c1) else None
            ret2_ex = (ret2 - (btc_c2/btc_cur-1)*100) if (ret2 is not None and btc_c2) else None
        else:
            ret1_ex = ret2_ex = None
        rows.append({'sym':sym,'date':d,'basis':basis,'oi_chg':oi_chg,'funding':funding,
                     'ret1':ret1,'ret2':ret2,'ret1_ex':ret1_ex,'ret2_ex':ret2_ex})
    if (idx+1)%50==0: print(f'progress {idx+1}/{len(syms)}', flush=True)
    time.sleep(0.05)

print('rows', len(rows))
json.dump(rows, open('/tmp/flow_ic_rows_200.json','w'), ensure_ascii=False, indent=1)

def spearman(a,b):
    pairs=[(x,y) for x,y in zip(a,b) if x is not None and y is not None]
    if len(pairs)<30: return None
    n=len(pairs)
    ra={v:i for i,v in enumerate(sorted(x for x,_ in pairs))}
    rb={v:i for i,v in enumerate(sorted(y for _,y in pairs))}
    ma=(n-1)/2; mb=(n-1)/2
    da=[ra[x]-ma for x,_ in pairs]; db=[rb[y]-mb for _,y in pairs]
    cov=sum(x*y for x,y in zip(da,db))/n
    va=sum(x*x for x in da)/n; vb=sum(y*y for y in db)/n
    return cov/math.sqrt(va*vb) if va*vb>0 else None

features=['basis','oi_chg','funding']
labels=['ret1','ret2','ret1_ex','ret2_ex']
print('\n===== IC (全样本) =====')
print(f"{'特征':<8}{'ret1':>10}{'ret2':>10}{'ret1_ex':>10}{'ret2_ex':>10}")
for feat in features:
    vals={}
    for lab in labels:
        vals[lab]=spearman([r[feat] for r in rows],[r[lab] for r in rows])
    print(f"{feat:<8}"+''.join(f'{vals[lab]:>10.4f}' if vals[lab] is not None else f'{"N/A":>10}' for lab in labels))

# 分组: basis/oi_chg 按五分位未来2日超额收益
print('\n===== 按指标五分位: 未来2日超额收益均值% =====')
for feat in ['basis','oi_chg']:
    valid=[r for r in rows if r.get(feat) is not None and r.get('ret2_ex') is not None]
    valid.sort(key=lambda r:r[feat])
    n=len(valid)
    print(f'\n{feat}: n={n}')
    for g in range(5):
        seg=valid[g*n//5:(g+1)*n//5]
        mean=sum(r['ret2_ex'] for r in seg)/len(seg)
        win=sum(1 for r in seg if r['ret2_ex']>0)/len(seg)*100
        print(f'  组{g+1}: 均值{mean:+.3f}% 胜率{win:.0f}%')
