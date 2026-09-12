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


def _perm_test_latest():
    """今日最后一次运行的 PERM-TEST 结果: {side: (normal%, shuf%, drop%)}.
    置换检验 = 防模型过拟合的当天体检(08:21即出, 无需等D+2)。drop>=5 健康, 0~5 偏弱, <0 过拟合。"""
    out = {}
    try:
        today = datetime.date.today().isoformat()
        with open('/home/myuser/.local/share/auto_trade/trade.log') as f:
            lines = [l.strip() for l in f if today in l]
        for l in lines:
            if '[PERM-TEST]' in l and 'Best=' in l:
                side = 'LONG' if '[LONG]' in l else ('SHORT' if '[SHORT]' in l else None)
                if side:
                    import re
                    m = re.search(r'normal=([\d.]+)% shuf=([\d.]+)% drop=([+\-][\d.]+)%', l)
                    if m:
                        out[side] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    except Exception:
        pass
    return out


def build_btc_alt_chart(days=30):
    """5.5b BTC vs 山寨走势对比图 (2026-09-02 用户需求): matplotlib PNG 供邮件CID内嵌.
    布局: 上=双线(BTC/山寨中位数, 均rebase到0%) 中=BTC 5日已实现vol柱(四灯同色) 下=山寨横截面离散度柱.
    数据源: notusdt_1d_full.json 已收盘日K (09:00晨报时=截至昨日), 山寨=非BTC全宇宙(K线≥60).
    返回 (png_path, 最新状态dict) 或 (None, None)."""
    try:
        import numpy as np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        with open(_KLINE_CACHE) as f:
            kl = json.load(f)['klines']
        btck = next(kl[s] for s in kl if s.startswith('BTCUSDT') and len(kl[s]) > 200)
        today0 = int(datetime.datetime.combine(datetime.date.today(), datetime.time(),
                        tzinfo=datetime.timezone.utc).timestamp() * 1000)
        btc_rows = [r for r in btck if r['t'] < today0][-days:]
        if len(btc_rows) < 10:
            return None, None
        # 山寨横截面: 每个样本日的 全宇宙日收益(中位数/离散度), K线>=60 天, 排除BTC
        tset = {r['t'] for r in btc_rows}
        xs = {t: [] for t in tset}
        for sym, kls in kl.items():
            if sym.startswith('BTCUSDT') or len(kls) < 60:
                continue
            for r in kls:
                if r['t'] in xs and r['o'] > 0:
                    xs[r['t']].append((r['c'] / r['o'] - 1) * 100)
        btc_ret = [(r['c'] / r['o'] - 1) * 100 for r in btc_rows]
        med_ret, disp = [], []
        for r in btc_rows:
            a = xs[r['t']]
            med_ret.append(float(np.median(a)) if len(a) >= 50 else 0.0)
            disp.append(float(np.std(a)) if len(a) >= 50 else 0.0)
        # rebase 到 0%
        btc_eq, alt_eq, b, a_ = [], [], 1.0, 1.0
        for br, mr in zip(btc_ret, med_ret):
            b *= (1 + br / 100); a_ *= (1 + mr / 100)
            btc_eq.append((b - 1) * 100)
            alt_eq.append((a_ - 1) * 100)
        # BTC vol5 (人口std, 与四灯/5.5节完全同口径)
        btc_vol5 = []
        for i in range(len(btc_rows)):
            if i < 4:
                btc_vol5.append(0.0)
                continue
            rets5 = btc_ret[i-4:i+1]
            m = sum(rets5) / 5
            btc_vol5.append((sum((v - m) ** 2 for v in rets5) / 5) ** 0.5)
        dstr = [datetime.datetime.fromtimestamp(r['t'] / 1000,
                tz=datetime.timezone.utc).strftime('%m-%d') for r in btc_rows]
        x = list(range(len(dstr)))
        # ==== 画图 (无中文字体, 标签用英文) ====
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(7.6, 6.2), dpi=140,
                                            sharex=True, gridspec_kw={'height_ratios': [2, 1, 1]})
        ax1.axhline(0, color='#999', lw=0.6, ls='--')
        ax1.plot(x, btc_eq, color='#F7931A', lw=1.8, label=f'BTC (last {btc_eq[-1]:+.1f}%)')
        ax1.plot(x, alt_eq, color='#4472C4', lw=1.8, label=f'Altcoin median (last {alt_eq[-1]:+.1f}%)')
        ax1.fill_between(x, btc_eq, alt_eq, where=[b >= a for b, a in zip(btc_eq, alt_eq)],
                          color='#F7931A', alpha=0.06, interpolate=True)
        ax1.fill_between(x, btc_eq, alt_eq, where=[b < a for b, a in zip(btc_eq, alt_eq)],
                          color='#4472C4', alpha=0.06, interpolate=True)
        ax1.set_ylabel('Rebased %')
        ax1.legend(loc='best', fontsize=8, framealpha=0.9)
        ax1.set_title(f'BTC vs Altcoin Median — last {len(dstr)}d (closed bars)', fontsize=10)
        ax1.grid(alpha=0.25, lw=0.4)
        # BTC vol5: 四灯同色 绿<=1.5 黄1.5~2 红>2
        c2 = ['#2e7d32' if v <= 1.5 else ('#f9a825' if v <= 2.0 else '#c62828') for v in btc_vol5]
        ax2.bar(x, btc_vol5, color=c2, width=0.72)
        ax2.axhline(1.5, color='#f9a825', lw=0.7, ls='--'); ax2.axhline(2.0, color='#c62828', lw=0.7, ls='--')
        ax2.set_ylabel('BTC vol5 %')
        ax2.grid(alpha=0.25, lw=0.4, axis='y')
        # 山寨横截面离散度: 绿<=6 黄6~8 红>8 (候选第5灯, 8/30=7.4 黄警示)
        c3 = ['#2e7d32' if v <= 6 else ('#f9a825' if v <= 8.0 else '#c62828') for v in disp]
        ax3.bar(x, disp, color=c3, width=0.72)
        ax3.axhline(6.0, color='#f9a825', lw=0.7, ls='--'); ax3.axhline(8.0, color='#c62828', lw=0.7, ls='--')
        ax3.set_ylabel('Alt XS-disp %')
        ax3.grid(alpha=0.25, lw=0.4, axis='y')
        step = max(1, len(dstr) // 10)
        ax3.set_xticks(x[::step]); ax3.set_xticklabels(dstr[::step], fontsize=7)
        for ax in (ax1, ax2, ax3):
            for s in ('top', 'right'):
                ax.spines[s].set_visible(False)
        fig.align_ylabels()
        fig.tight_layout()
        out_dir = os.path.join(BASE, 'data', 'charts')
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f'btc_alt_{datetime.date.today().isoformat()}.png')
        fig.savefig(out, bbox_inches='tight')
        plt.close(fig)
        state = {'btc_last': round(btc_eq[-1], 1), 'alt_last': round(alt_eq[-1], 1),
                 'btc_vol5': round(btc_vol5[-1], 2), 'disp_last': round(disp[-1], 2),
                 'n_days': len(dstr)}
        return out, state
    except Exception as e:
        import traceback
        print(f'[chart] 生成失败: {e}\n{traceback.format_exc()}')
        return None, None


def _refresh_tracker():
    """晨报前刷新预测tracker(2026-09-06 从 _format_trade_summary 移出: 格式化函数不应有写盘副作用)."""
    try:
        import daily_predictor as dp
        dp.LOG_DIR = os.path.join(BASE, 'data')
        dp.TRACK_FILE = os.path.join(dp.LOG_DIR, 'prediction_tracker.json')
        dp.verify_yesterday()
    except Exception:
        pass


def _format_trade_summary():
    """读取今日 trade.log, 输出结构化中文摘要(替代原始日志行平铺)"""
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
    """LONG TOP10 列表 + 成交额(2026-08-08 用户新增).
    2026-09-06 修: 优先今日pred文件, 缺失时明示'旧数据'而非静默冒充今日; glob 去2026死锁."""
    try:
        import glob
        today = datetime.date.today().isoformat()
        today_f = f'{BASE}/data/pred_{today}.json'
        if os.path.exists(today_f):
            pf, stale = today_f, False
        else:
            preds = sorted(glob.glob(f'{BASE}/data/pred_*.json'))
            pf, stale = (preds[-1], True) if preds else (None, False)
        if not pf:
            return '(无预测文件)'
        pred = json.load(open(pf))
        top10 = pred.get('top10_long', [])
        if not top10:
            return '(今日无 LONG TOP10)'
        # K线缓存成交额(最后1根 q = 24h 成交额 U)
        kl = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
        stale_tag = ' ⚠️旧数据(今日预测尚未生成!)' if stale else ''
        lines = [f"=== LONG TOP10 ({pred.get('date', '')[:10]}){stale_tag} ===",
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
                    # 2026-09-06 修: 主臂只累加与S5相同的已结算日(两臂上线日不同/结算进度不同天
                    # 会造成口径错位, 9/6前直接减主臂全部历史累计)
                    main_same = [d for d in s5_settled
                                 if d in hb and hb[d].get('n_settled',0) >= hb[d].get('n_total',99)]
                    main_tot = sum(hb[d].get('day_pnl_u',0) for d in main_same)
                    diff = s5_tot - main_tot  # 同窗口对照
                    c3 = '#0a0' if s5_tot >= 0 else '#c00'
                    c4 = '#0a0' if diff >= 0 else '#c00'
                    s5_html = ("<div style='font-size:11px;margin-top:4px;'>"
                               f"⚖️ S5对照臂(SHORT仅前5笔, LONG同主臂) {len(s5_settled)}天: "
                               f"<b style='color:{c3}'>{s5_tot:+.1f}U</b> vs 主臂同期({len(main_same)}天) {main_tot:+.1f}U — "
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


def section_2x2():
    """3.8b 影子臂 2×2 结构对照 (2026-09-12 用户批准加入晨报).

    为什么需要它: 实盘 9/3(果)/9/8(米) 把「持有时长 48h→72h」与「止损 5%→8%」**同时**改了,
    而原影子对照臂两项都没动 → 10/23 终审做 "72h vs 48h" 对比时 SL 变量未被控制(归因被污染)。
    本表把 (SL5/SL8)×(48h/72h) 四档并排: 主档=SL5/48h, 另三档由 08:46 cron 与主档同源同日生成,
    因此"每笔U"之差**只**来自结构参数, 并给出单变量增量, 直接回答"收益提升来自哪一项"。

    口径: LONG 侧(实盘为纯LONG臂)、300U 名义、08:21 入场、1m 全费用、已含修正后资金费口径。
    缺档/读取失败均优雅降级, 不影响晨报其余部分。
    """
    try:
        D = '/home/myuser/websocket_new/data'
        files = [('base',   'SL-5%/48h <b>(原基线)</b>', 'hybrid_tracker.json'),
                 ('sl8',    'SL-8%/48h',                 'hybrid_tracker_sl8.json'),
                 ('h72',    'SL-5%/72h',                 'hybrid_tracker_h72.json'),
                 ('sl8h72', 'SL-8%/72h <b>(实盘现行)</b>', 'hybrid_tracker_sl8h72.json')]
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        stats, missing = {}, []
        for key, label, fn in files:
            p = os.path.join(D, fn)
            if not os.path.exists(p):
                missing.append(fn)
                continue
            try:
                d = json.load(open(p))
            except Exception as e:
                missing.append(f'{fn}(读取失败:{e})')
                continue
            tr = [t for day in d for t in d[day].get('trades', [])
                  if t.get('direction') == 'LONG' and t.get('net_u') is not None]
            if not tr:
                continue
            sl = [t for t in tr if t.get('trigger') == '止损']
            ex = [t for t in tr if t.get('trigger') == '到期']
            tot = sum(t['net_u'] for t in tr)
            stats[key] = {'label': label, 'n': len(tr), 'tot': tot, 'per': tot / len(tr),
                          'exp': len(ex) / len(tr) * 100,
                          'slm': sum(t['net_u'] for t in sl) / max(len(sl), 1),
                          'exm': sum(t['net_u'] for t in ex) / max(len(ex), 1)}
        if not stats:
            return '<p style="color:#c00">(2×2对照: 四档均不可读, 检查 data/hybrid_tracker*.json)</p>'
        rows = []
        for key, label, _ in files:
            s = stats.get(key)
            if not s:
                continue
            c = '#0a0' if s['per'] >= 0 else '#c00'
            rows.append(f"<tr><td {cell}>{label}</td><td {cell}>{s['n']}</td>"
                        f"<td {cell}>{s['tot']:+.1f}</td>"
                        f"<td {cell}><b style='color:{c}'>{s['per']:+.2f}</b></td>"
                        f"<td {cell}>{s['exp']:.1f}%</td><td {cell}>{s['slm']:+.2f}</td>"
                        f"<td {cell}>{s['exm']:+.2f}</td></tr>")
        dlt = ''
        b = stats.get('base')
        if b and b['per']:
            parts = []
            for key, tag in (('sl8', '仅改止损'), ('h72', '仅改持有'), ('sl8h72', '两者同时')):
                s = stats.get(key)
                if s:
                    parts.append(f"{tag} <b>{s['per'] - b['per']:+.2f}U/笔({(s['per'] / b['per'] - 1) * 100:+.0f}%)</b>")
            if parts:
                dlt = ("<div style='font-size:11px;color:#333;margin-top:3px;'>"
                       "<b>单变量增量(vs 原基线)</b>: " + ' | '.join(parts) + "</div>")
        table = ("<table style='border-collapse:collapse;'>"
                 f"<tr><th {hd}>结构(LONG侧 · 300U名义)</th><th {hd}>笔数</th><th {hd}>累计U</th>"
                 f"<th {hd}>每笔U</th><th {hd}>到期率</th><th {hd}>止损单均</th><th {hd}>到期单均</th></tr>"
                 + ''.join(rows) + "</table>")
        note = ("<div style='font-size:10px;color:#666;'>同源同日(同一批pred、同一天08:21入场) → "
                "差异<b>只</b>来自结构参数 | 变体档由 08:46 cron 与主档同源维护(独立文件, 不覆盖主档) | "
                "到期率≠到期胜率 | 单变量读数: 仅改持有(72h)≈全部收益提升, 仅放宽止损为次要放大器</div>")
        if missing:
            note = (f"<div style='font-size:10px;color:#c00;'>⚠️ 缺档: {', '.join(missing)}"
                    "(08:46 cron 未跑/失败? 查 logs/hybrid_tracker_variants.log)</div>") + note
        return table + dlt + note
    except Exception as e:
        return f'<p style="color:#c00">(2×2对照生成失败: {e})</p>'


def section_residual():
    """RESIDUAL影子臂 3.9: LONG残差标签模型TOP10 vs 主臂LONG 同规则对照 (2026-09-01上线).
    治LONG标签beta假阳性: 残差标签=币48hret-当日宇宙中位>5pp (GPU 180d双窗 Sharpe 32.75/26.89 vs 基线21.33/22.12).
    结算规则与3.8主臂LONG完全相同: 无止盈/SL-5%/48h/08:21入场/300U名义/1m全费用."""
    try:
        rs_path = '/home/myuser/websocket_new/data/residual_tracker.json'
        hb_path = '/home/myuser/websocket_new/data/hybrid_tracker.json'
        # 今日影子臂选币 (pred 文件 top10_long_residual 字段)
        picks_html = ''
        today_str = datetime.date.today().isoformat()
        try:
            pf = json.load(open(f'/home/myuser/websocket_new/data/pred_{today_str}.json'))
            cands = pf.get('top10_long_residual', [])
            if cands:
                picks_html = ("<div style='font-size:11px;margin-top:4px;'><b>今日影子臂LONG TOP10:</b> "
                              + ' | '.join(f"{c['symbol']}({c['prob']:.0f}%)" for c in cands) + '</div>')
            else:
                picks_html = ("<div style='font-size:10px;color:#888;'>今日pred无top10_long_residual字段 "
                              "(影子臂9/1上线, 自9/2预测起生效)</div>")
        except Exception:
            pass
        if not os.path.exists(rs_path):
            return ('<p style="color:#888">(RESIDUAL影子臂: 尚无存档, residual_tracker 首次结算预计9/4)</p>'
                    + picks_html)
        rs = json.load(open(rs_path))
        hb = json.load(open(hb_path)) if os.path.exists(hb_path) else {}
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        rows = []
        cum_r = 0.0
        cum_h = 0.0
        n_days = 0
        pending_note = []
        for d in sorted(rs.keys()):
            e = rs[d]
            if not e.get('trades'):
                continue  # 无选币日(字段缺失), 不进表
            done = e.get('n_settled', 0) >= e.get('n_total', 99)
            if not done:
                pending_note.append(f"{d[5:]} 在持{e.get('n_total',0)-e.get('n_settled',0)}笔")
                continue
            # 2026-09-12 修: 原只要求**残差臂**当天已盖棺 —— 主臂未盖棺时下面的 h_u 只累加已结算部分,
            # 差额会被静默扭曲(主臂每日 20 笔 vs 残差 10 笔, 结算更易滞后)。改为与 S5 节同款闸门:
            # 两侧当天都盖棺才进对照表, 否则记入表注。
            _hb_day = hb.get(d, {})
            if not _hb_day or _hb_day.get('n_settled', 0) < _hb_day.get('n_total', 99):
                pending_note.append(f"{d[5:]} 主臂未盖棺({_hb_day.get('n_settled', 0)}/{_hb_day.get('n_total', 0)}), 暂不入对照")
                continue
            r_u = e.get('day_pnl_u', 0)
            h_u = sum(t['net_u'] for t in _hb_day.get('trades', [])
                      if t.get('net_u') is not None and t.get('direction') == 'LONG')
            cum_r += r_u
            cum_h += h_u
            n_days += 1
            diff = r_u - h_u
            c1 = '#0a0' if r_u >= 0 else '#c00'
            c4 = '#0a0' if diff >= 0 else '#c00'
            c5 = '#0a0' if (cum_r - cum_h) >= 0 else '#c00'
            rows.append(
                f"<tr><td {cell}>{d}</td>"
                f"<td {cell}>{e.get('n_settled',0)}笔</td>"
                f"<td {cell}><b style='color:{c1}'>{r_u:+.1f}</b></td>"
                f"<td {cell}>{h_u:+.1f}</td>"
                f"<td {cell}><b style='color:{c4}'>{diff:+.1f}</b></td>"
                f"<td {cell}><b style='color:{c5}'>{cum_r - cum_h:+.1f}</b></td></tr>")
        if rows:
            c2 = '#0a0' if cum_r >= 0 else '#c00'
            c6 = '#0a0' if (cum_r - cum_h) >= 0 else '#c00'
            rows.append(f"<tr><td {hd}><b>合计 {n_days}天</b></td><td {cell}></td>"
                        f"<td {cell}><b style='color:{c2}'>{cum_r:+.1f}U</b></td>"
                        f"<td {cell}>{cum_h:+.1f}U</td><td {cell}></td>"
                        f"<td {cell}><b style='color:{c6}'>{cum_r - cum_h:+.1f}U</b></td></tr>")
        else:
            rows.append(f"<tr><td {cell} colspan='6' style='color:#888;'>尚无盖棺日(首笔9/2开仓, 9/4首次结算)</td></tr>")
        pending_html = ''
        if pending_note:
            pending_html = (f"<div style='font-size:10px;color:#888;'>未到期(不进表): "
                            + ' | '.join(pending_note) + '</div>')
        return ("<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>日期</th><th {hd}>笔数</th>"
                f"<th {hd}>影子臂当日U</th><th {hd}>主臂LONG当日U</th>"
                f"<th {hd}>当日差(影子-主)</th><th {hd}>累计差</th></tr>"
                + ''.join(rows) + "</table>"
                + pending_html + picks_html
                + "<div style='font-size:10px;color:#666;'>影子臂=残差标签LONG模型TOP10(币48hret-宇宙中位>5pp, "
                "治beta假阳性) | 出场与主臂LONG完全相同: 无止盈/SL-5%/48h | SHORT与主臂相同不重复结算 | "
                "GPU 180d双窗验证 Sharpe 32.75/26.89 vs 基线21.33/22.12 | 纯旁路不影响实盘</div>")
    except Exception as e:
        return f'<p style="color:#c00">(RESIDUAL影子臂生成失败: {e})</p>'


def section_residual_picks():
    """3.9b 主LONG vs 残差LONG 当日选币差异 (2026-09-03 用户需求):
    当日对照表(重合币/各自独有币+双方概率) + 近7日重合度趋势。
    解读: 重合度持续>70%=两模型本质同源, 残差边际改进有限; 40-60%摆动=残差在看不同的东西。"""
    try:
        import datetime as _dt
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        today_str = _dt.date.today().isoformat()

        def _load(day):
            try:
                d = json.load(open(f'/home/myuser/websocket_new/data/pred_{day}.json'))
                ml = d.get('top10_long') or []
                rl_ = d.get('top10_long_residual') or []
                if ml and rl_:
                    return {c['symbol']: c['prob'] for c in ml}, {c['symbol']: c['prob'] for c in rl_}
            except Exception:
                pass
            return None, None

        # ==== 当日对照 ====
        ml, rl_ = _load(today_str)
        if ml is None:
            return ("<div style='font-size:11px;color:#888;'>(3.9b 选币差异: 今日pred尚无双臂TOP10)</div>")
        overlap = [s for s in rl_ if s in ml]
        only_r = [s for s in rl_ if s not in ml]
        only_m = [s for s in ml if s not in rl_]
        # 重合行
        rows = []
        for s in overlap:
            rows.append(f"<tr><td {cell}>{s}</td><td {cell}>{ml[s]:.0f}%</td><td {cell}>{rl_[s]:.0f}%</td>"
                        f"<td {cell} style='color:#888;'>重合</td></tr>")
        # 残差独有 (高亮: 残差臂实盘开的就是这些+重合币)
        for s in only_r:
            rows.append(f"<tr><td {cell}><b>{s}</b></td><td {cell} style='color:#999;'>—</td>"
                        f"<td {cell}><b style='color:#1565c0;'>{rl_[s]:.0f}%</b></td>"
                        f"<td {cell} style='background:#e3f2fd;color:#1565c0;'>残差独有</td></tr>")
        # 主臂独有
        for s in only_m:
            rows.append(f"<tr><td {cell}>{s}</td><td {cell}>{ml[s]:.0f}%</td>"
                        f"<td {cell} style='color:#999;'>—</td>"
                        f"<td {cell} style='color:#e65100;'>主臂独有</td></tr>")
        # 2026-09-06 修: 分母用实际榜单长度(榜单<10币时 *10 会失真)
        den = len(rl_) if rl_ else 1
        pct = round(len(overlap) / den * 100)
        # ==== 近7日重合度趋势 ====
        trend = []
        for i in range(6, -1, -1):
            day = (_dt.date.today() - _dt.timedelta(days=i)).isoformat()
            m2, r2 = _load(day)
            if m2 is not None:
                ov = sum(1 for s in r2 if s in m2)
                trend.append((day[5:], round(ov / len(r2) * 100) if r2 else 0))
        trend_html = ''
        if len(trend) >= 2:
            trend_html = ("<div style='font-size:11px;margin-top:4px;'>近7日重合度: "
                          + ' | '.join(f"{d}:{p}%" for d, p in trend) + "</div>")
        return (f"<div style='font-size:12px;margin:2px 0 6px;'>今日重合度: "
                f"<b>{len(overlap)}/{den} ({pct}%)</b> — 残差独有 {len(only_r)} 币, 主臂独有 {len(only_m)} 币</div>"
                "<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>币种</th><th {hd}>主臂概率</th><th {hd}>残差概率</th><th {hd}>归属</th></tr>"
                + ''.join(rows) + "</table>" + trend_html
                + "<div style='font-size:10px;color:#666;'>重合币=两模型共识(自身动量强); 残差独有币=已证明相对强度"
                "(跑赢宇宙中位)但绝对涨幅未达标主臂阈值的币 | 重合度>70%=两模型同源边际改进有限; 40~60%=在看不同的东西 | "
                "果实盘持仓=主LONG榜(2026-09-13起由残差榜切换); 残差臂转为纯影子采集</div>")
    except Exception as e:
        return f'<p style="color:#c00">(选币差异生成失败: {e})</p>'


def section_residual_survival():
    """3.9c 果实盘批次生存表 (2026-09-03 用户需求; 2026-09-13 起选币由残差榜切换为主LONG榜):
    每批'开仓N笔→存活/止损/到期'动态 + 存活率%。直观看出每批选币的成色衰减速度。
    数据: residual_live_state.json 的 days(开仓名单)/open(在持)/history(已离场, 含trigger)。"""
    try:
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        hd = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:#f0f0f0;'"
        sp = '/home/myuser/websocket_new/data/residual_live_state.json'
        if not os.path.exists(sp):
            return "<div style='font-size:11px;color:#888;'>(3.9c 批次生存表: 无实盘state)</div>"
        st = json.load(open(sp))
        days_opened = st.get('days', {})
        open_pos = list(st.get('open', {}).values())
        # 已离场按批分组
        gone = {}
        for r in st.get('history', []):
            if r.get('trigger') in ('链路测试', '链路测试2(algoSL)'):
                continue  # 排除测试单
            gone.setdefault(r['date'], []).append(r)
        rows = []
        for d in sorted(days_opened.keys()):
            opened = [s for s in days_opened[d].get('opened', [])]
            if not opened or d < '2026-09-02':
                continue  # 9/2起正式策略批
            # 存活按持仓的批次标签归属: 重开仓(如9/2批BROCCOLI714在9/5批重启)计入新批次, 不再虚增老批次存活数
            alive = [s for s in opened
                     if any(p.get('symbol') == s and p.get('date') == d for p in open_pos)]
            stopped = [r for r in gone.get(d, []) if r.get('trigger') == '止损']
            expired = [r for r in gone.get(d, []) if r.get('trigger') == '到期']
            other = len(gone.get(d, [])) - len(stopped) - len(expired)
            surv = len(alive) / len(opened) * 100 if opened else 0
            # 存活率配色: 绿≥70 黄40-70 红<40
            sc = '#0a0' if surv >= 70 else ('#b8860b' if surv >= 40 else '#c00')
            stop_u = sum(r.get('net_u', 0) for r in stopped)
            exp_u = sum(r.get('net_u', 0) for r in expired)
            rows.append(
                f"<tr><td {cell}>{d}</td><td {cell}>{len(opened)}笔</td>"
                f"<td {cell}><b style='color:{sc};'>{len(alive)}</b></td>"
                f"<td {cell}>{len(stopped)}</td><td {cell}>{len(expired)}</td>"
                f"<td {cell}>{other}</td>"
                f"<td {cell}><b style='color:{sc};'>{surv:.0f}%</b></td>"
                f"<td {cell}>{stop_u:+.1f}</td><td {cell}>{exp_u:+.1f}</td></tr>")
        if not rows:
            return "<div style='font-size:11px;color:#888;'>(3.9c 批次生存表: 尚无正式批次)</div>"
        return ("<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>批次</th><th {hd}>开仓</th><th {hd}>存活</th><th {hd}>止损</th>"
                f"<th {hd}>到期平</th><th {hd}>其他</th><th {hd}>存活率</th>"
                f"<th {hd}>止损净U</th><th {hd}>到期净U</th></tr>"
                + ''.join(rows) + "</table>"
                + "<div style='font-size:10px;color:#666;'>果实盘每批生存动态 (9/2起正式批; 72h持有, SL-8%盘中触发[9/7起由5%调]; 9/13起选币=主LONG榜) | "
                "存活率配色: 绿≥70%/黄40~70%/红<40% | 止损净U=该批已止损单的真实净亏损合计 | "
                "存活=本批持仓仍在等待72h到期(按批次标签归属, 币被后续批次重开计入新批) | '其他'=手动/异常离场</div>")
    except Exception as e:
        return f'<p style="color:#c00">(批次生存表生成失败: {e})</p>'


def section_mi_equity():
    """3.9e 米实盘权益 (2026-09-09 用户需求): 第二账户(纯LONG臂, 125U/5x/SL-8%/72h)总权益,
    晨报直读。数据: HYBRID_BINANCE 凭证 /fapi/v2/account → totalMarginBalance(钱包+浮盈) + state账本。"""
    try:
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import hybrid_live as hl
        acct = hl.signed('GET', '/fapi/v2/account')
        if not isinstance(acct, dict) or 'totalMarginBalance' not in acct:
            return f"<div style='font-size:11px;color:#c00;'>(米权益获取失败: {str(acct)[:80]})</div>"
        eq = float(acct['totalMarginBalance'])
        avail = float(acct.get('availableBalance', 0))
        st = json.load(open('/home/myuser/websocket_new/data/hybrid_live_state.json'))
        n_open = len(st.get('open', {}))
        realized = sum(h.get('net_u', 0) for h in st.get('history', []))
        principal = 1448.22  # 本金1万CNY入金折算
        pct = (eq / principal - 1) * 100
        c = '#0a0' if pct >= 0 else '#c00'
        return (f"<div style='font-size:13px;'><b>米总权益: <span style='color:{c};'>{eq:.2f}U</span></b>"
                f" <span style='font-size:11px;color:#555;'>(本金1万CNY≈1448.22U, "
                f"<span style='color:{c};'>{pct:+.2f}%</span>)"
                f" | 可用 {avail:.2f}U | 在持 {n_open} 笔 | 累计已实现 {realized:+.2f}U"
                f" <span style='color:#888;'>(含链路测试-0.16U)</span></span></div>")
    except Exception as e:
        return f'<div style="font-size:11px;color:#c00;">(米权益生成失败: {e})</div>'


def _short_breakeven_wr():
    """SHORT TOP5 的**含费**盈亏平衡胜率 (与 audit/hybrid_tracker.settle_hybrid 同一费用模型)。

    2026-09-12 修: 原页脚与红/黄灯分界都用理想化的 33.3%(=2:1赔率), 未计手续费与滑点,
    比含费真值低约 1.1pp → "33~34.4%" 这一段会被误读成"还没到危险线"。
    赢 = +10% −0.12%(taker×2) −0.02%(出场滑点) ; 亏 = −5% −0.12% −0.05%(止损滑点)。
    ⚠️ **未计资金费** —— 空头在挤压行情还要倒付 funding, 故真实平衡线只会更高。
    """
    fee = 0.001 + 0.0002          # 与 settle_hybrid 的 FEE 一致
    win = 0.10 - fee - 0.0002     # TP 出场滑点
    loss = 0.05 + fee + 0.0005    # SL 出场滑点
    return loss / (loss + win) * 100.0


def section_short_top5():
    """3.9d SHORT TOP5 试盘时机 (2026-09-07 用户需求):
    每日SHORT prob前5的滚动胜率 vs **含费盈亏平衡线**(≈34.4%, 见 _short_breakeven_wr), 只输出一个结论:
    当前适不适合小资金试盘。判定规则见 ~/.trae/skills/short-top5-timing/SKILL.md
    红灯: 滚动胜率<含费平衡线 | 黄灯: 平衡线~40%无资金面配合 | 绿灯: ≥40%且放量潮/BTC破位。
    (2026-09-12 修: 原用理想化 33.3%, 未计手续费/滑点 → 边界偏低 1.1pp, 已改为含费口径)
    数据: hybrid_tracker.json SHORT侧; 生成失败自动降级, 不影响晨报。"""
    try:
        d = json.load(open('/home/myuser/websocket_new/data/hybrid_tracker.json'))
        days = sorted(d.keys())
        recent = days[-10:]
        rtp = rn = 0
        alltp = alln = 0
        for day in days:
            shorts = sorted([t for t in d[day]['trades'] if t['direction'] == 'SHORT'],
                            key=lambda t: -t['prob'])
            for t in shorts[:5]:
                trig = t.get('trigger')
                if trig in ('止盈', '止损', '到期'):
                    alln += 1
                    alltp += (trig == '止盈')
                    if day in recent:
                        rn += 1
                        rtp += (trig == '止盈')
        if rn == 0:
            return ("<div style='font-size:11px;color:#888;'>(3.9d SHORT TOP5: 近10日尚无已结算批次)</div>")
        wr = rtp / rn * 100
        all_wr = alltp / alln * 100 if alln else 0
        # 资金面信号: 山寨放量潮口径(非TOP50放量币数>150 — 简化版: 用近3日hybrid在SHORT侧
        # 止盈占比代理脉冲形态; 完整口径以山寨资金SKILL判定为准, 此处只做胜率主判)
        BE = _short_breakeven_wr()   # 含费平衡线(未计funding), 见函数注释
        if wr < BE:
            color, bg, verdict = '#c00', '#ffebee', f'🔴 不适合 — 滚动胜率低于含费盈亏平衡线{BE:.1f}%'
        elif wr < 40:
            color, bg, verdict = '#b8860b', '#fffde7', f'🟡 观察 — 高于平衡线{BE:.1f}%但未达40%, 且无资金面配合'
        else:
            color, bg, verdict = '#1b5e20', '#e8f5e9', '🟢 数据条件满足 — 是否试盘由用户拍板'
        cell = "style='padding:2px 8px;border:1px solid #ccc;font-size:12px;'"
        cell_bg = f"style='padding:2px 8px;border:1px solid #ccc;font-size:12px;background:{bg};'"
        return (
            f"<table style='border-collapse:collapse;'><tr>"
            f"<td {cell_bg}><b style='color:{color};'>{verdict}</b></td></tr>"
            f"<tr><td {cell}>近10日已结算 {rn}笔: 胜率<b>{wr:.0f}%</b> ({rtp}止盈) | "
            f"8/3以来全程 {alln}笔: {all_wr:.0f}% | 含费盈亏平衡线={BE:.1f}%(TP10/SL5, 未计funding) | "
            f"绿灯另需: 放量潮(>150币放量/总额>75亿)或BTC破位 | 近10日批次多数T+2才结算, 滚动值有滞后</td></tr></table>"
            f"<div style='font-size:10px;color:#666;'>SHORT TOP5=每日prob最高5笔做空(TP10/SL5/48h影子口径) | "
            f"历史锚: 8月脉冲市38%/日均+12.6U, 8/23后存量市32%/日均-4.8U | 试盘规格与停止线见 short-top5-timing SKILL §3</div>")
    except Exception as e:
        return f"<div style='font-size:11px;color:#888;'>(3.9d SHORT TOP5 生成失败: {e})</div>"


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
        # IC 领先预警 (2026-08-28 复盘发现: IC_L 5日均转负(8/12晨报)领先 AUC_L 崩塌(8/20晨报) 8 天,
        # 即"IC负而AUC好"形态是风格切换早期信号; 但同样形态也出现在修复尾声(8/27), 用 IC 均值时间导数区分:
        # 下行=恶化前兆(8/12型), 回升=修复尾声(8/27型)。n=1 规律, 边积累边验证。)
        ic_lead_html = ''
        if len(clean) >= 8:
            def _ravg(sub, key):
                vs = [x[key] for x in sub if x.get(key) is not None]
                return sum(vs)/len(vs) if vs else None
            ml_now = _ravg(clean[-5:], 'ic_long')
            ml_prev = _ravg(clean[-10:-5], 'ic_long')
            al_now = _ravg(clean[-5:], 'auc_long')
            if (ml_now is not None and ml_now < 0 and al_now is not None and al_now >= 0.55
                    and ml_prev is not None):
                slope = ml_now - ml_prev
                # 尾部交叉验证(2026-09-01 加): 均值斜率会被窗口边界效应欺骗 —
                # 9/1案例: 前窗含冲击深负日导致斜率>0, 但最近2天单日IC已重新深负(8/29 -0.17 / 8/30 -0.29),
                # 修复被新回踩打断, 均值还来不及反应。最近2日深负(<-0.10)时强制降级, 不显示修复尾声。
                recent2 = [x.get('ic_long') for x in clean[-2:]]
                tail_deteriorating = (len(recent2) == 2 and all(isinstance(v, (int, float)) and v < -0.10 for v in recent2))
                if slope < -0.01:
                    ic_lead_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;"
                                    f"background:#ffe0b2;border-left:4px solid #e65100;'>"
                                    f"🟠 <b>IC领先预警(8/12形态·早期)</b>: IC_L 5日均={ml_now:+.2f} 已负 而 AUC_L={al_now:.2f} 仍健康 — "
                                    f"排序已反向、分类尚好。IC均值仍在下行({ml_prev:+.2f}→{ml_now:+.2f}), "
                                    f"历史该形态领先 AUC 崩塌约 8 天(n=1), 关注 LONG 仓位与 BTC 波动。</div>")
                elif tail_deteriorating:
                    last2 = clean[-2]['date'][5:]
                    ic_lead_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;"
                                    f"background:#ffe0b2;border-left:4px solid #e65100;'>"
                                    f"🟠 <b>IC修复中断·重新走弱</b>: 5日均斜率虽为正({ml_prev:+.2f}→{ml_now:+.2f}, 窗口惯性), "
                                    f"但最近2日单日IC连续深负(≥-0.10, 最近{last2}) — 回升被新回踩打断, "
                                    f"均值尚未反映。视为8/12形态候选, 盯后续BTC波动与LONG仓位。</div>")
                elif slope > 0.01 and ml_now > -0.05:
                    # 修复尾声需满足: 均值回升 且已逼近转正(>-0.05) — 8/15-8/17曾出现半路反弹误报(回升但仍在-0.02~-0.04深负后继续崩), 加此条件过滤
                    ic_lead_html = (f"<div style='font-size:12px;margin-top:4px;padding:4px 8px;"
                                    f"background:#e8f5e9;border-left:4px solid #0a0;'>"
                                    f"🟢 <b>IC修复尾声(8/27形态)</b>: IC_L 5日均={ml_now:+.2f} 已回升"
                                    f"({ml_prev:+.2f}→{ml_now:+.2f})且逼近转正, AUC_L={al_now:.2f} 先行恢复 — "
                                    f"预计 1-3 天 IC 转正, 无需动作。</div>")
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
        # ===== 四灯驾驶舱: IC(风格) / VOL(行情) / AUC(确认) / PERM(模型本体) =====
        # 每灯管一种病, 全绿=模型最佳状态
        def _ravg(sub, key):
            vs = [x[key] for x in sub if x.get(key) is not None]
            return sum(vs)/len(vs) if vs else None
        ml5, ms5 = _ravg(clean[-5:], 'ic_long'), _ravg(clean[-5:], 'ic_short')
        ml5_prev = _ravg(clean[-10:-5], 'ic_long')
        al5, as5 = _ravg(clean[-5:], 'auc_long'), _ravg(clean[-5:], 'auc_short')
        al_last = last.get('auc_long')
        vol_now = _btc_vol_latest()
        perm = _perm_test_latest()

        def _lamp(color, label, value, detail):
            bg = {'g': '#c8e6c9', 'y': '#fff9c4', 'r': '#ffcdd2', 'n': '#eeeeee'}[color]
            sym = {'g': '🟢', 'y': '🟡', 'r': '🔴', 'n': '⚪'}[color]
            return (f"<td style='{cell_css}background:{bg};'><b>{sym} {label}</b><br>"
                    f"<span style='font-size:12px;'>{value}</span><br>"
                    f"<span style='font-size:10px;color:#555;'>{detail}</span></td>")

        # 灯1 IC: 5日均L/S 方向 (期望L正S负); 领先预警逻辑与下方横幅一致
        if ml5 is None:
            ic_lamp = _lamp('n', 'IC 排序', 'N/A', '样本不足')
        elif ml5 >= 0 and (ms5 is None or ms5 <= 0):
            ic_lamp = _lamp('g', 'IC 排序', f'L {ml5:+.2f} / S {ms5:+.2f}' if ms5 is not None else f'L {ml5:+.2f}',
                            '排序正常 · 应L正S负')
        elif (ml5_prev is not None and ml5 > ml5_prev and ml5 > -0.05
              and (al5 or 0) >= 0.55):
            ic_lamp = _lamp('y', 'IC 排序', f'L {ml5:+.2f} / S {ms5:+.2f}' if ms5 is not None else f'L {ml5:+.2f}',
                            '修复尾声 · 1-3天转正')
        else:
            ic_lamp = _lamp('r', 'IC 排序', f'L {ml5:+.2f} / S {ms5:+.2f}' if ms5 is not None else f'L {ml5:+.2f}',
                            '排序反向 · 领先AUC崩~8天(n=1)')
        # 灯2 VOL
        if vol_now is None:
            vol_lamp = _lamp('n', 'BTC 波动', 'N/A', '无数据')
        elif vol_now <= 1.5:
            vol_lamp = _lamp('g', 'BTC 波动', f'{vol_now:.1f}%', '平静 · alpha窗口')
        elif vol_now <= 2.0:
            vol_lamp = _lamp('y', 'BTC 波动', f'{vol_now:.1f}%', '警戒')
        else:
            vol_lamp = _lamp('r', 'BTC 波动', f'{vol_now:.1f}%', '高波动 · LONG受损')
        # 灯3 AUC: 最新干净日单日 + 5日均
        if al_last is None:
            auc_lamp = _lamp('n', 'AUC 分类', 'N/A', '样本不足')
        elif al_last >= 0.55 and (al5 or 0) >= 0.60:
            auc_lamp = _lamp('g', 'AUC 分类', f'{al_last:.2f} (5日均{al5:.2f})', '分类健康')
        elif al_last >= 0.55:
            auc_lamp = _lamp('y', 'AUC 分类', f'{al_last:.2f} (5日均{al5:.2f})', '单日好/均值修复中')
        else:
            auc_lamp = _lamp('r', 'AUC 分类', f'{al_last:.2f} (5日均{al5:.2f})', '分类失效 · D+2确认')
        # 灯4 PERM: 今日置换检验 drop
        if not perm:
            perm_lamp = _lamp('n', 'PERM 过拟合', 'N/A', '今日无记录')
        else:
            worst = min(v[2] for v in perm.values())
            txt = ' / '.join(f'{k} drop {v[2]:+.1f}%' for k, v in sorted(perm.items()))
            if worst >= 5:
                perm_lamp = _lamp('g', 'PERM 过拟合', txt, '真实信号 · 当天08:21出')
            elif worst >= 0:
                perm_lamp = _lamp('y', 'PERM 过拟合', txt, '偏弱')
            else:
                perm_lamp = _lamp('r', 'PERM 过拟合', txt, '过拟合 · 已阻断该侧')
        dash = ("<table style='border-collapse:collapse;margin-bottom:6px;'><tr>"
                + ic_lamp + vol_lamp + auc_lamp + perm_lamp + "</tr></table>"
                "<div style='font-size:10px;color:#666;margin-bottom:4px;'>"
                "四灯驾驶舱: 🟢IC排序(风格) + 🟢BTC波动(行情) + 🟢AUC分类(确认) + 🟢PERM过拟合(模型本体) "
                "— 全绿=模型最佳状态; 红灯按灯动作(IC红=关注LONG档, VOL红=预期AUC将崩, AUC红=降LONG档, PERM红=该侧已阻断)。</div>")
        return (f"<div style='font-size:12px;margin-bottom:4px;'>{head}</div>"
                + dash
                + "<table style='border-collapse:collapse;'>"
                f"<tr><th {hd}>日期</th><th {hd}>IC_L</th><th {hd}>IC_S</th>"
                f"<th {hd}>TOP1L</th><th {hd}>空前5</th><th {hd}>BTC 5日波动</th></tr>"
                + ''.join(rows) + "</table>"
                + verdict_html + ic_lead_html + alert_html
                + "<div style='font-size:10px;color:#666;margin-top:3px;'>"
                "口径注: IC/AUC/VOL为48h日线口径(open[D]→close[D+2]), D+2确认; PERM为当日08:21实时。"
                "BTCvol底色: 绿≤1.5%平静 / 黄1.5~2%警戒 / 红>2%高波动。"
                "系统吃横盘期alpha: r(vol,AUC_L)=-0.52 — 平静期双侧正常, 高波动期LONG分类力崩、SHORT独立alpha仍可, "
                "恢复期3-5天。IC_L深红(<-0.15)为排序显著反向。</div>")
    except Exception as e:
        return f'<p style="color:#c00">(前向批作业读取失败: {e})</p>'


# ══════════════════════════════════════════════════════════════════════════════
# 5.6 BTC EMA7/EMA28 纠缠闸门 (2026-09-13 用户要求加入晨报, 用于盯一条待验证线索)
#
# 线索(用户 2026-09-13 提出)的最终裁定 —— ⚠️ 2026-09-13 当日已用 649 笔/65 日复核, 结论=未证实:
#   ⚠️ **效果量测不出来, 不是"没有"**: 点估计方向一直为正(批级 +9.6U/笔、币级 +1.1pp),
#      但 t=1.23 / 95%CI 跨零。批级每笔 PnL 标准差 ≈24U, 效果量 9.6U → 信噪比 0.4;
#      需每组 ≈99 批 ≈99 个交易日 才能分辨。→ 本项=零成本观察, 不投入实验成本。
#   ⚠️ **用户提的三个机制全部被否**: ① 纠缠期山寨独立波动反而更小(t=-4.15) ② "BTC平静→山寨起飞"
#      无效(t=+0.08) ③ 纠缠期上榜币止损率无差别(四档 43.5%~47.9%)。
#      → 方向(点估计)对, 但原因全不对 = 典型的"巧合未被排除"。
#
# 仍保留的历史点估计(仅供追踪, 不可当证据):
#   批收益: 纠缠 +11.80U vs 分离 +2.11U 每笔(基线48h档, 18/21批); 中位批 +7.50U vs -2.93U
#   跨结构复现: 官方72h档 +19.49U vs +10.03U (差同为 ~+9.5U)
#   500天事件研究(11次"从分离压缩进入贴近"): 持续≥5天 82%、≥10天 55%、中位 10 天 (底率 43%)
#
# 用途(用户定调 2026-09-13): **不是交易信号, 是降低回测成本的预筛观察项** —
#   若日后证实分离期为负期望, 可据此推迟 GPU A/B 省 ~2h/轮; 目前在证实前不据它做决策.
# 证伪线: ① 下次纠缠重现后批每笔未回到 +10U 量级 → 归为巧合
#         ② 累计 ~99 个交易日样本后仍 t<1.96 → 判为不可测, 长期摘除
#         ③ TOP10 模拟器(须 SL_PCT=8)在其他历史纠缠段不改善 → 判为行情指纹
#
# ⚠️ 本函数纯只读展示, 不参与任何交易决策, 失败自动降级为一行提示, 不影响晨报其余部分.
# ══════════════════════════════════════════════════════════════════════════════
EMA_NEAR = 1.75      # |gap| <= 该值 = 贴近区(纠缠)
EMA_SEP = 2.75       # |gap| >  该值 = 分离区


def _btc_ema_gap_state():
    """BTC EMA7/EMA28 纠缠状态(纯只读). 数据源=生产同款日K缓存, 排除今日未收盘bar.
    返回 dict 或 None(失败时)."""
    btck = _btc_klines()
    if btck is None or len(btck) < 40:
        return None
    try:
        # ⚠️ 2026-09-13 修正: 必须排除"未收盘"的当日 bar. 缓存由 websocket/采集写入,
        # 当日那根的 c 是盘中中间值(实测 9/12 缓存 77239.9 vs 实际收盘 77159.2, 差 0.10%),
        # 若纳入会让 gap 偏移 ~0.03pp 并与事件研究口径不符.
        # 日K t 为 UTC 00:00, 收盘时刻 = t + 86400000; 只保留 t+DAY <= now 的已收盘 bar.
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        DAY = 86400000
        rows = [r for r in btck if r['t'] + DAY <= now_ms]
        if len(rows) < 40:
            return None
        closes = [r['c'] for r in rows]
        dates = [datetime.datetime.fromtimestamp(r['t'] / 1000,
                     datetime.timezone.utc).strftime('%Y-%m-%d') for r in rows]
        # 与本地核算脚本同口径: k=2/(n+1), 首值以首个close为种子
        def _ema(vals, n):
            k = 2.0 / (n + 1)
            out, prev = [], None
            for v in vals:
                prev = v if prev is None else v * k + prev * (1 - k)
                out.append(prev)
            return out
        e7, e28 = _ema(closes, 7), _ema(closes, 28)
        gap = [(e7[i] - e28[i]) / e28[i] * 100 for i in range(len(closes))]
        g = gap[-1]
        state = '贴近(纠缠)' if abs(g) <= EMA_NEAR else ('过渡带' if abs(g) <= EMA_SEP else '分离')
        # 当前状态已持续天数(含今日)
        thr = EMA_NEAR if state == '贴近(纠缠)' else (None if state == '过渡带' else EMA_SEP)
        held = 0
        if thr is not None:
            for x in reversed(gap):
                if (abs(x) <= thr) if state == '贴近(纠缠)' else (abs(x) > thr):
                    held += 1
                else:
                    break
        # 过渡带(已跌破分离边界但未达贴近区)已持续天数
        held_trans = 0
        if state == '过渡带':
            for x in reversed(gap):
                if abs(x) <= EMA_SEP:
                    held_trans += 1
                else:
                    break
        # 最近一次"进入贴近区"的日期(与事件研究同定义: 且此前10日内曾处分离区)
        enter_date = None
        last = -999
        for i in range(len(gap)):
            if abs(gap[i]) > EMA_NEAR:
                continue
            if i - last < 10:
                continue
            win = [abs(x) for x in gap[max(0, i - 10):i]]
            if not win or max(win) <= EMA_SEP:
                continue
            enter_date, last = dates[i], i
        # 最近一次"跌破分离边界"的日期: 取当前 |gap|<=EMA_SEP 连续段的起点.
        # (原写法找"单日跨过边界"的跃变, 若边界当日即序列端点或中间缺 bar 会漏 → 已改稳健写法)
        break_date = None
        if abs(g) <= EMA_SEP:
            j = len(gap) - 1
            while j > 0 and abs(gap[j - 1]) <= EMA_SEP:
                j -= 1
            break_date = dates[j]
        # 当前"分离段"的起点(用于分离态展示): 取当前 |gap|>EMA_SEP 连续段的起点
        sep_start = None
        if abs(g) > EMA_SEP:
            j = len(gap) - 1
            while j > 0 and abs(gap[j - 1]) > EMA_SEP:
                j -= 1
            sep_start = dates[j]
        # 压缩速度 + 线性外推到 0 的天数.
        # ⚠️ 用**近5日窗口**, 不用"整段起算": 分离段往往先扩张后收缩(如 8/20 起先冲到 +10.07 再回落),
        #    整段均值会互相抵消得到 ≈0 的速度 → 外推出荒谬的天数(实测曾得 178 天). 近5日才反映"当前速度".
        rate = days_to_zero = None
        span = 5 if len(gap) > 6 else len(gap) - 1
        if span >= 1:
            rate = (gap[-1] - gap[-1 - span]) / span
            if rate != 0 and (gap[-1 - span] > 0) == (gap[-1] > 0) and abs(gap[-1]) < abs(gap[-1 - span]):
                days_to_zero = abs(gap[-1] / rate)
        # 贴近区内穿越零线次数(近 40 天, 衡量"是否在反复纠缠")
        win40 = gap[-40:]
        crossings = sum(1 for i in range(1, len(win40)) if win40[i] * win40[i - 1] < 0)
        return dict(date=dates[-1], gap=g, prev_gap=gap[-2], state=state, held=held,
                    held_trans=held_trans, sep_start=sep_start,
                    enter_date=enter_date, break_date=break_date, rate=rate,
                    days_to_zero=days_to_zero, crossings=crossings,
                    n_bars=len(rows),
                    gap_series=[(dates[i], gap[i]) for i in range(len(gap) - 20, len(gap))])
    except Exception:
        return None


def section_ema_gate():
    """5.6 BTC EMA7/EMA28 纠缠闸门(只读观察项; 未证实, 见上方裁定注释)."""
    s = _btc_ema_gap_state()
    if not s:
        return ("<div style='font-size:11px;color:#999;'>(5.6 纠缠闸门: BTC日K缓存不可用或计算失败, 略过)</div>")
    g, st = s['gap'], s['state']
    color = {'贴近(纠缠)': '#1b5e20', '过渡带': '#e65100', '分离': '#b71c1c'}.get(st, '#333')
    bg = {'贴近(纠缠)': '#e8f5e9', '过渡带': '#fff3e0', '分离': '#ffebee'}.get(st, '#eee')
    chg = g - s['prev_gap']
    arrow = '↓' if chg < 0 else ('↑' if chg > 0 else '→')
    # 历史参考常数(500天事件研究 11 次压缩事件, 固定值不重算以保持轻量)
    hist = ('历史同类事件 11 次: 落入贴近后持续 ≥5天 <b>82%</b>(底率43%) · ≥10天 <b>55%</b> · '
            '中位持续 <b>10 天</b>; 从跌破 2.75% 到进入贴近区 中位 <b>2 天</b>(91% 在5天内)')
    extra = ''
    if st == '贴近(纠缠)':
        extra = (f"已持续第 <b>{s['held']}</b> 天 | 近40日穿越零线 <b>{s['crossings']}</b> 次"
                 f"(反复穿越=真纠缠) | 最近进入 <b>{s['enter_date'] or '—'}</b>")
    elif st == '过渡带':
        e = f"，已 {s['held_trans']} 天" if s.get('held_trans') else ''
        extra = (f"⚠️ 已跌破分离边界但尚未进入贴近区{e}"
                 f" | 历史 91% 在 5 天内进入贴近区, 中位 2 天")
    else:
        extra = f"已连续分离 <b>{s['held']}</b> 天" + (
            f" (本段自 <b>{s.get('sep_start') or '—'}</b> 起)" if s.get('sep_start') else '') + (
            f" | 日压 {s['rate']:+.3f}pp, 外推贴零还需 ~<b>{s['days_to_zero']:.0f}</b> 天"
            if s.get('days_to_zero') else '')
    # 近 20 日 sparkline(文字条)
    rows = s['gap_series']
    mx = max(abs(x[1]) for x in rows) or 1.0
    spark = ''
    if rows:
        cells = []
        for d, v in rows:
            blk = int(min(abs(v) / mx, 1.0) * 6)
            ch = '█' * blk if blk else '·'
            c = '#b71c1c' if abs(v) > EMA_SEP else ('#1b5e20' if abs(v) <= EMA_NEAR else '#e65100')
            cells.append(f"<span style='display:inline-block;color:{c}'>{ch}</span>")
        spark = (f"<div style='font-size:10px;font-family:Consolas,monospace;line-height:1.2;'>"
                 f"{''.join(cells)}<br><span style='color:#888'>"
                 f"{rows[0][0]} → {rows[-1][0]} (条越高=分离越远; 绿=贴近 橙=过渡 红=分离)</span></div>")
    return f"""<div style="font-size:12px;line-height:1.7;">
<span style='background:{bg};color:{color};padding:2px 8px;border-radius:3px;font-weight:bold;'>{st}</span>
&nbsp; {s['date']} BTC EMA7−EMA28 gap = <b>{g:+.3f}%</b> <span style='color:{color}'>{arrow}{abs(chg):.3f}</span>
<div style='font-size:11px;color:#555;'>{extra}</div>
<div style='font-size:11px;color:#777;margin-top:3px;'>{hist}</div>
{spark}
<div style='font-size:10px;color:#999;'>⚠️ <b>已终审(2026-09-13): 对系统收益无效应, 保留为纯观察项</b> — 350天walk-forward(3480笔)
+ 468天市场结构检验, 三组检验(阈值/中位分割/纯态月)全部CI含0。真实关系 = 纠缠期山寨"动得更小"(离散度−0.9pp显著),
但不增加共振、不改变方向、不转化为收益优势。<b>不是交易信号, 不参与任何决策</b>。详见 Hindsight 归档 + AGENTS.md §8.2。</div>
</div>"""


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
    # 2026-09-12 修: 原取全历史最后3条 -> 当日训练未跑时会把昨日探针渲染成今日"✅3/3"(哨兵假绿,
    #   正是该探针要防的失效形态); 改为按当日日期过滤, 无当日记录即明示.
    try:
        log_file = '/home/myuser/.local/share/auto_trade/trade.log'
        today_str = datetime.date.today().isoformat()
        with open(log_file) as f:
            slines = [l.split('] ', 1)[-1].strip() for l in f
                      if '[SAMPLECHK]' in l and today_str in l]
        if slines:
            slines = slines[-3:]
            parts.append('[SAMPLECHK 特征体检] ' + ('✅ 3/3' if len(slines) == 3 else f'⚠️ 仅{len(slines)}/3'))
            for s in slines:
                parts.append('  ' + s)
        else:
            parts.append(f'[SAMPLECHK 特征体检] ⚠️ {today_str} 无记录(当日训练未跑或探针缺失?)')
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
        # 仓库体积监控 (2026-09-02): GitHub 建议<1GB/软限5GB — 绿<500MB 黄<2GB 红≥2GB
        repo_mb = st.get('repo_mb')
        if repo_mb is not None:
            if repo_mb >= 2000:
                lines.append(f'仓库体积: {repo_mb:.0f}MB 🚨 已超2GB, 接近GitHub软限(5GB), 需清理历史大文件')
            elif repo_mb >= 500:
                lines.append(f'仓库体积: {repo_mb:.0f}MB 🟡 过半(GitHub建议<1GB), 关注增速')
            else:
                lines.append(f'仓库体积: {repo_mb:.0f}MB 🟢 (GitHub建议<1GB/软限5GB)')
        return '\n'.join(lines)
    except Exception as e:
        return f'[GitHub同步] ⚠️ 读取失败: {e}'


def _send_digest(subject, body_html, chart_path):
    """带一次重试的发送(2026-09-06 加: SMTP瞬时故障当天晨报不再直接丢失).
    注: digest_guard 保险丝只兜编译损坏; 本重试兜瞬时SMTP故障; 持续SMTP故障
    两个通道都失效, 由 09:15 系统体检(agent health_check)邮件尝试告警."""
    import time as _t
    for attempt in (1, 2):
        try:
            send_email(subject, '', body_html=body_html,
                       inline_images={'btcaltchart': chart_path} if chart_path else None)
            return True
        except Exception as e:
            print(f'[晨报] 发送失败(第{attempt}次): {e}')
            if attempt == 1:
                _t.sleep(20)
    return False


def main():
    today = datetime.date.today().isoformat()
    _refresh_tracker()
    # 文本节转 pre; 第2节(前向结算)为 HTML 表格
    pre_style = ("style=\"white-space:pre-wrap;font-size:11px;"
                 "font-family:'SimHei','Microsoft YaHei','PingFang SC',Consolas,monospace;line-height:1.5;\"")
    # 口径标签: 绿=48h影子/前向口径(3.8影子臂/前向结算), 橙=72h逻辑(老日线口径, 仅参考)
    # ⚠️ 2026-09-12 更正: 绿标签原注释为"与生产执行一致"已失效 — 实盘 9/3(果)/9/8(米)起为 72h+SL-8%,
    #    仅影子臂与前向结算仍走 48h/SL-5%。实盘规则标签见 tag48_exec。
    tag_style = ("font-size:11px;padding:1px 6px;border-radius:3px;"
                 "font-family:'SimHei','Microsoft YaHei';")
    tag48_exec = f"<span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>实盘执行规则(9/7~9/8起): LONG无止盈 · SL-8% · 72h · 果08:21/米08:23开仓 · 米SHORT已关</span>"
    tag48 = f"<span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>48h逻辑 · 1m口径 · 08:21开仓 · SL-5%/TP+10%/48h到期</span>"
    tag72 = f"<span style='{tag_style}background:#fff3e0;color:#e65100;'>72h逻辑 · 日线口径(老) · open[T]入场 · 扫T~T+2三根日线 · 与实盘口径不同仅参考</span>"
    tag_none = f"<span style='{tag_style}background:#eee;color:#666;'>无结算口径</span>"
    # 5.5b BTC vs 山寨走势对比图 (2026-09-02): 8/30教训 — BTC vol灯绿但山寨横截面已在崩(中位-3.23%/89%下跌),
    # 四灯只看BTC看不见山寨独立冲击 → 此图补盲区; 生成失败自动降级为文字行, 不影响晨报其余部分
    chart_path, chart_state = build_btc_alt_chart()
    chart_html = ''
    if chart_path:
        _s = chart_state or {}
        chart_html = f"""
<b>5.5b BTC vs 山寨走势对比图 (近{_s.get('n_days', 30)}天已收盘)</b> <span style='{tag_style}background:#fff3e0;color:#e65100;'>上:BTC/山寨中位数(均从0%起) · 中:BTC vol5(四灯同色) · 下:山寨横截面离散度(候选第5灯)</span>
<div style='margin:6px 0;'><img src="cid:btcaltchart" style="max-width:100%;border:1px solid #ddd;border-radius:4px;"></div>
<div style='font-size:11px;color:#555;'>BTC近30日 <b>{_s.get('btc_last', 0):+.1f}%</b> vs 山寨中位数 <b>{_s.get('alt_last', 0):+.1f}%</b>
 | 最新BTC vol5 <b>{_s.get('btc_vol5', 0):.2f}%</b>(绿≤1.5/黄1.5~2/红>2, 与5.5节四灯同口径)
 | 最新山寨离散度 <b>{_s.get('disp_last', 0):.2f}%</b>(绿≤6/黄6~8/红>8)
 | 山寨离散度=当日全宇宙日收益横截面std, 是"山寨自己的vol灯": 8/30该值7.4%(黄)时BTC vol仅1.57%(黄绿), 山寨先崩BTC没动 — 第5灯候选, 阈值待60天校准</div>"""
    else:
        chart_html = "\n<div style='font-size:11px;color:#999;'>(5.5b 对比图生成失败, 略过)</div>"
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
<b>3.8b 影子臂 2×2 结构对照 (SL档位 × 持有时长)</b> <span style='{tag_style}background:#e3f2fd;color:#1565c0;'>变量受控 · LONG侧 · 同源同日 · 为10/23终审提供拆分证据</span>
{section_2x2()}
<b>3.9 RESIDUAL影子臂 LONG对照 (残差标签: 币ret-宇宙中位>5pp)</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>影子验证 · 残差标签LONG模型 · 出场同3.8主臂LONG · 纯旁路不影响实盘</span>
{section_residual()}
<b>3.9b 主LONG vs 残差LONG 当日选币差异</b> <span style='{tag_style}background:#e3f2fd;color:#1565c0;'>重合币/独有币对照 · 双方概率 · 近7日重合度趋势</span>
{section_residual_picks()}
<b>3.9c 果实盘批次生存表</b> <span style='{tag_style}background:#fff3e0;color:#e65100;'>每批开仓N笔 → 存活/止损/到期 · 存活率 · 批内净U · 9/13起选币=主LONG榜</span>
{section_residual_survival()}
<b>3.9e 米实盘权益 (第二账户·纯LONG臂)</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>125U/5x/SL-8%/72h · 08:23开仓 · SHORT已关</span>
{section_mi_equity()}
<b>3.9d SHORT TOP5 试盘时机</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>滚动胜率 vs 含费盈亏线34.4%(未计funding) · 只输出一个结论: 适不适合小资金试盘</span>
{section_short_top5()}
<b>4. 强势股续涨 + 每日资金榜</b> {tag_none}
<pre {pre_style}>{section_momentum()}</pre>
<b>5. 系统健康</b> {tag_none}
<pre {pre_style}>{section_health()}</pre>
<b>5.5 前向批作业 · 模型质量与BTC波动 regime</b> <span style='{tag_style}background:#e8f5e9;color:#1b5e20;'>公证预测对答案 · 48h日线口径 · D+2确认</span>
{section_forward_ic()}
<b>5.6 BTC EMA7/EMA28 纠缠闸门 (只读观察 · ⚠️未证实)</b> <span style='{tag_style}background:#ffebee;color:#b71c1c;'>效果量测不出来(t=1.23/CI跨零), 三个机制均被否 · 只作零成本追踪 · 不参与交易决策, 暂不作回测预筛依据 · 2026-09-13 加</span>
{section_ema_gate()}
{chart_html}
<b>6. GitHub 同步</b> {tag_none}
<pre {pre_style}>{section_github_sync()}</pre>"""
    if _send_digest(f'晨报总览 {today}', body_html, chart_path):
        print('digest sent')
    else:
        print('digest SEND FAILED: 重试后仍失败(见上方错误), 本日晨报未发出')
        sys.exit(1)


if __name__ == '__main__':
    main()
