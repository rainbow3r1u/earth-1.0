#!/usr/bin/env python3
"""模型标尺 (2026-09-13 立, 用户指示"以后有标尺可以测量是否被改过")。

原理: 取一份【冻结的特征输入】(某个历史日的 pred_feats: feats + bounds, 永不改变),
      每天用当日的生产模型对它推理, 得到一条固定的概率向量, 记入 append-only 账本。
      **输入固定 → 输出变化只能来自模型本身** → 连续两日的 Spearman 就是"模型被改过没有"的标尺。

判读:
  ρ ≥ 0.85  → 正常(每日全量重训的固有漂移, 见基线库)
  ρ 0.5~0.85 → 异常, 需查(Bug修复/特征变更/参数变更)
  ρ < 0.5   → 模型已实质改变(历史案例: 前视泄漏期 0.20)

为什么不用模型文件比对: pkl 只留 5 天且从不入 git → 模型实物留不住; 标尺只需每日一行概率向量。
输入侧防篡改: 记录冻结输入的 MD5; 输入一旦被改, 账本当天就会出现 probe_md5 变化。

用法:
  python3 scripts/model_probe.py              # 追加今日(幂等)
  python3 scripts/model_probe.py --backfill   # 用现存全部历史模型补齐基线
  python3 scripts/model_probe.py --report     # 打印标尺序列与异常告警
"""
import os, sys, json, glob, pickle, hashlib, argparse, datetime
import numpy as np

BASE = '/home/myuser/websocket_new'
sys.path.insert(0, BASE)
LEDGER = f'{BASE}/data/model_probe.jsonl'
MODELS = os.path.expanduser('~/.local/share/auto_trade/models')
PROBE_DAY = '2026-09-13'          # 冻结输入日(一旦确定不可更改; 变更必须走 PROBE_VERSION)


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def load_probe():
    """冻结输入: 固定日的 feats + bounds (生产推理口径)"""
    p = f'{BASE}/data/pred_feats_{PROBE_DAY}.npz'
    if not os.path.exists(p):
        cands = sorted(glob.glob(f'{BASE}/data/pred_feats_*.npz'))
        if not cands:
            raise SystemExit('无可用的 pred_feats 冻结输入')
        p = cands[-1]
        print(f'⚠️ 冻结输入 {PROBE_DAY} 缺失, 回退到 {os.path.basename(p)}')
    import auto_dual_trade as adt
    z = np.load(p, allow_pickle=True)
    feats = np.nan_to_num(z['feats'].astype(np.float32), nan=0.0)
    bounds = z['bounds'].tolist()
    X = adt.dp._apply_winsor(feats, bounds)
    X[:, 100:932] = 0.0
    X[:, 72:91] = 0.0
    if os.environ.get('ETF_ON', '0') != '1':
        X[:, 46:48] = 0.0
    return X, [str(s) for s in z['syms']], os.path.basename(p), md5(p)


def soup_models(side, tag):
    """生产 SOUP 语义: 当日 + 最近2个历史日期的模型"""
    tag_c = tag.replace('-', '')          # 模型文件名日期为紧凑格式 YYYYMMDD
    files = sorted(glob.glob(f'{MODELS}/xgb_daily_{side}_2*.pkl'), reverse=True)
    out = []
    for f in files:
        d = os.path.basename(f).split('_')[-1].replace('.pkl', '')
        if d <= tag_c:
            out.append(f)
        if len(out) >= 3:
            break
    return out


def probe_once(X, tag):
    res = {}
    for side in ('long', 'short'):
        fs = soup_models(side, tag)
        if not fs:
            return None
        ps = []
        for f in fs:
            with open(f, 'rb') as fh:
                ps.append(pickle.load(fh).predict_proba(X)[:, 1])
        res[side] = np.mean(ps, axis=0) * 100
        res[side + '_models'] = [os.path.basename(f) for f in fs]
        res[side + '_md5'] = [md5(f)[:12] for f in fs]
    return res


def spearman(a, b):
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else float('nan')


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


def record(tag, hist, X, syms, probe_name, probe_md5, verbose=True):
    r = probe_once(X, tag)
    if r is None:
        print(f'{tag}: 无可用模型'); return None
    rec = {'date': tag, 'probe': probe_name, 'probe_md5': probe_md5, 'n_probe': len(syms),
           'long_models': r['long_models'], 'short_models': r['short_models'],
           'long_md5': r['long_md5'], 'short_md5': r['short_md5'],
           'long_soup': [round(float(x), 2) for x in r['long']],
           'short_soup': [round(float(x), 2) for x in r['short']]}
    prev = None
    for h in hist:
        if h['date'] < tag and h.get('probe') == probe_name:
            prev = h
    if prev and len(prev.get('long_soup', [])) == len(rec['long_soup']):
        rec['rho_long_vs_prev'] = round(spearman(np.array(rec['long_soup']), np.array(prev['long_soup'])), 4)
        rec['rho_short_vs_prev'] = round(spearman(np.array(rec['short_soup']), np.array(prev['short_soup'])), 4)
    top = np.argsort(-r['long'])[:10]
    rec['long_top10'] = [syms[i] for i in top]
    if verbose:
        rho = rec.get('rho_long_vs_prev')
        flag = ''
        if rho is not None:
            flag = ' ✅' if rho >= 0.85 else (' ⚠️异常' if rho >= 0.5 else ' ❌模型已改变')
        print(f'[{tag}] 探针 {len(syms)} 币  模型={rec["long_models"][0][-12:]}  '
              f'max_prob={r["long"].max():.1f}  ρ(vs前次)={rho}{flag}')
        print(f'        固定输入上的 top10: {", ".join(rec["long_top10"][:5])} ...')
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true', help='用现存全部历史模型补齐基线')
    ap.add_argument('--report', action='store_true', help='打印标尺序列')
    a = ap.parse_args()
    hist = load()

    if a.report:
        rows = [h for h in hist if h.get('rho_long_vs_prev') is not None]
        print(f'=== 模型标尺 (账本 {len(hist)} 天) ===')
        print(f'{"日期":12s} {"ρ(LONG)":>9s} {"ρ(SHORT)":>9s} {"max_prob":>9s}  判定')
        for h in hist:
            rho = h.get('rho_long_vs_prev')
            s = '基线(首条)' if rho is None else (f'{rho:.4f}')
            rs = h.get('rho_short_vs_prev'); rs = '—' if rs is None else f'{rs:.4f}'
            mx = max(h.get('long_soup', [0]))
            verdict = '基线' if rho is None else ('正常 ✅' if rho >= 0.85 else ('异常 ⚠️' if rho >= 0.5 else '模型已改变 ❌'))
            print(f'{h["date"]:12s} {s:>9s} {rs:>9s} {mx:9.1f}  {verdict}')
        if rows:
            v = [h['rho_long_vs_prev'] for h in rows]
            print(f'\n正常漂移基线: ρ 均值 {np.mean(v):.4f}  最小 {np.min(v):.4f}  最大 {np.max(v):.4f}')
            print('判据: ρ≥0.85 正常; 0.5~0.85 需查; <0.5 模型已实质改变')
        return 0

    X, syms, pname, pmd5 = load_probe()

    if a.backfill:
        tags = set()
        for side in ('long', 'short'):
            for f in glob.glob(f'{MODELS}/xgb_daily_{side}_2*.pkl'):
                tags.add(os.path.basename(f).split('_')[-1].replace('.pkl', ''))
        tags = sorted(tags)
        print(f'补齐 {len(tags)} 个日期: {tags}')
        for t in tags:
            tag = f'{t[:4]}-{t[4:6]}-{t[6:]}'
            rec = record(tag, [h for h in hist if h['date'] != tag], X, syms, pname, pmd5)
            if rec:
                hist = [h for h in hist if h['date'] != tag] + [rec]
        hist.sort(key=lambda h: h['date'])
        with open(LEDGER, 'w') as f:
            for h in hist:
                f.write(json.dumps(h, ensure_ascii=False) + '\n')
        return 0

    today = datetime.date.today().isoformat()
    rec = record(today, hist, X, syms, pname, pmd5)
    if rec is None:
        return 1
    hist = [h for h in hist if h['date'] != today] + [rec]
    hist.sort(key=lambda h: h['date'])
    with open(LEDGER, 'w') as f:
        for h in hist:
            f.write(json.dumps(h, ensure_ascii=False) + '\n')
    rho = rec.get('rho_long_vs_prev')
    if rho is not None and rho < 0.85:
        print(f'⚠️ 标尺告警: ρ={rho} 低于正常漂移基线 → 模型/特征/参数可能被改动, 请比对 prod_fingerprint')
    print(f'[model_probe] {today} 已记录 → {LEDGER}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
