#!/usr/bin/env python3
"""彻底重架: 把缓存中 dims13-16 替换为"前一日样本值"(= 生产当日08:05可得口径), 输出 _noleak 缓存"""
import os, glob, numpy as np, time
D="/home/linux/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund"
OUT="/home/linux/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund_noleak"
LEAK=[13,14,15,16]
os.makedirs(OUT, exist_ok=True)
fs=sorted(glob.glob(D+"/*.npz"), key=lambda p:int(os.path.basename(p)[:-4]))
t0=time.time(); prev={}; nfix=0; nfile=0
for i,p in enumerate(fs):
    z=np.load(p, allow_pickle=True)
    syms=[str(s) for s in z["syms"]]; F=z["feats"].copy(); lab=z["labels"]
    F2=F.astype(np.float64)
    for k,s in enumerate(syms):
        if s in prev:
            F2[k,LEAK]=prev[s]; nfix+=1
    prev={s:F2[k,LEAK].copy() for k,s in enumerate(syms)}
    np.savez_compressed(f"{OUT}/{os.path.basename(p)}", feats=F2.astype(np.float32), labels=lab, syms=z["syms"])
    nfile+=1
    if nfile%500==0:
        print(f"  {nfile}/{len(fs)} 已修 {nfix} 样本  用时 {time.time()-t0:.0f}s", flush=True)
print(f"完成: {nfile} 天, 修正 {nfix} 个(币,日)样本, 用时 {time.time()-t0:.0f}s -> {OUT}")
