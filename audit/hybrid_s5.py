#!/usr/bin/env python3
"""SHORT TOP5 对照臂 (2026-08-25 上线, 从主影子臂派生)

假设来源: 全宇宙概率快照 21 天分析 — SHORT 胜率/收益高度浓缩在前 5 名
  (1-5名: 胜率61.9% 平均+9.47% | 6-10名: 43.8% -2.97% | 尾部: 9.4%)
  且利润几乎全部产生于 BTC 横盘期(8/3~8/18 +996%), 暴拉三天(-111%)是唯一亏损段
  → 头部 alpha 是独立选币行情, 非大盘 beta。

对照设计 (与主臂唯一差异 = SHORT 只开前 5 笔):
  主臂  hybrid_tracker.json:    LONG top10(无TP,SL5%,持到48h) + SHORT top10(TP10/SL5)
  本臂  hybrid_tracker_s5.json: LONG 同主臂 + SHORT 仅 top5(TP10/SL5)
  两臂差 = SHORT 6-10 名那 5 笔的净贡献, 直接可算。

实现: settle_hybrid 结果确定于 (sym,date,direction,prob), 主臂已全部结算,
      本臂纯派生(读主臂存档, LONG 原样 + SHORT 方向取前5), 零 API 调用。
      前提: 主臂 trades 顺序 = LONG top10(概率序) + SHORT top10(概率序)。
cron: 25 9 * * * (主臂 09:20 之后)
晨报: daily_digest_email.py 3.8 节对照行。
"""
import os, json
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(BASE, '..', 'data', 'hybrid_tracker.json')
S5 = os.path.join(BASE, '..', 'data', 'hybrid_tracker_s5.json')

def derive():
    if not os.path.exists(MAIN):
        print('主臂存档不存在, 跳过'); return
    main = json.load(open(MAIN))
    out = {}
    for day, e in main.items():
        trades = e.get('trades', [])
        # SHORT 按出现顺序取前5(= 概率降序 top5), LONG 原样
        n_short = 0
        s5_trades = []
        for t in trades:
            if t.get('direction') == 'SHORT':
                n_short += 1
                if n_short <= 5:
                    s5_trades.append(t)
            else:
                s5_trades.append(t)
        ok = [t for t in s5_trades if t.get('net_u') is not None]
        out[day] = {'updated': datetime.now(timezone.utc).isoformat(),
                    'derived_from': 'hybrid_tracker.json',
                    'day_pnl_u': round(sum(t['net_u'] for t in ok), 1),
                    'n_settled': len(ok), 'n_total': len(s5_trades),
                    'trades': s5_trades}
    with open(S5, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    settled = [d for d in out if out[d]['n_settled'] >= out[d]['n_total']]
    tot = sum(out[d]['day_pnl_u'] for d in settled)
    # 主臂对照
    m_settled = [d for d in main if main[d].get('n_settled',0) >= main[d].get('n_total',99)]
    m_tot = sum(main[d].get('day_pnl_u',0) for d in m_settled)
    print(f'[S5臂] 已到期{len(settled)}天: S5 {tot:+.1f}U vs 主臂 {m_tot:+.1f}U '
          f'(SHORT6-10贡献 {m_tot-tot:+.1f}U) → {S5}')

if __name__ == '__main__':
    derive()
