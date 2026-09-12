#!/usr/bin/env python3
"""纠缠闸门 · 扩展样本驱动 (2026-09-13) — 在 GPU 上跑, 自包含, 不改动 gpu_backtest_exp.py 逻辑。

目的: 用户命题 = "BTC 没有方向(EMA7/EMA28 纠缠) → 山寨开始动 → 叠加系统 TOP10 正期望, 这条路对"。
      2026-09-13 用 65 天样本(649笔)无法判定(批级 t=1.23 · 币级 95%CI 跨零 · 止损率四档无差别)。
      GPU 端 by_day 缓存有 363 天, walk-forward 重训约 50 分钟即可把样本扩到 ~5.5 倍。

⚠️ 必须匹配生产口径 (用户 2026-09-13 强调): 生产 SOUP_ON=True —— 今日模型 + 最近2个历史模型概率平均,
   用于防抖动(否则每天选出的币不一样)。故本脚本 SOUP 默认开启。
   ⚠️ SOUP 状态是纯内存、不落盘 → **本脚本不可中断续跑**; 中断后前2天集成数不足会不可比。

设计(吸取三轮作用域/口径 bug 的教训):
  - 完全自包含: 只 import gpu_backtest_exp 复用"训练+预测"这一件事, 其余全部自己写
  - 候选名单 + 独立逐币结算 (T0 open 入场 / 扫 T0..T2 / 平 T2 open, 与影子档标定 93.4%)
    全部在循环内**边跑边落盘** → 随时可查, 中断也不丢
  - 不与引擎自身的组合结算(跳过在持等)混用 — 那套口径含"跳过"规则, 不适合逐币检验

用法(GPU 上, 约 50 分钟):
    cd ~/websocket_new
    env NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=0 \\
        OUT=/tmp/gate_soup.json DAYS=350 SOUP=1 \\
        python3 gate_dump_run.py
产出: /tmp/gate_soup.json = [{day, syms[], probs[], settled{sym:{net,trig}}}]
      settled 由**下一个预测日**在结算时回填(故最后2天无 settled)
"""
import os, sys, json, time, datetime

HOME = os.path.expanduser('~')
os.chdir(f'{HOME}/websocket_new')
sys.path.insert(0, f'{HOME}/websocket_new')

import gpu_backtest_exp as E      # 复用 load_klines_offline + train_and_predict_batch

OUT = os.environ.get('OUT', '/tmp/gate_soup.json')
DAYS = int(os.environ.get('DAYS', '350'))
STRIDE = int(os.environ.get('STRIDE', '1'))
SL_PCT = float(os.environ.get('SL_PCT', '8')) / 100.0
HOLD = 3                          # 入场日 T0 起算, 持有3根


def _dt(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')


def dump(day_map, path):
    with open(path, 'w') as f:
        json.dump([day_map[k] for k in sorted(day_map)], f)


def main():
    t0 = time.time()
    E.log(f'=== 纠缠闸门扩展样本驱动 === OUT={OUT} DAYS={DAYS} SOUP={E.SOUP_ON} SL={SL_PCT:.0%}')

    klines = E.load_klines_offline()
    sdays = sorted(int(f.replace('.npz', '')) for f in os.listdir(E.CACHE_DIR) if f.endswith('.npz'))
    END = len(sdays) - 1 - E.WF_OFFSET
    START = max(30, END - DAYS)
    tasks = []
    for d in range(START, END, STRIDE):
        if E.MODE == 'lag':
            pred_ts = sdays[d]
            entry_ts = sdays[d + 1] if d + 1 < len(sdays) else sdays[d]
            tr = sdays[max(0, d - E.TRAIN_WINDOW):d]
        elif E.MODE == 'nolag':
            pred_ts = entry_ts = sdays[d]
            tr = sdays[max(0, d - E.TRAIN_WINDOW):d - 1]
        else:
            pred_ts = entry_ts = sdays[d]
            tr = sdays[max(0, d - E.TRAIN_WINDOW):d - 2]
        tasks.append((tr, pred_ts, entry_ts))
    E.log(f'预测日 {len(tasks)} 个  ({_dt(tasks[0][2])} ~ {_dt(tasks[-1][2])})')

    day_map = {}
    day_order = []
    open_pos = {}        # (sym, entry_day) -> dict(entry, sl, held_days, entry_idx)
    soup_hist = []       # SOUP 时间集成(生产口径); 纯内存, 不可续跑
    for i, (tr, pred_ts, entry_ts) in enumerate(tasks):
        day = _dt(entry_ts)

        # ---- 1) 先把已持有到期的/触发SL的结算掉(用当日K线) ----
        for key in list(open_pos.keys()):
            sym, eday = key
            p = open_pos[key]
            kd = klines.get(sym)
            if not kd:
                del open_pos[key]
                continue
            ki = E.dp._find_kline_index(kd, entry_ts)
            if ki is None:
                continue
            pos_ki = p['entry_idx'] + p['held']
            if pos_ki >= len(kd):
                continue
            bar = kd[pos_ki]
            hit = bar['l'] <= p['sl']
            p['held'] += 1
            expired = p['held'] >= HOLD
            if hit or expired:
                exitp = p['sl'] if hit else kd[min(p['entry_idx'] + HOLD - 1, len(kd) - 1)]['o']
                rec = day_map.get(eday)
                if rec is not None:
                    rec.setdefault('settled', {})[sym] = {
                        'net': round((exitp / p['entry'] - 1) * 100, 3),
                        'trig': 0 if not hit else p['held'] - 1,
                        'entry': p['entry'],
                    }
                del open_pos[key]

        # ---- 2) 训练+预测当日 top10 (生产 SOUP 口径) ----
        r = E.train_and_predict_batch(tr, pred_ts, entry_ts, klines, soup_hist)
        if r and r[0] == 'top10':
            _, _, cands = r
            rec = {'day': day, 'syms': [c[0] for c in cands],
                   'probs': [round(c[1] * 100, 2) for c in cands], 'settled': {}}
            day_map[day] = rec
            day_order.append(day)
            # ---- 3) 登记新持仓(独立结算, 不受"跳过在持"影响) ----
            for sym, _prob in cands:
                kd = klines.get(sym)
                if not kd:
                    continue
                ki = E.dp._find_kline_index(kd, entry_ts)
                if ki is None or ki + HOLD - 1 >= len(kd):
                    continue
                ep = kd[ki]['o']
                if not ep or ep <= 0:
                    continue
                open_pos[(sym, day)] = {'entry': ep, 'sl': ep * (1 - SL_PCT),
                                        'held': 0, 'entry_idx': ki}

        if (i + 1) % 5 == 0 or i == len(tasks) - 1:
            dump(day_map, OUT)
            el = time.time() - t0
            E.log(f'  {i+1}/{len(tasks)} 候选 {len(day_map)} 天 在持 {len(open_pos)}  '
                  f'用时 {el:.0f}s 预计总 {el/(i+1)*len(tasks):.0f}s')
    dump(day_map, OUT)
    ns = sum(len(v.get('settled') or {}) for v in day_map.values())
    E.log(f'完成: {len(day_map)} 天候选, {ns} 笔已结算 -> {OUT}  总用时 {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
