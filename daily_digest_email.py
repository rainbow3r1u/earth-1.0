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


def section_trade():
    """交易摘要: 复用 auto_dual_trade 日报的正文构造(含 verify_yesterday 保持 tracker 新鲜)"""
    try:
        import auto_dual_trade as adt
        body = adt.build_daily_report_body(include_verify=False)
        return body if body else '(今日无交易日志)'
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
