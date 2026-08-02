#!/usr/bin/env python3
"""生产全链路审计 · 阶段2: 运行后校验 (08:25 cron)
对比 train_data_latest.npz 的抽查样本特征, 与:
  A) 运行前快照 K 线算出的理论值 (验证"内存旧版"假设)
  B) 运行后磁盘 K 线算出的理论值 (验证"当前代码+磁盘"一致性)
输出判定: npz 特征匹配 A / 匹配 B / 都不匹配 — 定位偏差环节。
只读, 不改生产。
"""
import os, json, glob, numpy as np
from datetime import datetime, timezone

AUDIT_DIR = '/home/myuser/websocket_new/audit'
today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
KLINE_FILE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
NPZ_FILE = '/home/myuser/.local/share/auto_trade/train_data_latest.npz'

def log(m):
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S")}] {m}', flush=True)

def compute_expected(closes_list, ts_ms):
    """复刻 _build_feat_impl 的 ret_1d_norm 计算 (特征日 = ts_ms 对应蜡烛)
    返回 (ret_1d_norm, ret_1d, vol_20d)"""
    ts_list = [r['t'] // 1000 for r in closes_list]
    j = ts_list.index(ts_ms // 1000)
    c = [r['c'] for r in closes_list]
    ret_1d = (c[j] - c[j-1]) / c[j-1] if c[j-1] > 0 else 0
    rets20 = [(c[x] - c[x-1]) / c[x-1] for x in range(j-18, j+1)] if j >= 18 else [0]
    vol20 = float(np.std(rets20)) if j >= 18 else 0.02
    clip = max(vol20, 0.002)
    return ret_1d / clip, ret_1d, vol20

# 1. 读运行前快照
snap_file = f'{AUDIT_DIR}/snapshot_{today}.json'
if not os.path.exists(snap_file):
    log(f'[AUDIT-2] 无快照 {snap_file}, 跳过 (检查 audit_snapshot.py cron 是否运行)')
    raise SystemExit(1)
snap = json.load(open(snap_file))
log(f'快照 ts={snap["ts"]}, klines_md5={snap["md5"]["klines"]}')

# 2. 读 npz, 抽查 0GUSDT(样本日 7/29, 特征日 7/28)
p = np.load(NPZ_FILE)
pX = p['X_train'].astype(np.float32)
log(f'npz: {len(pX)} 行, train_days 首尾: '
    f'{datetime.fromtimestamp(int(p["train_days"][0]), tz=timezone.utc).strftime("%m-%d")}~'
    f'{datetime.fromtimestamp(int(p["train_days"][-1]), tz=timezone.utc).strftime("%m-%d")}')

# 用快照 K 线中的 0GUSDT 找到特征日蜡烛 (取快照里 o/c/q 对应的 t)
probe = snap['probe_klines']['0GUSDT']
# npz 训练窗最后样本日 = 7/30, 特征日 = 7/29 及以前; 快照最近6天 = 7/28~8/2
# 候选特征日: 7/28、7/29 (倒数第6、5根), 这些样本日(7/29、7/30)在 npz 训练窗内
candidates = []
for idx in (-6, -5):  # 07-28, 07-29
    kk = probe[idx]
    candidates.append({
        'feat_date': datetime.fromtimestamp(kk['t']//1000, tz=timezone.utc).strftime('%m-%d'),
        'ts_ms': kk['t'], 'c': kk['c'], 'q': float(kk['q']),
    })
log('候选特征日: ' + ', '.join(f"{c['feat_date']}(q={c['q']:.2f})" for c in candidates))

# npz 里 944 == q 的行 (逐个候选找)
npz_col0 = None; chosen = None
for c in candidates:
    cand = np.where(pX[:, 944] == c['q'])[0]
    if len(cand):
        npz_col0 = float(pX[cand[0], 0])
        chosen = c
        log(f'npz 匹配: 特征日 {c["feat_date"]} → 行 {cand[0]}, 列0={npz_col0:.4f}')
        break
    else:
        log(f'npz 中无 944 == q({c["q"]:.2f}) 的行 (特征日 {c["feat_date"]})')
if chosen is None:
    log('!! 所有候选特征日均未匹配 (npz 特征日偏移或 944 被裁剪)')

# 3. 理论值 A: 用快照 K 线 (运行前版本)
if chosen:
    exp_a, ret_a, vol_a = compute_expected(probe, chosen['ts_ms'])
    log(f'理论A(快照/运行前K线): 特征日{chosen["feat_date"]} 列0={exp_a:.4f} (ret_1d={ret_a:.4f} vol20={vol_a:.4f})')

    # 4. 理论值 B: 用当前磁盘 K 线 (运行后版本)
    with open(KLINE_FILE) as f:
        klines_now = json.load(f)['klines']
    k_now = klines_now.get('0GUSDT', [])
    exp_b, ret_b, vol_b = compute_expected(k_now, chosen['ts_ms'])
    log(f'理论B(磁盘/运行后K线): 列0={exp_b:.4f} (ret_1d={ret_b:.4f} vol20={vol_b:.4f})')

    # 4b. 理论值 C: 币安 API 实时 K 线 (生产训练实际数据源, 8/2 审计确认 fetch_klines_full 直连 API)
    import requests
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/klines',
            params={'symbol': '0GUSDT', 'interval': '1d', 'limit': 1500}, timeout=15)
        api_kl = [{'t': int(x[0]), 'o': float(x[1]), 'h': float(x[2]), 'l': float(x[3]),
                   'c': float(x[4]), 'q': float(x[7])} for x in r.json()]
        exp_c, ret_c, vol_c = compute_expected(api_kl, chosen['ts_ms'])
        log(f'理论C(币安API实时): 列0={exp_c:.4f} (ret_1d={ret_c:.4f} vol20={vol_c:.4f})')
    except Exception as e:
        exp_c = None
        log(f'理论C: 获取失败 {e}')

    # 5. 判定
    verdict = 'UNKNOWN'
    if npz_col0 is not None:
        if exp_c is not None and abs(npz_col0 - exp_c) < 0.01:
            verdict = 'MATCH_C_币安API版 → npz 与 API 实时 K 线一致 (偏差非 K 线来源, 查其他环节)'
        elif abs(npz_col0 - exp_a) < 0.01:
            verdict = 'MATCH_A_运行前K线 → 构建用了运行前快照数据'
        elif abs(npz_col0 - exp_b) < 0.01:
            verdict = 'MATCH_B_运行后K线 → npz与磁盘K线一致, 偏差在别处(需继续查)'
        else:
            verdict = f'NO_MATCH 列0={npz_col0:.4f} vs A={exp_a:.4f} B={exp_b:.4f} C={exp_c if exp_c is not None else "N/A"} → 现场瞬态/内存异常(8/3 时点无法事后复现)'
    log(f'判定: {verdict}')
else:
    exp_a = exp_b = None
    verdict = 'NO_MATCH 无候选特征日可对比'

# 6. 附加: 运行后 K 线 md5 vs 快照 md5
import hashlib
def md5(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()
md5_now = md5(KLINE_FILE)
log(f'K线MD5: 运行前={snap["md5"]["klines"]} 运行后={md5_now} '
    f'{"(未变化)" if md5_now == snap["md5"]["klines"] else "(!! 运行中被刷新 !!)"}')

result = {
    'ts': datetime.now(timezone.utc).isoformat(),
    'snapshot': snap['ts'],
    'npz_rows': int(len(pX)),
    'npz_col0': npz_col0,
    'expected_A_presnap': exp_a,
    'expected_B_postsnap': exp_b,
    'klines_md5_pre': snap['md5']['klines'],
    'klines_md5_post': md5_now,
    'verdict': verdict,
}
out = f'{AUDIT_DIR}/verify_{today}.json'
with open(out, 'w') as f:
    json.dump(result, f, ensure_ascii=False, indent=1)
log(f'结果已写: {out}')
