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
    # 1) 钱包状态
    for l in lines:
        c = clean(l)
        if '钱包' in c:
            out.append(f'💰 钱包: {c.split("钱包:",1)[-1].strip()}')
            break
    # 2) 训练信息
    for l in lines:
        c = clean(l)
        if c.startswith('样本:') or c.startswith('训练:'):
            out.append(f'📚 训练: {c}')
        if '特征维度' in c:
            out.append(f'🔬 {c}')
        if '预测日期' in c:
            out.append(f'📅 {c}')
        if '板块热度' in c and '预计算' not in c:
            out.append(f'🗂 {c}')
    # 3) 置换检验(翻译成中文, 分侧)
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
    # 4) 阻断/信号结论
    for l in lines:
        c = clean(l)
        if '过拟合' in c and '禁止' in c:
            out.append(f'⛔ {c}')
        if c.startswith('置信度不足'):
            out.append(f'🚫 {c}')
    # 5) 今日信号(决策依据)
    for l in lines:
        c = clean(l)
        if '决策依据' in c or ('信号' in c and 'prob=' in c and '开仓:' not in c):
            out.append(f'🎯 {c}')
    # 6) 交易动作
    for l in lines:
        c = clean(l)
        if c.startswith('开仓:') or c.startswith('跳过交易') or c.startswith('本金不足') or c.startswith('空仓') or c.startswith('无有效信号'):
            out.append(f'⚙️ {c}')
    # 7) 兜底: 关键词行(过滤原始 PERM 英文长行)
    if len(out) <= 2:
        keywords = ['PERM-CAND', '做多概率', '做空概率', '止盈', '止损']
        for l in lines:
            c = clean(l)
            if any(k in c for k in keywords):
                out.append(c)
    return '\n'.join(out) if out else '(今日无交易日志)'


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
        return adt._build_verify_summary(tracker)
    except Exception as e:
        return f'(验证数据读取失败: {e})'


def section_momentum():
    try:
        from daily_momentum_email import build_momentum_body
        return build_momentum_body()
    except Exception as e:
        return f'(强势股/资金榜生成失败: {e})'


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
    # 4b. 服务/接口健康报告(原 alert_monitor --report)
    try:
        from alert_monitor import generate_health_report
        parts.append('[服务健康报告]\n' + generate_health_report())
    except Exception as e:
        parts.append(f'(服务健康报告生成失败: {e})')
    return '\n'.join(parts)


def main():
    today = datetime.date.today().isoformat()
    body = f"""晨报总览 {today}

===== 1. 交易摘要 =====
{section_trade()}

===== 2. 2日验证命中率 =====
{section_verify()}

===== 3. 强势股续涨 + 每日资金榜 =====
{section_momentum()}

===== 4. 系统健康 =====
{section_health()}
"""
    send_email(f'晨报总览 {today}', body, priority='info')
    print('digest sent')


if __name__ == '__main__':
    main()
