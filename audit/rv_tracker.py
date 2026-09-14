#!/usr/bin/env python3
"""rv 池内加权 · 前向追踪 (2026-09-14 立, 用户指示"挂晨报追踪, 和金矿格一起")

假设(来自 2026-09-14 回测发现, 见 AGENTS §8.6):
  在模型每日 TOP10 池内, **前一日 1m 已实现波动(rv) 更高**的币 → 右尾更多、每笔U更高
  实测(45天/450笔): rv高档 每笔+9.33U / 到期单均+48.40 / ≥+33% 11.76%
                   rv低档 每笔+3.36U / 到期单均 +7.11 / ≥+33%  2.01%   (CI不重叠)

本脚本只做**观测记账**, 不参与任何交易决策(与金矿格同规格):
  ① 取当日生产选币(data/pred_{D}.json → top10_long)
  ② 用**币安批量端口**(铁律8, 不打API)拉 D-1 的 1m 日包 → 算 rv
  ③ 池内按 rv 分组: 高3 / 中4 / 低3
  ④ 与影子档 data/hybrid_tracker_sl8h72.json(SL-8%/72h/300U, 与实盘同结构)对齐结算
  ⑤ 账本 data/rv_shadow.json; 输出一行心跳给晨报

用法: python3 audit/rv_tracker.py          # 记录今日 + 重算全部已到期组
      python3 audit/rv_tracker.py --line   # 只输出晨报用的一行
"""
import os, io, sys, json, csv, zipfile, datetime, argparse
from concurrent.futures import ThreadPoolExecutor
import requests

BASE = '/home/myuser/websocket_new'
LEDGER = f'{BASE}/data/rv_shadow.json'
SHADOW = f'{BASE}/data/hybrid_tracker_sl8h72.json'      # 与实盘同结构(SL-8%/72h/300U)
PORTAL = 'https://data.binance.vision/data/futures/um/daily/klines'
NOTIONAL = 300.0
GRP_HI, GRP_LO = 3, 3        # 池内 rv 最高3 / 最低3
S = requests.Session()
S.headers.update({'User-Agent': 'rv-shadow/1.0'})


def today():
    return datetime.datetime.now().strftime('%Y-%m-%d')


def dminus(d, n=1):
    x = datetime.datetime.strptime(d, '%Y-%m-%d') - datetime.timedelta(days=n)
    return x.strftime('%Y-%m-%d')


def picks(d):
    p = f'{BASE}/data/pred_{d}.json'
    if not os.path.exists(p):
        return []
    try:
        return [x['symbol'] for x in (json.load(open(p)).get('top10_long') or [])]
    except Exception:
        return []


def rv_of(sym, day):
    """从批量端口取 D-1 的 1m 日包算已实现波动 (无前视: 该日已收盘)"""
    url = f'{PORTAL}/{sym}/1m/{sym}-1m-{day}.zip'
    for _ in range(3):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                break
        except Exception:
            pass
        import time as _t; _t.sleep(1.5)
    else:
        return None
    ss = 0.0; prev = None; n = 0
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for nm in z.namelist():
                with z.open(nm) as f:
                    for line in io.TextIOWrapper(f, encoding='utf-8'):
                        p = line.strip().split(',')
                        if len(p) < 5:
                            continue
                        try:
                            c = float(p[4])
                        except ValueError:
                            continue
                        if prev is not None and prev > 0:
                            rr = c/prev - 1
                            ss += rr*rr
                        prev = c; n += 1
    except Exception:
        return None
    if n < 1000:
        return None
    return (ss ** 0.5) * 100.0      # %


def build_groups(d):
    """当日池内按 rv 分三组"""
    pk = picks(d)
    if len(pk) < 6:
        return None
    rvd = dminus(d, 1)
    with ThreadPoolExecutor(max_workers=5) as ex:
        vals = list(ex.map(lambda s: (s, rv_of(s, rvd)), pk))
    got = [(s, v) for s, v in vals if v is not None]
    missing = [s for s, v in vals if v is None]
    if len(got) < 6:
        return dict(date=d, rv_day=rvd, hi=[], mid=[], lo=[], missing=missing, rv={})
    got.sort(key=lambda x: -x[1])
    hi = [s for s, _ in got[:GRP_HI]]
    lo = [s for s, _ in got[-GRP_LO:]]
    mid = [s for s, _ in got[GRP_HI:len(got)-GRP_LO]]
    return dict(date=d, rv_day=rvd, hi=hi, mid=mid, lo=lo, missing=missing,
                rv={s: round(v, 4) for s, v in got})


def load_shadow():
    """影子档(SL-8%/72h)逐日逐笔结果 → {(date,sym): (net_u, trigger)}"""
    try:
        d = json.load(open(SHADOW))
    except Exception:
        return {}
    out = {}
    for day, v in d.items():
        for t in v.get('trades', []):
            if t.get('direction') != 'LONG' or t.get('net_u') is None:
                continue
            if v.get('n_settled', 0) != v.get('n_total', 0):
                continue          # 该日未全部结算 → 先不算
            out[(day, t['symbol'])] = (t['net_u'], t.get('trigger'))
    return out


def stats(syms, day, sh):
    rows = [sh[(day, s)] for s in syms if (day, s) in sh]
    if not rows:
        return None
    us = [r[0] for r in rows]
    ex = [r[0] for r in rows if r[1] == '到期']
    return dict(n=len(rows), per=sum(us)/len(rows), tot=sum(us),
                nex=len(ex), exm=(sum(ex)/len(ex) if ex else None),
                big=sum(1 for r in rows if r[0] >= NOTIONAL*0.33))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--line', action='store_true', help='只输出晨报用的一行')
    a = ap.parse_args()
    led = json.load(open(LEDGER)) if os.path.exists(LEDGER) else {}
    d = today()
    if not a.line:
        # ① 记录今日分组(已存在则跳过)
        if d not in led:
            g = build_groups(d)
            if g:
                led[d] = g
                json.dump(led, open(LEDGER, 'w'), ensure_ascii=False, indent=0)
                print(f'[记录] {d} rv日={g["rv_day"]} 高{len(g["hi"])}/中{len(g["mid"])}/低{len(g["lo"])}'
                      + (f' 缺数据{len(g["missing"])}' if g['missing'] else ''))
            elif not picks(d):
                print(f'[等待] {d} 尚无 pred 存档(08:05 预测未跑), 稍后重跑即可')
            else:
                print(f'[跳过] {d} 选币不足或全部无 1m 数据')
    # ② 重算全部已到期组
    sh = load_shadow()
    agg = dict(hi=[], mid=[], lo=[])
    per_day = {}
    for day in sorted(led):
        g = led[day]
        row = {}
        for k, tag in (('hi', 'hi'), ('mid', 'mid'), ('lo', 'lo')):
            st = stats(g.get(k, []), day, sh)
            row[tag] = st
            if st:
                agg[tag].append(st)
        per_day[day] = row
    def roll(tag):
        rows = agg[tag]
        if not rows:
            return None
        n = sum(r['n'] for r in rows); tot = sum(r['tot'] for r in rows)
        nex = sum(r['nex'] for r in rows)
        exs = [r['exm'] for r in rows if r['exm'] is not None]
        return dict(n=n, per=tot/n, tot=tot, nex=nex,
                    exm=(sum(exs)/len(exs) if exs else None),
                    big=sum(r['big'] for r in rows), days=len(rows))
    H, M, L = roll('hi'), roll('mid'), roll('lo')
    # ③ 输出
    if a.line:
        if not H or not L:
            print('🔬 rv加权追踪: 累积中(尚无已结算数据)')
            return 0
        print(f'🔬 rv加权追踪(只读): 累计 {H["days"]}天 | '
              f'rv高 {H["per"]:+.2f}U/笔({H["n"]}笔, 到期单均{(H["exm"] or 0):+.1f}, ≥+33% {H["big"]}笔) vs '
              f'rv低 {L["per"]:+.2f}U/笔({L["n"]}笔, 到期单均{(L["exm"] or 0):+.1f}, ≥+33% {L["big"]}笔)'
              + (f' | 倍数 {H["per"]/L["per"]:.2f}x' if L['per'] > 0 and H['per'] > 0 else
                 f' | 差 {H["per"]-L["per"]:+.2f}U/笔'))
        return 0
    print(f'=== rv 池内加权追踪(只读观测) {d} ===')
    g = led.get(d)
    if g:
        print(f'  今日分组(rv日={g["rv_day"]}):')
        print(f'    rv高 {len(g["hi"])}只 {g["hi"]}')
        print(f'    rv中 {len(g["mid"])}只')
        print(f'    rv低 {len(g["lo"])}只 {g["lo"]}')
        if g['missing']:
            print(f'    ⚠️ 无1m数据: {g["missing"]}')
    print(f'\n  {"组":6s} {"已结算笔数":>9s} {"每笔U":>8s} {"到期单均":>9s} {"≥+33%笔":>8s} {"合计U":>9s}')
    for tag, lbl in (('hi', 'rv高'), ('mid', 'rv中'), ('lo', 'rv低')):
        r = roll(tag)
        if r:
            print(f'  {lbl:6s} {r["n"]:>9d} {r["per"]:>+8.2f} {(r["exm"] or 0):>+9.2f} {r["big"]:>8d} {r["tot"]:>+8.1f}')
    if H and L:
        cmp = '✅ 方向与回测一致' if H['per'] > L['per'] else '⚠️ 与回测方向相反'
        r = (f'{H["per"]/L["per"]:.2f}x' if L['per'] > 0 and H['per'] > 0 else f'差 {H["per"]-L["per"]:+.2f}U/笔')
        print(f'\n  → rv高 vs rv低 = {r}   {cmp}  (仅 {H["days"]} 天已结算, 样本极小 → 只作观测)')
    print(f'\n  账本: {LEDGER}  (每日追加; 终审节点与金矿格同步)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
