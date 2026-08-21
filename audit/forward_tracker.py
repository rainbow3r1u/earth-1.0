#!/usr/bin/env python3
"""前向结果自动存档 (2026-08-02, 用户决策: Top10 排序质量研究的数据基础)

- 每天结算 7/28 起全部预测日的 Top10(LONG+SHORT, 1m 修正口径, 48h 到期)
- 结果存档 data/forward_tracker.json (按预测日, 含每币: 方向/prob/结果/触发/对错/无止损48h/最大反向/入场/到期时刻)
- 幂等增量: 已全到期的日期跳过重算; 未到期的每次刷新
- cron: 10 9 * * * (晨报后), 只读+写存档, 不影响交易
"""
import os, sys, json, glob
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import forward_settle as fs

TRACKER = os.path.join(BASE, '..', 'data', 'forward_tracker.json')
START = '2026-07-28'

def load_tracker():
    try:
        return json.load(open(TRACKER))
    except Exception:
        return {}

def save_tracker(t):
    os.makedirs(os.path.dirname(TRACKER), exist_ok=True)
    with open(TRACKER, 'w') as f:
        json.dump(t, f, ensure_ascii=False, indent=1)

def is_all_settled(day_entry):
    trs = day_entry.get('trades', [])
    return bool(trs) and all(r.get('result') not in ('⏳未到期', '无数据') for r in trs)

def main():
    tracker = load_tracker()
    days = sorted(glob.glob(os.path.join(fs.PRED_DIR, 'pred_*.json')))
    days = [os.path.basename(f).replace('pred_', '').replace('.json', '') for f in days
            if os.path.basename(f) >= f'pred_{START}.json']
    now = datetime.now(timezone.utc).isoformat()
    settled_days = 0
    pending_days = 0
    for day in days:
        if day in tracker and is_all_settled(tracker[day]):
            settled_days += 1
            continue
        # 结算该日 Top10 (LONG+SHORT 分开)
        results = fs.settle_days([day], top10=True)
        trades = []
        for r in results:
            trades.append({
                'symbol': r['sym'], 'direction': r['direction'], 'prob': round(r['prob'], 1),
                'entry': r['entry'], 'result': r['result'], 'trigger': r['trigger'],
                'time': r['time'], 'price': r['price'],
                'dir_ok': r.get('dir_ok'), 'dir_ret': round(r['dir_ret'], 2) if r.get('dir_ret') is not None else None,
                'max_retrace': round(r['max_retrace'], 2) if r.get('max_retrace') is not None else None,
                'max_retrace_no_sl': round(r['max_retrace_no_sl'], 2) if r.get('max_retrace_no_sl') is not None else None,
            })
        tracker[day] = {'updated': now, 'trades': trades}
        if is_all_settled(tracker[day]):
            settled_days += 1
        else:
            pending_days += 1
        print(f'  {day}: {len(trades)} 笔结算', flush=True)
    save_tracker(tracker)
    print(f'[forward_tracker] 完成: 已到期日 {settled_days}, 未到期日 {pending_days}, 存档 {TRACKER}')

if __name__ == '__main__':
    main()
