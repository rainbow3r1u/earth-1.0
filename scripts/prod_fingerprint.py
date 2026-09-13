#!/usr/bin/env python3
"""生产指纹账本 (2026-09-13 立, 用户指示) — 解决"今天的模型/代码是不是还是原来那条流水线"。

背景: 系统每日全量重训 → "同一个模型文件"不存在; 而模型 pkl 只留 5 天、数据快照只留 10 天、
      pkl 从不入 git(三重屏障: 仓库外目录 + .gitignore + 同步脚本排除扩展名),
      导致"8/03 的模型"事后无法比对(2026-09-13 用户质询暴露此洞)。
方案: 每日追加一行【指纹】到 data/prod_fingerprint.jsonl(append-only, 永久保留):
      git HEAD + 生产核心文件 MD5 + 当日模型 pkl MD5 + 训练数据 npz MD5 + 特征矩阵 MD5 + 工作区状态。
      **存哈希不存文件** — pkl 0.86MB×2×365 ≈ 630MB/年 无法入 git, 哈希一年仅 ~23KB。
      与昨日指纹不同 → 在记录里标注 changed 字段, 一眼看出"哪天动了什么"。

用法: python3 scripts/prod_fingerprint.py            # 追加今日指纹(幂等: 同日覆盖)
      python3 scripts/prod_fingerprint.py --show     # 打印最近 20 条 + 变更历史
      python3 scripts/prod_fingerprint.py --diff     # 只对比最近两条, 列出变化文件
"""
import os, sys, json, hashlib, subprocess, argparse, datetime

BASE = '/home/myuser/websocket_new'
LEDGER = f'{BASE}/data/prod_fingerprint.jsonl'
MODELS = '/home/myuser/.local/share/auto_trade/models'

# 生产核心文件(改动即影响模型/交易行为)
CORE_FILES = [
    'auto_dual_trade.py',            # 主流程: 特征构建/训练/预测
    'daily_predictor.py',            # 特征工程库
    'utils/feature_builder.py',      # 946 维向量组装
    'guardian.py',                   # 守护 + 周期任务
    '/home/myuser/backtester/config/current_params.json',   # 实盘参数(仓库外! 覆盖代码默认)
    'audit/residual_live.py',        # 果实盘执行器
    'audit/hybrid_live.py',          # 刘执行器
]


# 外部数据文件(每日被采集器重写; 事后回刷会改变历史特征 → 需永久留痕)
EXT_FILES = [
    '/home/myuser/stablecoin_data/stablecoin_exchange_netflow.json',
    '/home/myuser/stablecoin_data/btc_coinbase_premium_index.json',
    '/home/myuser/stablecoin_data/btc_coinbase_premium_gap.json',
    '/home/myuser/stablecoin_data/btc_korea_premium_index.json',
    '/home/myuser/coingecko_data/btc_dominance.json',
    '/home/myuser/coingecko_data/btc_mcap.json',
    '/home/myuser/hashrate_data/hashrate_history.json',
    '/home/myuser/blockchair_data/btc_chain.csv',
    '/home/myuser/defillama_data/btc_chain_tvl.json',
    'data/fear_greed_history.json',
    'data/macro_assets.json',
    'data/liq_daily.json',
    'data/liq_levels_daily.json',
]


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None


def git(*args):
    try:
        return subprocess.run(['git'] + list(args), cwd=BASE, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception:
        return ''


def collect(today):
    rec = {'date': today, 'ts': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    rec['git_head'] = git('rev-parse', '--short', 'HEAD')
    rec['git_branch'] = git('rev-parse', '--abbrev-ref', 'HEAD')
    dirty = []
    for l in git('status', '--porcelain').splitlines():
        if not l.strip():
            continue
        parts = l.split(maxsplit=1)
        if len(parts) < 2:
            continue
        path = parts[1].strip()
        if path.startswith('data/') or path.startswith('gate_') or parts[0] == '??':
            continue          # 排除 data 日常churn / 实验产物 / 未跟踪文件
        dirty.append(path)
    rec['dirty_core'] = dirty                     # 生产文件未提交改动 = 漂移风险
    rec['core_files'] = {}
    for rel in CORE_FILES:
        p = rel if os.path.isabs(rel) else os.path.join(BASE, rel)
        rec['core_files'][rel] = md5_file(p) if os.path.exists(p) else 'MISSING'
    # 当日模型 pkl(两侧)
    rec['models'] = {}
    for side in ('long', 'short'):
        p = f'{MODELS}/xgb_daily_{side}_{today.replace("-","")}.pkl'
        if not os.path.exists(p):
            p = f'{MODELS}/xgb_daily_{side}.pkl'
        rec['models'][side] = {'path': os.path.basename(p), 'md5': md5_file(p),
                               'size': os.path.getsize(p) if os.path.exists(p) else None}
    # 外部数据文件 MD5 (2026-09-13 增: 今日查出 st 值被事后修订 → 修订必须永久留痕)
    rec['ext_data'] = {}
    for rel in EXT_FILES:
        p2 = rel if os.path.isabs(rel) else os.path.join(BASE, rel)
        rec['ext_data'][os.path.basename(rel)] = (md5_file(p2) if os.path.exists(p2) else 'MISSING')
    # 训练数据 + 当日特征矩阵
    npz = '/home/myuser/.local/share/auto_trade/train_data_latest.npz'
    rec['train_npz'] = {'md5': md5_file(npz), 'size': os.path.getsize(npz) if os.path.exists(npz) else None}
    pf = f'{BASE}/data/pred_feats_{today}.npz'
    rec['pred_feats'] = {'exists': os.path.exists(pf), 'md5': md5_file(pf) if os.path.exists(pf) else None}
    # 特征零值区间(掩码生效图案; 任何掩码/数据变动都会改变它)
    try:
        import numpy as _np
        _z = _np.load(pf, allow_pickle=True)
        _f = _z['feats'].astype(float)
        _zcols = _np.where(_np.all(_f == 0, axis=0))[0]
        _runs = []
        _s = None
        for _j in range(_f.shape[1] + 1):
            _is = _j in set(_zcols.tolist())
            if _is and _s is None:
                _s = _j
            elif not _is and _s is not None:
                _runs.append([_s, _j - 1])
                _s = None
        rec['pred_feats_zero_runs'] = _runs
    except Exception:
        rec['pred_feats_zero_runs'] = None
    return rec


def load():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def diff_records(a, b):
    """返回 a→b 的变化清单"""
    ch = []
    if a.get('git_head') != b.get('git_head'):
        ch.append(f"git {a.get('git_head')} → {b.get('git_head')}")
    for rel in CORE_FILES:
        if a.get('core_files', {}).get(rel) != b.get('core_files', {}).get(rel):
            ch.append(f"代码变更: {rel}")
    for side in ('long', 'short'):
        if a.get('models', {}).get(side, {}).get('md5') != b.get('models', {}).get(side, {}).get('md5'):
            ch.append(f"模型变更: {side}")
    if a.get('train_npz', {}).get('md5') != b.get('train_npz', {}).get('md5'):
        ch.append('训练数据变更(train_npz MD5 不同)')
    for k in sorted(set(a.get('ext_data', {})) | set(b.get('ext_data', {}))):
        if a.get('ext_data', {}).get(k) != b.get('ext_data', {}).get(k):
            ch.append(f'外部数据变更/回刷: {k}')
    if a.get('pred_feats_zero_runs') != b.get('pred_feats_zero_runs'):
        ch.append(f'特征零值图案变更(掩码或数据): {a.get("pred_feats_zero_runs")} → {b.get("pred_feats_zero_runs")}')
    if b.get('dirty_core'):
        ch.append(f"⚠️ 工作区有生产文件未提交改动: {b['dirty_core']}")
    return ch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--show', action='store_true', help='打印最近20条与变更历史')
    ap.add_argument('--diff', action='store_true', help='对比最近两条')
    a = ap.parse_args()
    hist = load()

    if a.show:
        print(f'指纹账本: {LEDGER}  ({len(hist)} 天)')
        for r in hist[-20:]:
            print(f"  {r['date']}  git={r.get('git_head')}  "
                  f"long={str(r.get('models',{}).get('long',{}).get('md5'))[:8]}  "
                  f"npz={str(r.get('train_npz',{}).get('md5'))[:8]}"
                  + (f"  ⚠️{len(r['dirty_core'])}个未提交" if r.get('dirty_core') else ''))
        if len(hist) >= 2:
            print('\n变更历史(相邻两日对比):')
            for i in range(1, len(hist)):
                ch = diff_records(hist[i-1], hist[i])
                if ch:
                    print(f"  {hist[i]['date']}: " + ' | '.join(ch))
                else:
                    print(f"  {hist[i]['date']}: 无变化")
        return 0

    if a.diff:
        if len(hist) < 2:
            print('账本不足两条'); return 1
        ch = diff_records(hist[-2], hist[-1])
        print(f"{hist[-2]['date']} → {hist[-1]['date']}:")
        print('\n'.join('  ' + c for c in ch) if ch else '  无变化 ✅')
        return 0

    # 追加/覆盖今日记录(幂等)
    today = datetime.date.today().isoformat()
    rec = collect(today)
    prev = None
    for r in hist:
        if r['date'] < today:
            prev = r
    if prev:
        rec['changed_vs_prev'] = diff_records(prev, rec)
    hist = [r for r in hist if r['date'] != today]
    hist.append(rec)
    hist.sort(key=lambda r: r['date'])
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, 'w') as f:
        for r in hist:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f'[fingerprint] {today} 已记录: git={rec["git_head"]} '
          f'long={str(rec["models"]["long"]["md5"])[:8]} npz={str(rec["train_npz"]["md5"])[:8]}')
    if rec.get('changed_vs_prev'):
        print('[fingerprint] ⚠️ 与昨日不同: ' + ' | '.join(rec['changed_vs_prev']))
    else:
        print('[fingerprint] 与昨日一致 ✅')
    if rec['dirty_core']:
        print(f'[fingerprint] ⚠️ 生产文件未提交改动: {rec["dirty_core"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
