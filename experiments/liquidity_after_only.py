#!/usr/bin/env python3
"""单独跑 After 流动性回测 (从已保存的排名文件读取)"""
import os, sys, json, math, numpy as np
from collections import defaultdict
from xgboost import XGBClassifier

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import daily_predictor as dp

RECENT_DAYS=90; STOP_LOSS=10.0; TAKE_PROFIT=10.0; PROB_THRESHOLD=60.0
TRADE_COST=0.2; TRAIN_DAYS=180; MIN_HISTORY=60; MIN_VOLUME=500000; TOP_N=150

def log(msg): print(msg, flush=True)

def check_exit(kls_daily, entry_i, entry_price, direction):
    if direction=='long':
        sp=entry_price*(1-STOP_LOSS/100); tp=entry_price*(1+TAKE_PROFIT/100)
        for off in [1,2]:
            idx=entry_i+off
            if idx>=len(kls_daily): continue
            k=kls_daily[idx]; lo=k['l']; hi=k['h']
            if lo<=sp: return True,-STOP_LOSS,off,'stop'
            if hi>=tp: return True,+TAKE_PROFIT,off,'take'
        idx=entry_i+2
        if idx<len(kls_daily):
            return False,(kls_daily[idx]['c']/entry_price-1)*100,2,'hold'
        return False,0,0,'expire'
    else:
        sp=entry_price*(1+STOP_LOSS/100); tp=entry_price*(1-TAKE_PROFIT/100)
        for off in [1,2]:
            idx=entry_i+off
            if idx>=len(kls_daily): continue
            k=kls_daily[idx]; hi=k['h']; lo=k['l']
            if hi>=sp: return True,-STOP_LOSS,off,'stop'
            if lo<=tp: return True,+TAKE_PROFIT,off,'take'
        idx=entry_i+2
        if idx<len(kls_daily):
            return False,(entry_price-kls_daily[idx]['c'])/entry_price*100,2,'hold'
        return False,0,0,'expire'

def load_rankings(path):
    """加载排名JSON: {day_ts: [sym1, sym2, ...]}"""
    with open(path) as f:
        raw = json.load(f)
    # 转换: str keys → int, list → set
    return {int(k): set(v) for k, v in raw.items()}

base = os.path.dirname(__file__)
data_dir = os.path.join(base, '..', 'data')

# 加载预保存数据
log("加载预计算数据...")
with open(os.path.join(data_dir, 'liq_klines_filtered.json')) as f:
    kf = json.load(f)
log(f"  {len(kf)} 币种")

with open(os.path.join(data_dir, 'liq_samples.json')) as f:
    all_s = json.load(f)
by_day = defaultdict(list)
for ts,sym,feat,ll,ls,ret in all_s:
    by_day[ts].append((sym,feat,ll,ls,ret))
sd = sorted(by_day.keys())
rd = sd[-RECENT_DAYS:]
log(f"  {len(sd)}d, {len(all_s)}条, recent={len(rd)}d")

lrank = load_rankings(os.path.join(data_dir, 'liq_rankings.json'))
log(f"  {len(lrank)}天排名")

ta = []
for di, pred_ts in enumerate(rd):
    if di%25==0: log(f"  After(Liq): {di}/{len(rd)} days")
    eligible = lrank.get(pred_ts, set())
    if not eligible: continue
    train_ts = [ts for ts in sorted(by_day.keys()) if ts < pred_ts][-TRAIN_DAYS:]
    if len(train_ts)<10: continue
    X_tr,y_l,y_s=[],[],[]
    for ts in train_ts:
        if ts+2*86400>pred_ts: continue
        for sym,feat,ll,ls,ret in by_day[ts]:
            if sym not in eligible: continue
            X_tr.append(feat); y_l.append(ll); y_s.append(ls)
    if not X_tr: continue
    X_tr=np.array(X_tr)
    bds=[(float(np.percentile(X_tr[:,j],1)),float(np.percentile(X_tr[:,j],99))) for j in range(X_tr.shape[1])]
    X_tr=dp._apply_winsor(X_tr,bds)
    pls=sum(y_l); pss=sum(y_s)
    if pls<5 or pss<5: continue
    ml=XGBClassifier(n_estimators=200,max_depth=5,learning_rate=0.05,
        scale_pos_weight=(len(y_l)-pls)/pls,random_state=42,eval_metric='logloss',verbosity=0,n_jobs=1)
    ml.fit(X_tr,y_l)
    ms=XGBClassifier(n_estimators=200,max_depth=5,learning_rate=0.05,
        scale_pos_weight=(len(y_s)-pss)/pss,random_state=43,eval_metric='logloss',verbosity=0,n_jobs=1)
    ms.fit(X_tr,y_s)
    ps=by_day[pred_ts]
    X_p=np.array([s[1] for s in ps])
    X_p=dp._apply_winsor(X_p,bds)
    pl=ml.predict_proba(X_p)[:,1]; ps2=ms.predict_proba(X_p)[:,1]
    bl=None; bs=None
    for idx,((sym,feat,ll,ls,ret),p1,p2) in enumerate(zip(ps,pl,ps2)):
        if sym not in eligible: continue
        kd=kf.get(sym,[]); 
        if len(kd)<MIN_HISTORY: continue
        tl=[k['t']//1000 for k in kd]
        try: ki=tl.index(pred_ts)
        except: continue
        if ki<5: continue
        vv=[kd[j]['q'] for j in range(ki-5,ki)]
        if np.mean(vv)<MIN_VOLUME: continue
        if bl is None or p1>bl[1]: bl=(sym,p1,ret)
        if bs is None or p2>bs[1]: bs=(sym,p2,ret)
    if bl is None and bs is None: continue
    lp=bl[1]*100 if bl else 0; sp2=bs[1]*100 if bs else 0
    if max(lp,sp2)<PROB_THRESHOLD: continue
    if bl and (not bs or lp>=sp2): sym,prob,ret=bl; direction='long'
    else: sym,prob,ret=bs; direction='short'
    kd=kf[sym]; tl=[k['t']//1000 for k in kd]
    try: ki=tl.index(pred_ts)
    except: continue
    ep=kd[ki]['c']
    hit,ret_pct,days,reason=check_exit(kd,ki,ep,direction)
    ta.append({'ts':pred_ts,'sym':sym,'dir':direction,'ret':ret_pct-TRADE_COST,
        'hit':hit,'days':days,'reason':reason,'prob':prob*100})

# 汇总
rets=[t['ret'] for t in ta]; cum=sum(rets)
sharpe=np.mean(rets)/np.std(rets)*np.sqrt(365/max(len(ta),1)) if np.std(rets)>0 else 0
wr=sum(1 for r in rets if r>0)/len(ta)*100 if ta else 0
stops=sum(1 for t in ta if t.get('reason')=='stop')
peak=0; running=0; max_dd=0
for r in rets:
    running+=r
    if running>peak: peak=running
    dd=(peak-running)/max(abs(peak),1)*100 if peak>0 else 0
    if dd>max_dd: max_dd=dd

log(f"\nAfter(Liq): {len(ta)}笔 Sharpe={sharpe:.2f} 累计={cum:+.1f}% 胜率={wr:.1f}% 止损={stops} 回撤={max_dd:.1f}%")

# 读取Before对比
with open(os.path.join(data_dir,'liquidity_backtest.json')) as f:
    r = json.load(f)
before = r['before']
log(f"\n{'='*50}")
log(f"  对比:")
log(f"  Before(量):  Sharpe={before['sharpe']:.2f} 累计={before['cum']:+.1f}% 胜率={before['wr']}% 止损={before['stops']}")
log(f"  After(流动性): Sharpe={sharpe:.2f} 累计={cum:+.1f}% 胜率={wr:.1f}% 止损={stops}")

r['after'] = {'sharpe':round(sharpe,2),'cum':round(cum,2),'trades':len(ta),'wr':round(wr,1),'stops':stops,'max_dd':round(max_dd,1)}
r['status'] = 'done'
with open(os.path.join(data_dir,'liquidity_backtest.json'),'w') as f:
    json.dump(r, f, indent=2)
log("结果已保存")
