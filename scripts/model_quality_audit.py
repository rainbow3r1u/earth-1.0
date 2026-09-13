#!/usr/bin/env python3
"""模型水准测评 (2026-09-13 立) — SKILL「模型水准测评」的唯一入口。

设计原则: **用户只需说一句"模型水准测评", 不需要记任何命令或参数。**
  无参数执行 = 六层判读 + 长窗口复盘(默认60天) 一次出全套。

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



# ───────────────────────── 长窗口复盘(四条结构性证据) ─────────────────────────
def layer_review(K, days=60):
    """长窗口(如2个月)复盘: 只看结构性证据, 不以盈亏下结论。

    回答: "这N天模型还行不行?" —— 盈亏是结果(受行情影响), 四条结构性证据才是原因(归模型)。
    """
    ab = layer_ability(K, days=days)
    if not ab:
        return None
    w = ab['w7']          # 窗口聚合(层内按 days 取窗口)
    full = ab['full']
    # ④ 档位分布 + 各档实测每笔U(用实盘结构 SL8/72h 的影子档)
    bands = [('荒 <1.0%', 0, 1.0), ('偏紧 1.0~1.5%', 1.0, 1.5), ('正常 ≥1.5%', 1.5, 99)]
    dist, exm_win, exm_n = {}, None, 0
    try:
        dd = json.load(open(f'{BASE}/data/hybrid_tracker_sl8h72.json'))
        settled = {k: v for k, v in dd.items() if v.get('n_settled', 0) == v.get('n_total', 0) and v.get('n_total', 0) > 0}
    except Exception:
        settled = {}
    # 用 ability 的逐日 base 率重建档位映射
    br_map = _daily_base(K)
    days_list = sorted(d for d in br_map if d >= '2026-08-03')[-days:]
    exs = []
    for lo_lbl, lo, hi in bands:
        sub = [d for d in days_list if lo <= br_map[d] * 100 < hi]
        per = []
        for d in sub:
            tr = [t for t in settled.get(d, {}).get('trades', [])
                  if t.get('direction') == 'LONG' and t.get('net_u') is not None]
            if tr:
                per.append(sum(t['net_u'] for t in tr) / len(tr))
            exs += [t['net_u'] for t in settled.get(d, {}).get('trades', [])
                    if t.get('direction') == 'LONG' and t.get('trigger') == '到期' and t.get('net_u') is not None]
        dist[lo_lbl] = dict(days=len(sub), pct=(len(sub) / len(days_list) * 100 if days_list else 0),
                            per=(st.mean(per) if per else None), n_days=len(per))
    if exs:
        exm_win, exm_n = st.mean(exs), len(exs)
    # ③ 2×2 排序(仅窗口内)
    stc = []
    for lbl, fn in [('SL-5%/48h(基线)', 'hybrid_tracker.json'), ('SL-8%/48h', 'hybrid_tracker_sl8.json'),
                    ('SL-5%/72h', 'hybrid_tracker_h72.json'), ('SL-8%/72h(实盘)', 'hybrid_tracker_sl8h72.json')]:
        try:
            d = json.load(open(f'{BASE}/data/{fn}'))
        except Exception:
            continue
        tr = [t for k in days_list for t in d.get(k, {}).get('trades', [])
              if t.get('direction') == 'LONG' and t.get('net_u') is not None]
        if tr:
            stc.append(dict(label=lbl, n=len(tr), per=sum(t['net_u'] for t in tr) / len(tr)))
    order_ok = all(stc[i]['per'] <= stc[i + 1]['per'] for i in range(len(stc) - 1)) if len(stc) >= 2 else None
    return dict(days=len(days_list), first=(days_list[0] if days_list else None),
                last=(days_list[-1] if days_list else None), w=w, full=full,
                dist=dist, exm=exm_win, exm_n=exm_n, stc=stc, order_ok=order_ok,
                band=ab['band'], base_pct=ab['base_pct'])


_BR_CACHE = {}


def _daily_base(K):
    """逐日宇宙底率(缓存), 供档位分布使用"""
    global _BR_CACHE
    if _BR_CACHE:
        return _BR_CACHE
    NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    IX = {s: {d: i for i, (d, *_) in enumerate(v)} for s, v in K.items()}
    out = {}
    for f in sorted(glob.glob(f'{BASE}/data/pred_2026-*.json')):
        d = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(f)).group(1)
        vals = []
        for s, v in K.items():
            m = IX[s]
            if d not in m:
                continue
            i = m[d]
            if i + 2 >= len(v):
                continue
            t2 = datetime.datetime.strptime(v[i + 2][0], '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
            if t2 + 86400000 > NOW:
                continue
            e = v[i][1]
            if e > 0:
                vals.append((v[i + 2][4] / e - 1) * 100)
        if len(vals) >= 300:
            out[d] = sum(1 for x in vals if x >= TAIL) / len(vals)
    _BR_CACHE = out
    return out



def review_lines(rv, requested):
    """长窗口复盘 → 行列表(默认流程与 --review 共用); 用户不需要记任何参数"""
    if not rv:
        return ['(无足够数据做复盘)']
    L = []
    ratio = rv['w']['lift'] / rv['full']['lift'] if rv['full']['lift'] else 0
    degenerate = rv['days'] >= rv['full']['days']
    L.append(f'=== 长窗口复盘 {rv["first"]} ~ {rv["last"]} ({rv["days"]}天窗口, 请求{requested}天) ===')
    L.append('   ⚠️ 复盘只看结构性证据 —— 盈亏是结果(受行情影响), 四条证据才是原因(归模型)')
    L.append('')
    L.append('① lift 模型相对能力')
    L.append(f'   窗口 lift {rv["w"]["lift"]:.2f}x vs 自身基线 {rv["full"]["lift"]:.2f}x = {ratio*100:.0f}%')
    if degenerate:
        L.append('   ⚠️ 窗口已覆盖全部历史(无更早基线) → 该比值恒为100%, 此时①无判别力; 需积累更久')
    else:
        L.append('   ' + ('✅ 达标(≥80%)' if ratio >= 0.8 else
                          '🟡 偏低(65~80%)' if ratio >= 0.65 else
                          '🔴 不达标(<65%) — 若供给已恢复则模型退化'))
    L.append('')
    L.append('② 到期单均 vs 同档基准')
    if rv['exm'] is not None:
        L.append(f'   窗口到期单均 {rv["exm"]:+.2f}U ({rv["exm_n"]}笔)   基准: 荒+30 / 偏紧+35 / 正常+106U')
        L.append(f'   窗口平均底率 {rv["base_pct"]:.2f}%  → 与"窗口自己档位"对应的基准比, 不跨档比')
    else:
        L.append('   (窗口内无到期单)')
    L.append('')
    L.append('③ 2×2 四档排序 (窗口内)')
    for r in rv['stc']:
        L.append(f'   {r["label"]:18s} {r["n"]:>4}笔  每笔 {r["per"]:+.2f}U')
    L.append('   → ' + ('✅ 排序未变(SL8/72h最优)' if rv['order_ok'] else '⚠️ 排序变了 — 结构结论动摇, 需复查'))
    L.append('')
    L.append('④ 供给档位分布 (把"该赚多少"的行情背景摊开)')
    L.append(f'   {"档位":16s} {"天数":>5s} {"占比":>7s} {"该档实测每笔U":>13s}')
    for k, v in rv['dist'].items():
        per = f'{v["per"]:+.2f}U' if v['per'] is not None else 'n/a'
        L.append(f'   {k:16s} {v["days"]:>5d} {v["pct"]:>6.1f}% {per:>13s}')
    normal_pct = rv['dist'].get('正常 ≥1.5%', {}).get('pct', 0)
    L.append(f'   → 正常档占比 {normal_pct:.1f}%' +
             ('  ⚠️ 偏低: 市场供给不足, 期间盈亏走平不代表模型坏' if normal_pct < 30 else '  ✅ 供给正常'))
    L.append('')
    bad = ((not degenerate) and ratio < 0.65 and normal_pct >= 30) or (rv['order_ok'] is False)
    L.append('── 复盘结论 ──')
    L.append('   ' + ('🔴 有结构性证据不达标 → 按证据定位(模型层 or 结构层)' if bad else
                      '✅ 四条结构性证据均未见异常 → 期间盈亏归因于行情供给, 非模型问题'))
    L.append('   证伪线: 底率回到≥1.5%(正常档) 而 lift < 自身基线×0.65 → 模型退化')
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--review', type=int, nargs='?', const=60, metavar='DAYS',
                    help='只看长窗口复盘(默认60天); 不带参数 = 六层+复盘全套')
    a = ap.parse_args()
    K = load_klines()

    if a.review:
        print('\n'.join(review_lines(layer_review(K, a.review), a.review)))
        return 0

    ident, ab = layer_identity(), layer_ability(K)
    stc, cell = layer_structure(), layer_cell(K)
    today = datetime.date.today().isoformat()
    L = [f'=== 模型水准测评 {today} ===', '']
    L += ['① 身份层 · 模型有没有被改过', f'   {ident["msg"]}']
    L.append('   ' + ('✅ 固定输入下的模型行为与历史基线一致(未被改动)' if ident.get('ok') is True else
                      '⚠️ ρ 低于 0.85 → 查 scripts/prod_fingerprint.py --diff 定位是代码/模型/数据哪一项变了'
                      if ident.get('ok') is False else f'(无判据) {ident["msg"]}'))
    L.append('')
    if ab:
        f, w = ab['full'], ab['w7']
        L += ['② 能力层 · 相对能力(lift)还在不在',
              f'   近7窗口: 顶部命中率 {w["hit"]*100:.2f}% ÷ 宇宙底率 {w["base"]*100:.2f}% = lift {w["lift"]:.2f}x',
              f'   自身基线({f["first"]}~{f["last"]}, {f["days"]}窗口/{f["n"]}笔): 命中 {f["hit"]*100:.2f}% / 底率 {f["base"]*100:.2f}% / lift {f["lift"]:.2f}x']
        r = w['lift'] / f['lift'] if f['lift'] else 0
        L.append(f'   → lift = 自身基线的 {r*100:.0f}%  ' +
                 ('✅ 完好' if r >= 0.8 else '🟡 偏弱观察' if r >= 0.65 else '🔴 退化(若供给已恢复)'))
        L += ['', '③ 供给层 · 市场给不给',
              f'   近7窗口宇宙底率 {ab["base_pct"]:.2f}% → 档位: {ab["band"]}',
              '   档位基准(每笔U 均值/中位): 荒 -3.92/-9.43 | 偏紧 -1.47/-6.65 | 正常 +18.74/+12.31', '']
        L += ['④ 捕捉层 · 抓到多少 / 够不够肥',
              f'   顶部命中 {round(w["hit"]*w["n"])}/{w["n"]} 笔 = {w["hit"]*100:.2f}%' +
              (f'   到期单均 {ab["exm"]:+.2f}U (基准: 荒+30 / 偏紧+35 / 正常+106U)' if ab['exm'] is not None else ''),
              '']
    if stc:
        L.append('⑤ 结构层 · 出场结构优劣有没有变')
        L += [f'   {r["label"]:18s} {r["n"]:>4}笔  每笔 {r["per"]:+.2f}U' for r in stc['rows']]
        L.append(f'   → 最佳档 {stc["best"]:+.2f}U/笔 = 基线 {stc["base"]:+.2f}U/笔 的 {stc["delta"]:+.0f}%  ' +
                 ('排序稳定 ✅' if stc['order_ok'] else '排序异常 ⚠️(需复查)'))
        L.append('')
    if cell:
        L.append('⑥ 金矿格 · C远×V高 (量能>0.70 且 距20日低>33%)')
        if cell['hits']:
            L.append(f'   {cell["date"]}: 命中 {len(cell["hits"])} 只 → {", ".join(cell["hits"])}')
        else:
            t = cell['near'][0] if cell['near'] else None
            s = (f'{t[1]} ({"量能✅" if t[2] else "量能❌"}{"距离✅" if t[3] else "距离❌"} {t[4]})'
                 if t else '数据不足')
            L.append(f'   {cell["date"]}: 格内 0 只 — 最接近 {s}')
        L.append('')
    L.append('── 综合判定 ──')
    if ab and stc and ident.get('ok') is not False:
        r = ab['w7']['lift'] / ab['full']['lift'] if ab['full']['lift'] else 0
        L.append('   模型能力完好(②≥基线八成, ⑤排序稳定) → 当期盈亏由供给层(③)解释 → 无需干预'
                 if (r >= 0.8 and stc['order_ok']) else '   ⚠️ 有层不达标, 见上')
    L += ['   证伪线: 底率回到 ≥1.5%(正常档) 而 lift 掉到自身基线 ×0.65 以下 → 模型退化, 才需动模型层',
          '   注意: 底率/命中率/lift 都是【同窗口事后量】(需T2收盘), 诊断用, 不作开仓闸门; IC 同理']

    rv = layer_review(K, days=60)
    if rv:
        L += ['', ''] + review_lines(rv, 60)

    if a.json:
        print(json.dumps(dict(identity=ident, ability=ab, structure=stc, cell=cell),
                         ensure_ascii=False, default=str))
    else:
        print('\n'.join(L))
    return 0


if __name__ == '__main__':
    sys.exit(main())
