#!/usr/bin/env python3
"""
GPU 365天流动性回测 — 含 Kronos 832维特征
"""
import os, sys, json, math, time, numpy as np
from datetime import datetime, timezone
from collections import defaultdict
from xgboost import XGBClassifier

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import daily_predictor as dp

KLINE_CACHE = '/root/reasonix-projects/backtester/data_cache/notusdt_1d_full.json'
RECENT_DAYS = 365; STOP_LOSS = 10.0; TAKE_PROFIT = 10.0; PROB_THRESHOLD = 60.0
TRADE_COST = 0.2; TRAIN_DAYS = 365; MIN_HISTORY = 60; MIN_VOLUME = 500000
TOP_N = 150; LOOKBACK = 30

def log(msg): print(msg, flush=True)

# ===== 流动性评分 =====
def liq_score(kls_30d):
    c=[k['c'] for k in kls_30d]; h=[k['h'] for k in kls_30d]; l=[k['l'] for k in kls_30d]
    v=[k['q'] for k in kls_30d]
    rng=[(h[i]-l[i])/c[i] if c[i]>0 else 0 for i in range(len(c))]
    rets=[(c[i]-c[i-1])/c[i-1] if c[i-1]>0 else 0 for i in range(1,len(c))]
    avg_r=np.mean(rng)*100; vol_r=np.std(rets)*100 if rets else 0
    av=np.mean(v); cv_v=np.std(v)/av if av>0 else 999
    return (avg_r*0.4+vol_r*0.4)*math.log(max(av,1))/(1+cv_v)

def vol_score(kls_30d):
    return np.mean([k['q'] for k in kls_30d])

# ===== 日级排名 =====
def precompute_rankings(full):
    days=set()
    for kls in full.values():
        for k in kls: days.add(k['t']//1000)
    all_days=sorted(days)
    vrank={}; lrank={}; union=set()
    for di,day_ts in enumerate(all_days):
        sv=[]; sl=[]
        for sym,kls in full.items():
            w=[k for k in kls if k['t']//1000<day_ts][-LOOKBACK:]
            if len(w)<LOOKBACK: continue
            sv.append((sym,vol_score(w))); sl.append((sym,liq_score(w)))
        sv.sort(key=lambda x:-x[1]); sl.sort(key=lambda x:-x[1])
        vrank[day_ts]=set(s[0] for s in sv[:TOP_N])
        lrank[day_ts]=set(s[0] for s in sl[:TOP_N])
        union.update(vrank[day_ts]); union.update(lrank[day_ts])
        if di%500==0: log(f'  排名: {di}/{len(all_days)}')
    log(f'  排名完成: {len(all_days)}d, union={len(union)}币')
    return vrank,lrank,all_days,union

# ===== 样本构建 (复用 daily_predictor 全管线) =====
def build_samples_full(full, oi_data, sector_map, sector_heats_all, btc_rets):
    """用 daily_predictor 的方式构建样本，含 Kronos"""
    all_s=[]
    for sym,kls in full.items():
        if len(kls)<MIN_HISTORY: continue
        oi_map=oi_data.get(sym,{})
        c=[k['c'] for k in kls]; o=[k['o'] for k in kls]; h=[k['h'] for k in kls]; l=[k['l'] for k in kls]
        v=[k['q'] for k in kls]; timestamps=[k['t']//1000 for k in kls]
        crets=dp._compute_returns(c); n=len(kls)
        for i in range(25,n-2):
            j=i-1
            try:
                r1=(c[j]-c[j-1])/c[j-1] if c[j-1]>0 else 0
                r3=(c[j]-c[max(0,j-3)])/c[max(0,j-3)] if c[max(0,j-3)]>0 else 0
                r5=(c[j]-c[max(0,j-5)])/c[max(0,j-5)] if c[max(0,j-5)]>0 else 0
                v20=np.std([(c[k]-c[k-1])/c[k-1] if c[k-1]>0 else 0 for k in range(j-18,j+1)]) if j>=20 else 0.02
                vf=max(v20,0.002)
                r1n=round(r1/vf,4); r3n=round(r3/(vf*1.732),4); r5n=round(r5/(vf*2.236),4)
                vola=np.std([(c[k]-c[k-1])/c[k-1] if c[k-1]>0 else 0 for k in range(j-3,j+1)]) if j>=5 else 0
                vr=v[j]/np.mean(v[max(0,j-5):j]) if j>=5 and np.mean(v[max(0,j-5):j])>0 else 1
                c20=c[j-19:j+1] if j>=20 else c[:j+1]
                pp=(c[j]-min(c20))/(max(c20)-min(c20)) if max(c20)!=min(c20) else 0.5
                amp=(h[j]-l[j])/o[j] if o[j]>0 else 0
                streak=sum(1 for k in range(j,max(0,j-7)-1,-1) if c[k]>o[k])
                div=1 if (c[j]>c[j-3] and v[j]<v[j-3]*0.7) else 0
                ts=timestamps[i]
                oin=oi_map.get(timestamps[j],0); oip=oi_map.get(timestamps[j-1],0)
                oic=(oin-oip)/oip if oip>0 else 0
                if sym=='BTCUSDT': b,a,r2,res=1.0,0.0,1.0,0.0
                else: b,a,r2,res=dp._regression_features(btc_rets,crets,j)
                sf=dp._get_sector_features(sym,ts-86400,sector_map,sector_heats_all)
                # 用已加载的全局缓存 (对齐 daily_predictor.py:321-338)
                from datetime import timezone as tz
                prev_date = datetime.fromtimestamp(ts - 86400, tz=tz.utc).strftime('%Y-%m-%d')
                etf = dp._etf_features.get(prev_date, [0]*2)
                chain = dp._chain_features.get(prev_date, [0]*4)
                sent = dp._sent_features.get(prev_date, [0]*6)
                fg = dp._fg_features.get(prev_date, [0]*1)
                st = dp._st_features.get(prev_date, [0]*3)
                cb = dp._cb_features.get(prev_date, [0]*3)
                cbg = dp._cbg_features.get(prev_date, [0]*3)
                bd = dp._bd_features.get(prev_date, [0]*3)
                kg = dp._kg_features.get(prev_date, [0]*3)
                hr = dp._hr_features.get(prev_date, [0]*3)
                liq = dp._liq_features.get(prev_date, [0]*7)
                tvl = dp._tvl_features.get(prev_date, [0]*6)
                ma = dp._ma_features.get(prev_date, [0]*3)
                ab = dp._ab_features.get(ts, [0]*1)
                # 确保所有部分都是list
                def _as_list(v, n=0):
                    if isinstance(v, list): 
                        if len(v) == n: return v
                        return (v + [0]*(n-len(v)))[:n]
                    if isinstance(v, (int, float)): return [float(v)] + [0.0]*(n-1)
                    return [0.0]*n
                mf = (_as_list(etf,2) + _as_list(chain,4) + _as_list(sent,6) + _as_list(fg,1) + 
                      _as_list(st,3) + _as_list(cb,3) + _as_list(cbg,3) + _as_list(bd,3) + 
                      _as_list(kg,3) + _as_list(hr,3) + _as_list(liq,7) + _as_list(tvl,6) + 
                      _as_list(ma,3) + _as_list(ab,1))
                rsi7=dp._compute_rsi(c,7,j); rsi14=dp._compute_rsi(c,14,j); rsi30=dp._compute_rsi(c,30,j)
                rs=dp._compute_rsi_series(c,14); rd=dp._compute_rsi_divergence(c,rs,j,window=20)
                vc=dp._compute_vol_clustering(c,j)
                feat = [r1n,r3n,r5n,vola,vr,pp,amp,streak,div,oic]+vc+[b,a,r2,res,rsi7,rsi14,rsi30]+rd+sf+mf
                # 加 Kronos
                kronos_feat = list(dp._kr_features.get(ts, [0.0]*832))[:832]
                while len(kronos_feat) < 832:
                    kronos_feat.append(0.0)
                feat.extend(kronos_feat[:832])
                nr=(c[i+1]-c[j])/c[j] if c[j]>0 and i+1<n else 0
                if abs(nr)>5.0: continue
                ll=1 if nr>0.05 else 0; ls=1 if nr<-0.05 else 0
                all_s.append((ts,sym,feat,ll,ls,nr*100))
            except: continue
    return all_s

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
        if idx<len(kls_daily): return False,(kls_daily[idx]['c']/entry_price-1)*100,2,'hold'
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
        if idx<len(kls_daily): return False,(entry_price-kls_daily[idx]['c'])/entry_price*100,2,'hold'
        return False,0,0,'expire'

def run_bt(by_day, recent_days, klines, rankings, label):
    trades=[]
    for di, pred_ts in enumerate(recent_days):
        if di%50==0: log(f'  {label}: {di}/{len(recent_days)}')
        eligible=rankings.get(pred_ts,set())
        if not eligible: continue
        train_ts=[ts for ts in sorted(by_day.keys()) if ts<pred_ts][-TRAIN_DAYS:]
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
            scale_pos_weight=(len(y_l)-pls)/pls,random_state=42,eval_metric='logloss',verbosity=0,
            device='cuda',n_jobs=12)
        ml.fit(X_tr,y_l)
        ms=XGBClassifier(n_estimators=200,max_depth=5,learning_rate=0.05,
            scale_pos_weight=(len(y_s)-pss)/pss,random_state=43,eval_metric='logloss',verbosity=0,
            device='cuda',n_jobs=12)
        ms.fit(X_tr,y_s)
        ps=by_day[pred_ts]; X_p=np.array([s[1] for s in ps]); X_p=dp._apply_winsor(X_p,bds)
        pl=ml.predict_proba(X_p)[:,1]; ps2=ms.predict_proba(X_p)[:,1]
        bl=None; bs=None
        for idx,((sym,feat,ll,ls,ret),p1,p2) in enumerate(zip(ps,pl,ps2)):
            if sym not in eligible: continue
            kd=klines.get(sym,[]);
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
        kd=klines[sym]; tl=[k['t']//1000 for k in kd]
        try: ki=tl.index(pred_ts)
        except: continue
        ep=kd[ki]['c']
        hit,ret_pct,days,reason=check_exit(kd,ki,ep,direction)
        trades.append({'ts':pred_ts,'sym':sym,'dir':direction,'ret':ret_pct-TRADE_COST,
            'hit':hit,'days':days,'reason':reason,'prob':prob*100})
    return trades

def summ(trades,label):
    if not trades: log(f'\n{label}: 无交易'); return 0,0,0,0,0
    rets=[t['ret'] for t in trades]; cum=sum(rets)
    sharpe=np.mean(rets)/np.std(rets)*np.sqrt(365/max(len(trades),1)) if np.std(rets)>0 else 0
    wr=sum(1 for r in rets if r>0)/len(rets)*100
    stops=sum(1 for t in trades if t.get('reason')=='stop')
    log(f'{label}: {len(trades)}笔 Sharpe={sharpe:.2f} 累计={cum:+.1f}% 胜率={wr:.1f}% 止损={stops}')
    return sharpe,cum,wr,stops

def main():
    t0=time.time()
    log("="*60)
    log(" 流动性筛选 365天回测 (含Kronos 832维)")
    log("="*60)
    
    log("[1/6] 加载K线...")
    with open(KLINE_CACHE) as f: full=json.load(f)['klines']
    log(f"  {len(full)}币种")
    
    log("[2/6] 预计算排名...")
    vrank,lrank,all_days,union=precompute_rankings(full)
    
    log("[3/6] 预计算Kronos...")
    all_ts=set()
    for kls in full.values():
        for k in kls: all_ts.add(k['t']//1000)
    dp._precompute_kronos_features(list(all_ts))
    log(f"  Kronos缓存: {len(dp._kr_features)}天")
    
    # 只对union中的币种构建样本 (大幅缩减)
    full_filtered = {s:full[s] for s in union if s in full}
    log(f"[4/6] 构建样本 (仅{len(full_filtered)}币, 含Kronos)...")
    import requests
    try:
        resp=requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo',timeout=15)
        fsyms=[s['symbol'] for s in resp.json()['symbols'] if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT']
    except: fsyms=list(full_filtered.keys())
    oi_data=dp.fetch_oi(fsyms)
    smap=dp._load_sector_map(); dp._sector_map_cache=smap
    if not dp._proto_map_local:
        try:
            with open('/root/defillama_data/protocol_map.json') as pf:
                dp._proto_map_local={k:v[0] for k,v in json.load(pf).items()}
        except: pass
    sheats=dp._precompute_sector_heats(full_filtered,smap) if smap else {}
    dp._etf_features=dp._load_etf_features(); dp._chain_features=dp._load_chain_features()
    dp._sent_features=dp._load_sent_features(); dp._fg_features=dp._load_fear_greed()
    dp._st_features=dp._load_stablecoin_netflow(); dp._cb_features=dp._load_coinbase_premium()
    dp._cbg_features=dp._load_cb_gap_features(); dp._bd_features=dp._load_btc_mcap()
    dp._kg_features=dp._load_korea_premium(); dp._hr_features=dp._load_hashrate_features()
    dp._liq_features=dp._load_liquidation_features(); dp._tvl_features=dp._load_chain_tvl()
    dp._ma_features=dp._load_macro_assets(); dp._ab_features=dp._load_btc_dominance_proxy()
    btc_kls=full_filtered.get('BTCUSDT',[]); btc_c=[k['c'] for k in btc_kls]
    btc_rets=dp._compute_returns(btc_c) if len(btc_c)>1 else []
    
    all_s=build_samples_full(full_filtered,oi_data,smap,sheats,btc_rets)
    by_day=defaultdict(list)
    for ts,sym,feat,ll,ls,ret in all_s: by_day[ts].append((sym,feat,ll,ls,ret))
    sd=sorted(by_day.keys()); rd=sd[-RECENT_DAYS:]
    log(f"  样本: {len(sd)}d, {len(all_s)}条, recent={len(rd)}d")
    n_feat=len(all_s[0][2]) if all_s else 0
    log(f"  特征维度: {n_feat}")
    
    log(f"[5/6] Before: Top{TOP_N} 按量...")
    tb=run_bt(by_day,rd,full_filtered,vrank,"Before(Vol)")
    s1,c1,wr1,st1=summ(tb,"Before(Vol)")
    
    # 中间保存
    import gc; gc.collect()
    log(f"[6/6] After:  Top{TOP_N} 按流动性...")
    ta=run_bt(by_day,rd,full_filtered,lrank,"After(Liq)")
    s2,c2,wr2,st2=summ(ta,"After(Liq)")
    
    log(f"\n{'='*60}")
    log(f"  对比总结 (耗时 {(time.time()-t0)/60:.0f}min, {n_feat}维)")
    log(f"{'='*60}")
    log(f"  指标            Before(量)     After(流动性)     变化")
    log(f"  总收益          {c1:>+10.1f}%     {c2:>+10.1f}%    {c2-c1:>+8.1f}%")
    log(f"  Sharpe          {s1:>10.2f}       {s2:>10.2f}      {s2-s1:>+8.2f}")
    log(f"  交易次数        {len(tb):>10}       {len(ta):>10}      {len(ta)-len(tb):>+8}")
    log(f"  胜率            {wr1:>9.1f}%      {wr2:>9.1f}%     {wr2-wr1:>+8.1f}%")
    log(f"  止损次数        {st1:>10}       {st2:>10}      {st2-st1:>+8}")
    
    result_file='/root/reasonix-projects/websocket_new/data/liquidity_kronos_bt.json'
    with open(result_file,'w') as f:
        json.dump({
            'before':{'sharpe':round(s1,2),'cum':round(c1,2),'trades':len(tb),'wr':round(wr1,1),'stops':st1},
            'after':{'sharpe':round(s2,2),'cum':round(c2,2),'trades':len(ta),'wr':round(wr2,1),'stops':st2},
            'config':{'dims':n_feat,'days':RECENT_DAYS,'train':TRAIN_DAYS,'top_n':TOP_N}
        },f,indent=2)
    log(f"\n结果已保存: {result_file}")

if __name__=='__main__':
    try:
        main()
    except Exception as e:
        import traceback
        log(f"FATAL: {e}")
        traceback.print_exc()
