#!/usr/bin/env python3
"""模型水准测评 (2026-09-13 立, 用户指示"做个skill叫模型水准测评")

一句话: 把"模型还行不行"拆成六层可测量的判读, 每层给读数 + 判据 + 归属(模型 or 行情)。
目的: 以后复查时不用重新推导逻辑 —— 六层的原理/口径/判读线见 SKILL `.agents/skills/model-quality-audit/SKILL.md`。

六层:
  ① 身份层 — 模型有没有被改过      → 固定输入标尺(model_probe 账本) 的 ρ
  ② 能力层 — 相对能力还在不在      → lift = 顶部命中率 ÷ 宇宙底率  (对比自身全期基线)
  ③ 供给层 — 市场给不给            → 近7窗口宇宙底率 落 荒/偏紧/正常 哪一档
  ④ 捕捉层 — 抓到多少 / 够不够肥   → 命中率 + 到期单均(对照结构基准)
  ⑤ 结构层 — 出场结构优劣有没有变  → 2×2 四档每笔U排序 + 单变量增量
  ⑥ 金矿格 — C远×V高 候选          → 格内币数 + 未命中时的最接近候选
综合判定 + 证伪线(什么数据会推翻"是行情不是模型"的结论)

用法: python3 scripts/model_quality_audit.py [--json]
"""
import os, sys, json, glob, re, argparse, datetime, statistics as st

BASE = '/home/myuser/websocket_new'
KLINE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
TAIL = 33.0        # 右尾门槛: 2日涨幅 ≥ +33%
VOLQ_GATE = 0.70   # 金矿格量能门槛
DIST_GATE = 33.0   # 金矿格距低点门槛


def dt(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')


def load_klines():
    kl = json.load(open(KLINE))['klines']
    return {s: sorted([(dt(r['t']), r['o'], r['h'], r['l'], r['c'], float(r.get('q', 0))) for r in rows])
            for s, rows in kl.items() if len(rows) >= 60}


# ───────────────────────── ① 身份层 ─────────────────────────
def layer_identity():
    p = f'{BASE}/data/model_probe.jsonl'
    if not os.path.exists(p):
        return dict(ok=None, msg='无账本(先跑 scripts/model_probe.py --backfill)')
    h = [json.loads(l) for l in open(p) if l.strip()]
    if not h:
        return dict(ok=None, msg='账本为空')
    last = h[-1]
    rho = last.get('rho_long_vs_prev')
    rhos = [x['rho_long_vs_prev'] for x in h if x.get('rho_long_vs_prev') is not None]
    base = st.mean(rhos) if rhos else None
    return dict(ok=(rho is None or rho >= 0.85), rho=rho, base=base,
                date=last['date'], probe=last.get('probe'), n_hist=len(h),
                msg=f"固定输入标尺 ρ={rho if rho is None else round(rho,4)} (历史均值 {None if base is None else round(base,4)})")


# ───────────────────────── ②③④ 能力/供给/捕捉 ─────────────────────────
def layer_ability(K, days=7, since='2026-08-03'):
    NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    IX = {s: {d: i for i, (d, *_) in enumerate(v)} for s, v in K.items()}

    def fwd2(s, d):
        v = K.get(s)
        if not v:
            return None
        m = IX[s]
        if d not in m:
            return None
        i = m[d]
        if i + 2 >= len(v):
            return None
        if v[i + 2][0]:
            pass
        t2 = datetime.datetime.strptime(v[i + 2][0], '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
        if t2 + 86400000 > NOW:
            return None
        e = v[i][1]
        return (v[i + 2][4] / e - 1) * 100 if e > 0 else None

    rows = []
    for f in sorted(glob.glob(f'{BASE}/data/pred_2026-*.json')):
        d = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(f)).group(1)
        try:
            tl = [x['symbol'] for x in (json.load(open(f)).get('top10_long') or [])]
        except Exception:
            continue
        if len(tl) < 5:
            continue
        top = [x for x in (fwd2(s, d) for s in tl) if x is not None]
        uni = [x for x in (fwd2(s, d) for s in K) if x is not None]
        if len(top) < 5 or len(uni) < 300:
            continue
        rows.append(dict(d=d, n=len(top),
                         hit=sum(1 for x in top if x >= TAIL) / len(top),
                         base=sum(1 for x in uni if x >= TAIL) / len(uni)))
    rows = [r for r in rows if r['d'] >= since]
    if not rows:
        return None
    def agg(sub):
        n = sum(r['n'] for r in sub)
        hit = sum(r['hit'] * r['n'] for r in sub) / n
        base = st.mean([r['base'] for r in sub])
        return dict(n=n, days=len(sub), hit=hit, base=base, lift=(hit / base if base else 0))
    full, w7 = agg(rows), agg(rows[-days:])
    full['first'], full['last'] = rows[0]['d'], rows[-1]['d']
    # 供给分档
    br = w7['base'] * 100
    band = '荒(<1.0%)' if br < 1.0 else ('偏紧(1.0~1.5%)' if br < 1.5 else '正常(≥1.5%)')
    # 到期单均(影子档 SL8/72h = 实盘现行结构)
    exm = None
    try:
        dd = json.load(open(f'{BASE}/data/hybrid_tracker_sl8h72.json'))
        sd = sorted([k for k, v in dd.items() if v.get('n_settled', 0) == v.get('n_total', 0) and v.get('n_total', 0) > 0])[-days:]
        ex = [t['net_u'] for k in sd for t in dd[k].get('trades', [])
              if t.get('direction') == 'LONG' and t.get('trigger') == '到期' and t.get('net_u') is not None]
        if ex:
            exm = st.mean(ex)
    except Exception:
        pass
    return dict(full=full, w7=w7, band=band, base_pct=br, exm=exm)


# ───────────────────────── ⑤ 结构层 ─────────────────────────
def layer_structure():
    FILES = [('SL-5%/48h(基线)', 'hybrid_tracker.json'), ('SL-8%/48h', 'hybrid_tracker_sl8.json'),
             ('SL-5%/72h', 'hybrid_tracker_h72.json'), ('SL-8%/72h(实盘)', 'hybrid_tracker_sl8h72.json')]
    out = []
    for lbl, fn in FILES:
        try:
            d = json.load(open(f'{BASE}/data/{fn}'))
        except Exception:
            continue
        tr = [t for k in d for t in d[k].get('trades', [])
              if t.get('direction') == 'LONG' and t.get('net_u') is not None]
        if not tr:
            continue
        out.append(dict(label=lbl, n=len(tr), per=sum(t['net_u'] for t in tr) / len(tr)))
    if len(out) < 2:
        return None
    base = out[0]['per']
    order_ok = all(out[i]['per'] <= out[i + 1]['per'] for i in range(len(out) - 1))
    return dict(rows=out, base=base, order_ok=order_ok,
                best=out[-1]['per'], delta=(out[-1]['per'] / base - 1) * 100 if base else 0)


# ───────────────────────── ⑥ 金矿格 ─────────────────────────
def layer_cell(K):
    """口径与晨报 section_long_top10 完全一致: 昨日完整bar量在前20根完整bar中的分位; 距20根最低点(entry=当日开盘)"""
    NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    today0 = (NOW // 86400000) * 86400000
    cands = sorted(glob.glob(f'{BASE}/data/pred_2026-*.json'))
    if not cands:
        return None
    pred = json.load(open(cands[-1]))
    syms = [x['symbol'] for x in (pred.get('top10_long') or [])]
    rows = []
    for sym in syms:
        bars = K.get(sym, [])
        done = [(d, o, h, l, c, q) for (d, o, h, l, c, q) in bars
                if datetime.datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000 + 86400000 <= NOW]
        if len(done) < 20:
            continue
        qs = [x[5] for x in done[-20:]]
        vq = sum(1 for x in qs if x <= qs[-1]) / 20.0
        lo = min(x[3] for x in done[-20:])
        tb = [x for x in bars if datetime.datetime.strptime(x[0], '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000 >= today0]
        if not tb:
            continue
        e = tb[0][1]
        if not e:
            continue
        dist = (e - lo) / e * 100
        rows.append(dict(sym=sym, volq=vq, dist=dist, cell=(vq > VOLQ_GATE and dist > DIST_GATE)))
    hits = [r for r in rows if r['cell']]
    near = []
    for r in rows:
        score = min(r['volq'] / VOLQ_GATE, r['dist'] / DIST_GATE)
        okv, okd = r['volq'] > VOLQ_GATE, r['dist'] > DIST_GATE
        gap = (f"距离差{DIST_GATE - r['dist']:.1f}pp" if okv and not okd else
               f"量能差{VOLQ_GATE - r['volq']:.2f}" if okd and not okv else
               f"距离差{DIST_GATE - r['dist']:.1f}pp/量能差{VOLQ_GATE - r['volq']:.2f}")
        near.append((score, r['sym'], okv, okd, gap, r['volq'], r['dist']))
    near.sort(reverse=True)
    return dict(date=pred.get('date', '')[:10], n=len(rows), hits=[h['sym'] for h in hits], near=near[:2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    K = load_klines()
    ident = layer_identity()
    ab = layer_ability(K)
    stc = layer_structure()
    cell = layer_cell(K)
    today = datetime.date.today().isoformat()

    L = []
    L.append(f'=== 模型水准测评 {today} ===')
    L.append('')
    L.append('① 身份层 · 模型有没有被改过')
    L.append(f'   {ident["msg"]}')
    if ident.get('ok') is True:
        L.append('   → ✅ 固定输入下的模型行为与历史基线一致(未被改动)')
    elif ident.get('ok') is False:
        L.append('   → ⚠️ ρ 低于 0.85 → 查 scripts/prod_fingerprint.py --diff 定位是代码/模型/数据哪一项变了')
    else:
        L.append(f'   → (无判据) {ident["msg"]}')
    L.append('')
    if ab:
        f, w = ab['full'], ab['w7']
        L.append('② 能力层 · 相对能力(lift)还在不在')
        L.append(f'   近7窗口: 顶部命中率 {w["hit"]*100:.2f}% ÷ 宇宙底率 {w["base"]*100:.2f}% = lift {w["lift"]:.2f}x')
        L.append(f'   自身基线({f["first"]}~{f["last"]}, {f["days"]}窗口/{f["n"]}笔): 命中 {f["hit"]*100:.2f}% / 底率 {f["base"]*100:.2f}% / lift {f["lift"]:.2f}x')
        ratio = w['lift'] / f['lift'] if f['lift'] else 0
        verdict = '✅ 完好' if ratio >= 0.8 else ('🟡 偏弱观察' if ratio >= 0.65 else '🔴 退化(若供给已恢复)')
        L.append(f'   → lift = 自身基线的 {ratio*100:.0f}%  {verdict}')
        L.append('')
        L.append('③ 供给层 · 市场给不给')
        L.append(f'   近7窗口宇宙底率 {ab["base_pct"]:.2f}% → 档位: {ab["band"]}')
        L.append('   档位基准(每笔U · 均值/中位): 荒 -3.92/-9.43 | 偏紧 -1.47/-6.65 | 正常 +18.74/+12.31')
        L.append('')
        L.append('④ 捕捉层 · 抓到多少 / 够不够肥')
        L.append(f'   顶部命中 {round(w["hit"]*w["n"])}/{w["n"]} 笔 = {w["hit"]*100:.2f}%'
                 + (f'   到期单均 {ab["exm"]:+.2f}U (结构基准: 荒+30 / 偏紧+35 / 正常+106)' if ab['exm'] is not None else ''))
        L.append('')
    if stc:
        L.append('⑤ 结构层 · 出场结构优劣有没有变')
        for r in stc['rows']:
            L.append(f'   {r["label"]:18s} {r["n"]:>4}笔  每笔 {r["per"]:+.2f}U')
        L.append(f'   → 最佳档 {stc["best"]:+.2f}U/笔 = 基线 {stc["base"]:+.2f}U/笔 的 {stc["delta"]:+.0f}%  '
                 f'排序{"稳定 ✅" if stc["order_ok"] else "异常 ⚠️(排序变了, 需复查)"}')
        L.append('')
    if cell:
        L.append('⑥ 金矿格 · C远×V高 (量能>0.70 且 距20日低>33%)')
        if cell['hits']:
            L.append(f'   {cell["date"]}: 命中 {len(cell["hits"])} 只 → {", ".join(cell["hits"])}')
        else:
            t = cell['near'][0] if cell['near'] else None
            s = f'{t[1]} ({"量能✅" if t[2] else "量能❌"}{"距离✅" if t[3] else "距离❌"} {t[4]})' if t else '数据不足'
            L.append(f'   {cell["date"]}: 格内 0 只 — 最接近 {s}')
        L.append('')
    # 综合
    L.append('── 综合判定 ──')
    if ab and stc and ident.get('ok') is not False:
        ratio = ab['w7']['lift'] / ab['full']['lift'] if ab['full']['lift'] else 0
        if ratio >= 0.8 and stc['order_ok']:
            L.append('   模型能力完好(②≥基线八成, ⑤排序稳定) → 当期盈亏由供给层(③)解释 → 无需干预')
        else:
            L.append('   ⚠️ 有层不达标, 见上')
    L.append('   证伪线: 底率回到 ≥1.5%(正常档) 而 lift 掉到自身基线 ×0.65 以下 → 模型退化, 才需动模型层')
    L.append('   注意: 底率/命中率/lift 都是【同窗口事后量】(需T2收盘), 诊断用, 不作开仓闸门; IC 同理')
    out = '\n'.join(L)
    if a.json:
        print(json.dumps(dict(identity=ident, ability=ab, structure=stc, cell=cell), ensure_ascii=False, default=str))
    else:
        print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
