#!/usr/bin/env python3
"""决定性检验: 篡改样本日D的K线, 看D日特征是否变化 → 变了=特征用到当日bar(前视)"""
import os, sys, datetime, copy
import numpy as np
sys.path.insert(0,"/home/linux/websocket_new"); os.chdir("/home/linux/websocket_new")
import gpu_backtest_exp as E
import daily_predictor as dp
import auto_dual_trade as adt
def dt(ts):
    ts=float(ts); ts=ts/1000.0 if ts>1e11 else ts
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
kl=E.load_klines_offline(); oi=E.load_oi_offline(kl)
sm=dp._load_sector_map(); dp._sector_map_cache=sm
heats=dp._precompute_sector_heats(kl,sm)
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
btc=kl.get('BTCUSDT',[]); br=dp._compute_returns([k['c'] for k in btc])
def ts2s(t):
    t=float(t); return t/1000.0 if t>1e11 else t
def feat_for(sym, kls_dict, D, brr):
    res=adt._build_feat_impl(sym, kls_dict[sym], oi.get(sym,{}), brr, sm, heats)
    for ts,s,f,ll,ls,ret in res:
        if dt(ts)==D: return np.array(f,dtype=float)
    return None
for D in ('2026-06-15','2026-07-15'):
    sym='0GUSDT'
    base=feat_for(sym, kl, D, br)
    # 篡改 D 当日 bar (该币 + BTC)
    kl2=copy.deepcopy(kl)
    for s in (sym,'BTCUSDT'):
        for k in kl2.get(s,[]):
            if dt(k['t'])==D:
                k['c']*=3.0; k['h']*=3.0; k['l']*=0.5; k['q']*=5.0; k['v']*=5.0
    br2=dp._compute_returns([k['c'] for k in kl2['BTCUSDT']])
    mod=feat_for(sym, kl2, D, br2)
    if base is None or mod is None:
        print(f'{D}: 取样本失败'); continue
    nz=np.nonzero(np.abs(base-mod)>1e-9)[0]
    print(f'{D} {sym}: 篡改当日K线后, 该日特征差异维 {len(nz)}/946  最大差 {np.abs(base-mod).max():.4f}')
    if len(nz): print(f'   差异维样例: {[(int(j), round(float(base[j]),4), round(float(mod[j]),4)) for j in nz[:8]]}')
print()
print('判定: 若差异>0 → 特征使用了样本日当日K线 = 相对08:21入场的前视')
