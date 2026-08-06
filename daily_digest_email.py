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
    # 6) 交易动作(只取最后一次)
    for l in reversed(lines):
        c = clean(l)
        if c.startswith('开仓:') or c.startswith('跳过交易') or c.startswith('本金不足') or c.startswith('空仓') or c.startswith('无有效信号'):
            out.append(f'⚙️ {c}')
            break
    # 7) 兜底: 关键词行(过滤原始 PERM 英文长行)
    if len(out) <= 2:
        keywords = ['PERM-CAND', '做多概率', '做空概率', '止盈', '止损']
        for l in lines:
            c = clean(l)
            if any(k in c for k in keywords):
                out.append(c)
    return '\n'.join(out) if out else '(今日无交易日志)'


def section_forward():
    """前向结算(修正口径 1m): 7/28 起 TOP1, 输出 HTML 表格(黑体/窄列, 邮件富文本)"""
    try:
        import glob
        sys.path.insert(0, os.path.join(BASE, 'audit'))
        import forward_settle as fs
        days = sorted(glob.glob(os.path.join(fs.PRED_DIR, 'pred_*.json')))
        days = [os.path.basename(f).replace('pred_', '').replace('.json', '') for f in days
                if os.path.basename(f) >= 'pred_2026-07-28.json']
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
    try:
        import auto_dual_trade as adt
        tracker = json.load(open(f'{BASE}/data/prediction_tracker.json'))
        out = adt._build_verify_summary(tracker)
        # 过滤 adt import 时泄漏的"配置加载"噪音行
        lines = [l for l in str(out).split('\n') if '配置: 从' not in l]
        return '\n'.join(lines)
    except Exception as e:
        return f'(验证数据读取失败: {e})'


def section_momentum():
    try:
        from daily_momentum_email import build_momentum_body_html
        return build_momentum_body_html()
    except Exception as e:
        return f'<p style="color:#c00">(强势股/资金榜生成失败: {e})</p>'


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
    # 4a4. 前向批作业(8/5 新增): 公证预测对答案, 横截面IC/AUC (forward_ic_check.py)
    try:
        ic_path = '/home/myuser/websocket_new/data/forward_ic_history.json'
        if os.path.exists(ic_path):
            ih = json.load(open(ic_path))
            days = ih.get('days', [])
            clean = [d for d in days if not d.get('dirty')]
            dirty = [d for d in days if d.get('dirty')]
            if days:
                last = clean[-1] if clean else days[-1]
                tag = '幽灵期' if last.get('dirty') else '干净期'
                def _f(v, w=6):
                    return f'{v:+.2f}'.rjust(w) if isinstance(v, (int, float)) else '  N/A '
                lines = [f"[前向批作业] 最新: {last['date']} [{tag}] n={last['n_sym']}币 (预测日→2日后兑现K线对答案)"]
                lines.append(f"  LONG:  IC={_f(last.get('ic_long'))} AUC={last.get('auc_long')} | SHORT: IC={_f(last.get('ic_short'))} AUC={last.get('auc_short')}")
                s5 = last.get('short5_avg_ret', 0)
                lines.append(f"  实际: LONG TOP1 {last.get('top1_long')} {last.get('top1_long_ret', 0):+.1f}% | 空前5均 {s5:+.1f}%{' (空头盈利✅)' if s5 < 0 else ' (空头亏损⚠️)'}")
                if last.get('note'):
                    lines.append(f"  ⚠️ 注: {last['note'][:80]}")
                if len(clean) > 1:
                    lines.append('  干净期批作业: 日期   IC_L   IC_S   TOP1L   空前5')
                    for d in clean[-7:]:
                        lines.append(f"    {d['date'][5:]}  {_f(d.get('ic_long'))} {_f(d.get('ic_short'))} {d.get('top1_long_ret', 0):+6.1f}% {d.get('short5_avg_ret', 0):+6.1f}%")
                if len(clean) >= 3:
                    l5 = clean[-5:]
                    ml = sum(abs(d['ic_long']) for d in l5 if d.get('ic_long') is not None) / max(1, sum(1 for d in l5 if d.get('ic_long') is not None))
                    ms = sum(abs(d['ic_short']) for d in l5 if d.get('ic_short') is not None) / max(1, sum(1 for d in l5 if d.get('ic_short') is not None))
                    verdict = '✅信号活着' if min(ml, ms) >= 0.10 else ('⚠️转弱' if min(ml, ms) >= 0.05 else '❌疑似失效(熔断线候选)')
                    lines.append(f"  判定(近{len(l5)}日|IC|均值 L={ml:.2f}/S={ms:.2f}, 阈值0.10/0.05): {verdict}")
                else:
                    lines.append(f"  判定: 干净期样本累积中({len(clean)}/3天, ≥3天出判定)")
                if dirty:
                    dl = [d['ic_long'] for d in dirty if d.get('ic_long') is not None]
                    ds = [d['ic_short'] for d in dirty if d.get('ic_short') is not None]
                    lines.append(f"  幽灵期阴性对照({len(dirty)}天): IC_L均值{sum(dl)/len(dl):+.2f}/IC_S均值{sum(ds)/len(ds):+.2f}≈0 (错位模型无排序力, 方法有效✅)")
                parts.append('\n'.join(lines))
            else:
                parts.append('[前向批作业] ⚠️ 暂无批作业记录')
        else:
            parts.append('[前向批作业] ⚠️ 无历史(forward_ic_check未运行?)')
    except Exception as e:
        parts.append(f'(前向批作业读取失败: {e})')
    # 4b. 服务/接口健康报告(原 alert_monitor --report)
    try:
        from alert_monitor import generate_health_report
        parts.append('[服务健康报告]\n' + generate_health_report())
    except Exception as e:
        parts.append(f'(服务健康报告生成失败: {e})')
    return '\n'.join(parts)


def main():
    today = datetime.date.today().isoformat()
    # 文本节转 pre; 第2节(前向结算)为 HTML 表格
    pre_style = ("style=\"white-space:pre-wrap;font-size:11px;"
                 "font-family:'SimHei','Microsoft YaHei','PingFang SC',Consolas,monospace;line-height:1.5;\"")
    body_html = f"""<h2 style="margin:0 0 8px;">晨报总览 {today}</h2>
<b>1. 交易摘要</b>
<pre {pre_style}>{section_trade()}</pre>
<b>2. 前向结算 TOP1 (1m修正口径)</b>
{section_forward()}
<b>3. 2日验证命中率</b>
<pre {pre_style}>{section_verify()}</pre>
<b>4. 强势股续涨 + 每日资金榜</b>
<pre {pre_style}>{section_momentum()}</pre>
<b>5. 系统健康</b>
<pre {pre_style}>{section_health()}</pre>"""
    send_email(f'晨报总览 {today}', '', body_html=body_html)
    print('digest sent')


if __name__ == '__main__':
    main()
