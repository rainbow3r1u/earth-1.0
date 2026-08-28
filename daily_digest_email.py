#!/usr/bin/env python3
"""每日晨报总览 — 合并原 4 封邮件为 1 封(9:00 发送):
  1) 交易摘要(今日预测/开仓/决策依据)      [原 8:19 每日预测日报]
  2) 2日验证命中率                          [原日报验证板块]
  3) 强势股续涨 + 每日资金榜                [原 8:27 daily_momentum_email]
  4) 系统健康(5项检查 + 服务健康报告)       [原 8:30 daily_health_check + 9:00 alert_monitor --report]
"""
import os, sys, json, datetime

BASE = '/home/myuser/websocket_new'
os.chdir(BASE)
sys.path.insert(0, BASE)
from alert_monitor import send_email

_KLINE_CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'


def _btc_klines():
    try:
        kl = json.load(open(_KLINE_CACHE))['klines']
        return next((kl[s] for s in kl if s.startswith('BTCUSDT') and len(kl[s]) > 200), None)
    except Exception:
        return None


def _btc_vol_at(date_str):
    """BTC 5日已实现波动%(人口std, 与2026-08-28分析口径一致): 指定预测日往前5根日K.
    发现: AUC_L 失效(8/18)领先 BTC 大波动(8/19 +7.13%) 1 天; r(vol,AUC_L)=-0.52."""
    btck = _btc_klines()
    if btck is None:
        return None
    try:
        ts_idx = {r['t']: i for i, r in enumerate(btck)}
        T = datetime.datetime.strptime(date_str, '%Y-%m-%d')
        t0 = int(datetime.datetime(T.year, T.month, T.day,
                                   tzinfo=datetime.timezone.utc).timestamp() * 1000)
        i = ts_idx.get(t0)
        if i is None or i < 4:
            return None
        rets = [btck[j]['c'] / btck[j]['o'] - 1 for j in range(i - 4, i + 1)]
        m = sum(rets) / len(rets)
        return (sum((v - m) ** 2 for v in rets) / len(rets)) ** 0.5 * 100
    except Exception:
        return None


def _btc_vol_latest():
    """最近5根已收盘BTC日K的已实现波动%(排除今日未收盘bar, 晨报09:00时=截至昨日)."""
    btck = _btc_klines()
    if btck is None:
        return None
    try:
        today0 = int(datetime.datetime.combine(datetime.date.today(), datetime.time(),
                        tzinfo=datetime.timezone.utc).timestamp() * 1000)
        rows = [r for r in btck if r['t'] < today0]
        if len(rows) < 5:
            return None
        rets = [r['c'] / r['o'] - 1 for r in rows[-5:]]
        m = sum(rets) / len(rets)
        return (sum((v - m) ** 2 for v in rets) / len(rets)) ** 0.5 * 100
    except Exception:
        return None


def _format_trade_summary():
    """读取今日 trade.log, 输出结构化中文摘要(替代原始日志行平铺)"""
    import daily_predictor as dp
    today_str = datetime.date.today().isoformat()
    log_file = '/home/myuser/.local/share/auto_trade/trade.log'
    try:
        with open(log_file) as f:
            lines = [l.strip() for l in f if today_str in l]
    except Exception:
        return '(今日无交易日志)'
    # 只取最后一次运行的日志(同日多次手动运行/重试会重复, 摘要只需最新一次)
    cut = 0
    for i, l in enumerate(lines):
        if '自动多空二选一交易启动' in l:
            cut = i
    lines = lines[cut:]
    if not lines:
        return '(今日无交易日志)'

    # 保持 tracker 新鲜(原 build_daily_report_body 内逻辑)
    try:
        dp.LOG_DIR = os.path.join(BASE, 'data')
        dp.TRACK_FILE = os.path.join(dp.LOG_DIR, 'prediction_tracker.json')
        dp.verify_yesterday()
    except Exception:
        pass

    def clean(l):
        c = l.split('] ', 1)[-1] if '] ' in l else l
        return c.replace('[PERM-TEST] ', '').strip()

    out = []
    # 1) 钱包状态(取最后一次)
    for l in reversed(lines):
        c = clean(l)
        if '钱包' in c:
            out.append(f'💰 钱包: {c.split("钱包:",1)[-1].strip()}')
            break
    # 2) 训练信息(每组只保留最后一次; 同日多次运行只显示最新)
    seen = {}
    for l in lines:
        c = clean(l)
        if c.startswith('样本:'):
            seen['sample'] = f'📚 {c}'
        if c.startswith('训练:'):
            seen['train'] = f'📚 {c}'
        if '特征维度' in c:
            seen['feat'] = f'🔬 {c}'
        if '预测日期' in c:
            seen['date'] = f'📅 {c}'
        if '板块热度' in c and '预计算' not in c:
            seen['sector'] = f'🗂 {c}'
    for k in ('sector', 'sample', 'train', 'date', 'feat'):
        if seen.get(k):
            out.append(seen[k])
    # 3) 置换检验(翻译成中文, 分侧, 只取最后一次)
    perm = {'LONG': None, 'SHORT': None}
    for l in lines:
        if '[PERM-TEST]' in l and 'Best=' in l:
            side = 'LONG' if '[LONG]' in l else ('SHORT' if '[SHORT]' in l else None)
            if side:
                import re
                m = re.search(r'normal=([\d.]+)% shuf=([\d.]+)% drop=([+\-][\d.]+)%', l)
                if m:
                    n, s, d = m.group(1), m.group(2), m.group(3)
                    verdict = '✅ 正常' if float(d) >= 5 else ('⚠️ 偏弱' if float(d) >= 0 else '❌ 过拟合')
                    perm[side] = f'{side} 侧: {verdict} (真实 {n}% vs 打乱 {s}%, drop {d}%)'
    if perm['LONG']:
        out.append(f'🧪 置换检验: {perm["LONG"]}')
    if perm['SHORT']:
        out.append(f'🧪 置换检验: {perm["SHORT"]}')
    # 4) 阻断/信号结论(只取最后一次)
    for l in reversed(lines):
        c = clean(l)
        if '过拟合' in c and '禁止' in c:
            out.append(f'⛔ {c}')
            break
    for l in reversed(lines):
        c = clean(l)
        if c.startswith('置信度不足'):
            out.append(f'🚫 {c}')
            break
    # 5) 今日信号(决策依据, 最后一次运行的 LONG+SHORT 都展示)
    decisions = {}
    for l in lines:
        c = clean(l)
        if '决策依据' in c:
            body = c.split('] ', 1)[-1] if '] ' in c else c
            if body.startswith('LONG'):
                decisions['long'] = f'🎯 {c}'
            elif body.startswith('SHORT'):
                decisions['short'] = f'🎯 {c}'
        elif ('信号' in c and 'prob=' in c and '开仓:' not in c):
            decisions['other'] = f'🎯 {c}'
    for k in ('long', 'short', 'other'):
        if decisions.get(k):
            out.append(decisions[k])
    # 6) 交易动作(2026-08-10 修: 多笔开仓全部列出, 不再只取最后一行)
    #    顺序: 按日志时间正序收集所有开仓/跳过/空仓动作行
    actions = []
    for l in lines:
        c = clean(l)
        if c.startswith('开仓:') or c.startswith('跳过交易') or c.startswith('本金不足') or c.startswith('空仓') or c.startswith('无有效信号'):
            actions.append(f'⚙️ {c}')
    if actions:
        out.extend(actions[:10])  # 上限10笔防刷屏(单日最多20笔多仓, 10个已足够)
    # 7) 兜底: 关键词行(过滤原始 PERM 英文长行)
    if len(out) <= 2:
        keywords = ['PERM-CAND', '做多概率', '做空概率', '止盈', '止损']
        for l in lines:
            c = clean(l)
            if any(k in c for k in keywords):
                out.append(c)
    return '\n'.join(out) if out else '(今日无交易日志)'


def section_forward():
    """前向结算(修正口径 1m): 7/28 起 TOP1, 只显示已满48h的日期, 与3.6/3.7统一口径."""
    try:
        import glob
        from datetime import datetime, timedelta, timezone
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import forward_settle as fs
        files = sorted(glob.glob(os.path.join(fs.PRED_DIR, 'pred_*.json')))
        now = datetime.now(timezone.utc)
        days = []
        for f in files:
            ds = os.path.basename(f).replace('pred_', '').replace('.json', '')
            if ds < '2026-07-28':
                continue
            try:
                maturity = datetime.strptime(ds, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=2, minutes=21)
            except Exception:
                continue
            if maturity <= now:
                days.append(ds)
        days.sort()
        results = fs.settle_days(days)
        return fs.tables_html(results)
    except Exception as e:
        return f'<p style="color:#c00">(前向结算生成失败: {e})</p>'


def section_trade():
    """交易摘要: 结构化中文摘要(替代原始日志平铺)"""
    try:
        return _format_trade_summary()
    except Exception as e:
        return f'(交易摘要读取失败: {e})'


def section_verify():
    """TOP10全开 近7天趋势 (48h 1m口径, 与3.6/3.7一致)."""
    try:
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import top10_forward as tf
        cache = tf.load_cache()
        if not cache:
            return '(暂无已结算数据)'
        days = sorted(cache)
        recent = days[-7:]
        lines = [f"\n=== 近{len(recent)}天趋势 · TOP10全开 (48h 1m) ==="]
        for ds in recent:
            r = cache[ds]
            pnls = [t['pnl'] for t in (r.get('long', []) + r.get('short', []))
                    if t.get('pnl') is not None]
            if not pnls:
                continue
            lines.append(f"  {ds}: {len(pnls)}笔 日均 {sum(pnls)/len(pnls):+.2f}% 合计 {sum(pnls):+.1f}%")
        recent_cache = {ds: cache[ds] for ds in recent if ds in cache}
        a = tf.agg(recent_cache)
        lines.append(f"  {len(recent_cache)}天汇总(48h等权复利): "
                     f"多空 {a['ALL']['cum']:+.1f}% | LONG {a['LONG']['cum']:+.1f}% | SHORT {a['SHORT']['cum']:+.1f}%")
        return '\n'.join(lines)
    except Exception as e:
        return f'(48h近7天趋势生成失败: {e})'


def section_long_top10():
    """LONG TOP10 列表 + 成交额(2026-08-08 用户新增)"""
    try:
        import glob
        preds = sorted(glob.glob(f'{BASE}/data/pred_2026-*.json'))
        if not preds:
            return '(无预测文件)'
        pred = json.load(open(preds[-1]))
        top10 = pred.get('top10_long', [])
        if not top10:
            return '(今日无 LONG TOP10)'
        # K线缓存成交额(最后1根 q = 24h 成交额 U)
        kl = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
        lines = [f"=== LONG TOP10 ({pred.get('date', '')[:10]}) ===",
                 f"{'#':>2} {'币种':<16} {'概率':>6} {'24h成交额':>10}"]
        for i, item in enumerate(top10[:10], 1):
            sym = item['symbol']
            p = float(item['prob'])
            p = p / 100 if p > 1 else p
            q = 0.0
            kls = kl.get(sym, [])
            if len(kls) >= 2:
                q = float(kls[-2].get('q', 0))   # 昨日完整成交额(最后1根是当日未收盘)
            if q >= 1e8:
                qs = f'{q/1e8:.2f}亿'
            else:
                qs = f'{q/1e6:.0f}M'
            lines.append(f'{i:>2} {sym:<16} {p*100:5.1f}% {qs:>10}')
        return '\n'.join(lines)
    except Exception as e:
        return f'(LONG TOP10 读取失败: {e})'


def section_top10_forward():
    """TOP10全开前向结算(8/3起, 1m口径): 多空全开/LONG全开/SHORT全开累计收益"""
    try:
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import top10_forward as tf
        cache = tf.update()  # 增量结算(历史已缓存, 只补新到期的天)
        a = tf.agg(cache)
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        rows = []
        for name, key in (('多空TOP10全开', 'ALL'), ('LONG TOP10全开', 'LONG'),
                          ('SHORT TOP10全开', 'SHORT')):
            g = a[key]
            color = '#0a0' if g['cum'] >= 0 else '#c00'
            avg_cell = (cell[:-2] + f";color:{color};'")  # 合并颜色进style
            rows.append(f"<tr><td {cell}><b>{name}</b></td>"
                        f"<td {cell}><b style='color:{color}'>{g['cum']:+.1f}%</b></td>"
                        f"<td {avg_cell}>{g['avg']:+.2f}%</td>"
                        f"<td {cell}>{g['settled']}</td>"
                        f"<td {cell}>{g['tp']}/{g['sl']}/{g['exp']}</td>"
                        f"<td {cell}>{g['days']}</td></tr>")
        return ("<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>组合</th><th {hd}>累计收益(复利)</th><th {hd}>笔均</th>"
                f"<th {hd}>笔数</th><th {hd}>TP/SL/48h</th><th {hd}>天数</th></tr>"
                + ''.join(rows) + "</table>"
                "<div style='font-size:10px;color:#666;'>口径: 08:21开仓, 1m结算, "
                "SL-5%/TP+10%/48h到期, 逐日等权复利; 毛收益未扣约0.2%/笔成本</div>")
    except Exception as e:
        return f'<p style="color:#c00">(TOP10前向结算生成失败: {e})</p>'


def section_top10_forward_u():
    """多空TOP10全开 逐日U盈亏: 固定名义300U/笔(保证金30U×10x), 不复利"""
    try:
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import top10_forward as tf
        cache = tf.load_cache()
        if not cache:
            return '<p style="color:#666">(暂无已结算数据)</p>'
        NOTIONAL, COST = 300.0, 0.002  # 名义300U/笔, 成本0.2%/笔(手续费+滑点)
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        rows = []
        cum = 0.0
        tot_n = 0
        for ds in sorted(cache):
            r = cache[ds]
            pnls = ([t['pnl'] for t in r['long'] if t.get('pnl') is not None] +
                    [t['pnl'] for t in r['short'] if t.get('pnl') is not None])
            if not pnls:
                continue
            day = NOTIONAL * sum(pnls) / 100 - len(pnls) * NOTIONAL * COST
            cum += day
            tot_n += len(pnls)
            c1 = '#0a0' if day >= 0 else '#c00'
            c2 = '#0a0' if cum >= 0 else '#c00'
            rows.append(f"<tr><td {cell}>{ds}</td>"
                        f"<td {cell}><b style='color:{c1}'>{day:+.1f}</b></td>"
                        f"<td {cell}><b style='color:{c2}'>{cum:+.1f}</b></td></tr>")
        c2 = '#0a0' if cum >= 0 else '#c00'
        rows.append(f"<tr><td {hd}><b>合计 {tot_n}笔</b></td><td {cell}></td>"
                    f"<td {cell}><b style='color:{c2}'>{cum:+.1f}U</b></td></tr>")
        return ("<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>日期</th><th {hd}>当日净盈亏(U)</th><th {hd}>累计(U)</th></tr>"
                + ''.join(rows) + "</table>"
                "<div style='font-size:10px;color:#666;'>固定名义300U/笔(保证金30U×10x杠杆), "
                "成本0.2%/笔(手续费+滑点), 不复利; 峰值40笔同时持仓占用保证金1200U</div>")
    except Exception as e:
        return f'<p style="color:#c00">(每日U盈亏生成失败: {e})</p>'


def section_hybrid():
    """混合结构影子臂 逐日U盈亏: 3.7 同款表格+同款哲学(只显示48h已全部到期的盖棺日)。
    LONG无止盈(SL5%/持有到期) + SHORT TP10%/SL5%, 08:21入场 strict48 全费用口径, 300U名义/笔."""
    try:
        hb_path = '/home/myuser/websocket_new/data/hybrid_tracker.json'
        if not os.path.exists(hb_path):
            return '<p style="color:#c00">(混合结构影子臂: 无存档, hybrid_tracker未运行?)</p>'
        hb = json.load(open(hb_path))
        days_h = sorted(hb.keys())
        if not days_h:
            return '<p style="color:#c00">(混合结构影子臂: 存档为空)</p>'
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        rows = []
        cum = 0.0
        n_days = 0
        pending_note = []
        for d in days_h:
            e = hb[d]
            done = e.get('n_settled', 0) >= e.get('n_total', 99)
            side_u = {'LONG': 0.0, 'SHORT': 0.0}
            side_n = {'LONG': 0, 'SHORT': 0}
            for t in e.get('trades', []):
                if t.get('net_u') is not None:
                    side_u[t['direction']] += t['net_u']
                    side_n[t['direction']] += 1
            if not done:
                # 3.7 哲学: 未到期日不进表, 浓缩成表注一行
                pnl_part = e.get('day_pnl_u', 0)
                pending_note.append(f"{d[5:]} 在持{e.get('n_total',0)-e.get('n_settled',0)}笔"
                                    f"(中途已结{e.get('n_settled',0)}笔{pnl_part:+.0f}U)")
                continue
            pnl = e.get('day_pnl_u', 0)
            cum += pnl
            n_days += 1
            c1 = '#0a0' if pnl >= 0 else '#c00'
            c2 = '#0a0' if cum >= 0 else '#c00'
            rows.append(
                f"<tr><td {cell}>{d}</td>"
                f"<td {cell}>L{side_n['LONG']}/{side_u['LONG']:+.0f} S{side_n['SHORT']}/{side_u['SHORT']:+.0f}</td>"
                f"<td {cell}><b style='color:{c1}'>{pnl:+.1f}</b></td>"
                f"<td {cell}><b style='color:{c2}'>{cum:+.1f}</b></td></tr>")
        c2 = '#0a0' if cum >= 0 else '#c00'
        rows.append(f"<tr><td {hd}><b>合计 {n_days}天</b></td><td {cell}></td>"
                    f"<td {cell}></td>"
                    f"<td {cell}><b style='color:{c2}'>{cum:+.1f}U</b></td></tr>")
        pending_html = ''
        if pending_note:
            pending_html = (f"<div style='font-size:10px;color:#888;'>未到期(不进表): "
                            + ' | '.join(pending_note) + '</div>')
        # S5 对照臂(8/25 上线): SHORT 只开 top5, LONG 同主臂; 两臂差 = SHORT6-10 净贡献
        s5_html = ''
        try:
            s5_path = '/home/myuser/websocket_new/data/hybrid_tracker_s5.json'
            if os.path.exists(s5_path):
                s5 = json.load(open(s5_path))
                s5_settled = [d for d in s5 if s5[d].get('n_settled',0) >= s5[d].get('n_total',99)]
                if s5_settled:
                    s5_tot = sum(s5[d].get('day_pnl_u',0) for d in s5_settled)
                    diff = s5_tot - cum  # cum = 主臂同期累计
                    c3 = '#0a0' if s5_tot >= 0 else '#c00'
                    c4 = '#0a0' if diff >= 0 else '#c00'
                    s5_html = ("<div style='font-size:11px;margin-top:4px;'>"
                               f"⚖️ S5对照臂(SHORT仅前5笔, LONG同主臂) {len(s5_settled)}天: "
                               f"<b style='color:{c3}'>{s5_tot:+.1f}U</b> vs 主臂 {cum:+.1f}U — "
                               f"SHORT6-10名净贡献 <b style='color:{c4}'>{-diff:+.1f}U</b>"
                               " (负=砍掉6-10更优)</div>")
        except Exception:
            pass
        return ("<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>日期</th><th {hd}>分侧(笔/U)</th>"
                f"<th {hd}>当日净盈亏(U)</th><th {hd}>累计(U)</th></tr>"
                + ''.join(rows) + "</table>"
                + pending_html + s5_html
                + "<div style='font-size:10px;color:#666;'>LONG无止盈(SL-5%持有到48h) + SHORT TP+10%/SL-5% | "
                "08:21入场 strict48 1m口径(保证金30U×10x杠杆=名义300U/笔), "
                "逐笔真实费用(taker+滑点+资金费), 不复利; 峰值40笔同时持仓占用保证金1200U</div>")
    except Exception as e:
        return f'<p style="color:#c00">(混合结构影子臂生成失败: {e})</p>'


def section_momentum():
    try:
        from daily_momentum_email import build_momentum_body_html
        return build_momentum_body_html()
    except Exception as e:
        return f'<p style="color:#c00">(强势股/资金榜生成失败: {e})</p>'


def section_forward_ic():
    """前向批作业(公证预测对答案): HTML 表格版。
    BTCvol 列带 regime 底色: 绿=平静(≤1.5%) / 黄=警戒(1.5-2%) / 红=高波动(>2%)。
    依据 2026-08-28 分析: r(vol,AUC_L)=-0.52 t=-3.79, 系统吃横盘期 alpha, vol 是利润周期主时钟。"""
    try:
        ic_path = '/home/myuser/websocket_new/data/forward_ic_history_48h.json'
        if not os.path.exists(ic_path):
            return '<p style="color:#c00">[前向批作业] ⚠️ 无历史(forward_ic_check未运行?)</p>'
        ih = json.load(open(ic_path))
        days = ih.get('days', [])
        clean = [d for d in days if not d.get('dirty')]
        if not days or not clean:
            return '<p style="color:#c00">[前向批作业] ⚠️ 暂无批作业记录</p>'
        last = clean[-1]
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        # 概览行
        def _f(v):
            return f'{v:+.2f}' if isinstance(v, (int, float)) else 'N/A'
        s5 = last.get('short5_avg_ret', 0)
        head = (f"最新: {last['date']} [干净期] n={last['n_sym']}币 "
                f"— LONG IC={_f(last.get('ic_long'))} AUC={last.get('auc_long')} | "
                f"SHORT IC={_f(last.get('ic_short'))} AUC={last.get('auc_short')} | "
                f"实际: LONG TOP1 {last.get('top1_long')} {last.get('top1_long_ret', 0):+.1f}% "
                f"/ 空前5均 {s5:+.1f}%"
                f"{' (空头盈利✅)' if s5 < 0 else ' (空头亏损⚠️)'}")
        # 7日表 (注意: 不能在一个td上放两个style属性, HTML只认第一个 — 颜色须合并进同一style)
        cell_css = "padding:2px 8px;border:1px solid #ccc;font-size:12px;"
        rows = []
        for d in clean[-7:]:
            v = _btc_vol_at(d['date'])
            # regime 底色: 绿=平静 黄=警戒 红=高波动
            if v is None:
                vc, vt = '#eee', 'N/A'
            elif v <= 1.5:
                vc, vt = '#c8e6c9', f'{v:.1f} 平静'
            elif v <= 2.0:
                vc, vt = '#fff9c4', f'{v:.1f} 警戒'
            else:
                vc, vt = '#ffcdd2', f'{v:.1f} 高波动'
            c_icl = '#c00' if (d.get('ic_long') or 0) < -0.15 else '#333'
            rows.append(
                f"<tr><td style='{cell_css}'>{d['date'][5:]}</td>"
                f"<td style='{cell_css}'><b style='color:{c_icl};'>{_f(d.get('ic_long'))}</b></td>"
                f"<td style='{cell_css}'>{_f(d.get('ic_short'))}</td>"
                f"<td style='{cell_css}'>{d.get('top1_long_ret', 0):+.1f}%</td>"
                f"<td style='{cell_css}'>{d.get('short5_avg_ret', 0):+.1f}%</td>"
                f"<td style='{cell_css}background:{vc};'><b>{vt}</b></td></tr>")
        # 判定行
        verdict_html = ''
        if len(clean) >= 3:
            l5 = clean[-5:]
            def _avg(key):
                vs = [d[key] for d in l5 if d.get(key) is not None]
                return sum(vs)/len(vs) if vs else None
            al, ash = _avg('auc_long'), _avg('auc_short')
            ml, ms = _avg('ic_long'), _avg('ic_short')
            m = min(al or 0, ash or 0)
            vc = '#0a0' if m >= 0.60 else ('#b8860b' if m >= 0.55 else '#c00')
            vv = '✅信号活着' if m >= 0.60 else ('⚠️转弱' if m >= 0.55 else '❌疑似失效')
            # IC方向异常: 期望 IC_L>0, IC_S<0 (5日均值口径, 与旧版一致)
            ic_bad = ((ml is not None and ml < 0) or (ms is not None and ms > 0))
            ic_note = (f" ⚠️IC方向异常(L={ml:+.2f}/S={ms:+.2f})"
                       f"<span style='color:#666;'>(应L正S负)</span>") if ic_bad else ''
            bg = 'background:#e8f5e9;border-left:4px solid #0a0;' if not ic_bad else 'background:#fff9c4;border-left:4px solid #b8860b;'
            verdict_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;{bg}'>"
                            f"判定(近{len(l5)}日AUC均值 L={al:.2f}/S={ash:.2f}, 阈值0.60): "
                            f"<b style='color:{vc};'>{vv}</b>{ic_note}</div>")
        # 预警区
        alert_html = ''
        al_last = last.get('auc_long')
        vol_at_aucday = _btc_vol_at(last['date'])
        vol_now = _btc_vol_latest()
        if isinstance(al_last, (int, float)) and vol_at_aucday is not None:
            if al_last < 0.55 and vol_at_aucday <= 2.0:
                state = ('BTC随后已放大(失效兑现期)' if (vol_now or 0) > 2.0
                         else 'BTC至今仍平静(领先大波动风险↑)')
                alert_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;"
                              f"background:#ffcdd2;border-left:4px solid #c00;'>"
                              f"🚨 <b>领先预警(8/18形态)</b>: {last['date'][5:]} AUC_L={al_last:.2f}已失效"
                              f" 而当日BTC波动仅{vol_at_aucday:.1f}%(平静) — 失效始于平静市而非已发冲击, "
                              f"{state} (n=1先例, 关注LONG仓位)</div>")
            elif al_last < 0.55:
                alert_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;"
                              f"background:#fff9c4;border-left:4px solid #b8860b;'>"
                              f"ℹ️ AUC_L={al_last:.2f}走弱 且当日BTC波动已放大({vol_at_aucday:.1f}%) — "
                              f"冲击兑现期(历史恢复3-5天), SHORT侧通常不受损</div>")
        return (f"<div style='font-size:12px;margin-bottom:4px;'>{head}</div>"
                "<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>日期</th><th {hd}>IC_L</th><th {hd}>IC_S</th>"
                f"<th {hd}>TOP1L</th><th {hd}>空前5</th><th {hd}>BTC 5日波动</th></tr>"
                + ''.join(rows) + "</table>"
                + verdict_html + alert_html
                + "<div style='font-size:10px;color:#666;margin-top:3px;'>"
                "口径注: 本节为48h日线口径(open[D]→close[D+2]), 与第2节/3.8节的1m结算口径不同, 仅评估排序质量非交易结算。"
                "BTCvol = BTC 5日已实现波动(底色: 绿≤1.5%平静 / 黄1.5~2%警戒 / 红>2%高波动)。"
                "系统吃横盘期alpha: r(vol,AUC_L)=-0.52 — 平静期双侧正常, 高波动期LONG分类力崩、SHORT独立alpha仍可, "
                "恢复期3-5天。IC_L深红(<-0.15)为排序显著反向。</div>")
    except Exception as e:
        return f'<p style="color:#c00">(前向批作业读取失败: {e})</p>'


def section_health():
    parts = []
    # 4a. 每日健康检查 4 项(原 daily_health_check.py; MD5同步检查已随观察端下线移除)
    try:
        import daily_health_check as dhc
        checks = []
        for name, fn in [('日志异常检查', dhc.check_trade_log),
                         ('特征NaN/Inf检查', dhc.check_feature_nan),
                         ('数据新鲜度', dhc.check_data_freshness),
                         ('持仓状态', dhc.check_positions)]:
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, f'检查执行失败: {e}'
            checks.append((name, ok, msg))
        all_ok = all(ok for _, ok, _ in checks)
        lines = [f'[健康检查] 总状态: {"OK" if all_ok else "ALERT"}', '']
        for name, ok, msg in checks:
            lines.append(f'{"✅" if ok else "❌"} {name}')
            lines.append(str(msg))
            lines.append('')
        parts.append('\n'.join(lines))
    except Exception as e:
        parts.append(f'(健康检查执行失败: {e})')
    # 4a2. SAMPLECHK 特征体检(幽灵防复发): 显示今日探针值 + 数量检查
    try:
        log_file = '/home/myuser/.local/share/auto_trade/trade.log'
        with open(log_file) as f:
            slines = [l.split('] ', 1)[-1].strip() for l in f if '[SAMPLECHK]' in l]
        if slines:
            slines = slines[-3:]
            parts.append('[SAMPLECHK 特征体检] ' + ('✅ 3/3' if len(slines) == 3 else f'⚠️ 仅{len(slines)}/3'))
            for s in slines:
                parts.append('  ' + s)
        else:
            parts.append('[SAMPLECHK 特征体检] ⚠️ 今日无记录(构建异常?)')
    except Exception as e:
        parts.append(f'(SAMPLECHK读取失败: {e})')
    # 4a3. 数据漂移监控(8/5 新增): 外部数据修订 + 重放探针校验 (data_drift_monitor.py)
    try:
        drift_path = '/home/myuser/websocket_new/data/drift_report.json'
        if os.path.exists(drift_path):
            dr = json.load(open(drift_path))
            if dr.get('status') == 'OK':
                parts.append(f"[数据漂移监控] ✅ OK (文件{len(dr.get('file_check', []))}项, "
                             f"重放探针: {dr.get('replay', {}).get('detail', 'SKIP')})")
            else:
                parts.append(f"[数据漂移监控] ⚠️ ALERT {len(dr.get('alerts', []))}项!")
                for a in dr.get('alerts', [])[:5]:
                    parts.append('  ⚠️ ' + a)
        else:
            parts.append('[数据漂移监控] ⚠️ 无报告(监控未运行?)')
    except Exception as e:
        parts.append(f'(数据漂移监控读取失败: {e})')
    # 4a4. 前向批作业(8/5 新增): 公证预测对答案 — 已升级为独立HTML小节 section_forward_ic(2026-08-28)
    pass
    # 4a5. 混合结构影子臂(8/24 新增): LONG无止盈+SHORT现行TP/SL, 1m全费用口径
    # 8/24 晚: 从纯文本升级为 3.7 同款 HTML 表格(独立章节 section_hybrid), 此处不再输出
    pass
    # 4b. 服务/接口健康报告(原 alert_monitor --report)
    try:
        from alert_monitor import generate_health_report
        parts.append('[服务健康报告]\n' + generate_health_report())
    except Exception as e:
        parts.append(f'(服务健康报告生成失败: {e})')
    return '\n'.join(parts)


def section_github_sync():
    """读取交易系统每日 GitHub 同步状态，写入晨报。"""
    try:
        status_path = '/home/myuser/websocket_new/logs/trading_system_sync_status.json'
        if not os.path.exists(status_path):
            return '[GitHub同步] ⚠️ 今日暂无同步状态(脚本未运行?)'
        st = json.load(open(status_path, encoding='utf-8'))
        d = st.get('date', '?')
        status = st.get('status', 'UNKNOWN')
        changed = st.get('changed', 0)
        removed = st.get('removed', 0)
        files = st.get('files', []) or []
        if status == 'NO_CHANGE':
            head = f'[GitHub同步] ✅ {d} 无变化，所有交易系统文件已与 GitHub 一致'
        elif status == 'CHANGED':
            head = f'[GitHub同步] 🔄 {d} 检测到 {changed} 个文件变化，已自动上传 GitHub'
        else:
            head = f'[GitHub同步] ⚠️ {d} 状态异常: {status}'
        lines = [head]
        if files:
            lines.append('变化文件:')
            for f in files[:20]:
                lines.append('  ' + f)
            if len(files) > 20:
                lines.append(f'  ... 共 {len(files)} 个')
        lines.append(f'删除/移除: {removed}')
        return '\n'.join(lines)
    except Exception as e:
        return f'[GitHub同步] ⚠️ 读取失败: {e}'


def main():
    today = datetime.date.today().isoformat()
    # 文本节转 pre; 第2节(前向结算)为 HTML 表格
    pre_style = ("style=\"white-space:pre-wrap;font-size:11px;"
                 "font-family:'SimHei','Microsoft YaHei','PingFang SC',Consolas,monospace;line-height:1.5;\"")
    # 口径标签: 绿=48h逻辑(与生产执行一致), 橙=72h逻辑(老日线口径, 仅参考)
    tag_style = ("font-size:11px;padding:1px 6px;border-radius:3px;"
                 "font-family:'SimHei','Microsoft YaHei';")
    tag48_exec = f"<span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>实盘执行规则: SL-5%/TP+10%/48h到期, 08:21开仓</span>"
    tag48 = f"<span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>48h逻辑 · 1m口径 · 08:21开仓 · SL-5%/TP+10%/48h到期</span>"
    tag72 = f"<span style='{tag_style}background:#fff3e0;color:#e65100;'>72h逻辑 · 日线口径(老) · open[T]入场 · 扫T~T+2三根日线 · 与实盘口径不同仅参考</span>"
    tag_none = f"<span style='{tag_style}background:#eee;color:#666;'>无结算口径</span>"
    body_html = f"""<h2 style="margin:0 0 8px;">晨报总览 {today}</h2>
<b>1. 交易摘要</b> {tag48_exec}
<pre {pre_style}>{section_trade()}</pre>
<b>2. 前向结算 TOP1 (1m修正口径)</b> {tag48}
{section_forward()}
<b>3. TOP10全开近7天趋势 (48h 1m口径)</b> {tag48}
<pre {pre_style}>{section_verify()}</pre>
<b>3.5 LONG TOP10 列表 + 成交额</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>今日预测 → 08:21已开仓(48h逻辑), 结算见3.6</span>
<pre {pre_style}>{section_long_top10()}</pre>
<b>3.6 TOP10全开前向结算 (8/3起)</b> {tag48}
{section_top10_forward()}
<b>3.7 多空TOP10全开 每日U盈亏 (固定名义300U/笔)</b> {tag48}
{section_top10_forward_u()}
<b>3.8 混合结构影子臂 每日U盈亏 (LONG无止盈+SHORT现行TP/SL)</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>影子验证 · 08:21开仓 · strict48 · 1m全费用 · 60天验证期至~10/23</span>
{section_hybrid()}
<b>4. 强势股续涨 + 每日资金榜</b> {tag_none}
<pre {pre_style}>{section_momentum()}</pre>
<b>5. 系统健康</b> {tag_none}
<pre {pre_style}>{section_health()}</pre>
<b>5.5 前向批作业 · 模型质量与BTC波动 regime</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>公证预测对答案 · 48h日线口径 · D+2确认</span>
{section_forward_ic()}
<b>6. GitHub 同步</b> {tag_none}
<pre {pre_style}>{section_github_sync()}</pre>"""
    send_email(f'晨报总览 {today}', '', body_html=body_html)
    print('digest sent')


if __name__ == '__main__':
    main()
