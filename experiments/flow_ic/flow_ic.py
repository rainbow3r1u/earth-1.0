#!/usr/bin/env python3
"""现货/合约资金流指标 IC 初筛.

数据源: 币安现货 + 币安U本位合约
指标:
  spot_taker_buy_ratio = 现货主动买入额 / 现货成交额
  fut_taker_buy_ratio  = 合约主动买入额 / 合约成交额
  basis_pct            = (合约收盘/现货收盘 - 1) * 100
  oi_chg_pct           = 合约持仓量日环比 %
  funding_rate         = 每日资金费率 (取当天最后一期)
标签: 未来1日/2日现货收益
"""
import requests, time, json, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DAYS = 60
TOP_N = 50
START = int((datetime.now(timezone.utc) - timedelta(days=DAYS+3)).timestamp()*1000)
END = int(datetime.now(timezone.utc).timestamp()*1000)

def get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r=requests.get(url, params=params, timeout=20)
            if r.status_code==200:
                return r.json()
            if r.status_code in (429,418):
                wait=min(30, 2**i*2); time.sleep(wait); continue
        except Exception:
            time.sleep(1)
    return None

def day_str(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')

# 1) 从本地日线取最近成交额前50的USDT永续
kl=json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
syms=[]
for sym, bars in kl.items():
    if not sym.endswith('USDT') or len(bars)<10: continue
    q=sum(b['q'] for b in bars[-5:])/5
    syms.append((sym,q))
syms.sort(key=lambda x:-x[1])
syms=[s for s,_ in syms[:TOP_N]]
print('symbols', syms)

# 2) 拉数据
rows=[]
for sym in syms:
    sp=get('https://api.binance.com/api/v3/klines', {'symbol':sym,'interval':'1d','startTime':START,'endTime':END,'limit':1000})
    fu=get('https://fapi.binance.com/fapi/v1/klines', {'symbol':sym,'interval':'1d','startTime':START,'endTime':END,'limit':1000})
    oi=get('https://fapi.binance.com/futures/data/openInterestHist', {'symbol':sym,'period':'1d','limit':DAYS+3})
    fr=get('https://fapi.binance.com/fapi/v1/fundingRate', {'symbol':sym,'startTime':START,'endTime':END,'limit':1000})
    if not sp or not fu:
        continue
    spd={day_str(int(k[0])): {'q':float(k[7]), 'tbq':float(k[10]), 'c':float(k[4])} for k in sp}
    fud={day_str(int(k[0])): {'q':float(k[7]), 'tbq':float(k[10]), 'c':float(k[4])} for k in fu}
    oid={}
    if oi:
        for x in oi:
            oid[day_str(x['timestamp'])] = float(x['sumOpenInterest'])
    frd={}
    if fr:
        for x in fr:
            frd[day_str(x['fundingTime'])] = float(x['fundingRate'])
    dates=sorted(set(spd)&set(fud))
    # 按日期顺序计算
    for i,d in enumerate(dates):
        s=spd[d]; f=fud[d]
        if s['q']<=0 or f['q']<=0: continue
        spot_tbr=s['tbq']/s['q']
        fut_tbr=f['tbq']/f['q']
        basis=(f['c']/s['c']-1)*100 if s['c']>0 else None
        prev_oi=oid.get(dates[i-1]) if i>0 else None
        oi_chg=((oid[d]/prev_oi-1)*100) if (d in oid and prev_oi and prev_oi>0) else None
        funding=frd.get(d)
        # 未来收益
        if i+1 < len(dates):
            c1=spd[dates[i+1]]['c']; ret1=(c1/s['c']-1)*100
        else:
            ret1=None
        if i+2 < len(dates):
            c2=spd[dates[i+2]]['c']; ret2=(c2/s['c']-1)*100
        else:
            ret2=None
        rows.append({'sym':sym,'date':d,'spot_tbr':spot_tbr,'fut_tbr':fut_tbr,
                     'basis':basis,'oi_chg':oi_chg,'funding':funding,
                     'ret1':ret1,'ret2':ret2})
    time.sleep(0.1)

print('总样本', len(rows))
json.dump(rows, open('/tmp/flow_ic_rows.json','w'), ensure_ascii=False, indent=1)

# 3) IC 计算
def spearman(a,b):
    pairs=[(x,y) for x,y in zip(a,b) if x is not None and y is not None]
    if len(pairs)<20: return None
    pairs.sort()
    ra={v:i for i,v in enumerate(sorted(x for x,_ in pairs))}
    rb={v:i for i,v in enumerate(sorted(y for _,y in pairs))}
    n=len(pairs)
    ma=(n-1)/2; mb=(n-1)/2
    da=[ra[x]-ma for x,_ in pairs]; db=[rb[y]-mb for _,y in pairs]
    cov=sum(x*y for x,y in zip(da,db))/n
    va=sum(x*x for x in da)/n; vb=sum(y*y for y in db)/n
    if va*vb<=0: return None
    return cov/math.sqrt(va*vb)

features=['spot_tbr','fut_tbr','basis','oi_chg','funding']
for label in ['ret1','ret2']:
    print(f'\n===== 对未来{label}的 IC (按币种均值 / 全样本) =====')
    print(f"{'特征':<12}{'有样本币数':>6}{'均值IC':>10}{'中位IC':>10}{'正IC占比':>8}{'全样本IC':>10}")
    for feat in features:
        per=[]
        for sym in set(r['sym'] for r in rows):
            a=[r[feat] for r in rows if r['sym']==sym]
            b=[r[label] for r in rows if r['sym']==sym]
            ic=spearman(a,b)
            if ic is not None: per.append(ic)
        all_a=[r[feat] for r in rows]; all_b=[r[label] for r in rows]
        all_ic=spearman(all_a,all_b)
        if per:
            mean=sum(per)/len(per); med=sorted(per)[len(per)//2]
            pos=sum(1 for x in per if x>0)/len(per)
            print(f"{feat:<12}{len(per):>6}{mean:>10.4f}{med:>10.4f}{pos:>8.0%}{all_ic if all_ic is None else round(all_ic,4):>10}")
        else:
            print(f"{feat:<12} 无数据")
