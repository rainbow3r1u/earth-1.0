#!/usr/bin/env python3
"""
回测监控脚本 - 每周自动跑90天回测, 对比baseline, 邮件告警

功能:
  1. 每周一09:30自动运行90天回测
  2. 解析回测结果(Sharpe/Cum/MaxDD/Win率/交易数)
  3. 与baseline(Sharpe=3.96)对比
  4. 漂移>20%或胜率<50%发邮件告警
  5. 正常情况发周报邮件

Cron配置 (每周一09:30):
  30 9 * * 1 cd /home/myuser/websocket_new && /usr/bin/python3 backtest_monitor.py >> logs/backtest_monitor.log 2>&1

手动运行:
  python3 backtest_monitor.py              # 运行回测+发邮件
  python3 backtest_monitor.py --dry-run    # 只解析最近一次回测结果, 不重新跑
"""
import os
import sys
import re
import time
import subprocess
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/myuser/websocket_new')
from alert_monitor import send_email, should_alert, load_alert_state, save_alert_state

CST = timezone(timedelta(hours=8))
LOG_DIR = '/home/myuser/websocket_new/logs'
BACKTEST_LOG = f'{LOG_DIR}/backtest_monitor.log'

# Baseline (3.96SHARPE)
BASELINE = {
    'sharpe': 3.96,
    'cum': 116.9,        # %
    'max_dd': 34.8,      # %
    'win_rate': 59,      # %
    'trades': 74,
}

# 告警阈值
SHARPE_DRIFT_THRESHOLD = 20   # %, Sharpe漂移超过20%告警
WIN_RATE_MIN = 50             # %, 胜率低于50%告警
TRADES_MIN = 30               # 交易数过少告警


def run_backtest():
    """运行90天回测, 返回完整日志输出"""
    print('[BACKTEST] 启动90天回测...')
    cmd = 'cd /home/myuser/3.96SHARPE_repo && /usr/bin/python3 gpu_backtest.py 90 1 off 12 10.0'
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=1800  # 30分钟超时
        )
        output = result.stdout + '\n' + result.stderr
        # 保存日志
        with open(BACKTEST_LOG, 'a') as f:
            f.write(f'\n[{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] 回测启动\n')
            f.write(output[-8192:])  # 保留最后8KB
        print(f'[BACKTEST] 回测完成, 日志: {BACKTEST_LOG}')
        return output
    except subprocess.TimeoutExpired:
        print('[BACKTEST] 回测超时(30分钟)')
        return 'TIMEOUT'
    except Exception as e:
        print(f'[BACKTEST] 回测异常: {e}')
        return f'ERROR: {e}'


def parse_backtest_result(output):
    """从回测输出解析关键指标"""
    if not output or output in ('TIMEOUT',) or output.startswith('ERROR'):
        return None, output

    result = {}

    # 解析Sharpe (多种格式)
    m = re.search(r'[Ss]harpe[:\s=]+([+\-]?\d+\.\d+)', output)
    if m:
        result['sharpe'] = float(m.group(1))

    # 解析Cumulative Return
    m = re.search(r'[Cc]um[:\s=]+([+\-]?\d+\.?\d*)\s*%?', output)
    if m:
        result['cum'] = float(m.group(1))

    # 解析Max Drawdown
    m = re.search(r'[Mm]ax[_\s]?[Dd][Dd]?[:\s=]+([+\-]?\d+\.?\d*)\s*%?', output)
    if m:
        result['max_dd'] = float(m.group(1))

    # 解析Win Rate
    m = re.search(r'[Ww]in[:\s=]+(\d+\.?\d*)\s*%?', output)
    if m:
        result['win_rate'] = float(m.group(1))
    # 备选: Win=44/74(59%)
    m = re.search(r'Win=(\d+)/(\d+)\((\d+)%\)', output)
    if m:
        result['win_rate'] = float(m.group(3))
        result['trades'] = int(m.group(2))

    # 解析Trades
    if 'trades' not in result:
        m = re.search(r'[Tt]rades[:\s=]+(\d+)', output)
        if m:
            result['trades'] = int(m.group(1))

    return result if result else None, output


def compare_with_baseline(result):
    """与baseline对比, 返回(告警列表, 报告)"""
    alerts = []
    lines = ['回测监控周报 - ' + datetime.now(CST).strftime('%Y-%m-%d'), '']
    lines.append('=' * 50)
    lines.append('【指标对比】')
    lines.append(f'{"指标":<15} {"本周":>10} {"Baseline":>10} {"漂移":>10}')
    lines.append('-' * 50)

    # Sharpe
    if 'sharpe' in result:
        sharpe = result['sharpe']
        drift = (sharpe - BASELINE['sharpe']) / BASELINE['sharpe'] * 100
        lines.append(f'{"Sharpe":<15} {sharpe:>10.2f} {BASELINE["sharpe"]:>10.2f} {drift:>+9.1f}%')
        if abs(drift) > SHARPE_DRIFT_THRESHOLD:
            alerts.append({
                'key': f'backtest_sharpe_drift_{datetime.now(CST).strftime("%Y-%m-%d")}',
                'subject': f'回测Sharpe漂移 {drift:+.1f}%',
                'body': f'Sharpe: {sharpe:.2f} (baseline: {BASELINE["sharpe"]:.2f})\n漂移: {drift:+.1f}%\n\n可能原因:\n1. 策略衰减\n2. 市场环境变化\n3. 数据质量问题',
                'priority': 'high',
            })
    else:
        lines.append(f'{"Sharpe":<15} {"N/A":>10} {BASELINE["sharpe"]:>10.2f} {"N/A":>10}')

    # Cum
    if 'cum' in result:
        lines.append(f'{"Cum%":<15} {result["cum"]:>10.1f} {BASELINE["cum"]:>10.1f}')

    # MaxDD
    if 'max_dd' in result:
        lines.append(f'{"MaxDD%":<15} {result["max_dd"]:>10.1f} {BASELINE["max_dd"]:>10.1f}')

    # Win Rate
    if 'win_rate' in result:
        wr = result['win_rate']
        lines.append(f'{"Win%":<15} {wr:>10.1f} {BASELINE["win_rate"]:>10.1f}')
        if wr < WIN_RATE_MIN:
            alerts.append({
                'key': f'backtest_low_winrate_{datetime.now(CST).strftime("%Y-%m-%d")}',
                'subject': f'回测胜率过低 {wr:.1f}%',
                'body': f'胜率: {wr:.1f}% (阈值: {WIN_RATE_MIN}%)\n\n可能原因:\n1. 模型过拟合\n2. 特征失效\n3. 训练窗口不适配当前市场',
                'priority': 'high',
            })

    # Trades
    if 'trades' in result:
        tr = result['trades']
        lines.append(f'{"Trades":<15} {tr:>10d} {BASELINE["trades"]:>10d}')
        if tr < TRADES_MIN:
            alerts.append({
                'key': f'backtest_low_trades_{datetime.now(CST).strftime("%Y-%m-%d")}',
                'subject': f'回测交易数过少 {tr}',
                'body': f'交易数: {tr} (阈值: {TRADES_MIN})\n\n交易过少可能导致统计不显著.',
                'priority': 'normal',
            })

    lines.append('=' * 50)
    lines.append('')
    lines.append('【Baseline】')
    lines.append(f'  Sharpe={BASELINE["sharpe"]}, Cum=+{BASELINE["cum"]}%, MaxDD={BASELINE["max_dd"]}%, Win={BASELINE["win_rate"]}%, Trades={BASELINE["trades"]}')
    lines.append('')
    lines.append('【告警阈值】')
    lines.append(f'  Sharpe漂移 > ±{SHARPE_DRIFT_THRESHOLD}%')
    lines.append(f'  胜率 < {WIN_RATE_MIN}%')
    lines.append(f'  交易数 < {TRADES_MIN}')
    lines.append('')
    if alerts:
        lines.append(f'【告警】发现 {len(alerts)} 个告警, 已发送邮件')
    else:
        lines.append('【告警】无告警, 策略运行正常')

    return alerts, '\n'.join(lines)


def main():
    # Dry-run模式: 只解析最近一次回测日志
    if '--dry-run' in sys.argv:
        print('[DRY-RUN] 解析最近一次回测日志...')
        if not os.path.exists(BACKTEST_LOG):
            print('无历史回测日志')
            sys.exit(1)
        with open(BACKTEST_LOG) as f:
            output = f.read()
        result, _ = parse_backtest_result(output)
        if not result:
            print('解析失败')
            sys.exit(1)
        alerts, report = compare_with_baseline(result)
        print(report)
        sys.exit(0)

    # 正式模式: 运行回测
    output = run_backtest()
    result, output = parse_backtest_result(output)

    if not result:
        # 回测失败, 发告警
        send_email(
            '回测监控失败 - 无法解析结果',
            f'回测输出:\n{output[:2000]}\n\n请手动检查回测脚本.',
            priority='high'
        )
        sys.exit(1)

    alerts, report = compare_with_baseline(result)
    print(report)

    # 发送周报邮件
    state = load_alert_state()
    report_key = f'backtest_report_{datetime.now(CST).strftime("%Y-W%W")}'
    if should_alert(report_key, state):
        priority = 'high' if alerts else 'info'
        send_email(
            f'回测监控周报 - {"有告警" if alerts else "正常"}',
            report,
            priority=priority
        )
        state[report_key] = time.time()

    # 发送告警邮件
    for alert in alerts:
        if should_alert(alert['key'], state):
            send_email(alert['subject'], alert['body'], alert['priority'])
            state[alert['key']] = time.time()

    save_alert_state(state)
    sys.exit(1 if alerts else 0)


if __name__ == '__main__':
    main()
