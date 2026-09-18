#!/usr/bin/env python3
"""Telegram 群定时推送: 今日20单信号 + 8/3以来每日收益.
环境变量:
  TG_BOT_TOKEN   @BotFather 给的 token
  TG_CHAT_ID     群/频道 id
不配置时本地预览。
"""
import json, os, sys, glob
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data'
CACHE = DATA / 'top10_forward_cache.json'

def _load_env_file():
    env_path = Path.home() / '.telegram_bot.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env_file()


def load_today_signals(date_str=None):
    if date_str is None:
        files = sorted(glob.glob(str(DATA / 'pred_2026-*.json')))
        if not files:
            return None, '无预测文件'
        date_str = Path(files[-1]).stem.replace('pred_', '')
    p = DATA / f'pred_{date_str}.json'
    if not p.exists():
        return None, f'无 {date_str} 预测文件'
    pred = json.load(open(p))
    lines = [f"📊 {date_str} 开仓信号（多空各TOP10）", "=== LONG ==="]
    for i, item in enumerate(pred.get('top10_long', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("=== SHORT ===")
    for i, item in enumerate(pred.get('top10_short', [])[:10], 1):
        lines.append(f"{i}. {item['symbol']}  {float(item['prob']):.1f}%")
    lines.append("⚠️ 仅作研究信号，不构成投资建议")
    return date_str, '\n'.join(lines)

def load_daily_pnl():
    if not CACHE.exists():
        return '暂无 top10_forward_cache'
    cache = json.load(open(CACHE))
    lines = ["📈 多空TOP10全开 每日收益（48h 1m口径）"]
    cum = 0.0
    NOTIONAL, COST = 300.0, 0.002
    total_n = 0
    for ds in sorted(cache):
        r = cache[ds]
        pnls = ([t['pnl'] for t in r.get('long', []) if t.get('pnl') is not None] +
                [t['pnl'] for t in r.get('short', []) if t.get('pnl') is not None])
        if not pnls:
            continue
        day = NOTIONAL * sum(pnls) / 100 - len(pnls) * NOTIONAL * COST
        cum += day
        total_n += len(pnls)
        lines.append(f"{ds}: {day:+.1f}U  累计 {cum:+.1f}U")
    lines.append(f"合计 {total_n}笔, 累计 {cum:+.1f}U")
    lines.append("⚠️ 模拟口径，非实盘收益")
    return '\n'.join(lines)

def send_tg(message):
    token = os.getenv('TG_BOT_TOKEN')
    chat_id = os.getenv('TG_CHAT_ID')
    if not token or not chat_id:
        print("========== 本地预览 ==========")
        print(message)
        print("==============================")
        return
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={'chat_id': chat_id, 'text': message}, timeout=15)
    print('TG send status', r.status_code, r.text[:200])
def load_liu_status():
    """刘账户资金三件套 + 持仓列表(按"距止盈由近到远"排序)。

    2026-09-17 用户指令: "把这个cron内容改成刘权益输出内容, 时间改成每30分钟发一次,
    并且附带持仓列表, 并且有还有多少止盈的排序, 距离越近排序越靠前"

    口径:
      · 资金三件套 = 钱包余额(totalWalletBalance) / 未实现盈亏(totalUnrealizedProfit)
                     / 总权益(totalMarginBalance), 与 liu-equity SKILL 同源
      · 距止盈 = (TP挂价 / 现价 − 1) × 100%  ← **当前价格还要涨多少才碰到止盈**
        数值越小 = 越接近触发 → 排越前
      · 已过 48h 窗口的单没有 TP 单 → 排在最后(它们当天不会再止盈, 只能等第3天到期)
    """
    import datetime
    sys.path.insert(0, str(BASE / 'audit'))
    import hybrid_live as H

    CST = datetime.timezone(datetime.timedelta(hours=8))
    acct = H.signed('GET', '/fapi/v2/account')
    wallet = float(acct['totalWalletBalance'])
    upnl = float(acct['totalUnrealizedProfit'])
    equity = float(acct['totalMarginBalance'])

    rate = None
    try:
        import requests
        rate = float(requests.get('https://open.er-api.com/v6/latest/USD', timeout=8).json()['rates']['CNY'])
    except Exception:
        try:
            import requests
            rate = float(requests.get('https://api.frankfurter.app/latest?from=USD&to=CNY', timeout=8).json()['rates']['CNY'])
        except Exception:
            rate = None

    st = json.load(open(DATA / 'hybrid_live_state.json'))
    now_ms = int(__import__('time').time() * 1000)

    # ★ 2026-09-17 修: 必须与**交易所实时持仓**核对, 不能只读账本。
    #   起因: 用户 22:15 的消息里看到 COTIUSDT 被归到"无止盈单(已过48h放开)" ——
    #   真相是它 22:10:05 止盈成交了, 但账本要等 :41 对账才落账(最多滞后34分钟);
    #   而 TP 条件单触发后即消失, 于是"账本还在 + TP单没了"被误判成"放开跑"。
    pr = H.signed('GET', '/fapi/v2/positionRisk')
    live_syms = {p['symbol'] for p in pr if abs(float(p.get('positionAmt', 0))) > 0} \
        if isinstance(pr, list) else set(st['open'])

    rows, no_tp, just_closed = [], [], []
    for sym, p in st['open'].items():
        try:
            age = (now_ms - p['open_time']) / 3600000
            if sym not in live_syms:
                # 已离场但账本未落账 → 查交易所成交, 报出真实结果
                aos = H.signed('GET', '/fapi/v1/allOrders',
                               {'symbol': sym, 'startTime': p['open_time'] - 2000, 'limit': 50})
                outs = [o for o in (aos if isinstance(aos, list) else [])
                        if o.get('side') == ('SELL' if p['direction'] == 'LONG' else 'BUY')
                        and o.get('status') == 'FILLED' and float(o.get('executedQty') or 0) > 0]
                if outs:
                    last = max(outs, key=lambda o: o.get('updateTime', 0))
                    xp = float(last['avgPrice'])
                    pnl = float(p['qty']) * ((xp - float(p['entry'])) if p['direction'] == 'LONG'
                                             else (float(p['entry']) - xp))
                    kind = '止盈' if p.get('tp_price') and abs(xp - float(p['tp_price'])) / float(p['tp_price']) < 0.02 \
                        else ('止损' if p.get('sl_price') and abs(xp - float(p['sl_price'])) / float(p['sl_price']) < 0.02 else '离场')
                    et = datetime.datetime.fromtimestamp(int(last['updateTime']) / 1000,
                                                         tz=datetime.timezone.utc).astimezone(CST)
                    just_closed.append((sym, kind, (xp / float(p['entry']) - 1) * 100, pnl, et))
                continue
            cur = H.get_price(sym) or float(p['entry'])
            ids, r = H.open_algo_ids(sym)
            L = [o for o in (r if isinstance(r, list) else [])]
            tp = [o for o in L if 'TAKE_PROFIT' in str(o.get('orderType'))]
            tpv = float(tp[0]['triggerPrice']) if tp else 0.0
            pnl = float(p['qty']) * (cur - float(p['entry']))
            if tpv:
                rows.append((sym, age, float(p['entry']), cur, tpv, pnl))
            elif now_ms < H.nominal_tp_end_ms(p):
                # 窗口内却没有TP单 → 异常(挂单丢失), 不是"放开跑"
                no_tp.append((sym, age, pnl, True))
            else:
                no_tp.append((sym, age, pnl, False))
        except Exception:
            no_tp.append((sym, (now_ms - p['open_time']) / 3600000, 0.0, True))

    # 距TP 由近到远(升序)
    rows.sort(key=lambda z: (z[4] / z[3] - 1))
    no_tp.sort(key=lambda z: z[1])
    just_closed.sort(key=lambda z: z[4])

    L = []
    L.append(f"💼 刘账户 · {datetime.datetime.now(CST):%m-%d %H:%M}")
    L.append("")
    if rate:
        L.append(f"钱包余额    {wallet:>9,.2f}U ≈ {wallet*rate:>10,.2f} CNY")
        L.append(f"未实现盈亏  {upnl:>+9,.2f}U")
        L.append(f"总权益      {equity:>9,.2f}U ≈ {equity*rate:>10,.2f} CNY")
    else:
        L.append(f"钱包余额    {wallet:>9,.2f}U")
        L.append(f"未实现盈亏  {upnl:>+9,.2f}U")
        L.append(f"总权益      {equity:>9,.2f}U")
    if just_closed:
        L.append("")
        for sym, kind, pct, pnl, et in just_closed:
            L.append(f"✅ 刚{kind}: {sym} {pct:+.2f}% ({pnl:+.2f}U) {et:%H:%M}")
        L.append("    (已成交, 待 :41 对账落账)")
    L.append("")
    # ★ 2026-09-18 修 bug: 原标题写 `持仓 {len(rows)} 笔` —— 而 rows 只含**有止盈单的**持仓,
    #   已过48h窗口(无TP单)的持仓被漏计 ⇒ 用户看到"还是只有12笔", 实际是 12(有TP) + 5(已过窗口) = 17。
    #   现在: 标题报**总数**; 无TP的也编号并入同一列表(排在末尾), 使"标题数字 = 列表行数"恒成立。
    n_total = len(rows) + len(no_tp)
    _note = f" (其中 {len(no_tp)} 笔已过48h窗口、无TP单, 排在末尾)" if no_tp else ""
    L.append(f"📋 持仓 {n_total} 笔{_note}")
    L.append("─" * 30)
    for i, (sym, age, e, cur, tpv, pnl) in enumerate(rows, 1):
        gap = (tpv / cur - 1) * 100
        cur_pct = (cur / e - 1) * 100
        L.append(f"{i:>2}. {sym}")
        L.append(f"    距TP {gap:>5.2f}%  |  现浮 {cur_pct:>+6.2f}% ({pnl:>+6.2f}U)  |  持{age:>4.0f}h")
    # 无TP单的持仓: 继续编号, 标明原因(与"标题数字=行数"一致)
    for k, (sym, age, pnl, is_anom) in enumerate(no_tp, len(rows) + 1):
        tag = "⚠️ 窗口内却无TP单(挂单丢失, 等对账重挂)" if is_anom else "已过48h窗口, 第3天放开跑(不上TP)"
        L.append(f"{k:>2}. {sym}")
        L.append(f"    无TP单 — {tag}  |  现浮 {pnl:>+6.2f}U  |  持{age:>4.0f}h")
    # 当日 08:21 刚开的批次 → 展示"触发即落袋"的金额
    if rows:
        gain = float(st['open'][rows[0][0]]['qty']) * (rows[0][4] - rows[0][2])
        L.append("─" * 30)
        L.append(f"🎯 最近一笔: {rows[0][0]} 还差 {rows[0][4]/rows[0][3]-1:+.2%} → 触发落袋约 {gain:+.2f}U")
    return '\n'.join(L)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'signal'
    if mode == 'pnl':
        send_tg(load_daily_pnl())
    elif mode == 'liu':
        send_tg(load_liu_status())
    else:
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        ds, msg = load_today_signals(date_str)
        send_tg(msg)
