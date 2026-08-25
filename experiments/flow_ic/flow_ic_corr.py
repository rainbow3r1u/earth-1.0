#!/usr/bin/env python3
"""相关性实验: basis/oi_chg 与残差特征的相关性 + 增量IC."""
import json, math
from datetime import datetime, timezone

rows=json.load(open('/tmp/flow_ic_rows_200.json'))
kl=json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']

# 预计算每币 close 序列和收益率序列
data={}
btc=kl.get('BTCUSDT',[])
btc_dates=[datetime.fromtimestamp(k['t']/1000,tz=timezone.utc).strftime('%Y-%m-%d') for k in btc]
btc_closes=[k['c'] for k in btc]
btc_rets=[0.0]+[(btc_closes[i]/btc_closes[i-1]-1) for i in range(1,len(btc_closes))]
btc_date_idx={d:i for i,d in enumerate(btc_dates)}
for sym,bars in kl.items():
    dates=[datetime.fromtimestamp(k['t']/1000,tz=timezone.utc).strftime('%Y-%m-%d') for k in bars]
    closes=[k['c'] for k in bars]
    rets=[0.0]+[(closes[i]/closes[i-1]-1) for i in range(1,len(closes))]
    data[sym]={'dates':dates,'closes':closes,'rets':rets,'idx':{d:i for i,d in enumerate(dates)}}

def residual(sym, date):
    d=data.get(sym)
    if not d: return None
    i=d['idx'].get(date)
    if i is None or i<20: return None
    bi=btc_date_idx.get(date)
    if bi is None or bi<20: return None
    x=btc_rets[bi-19:bi+1]
    y=d['rets'][i-19:i+1]
    n=len(x)
    if n<5: return None
    xm=sum(x)/n; ym=sum(y)/n
    cov=sum((a-xm)*(b-ym) for a,b in zip(x,y))/(n-1)
    var=sum((a-xm)**2 for a in x)/n
    if var<=1e-12: return None
    beta=cov/var; alpha=ym-beta*xm
    return y[-1]-(alpha+beta*x[-1])

# 合并残差
for r in rows:
    r['residual']=residual(r['sym'], r['date'])

def spearman(a,b):
    pairs=[(x,y) for x,y in zip(a,b) if x is not None and y is not None]
    if len(pairs)<50: return None
    n=len(pairs)
    ra={v:i for i,v in enumerate(sorted(x for x,_ in pairs))}
    rb={v:i for i,v in enumerate(sorted(y for _,y in pairs))}
    ma=(n-1)/2; mb=(n-1)/2
    da=[ra[x]-ma for x,_ in pairs]; db=[rb[y]-mb for _,y in pairs]
    cov=sum(x*y for x,y in zip(da,db))/n
    va=sum(x*x for x in da)/n; vb=sum(y*y for y in db)/n
    return cov/math.sqrt(va*vb) if va*vb>0 else None

print('样本', len(rows), '有残差', sum(1 for r in rows if r.get('residual') is not None))

print('\n=== 特征与残差的相关性 ===')
for feat in ['basis','oi_chg','funding']:
    ic=spearman([r.get(feat) for r in rows],[r.get('residual') for r in rows])
    print(f'{feat} vs residual: {ic if ic is None else round(ic,4)}')

print('\n=== 残差本身对未来超额收益的 IC ===')
for lab in ['ret1_ex','ret2_ex']:
    ic=spearman([r.get('residual') for r in rows],[r.get(lab) for r in rows])
    print(f'residual vs {lab}: {ic if ic is None else round(ic,4)}')

print('\n=== 在残差五分位内, basis/oi_chg 对未来2天超额收益的 IC ===')
valid=[r for r in rows if r.get('residual') is not None and r.get('ret2_ex') is not None]
valid.sort(key=lambda r:r['residual'])
n=len(valid)
for feat in ['basis','oi_chg']:
    ics=[]
    for g in range(5):
        seg=valid[g*n//5:(g+1)*n//5]
        ic=spearman([r.get(feat) for r in seg],[r.get('ret2_ex') for r in seg])
        ics.append(ic)
    avg=sum(x for x in ics if x is not None)/len([x for x in ics if x is not None]) if any(x is not None for x in ics) else None
    print(f'{feat}: 各残差组IC={[round(x,4) if x is not None else None for x in ics]} 平均={avg if avg is None else round(avg,4)}')
