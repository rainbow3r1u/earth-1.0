#!/usr/bin/env python3
"""重放=生产 自动校验 (2026-08-05 新增)
每天 8:30 后运行: 用当天的 pred_feats_YYYY-MM-DD.npz (原始特征+winsor边界) + 生产模型 pkl
复刻生产 SOUP 预测, 与 pred_YYYY-MM-DD.json 的 TOP1 对比。
依赖: auto_dual_trade.py 已实现预测特征存档 (data/pred_feats_*.npz)。
用法: python3 audit/replay_verify.py [YYYY-MM-DD] [--npz 自定义路径]
"""
import os, sys, json, glob, pickle, argparse
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, '/home/myuser/websocket_new')
import auto_dual_trade as adt

def day_ts(s):
    return int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', nargs='?', default=datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    ap.add_argument('--npz', default=None)
    args = ap.parse_args()
    ds = args.date
    tag = ds.replace('-', '')
    d = day_ts(ds)
    ROOT = '/home/myuser/websocket_new'
    npz_path = args.npz or f'{ROOT}/data/pred_feats_{ds}.npz'
    pred_path = f'{ROOT}/data/pred_{ds}.json'

    if not os.path.exists(npz_path):
        print(f'[{ds}] 无预测特征存档: {npz_path} (今日 8:05 起才有)', flush=True)
        return 1
    if not os.path.exists(pred_path):
        print(f'[{ds}] 无生产预测存档: {pred_path}', flush=True)
        return 1

    z = np.load(npz_path, allow_pickle=True)
    syms = [str(s) for s in z['syms']]
    feats = z['feats'].astype(np.float32)
    bounds = z['bounds'].tolist()
    print(f'[{ds}] 特征存档: {len(syms)} 币, feats={feats.shape}, bounds={len(bounds)}', flush=True)

    # 生产 X_pred 处理: nan填充 → winsor(当日边界) → 死列置零
    Xp = np.nan_to_num(feats, nan=0.0, copy=False)
    Xp = adt.dp._apply_winsor(Xp, bounds)
    Xp[:, 100:932] = 0.0
    Xp[:, 72:91] = 0.0
    if os.environ.get('ETF_ON', '0') != '1':
        Xp[:, 46:48] = 0.0

    # 生产 SOUP: 今日 + 最近2个历史模型 (与 train_and_predict 的 SOUP 逻辑一致)
    models_dir = os.path.join(os.path.expanduser('~/.local/share/auto_trade'), 'models')
    def soup(side):
        files = sorted(glob.glob(f'{models_dir}/xgb_daily_{side}_2*.pkl'), reverse=True)
        ps = []
        for f in files:
            dt = os.path.basename(f).split('_')[-1].replace('.pkl', '')
            if dt <= tag:
                with open(f, 'rb') as fh:
                    ps.append(pickle.load(fh).predict_proba(Xp)[:, 1])
            if len(ps) >= 3:
                break
        return np.mean(ps, axis=0) if ps else None

    pl, ps = soup('long'), soup('short')
    if pl is None or ps is None:
        print(f'[{ds}] 模型缺失, 无法校验', flush=True)
        return 1

    # 生产有效性过滤 (K线>=30 + 预测日前5日均成交额>=MIN_VOLUME_24H)
    with open(adt.KLINE_CACHE_FILE) as f:
        klines = json.load(f)['klines']
    valid = np.zeros(len(syms), dtype=bool)
    for i, sym in enumerate(syms):
        kls = klines.get(sym, [])
        if len(kls) < 30:
            continue
        k_idx = next((j for j, k in enumerate(kls) if int(k['t']) >= d * 1000), len(kls))
        if k_idx < 5:
            continue
        if np.mean([k['q'] for k in kls[k_idx-5:k_idx]]) >= adt.MIN_VOLUME_24H:
            valid[i] = True
    pl = np.where(valid, pl, -1.0)
    ps = np.where(valid, ps, -1.0)
    il, is_ = int(np.argmax(pl)), int(np.argmax(ps))

    prod = json.load(open(pred_path))
    bl, bs = prod.get('best_long'), prod.get('best_short')
    def rank_of(sym, probs):
        if sym not in syms:
            return None
        return sorted(range(len(probs)), key=lambda i: -probs[i]).index(syms.index(sym))

    ok_l = bool(bl and bl['symbol'] == syms[il])
    ok_s = bool(bs and bs['symbol'] == syms[is_])
    rl = rank_of(bl['symbol'], pl) if bl else None
    rs_ = rank_of(bs['symbol'], ps) if bs else None
    line = (f'[{ds}] LONG 重放={syms[il]} {pl[il]*100:.1f}% vs 生产={bl["symbol"] if bl else None} '
            f'{bl.get("prob") if bl else ""}% {"✓" if ok_l else "✗"}(rank={rl}) | '
            f'SHORT 重放={syms[is_]} {ps[is_]*100:.1f}% vs 生产={bs["symbol"] if bs else None} '
            f'{bs.get("prob") if bs else ""}% {"✓" if ok_s else "✗"}(rank={rs_})')
    print(line, flush=True)
    with open(f'{ROOT}/logs/replay_verify.log', 'a') as f:
        f.write(datetime.now(timezone.utc).isoformat() + ' ' + line + '\n')
    return 0 if (ok_l and ok_s) else 2

if __name__ == '__main__':
    sys.exit(main())
