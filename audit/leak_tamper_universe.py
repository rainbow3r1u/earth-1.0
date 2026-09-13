#!/usr/bin/env python3
"""全宇宙篡改: 改掉 D 日【所有币】的 bar → 枚举全部泄漏维(含板块热度派生)"""
import os, sys, datetime, copy
import numpy as np
sys.path.insert(0,"/home/linux/websocket_new"); os.chdir("/home/linux/websocket_new")
import gpu_backtest_exp as E, daily_predictor as dp, auto_dual_trade as adt
def dt(ts):
    ts=float(ts); ts=ts/1000.0 if ts>1e11 else ts
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
kl=E.load_klines_offline(); oi=E.load_oi_offline(kl)
sm=dp._load_sector_map(); dp._sector_map_cache=sm
for k,f in [('_etf_features',dp._load_etf_features),('_chain_features',dp._load_chain_features),
            ('_sent_features',dp._load_sent_features),('_fg_features',dp._load_fear_greed),
            ('_st_features',dp._load_stablecoin_netflow),('_cb_features',dp._load_coinbase_premium),
            ('_cbg_features',dp._load_cb_gap_features),('_bd_features',dp._load_btc_mcap),
            ('_kg_features',dp._load_korea_premium),('_hr_features',dp._load_hashrate_features),
            ('_liq_features',dp._load_liquidation_features),('_tvl_features',dp._load_chain_tvl),
            ('_ma_features',dp._load_macro_assets),('_ab_features',dp._load_btc_dominance_proxy)]:
    try: setattr(dp,k,f())
    except Exception: setattr(dp,k,{})
dp._kr_features={}
D='2026-06-15'; sym='0GUSDT'
def build(kls):
    heats=dp._precompute_sector_heats(kls,sm)
    br=dp._compute_returns([k['c'] for k in kls.get('BTCUSDT',[])])
    res=adt._build_feat_impl(sym, kls[sym], oi.get(sym,{}), br, sm, heats)
    for ts,s,f,ll,ls,ret in res:
        if dt(ts)==D: return np.array(f,dtype=float)
    return None
a=build(kl)
# 全宇宙篡改 D 日 bar
kl2=copy.deepcopy(kl)
n=0
for s in kl2:
    for k in kl2[s]:
        if dt(k['t'])==D:
            k['c']*=3.0; k['h']*=3.0; k['l']*=0.5; k['q']*=5.0; k['v']*=5.0; n+=1
b=build(kl2)
nz=list(np.nonzero(np.abs(a-b)>1e-9)[0])
print(f'篡改 {n} 个币的 {D} bar → 样本({sym},{D}) 特征差异维 {len(nz)}')
print(f'  差异维: {nz}')
print(f'  判定: {"仅 4 维(13-16) → 泄漏只在价格类特征, 补丁可完全修复" if set(nz)=={13,14,15,16} else "⚠️ 还有其它维 → 需按日截断重建(含heats)"}')
