#!/usr/bin/env python3
"""前向批作业 (Forward IC Check) — 用兑现的K线给公证过的预测对答案

每天取 N 天前的 pred_YYYY-MM-DD.json (当日8:20已git push公证, 预测先于结果, 不可篡改),
与该币实际 48h 收益 (close[T+1]/open[T]-1, 48h 日线口径) 对照, 计算:
  - 横截面 rank IC (Spearman): 概率排序 vs 实际收益排序 (LONG应为正, SHORT应为负)
  - AUC: LONG概率区分 ">+5%" 的能力 / SHORT概率区分 "<-5%" 的能力
  - 交易视角: LONG TOP1 实际收益 / SHORT 前5平均实际收益
结果追加到 data/forward_ic_history_48h.json, 晨报 4a4 节读取展示。

判定 (干净期近5日滚动, **以 AUC 为主判** — 2026-09-12 更正: 原文档写 |IC|>=0.10/0.05~0.10 与代码不符):
  AUC均值 min(L,S) >= 0.60 = 信号活着; 0.55~0.60 = 转弱; < 0.55 = 疑似失效(熔断线候选)。
  IC_L/IC_S 均值同时打印, 方向异常(IC_L<0 或 IC_S>0)附加提示。阈值初始拍定, 待分布积累后校准。

用法: python3 forward_ic_check.py          # 幂等: 补评所有已兑现但未记录的预测日
Cron: 50 8 * * *  (K线缓存8:05已刷新, 9:00晨报前)
"""
import os, sys, json, glob
import numpy as np
from datetime import date, datetime, timedelta, timezone

BASE = '/home/myuser/websocket_new'
PRED_DIR = os.path.join(BASE, 'data')
KLINE_CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
HIST_PATH = os.path.join(BASE, 'data', 'forward_ic_history_48h.json')
DIRTY_UNTIL = '2026-08-02'   # 幽灵期(列错位模型)最后一天, 之前预测仅作阴性对照
DAY_MS = 86400_000
MIN_SYMBOLS = 50             # 有效币种低于此数视为未兑现/未更新, 跳过
MIN_COVER = 300              # 2026-09-12: 已入库但 n_sym 低于此值视为"覆盖不足", 允许重评并替换(宇宙约520~546)

def log(msg):
    print(f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] {msg}', flush=True)

def _fmt(v, spec='+.3f'):
    """None 安全格式化 (2026-09-12): IC/AUC 可为 None(如全市场无币 48h>+5% 时 auc 返回 None),
    原直接 `{v:+.3f}` 会 TypeError 且发生在写盘之前 → 该日永不入库、每天复发、整条 IC 管道卡死。"""
    return format(v, spec) if isinstance(v, (int, float)) else 'None'

def _avg_ranks(x):
    """tie-aware 平均秩 (纯numpy)"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    order = np.argsort(x, kind='mergesort')
    ranks = np.empty(n)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and x[order[j + 1]] == x[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks

def spearman(a, b):
    if len(a) < 5:
        return None
    r = np.corrcoef(_avg_ranks(a), _avg_ranks(b))[0, 1]
    return float(r) if np.isfinite(r) else None

def auc(scores, y):
    y = np.asarray(y).astype(bool)
    n_pos, n_neg = int(y.sum()), int(len(y) - y.sum())
    if n_pos == 0 or n_neg == 0:
        return None
    r = _avg_ranks(np.asarray(scores, dtype=float))
    return float((r[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))

class KlineIndex:
    """每币 t→行 的 searchsorted 索引, 支持任意历史日随机访问"""
    def __init__(self, klines):
        self.ts, self.os, self.cs, self.qs = {}, {}, {}, {}
        for sym, rows in klines.items():
            if not rows:
                continue
            self.ts[sym] = np.array([r['t'] for r in rows], dtype=np.int64)
            self.os[sym] = np.array([r.get('o', np.nan) for r in rows], dtype=float)
            self.cs[sym] = np.array([r.get('c', np.nan) for r in rows], dtype=float)
            self.qs[sym] = np.array([r.get('q', np.nan) for r in rows], dtype=float)

    def get(self, sym, t_open, t_close1):
        """返回 (open[T], close[T+1]) 或 None (48h 日线口径)"""
        ts = self.ts.get(sym)
        if ts is None or len(ts) == 0:
            return None
        i = np.searchsorted(ts, t_open)
        if i >= len(ts) or ts[i] != t_open:
            return None
        j = np.searchsorted(ts, t_close1)
        if j >= len(ts) or ts[j] != t_close1:
            return None
        o, c = self.os[sym][i], self.cs[sym][j]
        if not (o > 0 and c > 0 and np.isfinite(o) and np.isfinite(c)):
            return None
        return o, c

    def liq_ok(self, sym, t_open):
        """预测日前5根已收盘K线的平均成交额 >= 50万U (生产宇宙口径)"""
        ts = self.ts.get(sym)
        if ts is None:
            return False
        i = np.searchsorted(ts, t_open)
        if i < 5:
            return False
        q5 = self.qs[sym][i - 5:i]
        return bool(np.nanmean(q5) >= 5e5)

def load_history():
    if os.path.exists(HIST_PATH):
        try:
            return json.load(open(HIST_PATH))
        except Exception:
            pass
    return {'updated': None, 'days': []}

def evaluate_date(d_str, kx):
    """对预测日 d_str 批作业; 返回 dict 或 None(未兑现/文件缺失/旧格式)"""
    path = os.path.join(PRED_DIR, f'pred_{d_str}.json')
    if not os.path.exists(path):
        return None
    try:
        pred = json.load(open(path))
    except Exception as e:
        log(f'  {d_str}: pred文件损坏 {e}')
        return None
    all_long, all_short = pred.get('all_long'), pred.get('all_short')
    if not all_long or not all_short:
        return None  # 旧格式(无全量概率), 跳过

    T = datetime.strptime(d_str, '%Y-%m-%d').date()
    t_open = int(datetime(T.year, T.month, T.day, tzinfo=timezone.utc).timestamp() * 1000)   # open[T] (UTC 00:00)
    t_close1 = t_open + DAY_MS                                           # close[T+1] (48h)

    sym_ret = {}
    for sym in {s for s, _ in all_long} | {s for s, _ in all_short}:
        oc = kx.get(sym, t_open, t_close1)
        if oc:
            sym_ret[sym] = oc[1] / oc[0] - 1.0
    if len(sym_ret) < MIN_SYMBOLS:
        log(f'  {d_str}: 仅{len(sym_ret)}币兑现, 跳过(K线未含该窗口?)')
        return None

    def _num(pairs):
        out = {}
        for s, p in pairs:
            try:
                out[s] = float(p)
            except (TypeError, ValueError):
                pass
        return out
    pl = {s: p for s, p in _num(all_long).items() if s in sym_ret}
    ps = {s: p for s, p in _num(all_short).items() if s in sym_ret}
    if len(pl) < MIN_SYMBOLS or len(ps) < MIN_SYMBOLS:
        return None
    ic_long = spearman(list(pl.values()), [sym_ret[s] for s in pl])
    ic_short = spearman(list(ps.values()), [sym_ret[s] for s in ps])
    auc_long = auc(list(pl.values()), np.array([sym_ret[s] > 0.05 for s in pl]))
    auc_short = auc(list(ps.values()), np.array([sym_ret[s] < -0.05 for s in ps]))

    liq_l = {s: p for s, p in pl.items() if kx.liq_ok(s, t_open)}
    liq_s = {s: p for s, p in ps.items() if kx.liq_ok(s, t_open)}
    ic_long_liq = spearman(list(liq_l.values()), [sym_ret[s] for s in liq_l]) if len(liq_l) >= 30 else None
    ic_short_liq = spearman(list(liq_s.values()), [sym_ret[s] for s in liq_s]) if len(liq_s) >= 30 else None

    top1 = max(pl.items(), key=lambda kv: kv[1])
    top1_short = max(ps.items(), key=lambda kv: kv[1])
    short5 = sorted(ps.items(), key=lambda kv: -kv[1])[:5]
    res = {
        'date': d_str,
        'dirty': d_str <= DIRTY_UNTIL,
        'n_sym': len(sym_ret), 'n_liq': len(liq_l),
        'ic_long': round(ic_long, 4) if ic_long is not None else None,
        'ic_short': round(ic_short, 4) if ic_short is not None else None,
        'auc_long': round(auc_long, 4) if auc_long is not None else None,
        'auc_short': round(auc_short, 4) if auc_short is not None else None,
        'ic_long_liq': round(ic_long_liq, 4) if ic_long_liq is not None else None,
        'ic_short_liq': round(ic_short_liq, 4) if ic_short_liq is not None else None,
        'top1_long': top1[0], 'top1_long_prob': top1[1],
        'top1_long_ret': round(sym_ret[top1[0]] * 100, 2),
        'top1_short': top1_short[0], 'top1_short_ret': round(sym_ret[top1_short[0]] * 100, 2),
        'short5_avg_ret': round(float(np.mean([sym_ret[s] for s, _ in short5])) * 100, 2),
    }
    # 2026-09-12 修: 原 note 赋值写在 return 之后(死代码, 且引用未定义的 res) → note 永不落档
    if pred.get('note'):
        res['note'] = pred['note'][:120]
    return res

def main():
    hist = load_history()
    done = {d['date']: d for d in hist['days']}
    log('加载K线缓存...')
    kx = KlineIndex(json.load(open(KLINE_CACHE))['klines'])
    max_t = max(ts[-1] for ts in kx.ts.values() if len(ts))
    eval_max = datetime.fromtimestamp(max_t / 1000, tz=timezone.utc).date() - timedelta(days=2)   # 48h口径: T+1收盘须已走完(最新一根可能未收盘)

    new_days = []
    for path in sorted(glob.glob(os.path.join(PRED_DIR, 'pred_*.json'))):
        d_str = os.path.basename(path)[5:-5]
        try:
            T = datetime.strptime(d_str, '%Y-%m-%d').date()
        except ValueError:
            continue
        # 2026-09-12 修: 原 `d_str in done` 永久跳过 → 首次评估时若K线缓存只覆盖部分宇宙
        #(≥MIN_SYMBOLS 但远少于全量)会永久留下有偏 IC。现覆盖不足(<MIN_COVER)的日子允许重评并替换。
        if T > eval_max:
            continue
        prev = done.get(d_str)
        if prev is not None and prev.get('n_sym', 0) >= MIN_COVER:
            continue
        res = evaluate_date(d_str, kx)
        if res:
            new_days.append(res)
            tag = '幽灵期' if res['dirty'] else '干净期'
            re_tag = f'[重评 n={prev.get("n_sym")}→{res["n_sym"]}]' if prev is not None else ''
            log(f"  评 {d_str} [{tag}]{re_tag} n={res['n_sym']}: IC_L={_fmt(res['ic_long'])} "
                f"IC_S={_fmt(res['ic_short'])} AUC_L={_fmt(res['auc_long'], '.3f')} AUC_S={_fmt(res['auc_short'], '.3f')} "
                f"| TOP1L {res['top1_long']} {_fmt(res['top1_long_ret'], '+.1f')}% | 空5均 {_fmt(res['short5_avg_ret'], '+.1f')}%")

    if new_days:
        n_add = sum(1 for r in new_days if r['date'] not in done)
        n_upd = len(new_days) - n_add
        repl = {r['date']: r for r in new_days}
        hist['days'] = sorted([d for d in hist['days'] if d['date'] not in repl] + new_days,
                              key=lambda d: d['date'])
        hist['updated'] = datetime.now().isoformat()
        tmp = HIST_PATH + '.tmp'
        json.dump(hist, open(tmp, 'w'), ensure_ascii=False, indent=1)
        os.replace(tmp, HIST_PATH)
        log(f'新增 {n_add} 天/重评替换 {n_upd} 天, 历史累计 {len(hist["days"])} 天 → {HIST_PATH}')
    else:
        log('无新的可评估预测日')

    # 分期汇总 (幽灵期=阴性对照, 干净期=真考核)
    for tag, sel in [('幽灵期(阴性对照)', lambda d: d['dirty']), ('干净期', lambda d: not d['dirty'])]:
        days = [d for d in hist['days'] if sel(d)]
        if not days:
            continue
        mls = [d['ic_long'] for d in days if d['ic_long'] is not None]
        mss = [d['ic_short'] for d in days if d['ic_short'] is not None]
        # 2026-09-12 修: 空列表 np.mean([])→NaN 会静默打印 nan 而无意义
        log(f'{tag} {len(days)}天: IC_L均值={np.mean(mls):+.3f} IC_S均值={np.mean(mss):+.3f}'
            if (mls and mss) else f'{tag} {len(days)}天: IC缺失, 跳过均值')
    clean = [d for d in hist['days'] if not d['dirty']]
    if len(clean) >= 3:
        last5 = clean[-5:]
        als = [d['auc_long'] for d in last5 if d.get('auc_long') is not None]
        ass = [d['auc_short'] for d in last5 if d.get('auc_short') is not None]
        mls = [d['ic_long'] for d in last5 if d['ic_long'] is not None]
        mss = [d['ic_short'] for d in last5 if d.get('ic_short') is not None]
        # 2026-09-12 修: 原空列表 np.mean([])→NaN, `min(NaN,...)` 比较恒 False → 静默打出 "❌疑似失效"
        if not als or not ass:
            log(f'干净期近{len(last5)}日 AUC 数据缺失(L={len(als)}/S={len(ass)}), 跳过判定')
        else:
            al, ash = float(np.mean(als)), float(np.mean(ass))
            ml = float(np.mean(mls)) if mls else float('nan')
            ms = float(np.mean(mss)) if mss else float('nan')
            m = min(al, ash)
            verdict = '✅信号活着' if m >= 0.60 else ('⚠️转弱' if m >= 0.55 else '❌疑似失效')
            bad_dir = (mls and ml < 0) or (mss and ms > 0)
            note = f' ⚠️IC方向异常(L={ml:+.2f}/S={ms:+.2f}), 头部选币质量需盯' if bad_dir else ''
            log(f'干净期近{len(last5)}日 AUC均值: L={al:.3f}/S={ash:.3f}, IC: L={ml:+.3f}/S={ms:+.3f} → {verdict}{note}')

    # 2026-09-11: 静默采集 regime-IC 对应关系, 不额外挂cron
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit'))
        import regime_ic_log
        regime_ic_log.main(quiet=True)
    except Exception as _e:
        log(f'regime_ic_log 静默采集失败: {_e}')

if __name__ == '__main__':
    main()
