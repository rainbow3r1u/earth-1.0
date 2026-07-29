#!/usr/bin/env python3
"""
每日健康检查脚本 — 每天08:30 CST运行

功能:
  1. 检查trade.log中的ERROR/CRITICAL/WARN/失败异常（只看最后一次运行）
  2. 检查特征矩阵NaN/Inf（从最近一次训练日志中提取）
  3. 检查数据文件新鲜度
  4. 检查持仓状态一致性
  5. 检查三端MD5同步（仅生产端执行）

邮件规则:
  - 生产端: 有异常时发邮件告警，无异常时发"OK"简报
  - 观察端: 不发邮件（避免重复），只打印到stdout

cron: 30 8 * * * /usr/bin/python3 /home/myuser/websocket_new/daily_health_check.py
"""
import os, sys, json, time, smtplib, hashlib, re
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

# ============ 配置 ============
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY = NOW.strftime('%Y-%m-%d')

# 邮件配置
try:
    from dotenv import load_dotenv
    load_dotenv('/home/myuser/websocket_new/.env')
    SMTP_USER = os.environ.get('SMTP_USER', '')
    SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
except Exception:
    SMTP_USER = ''
    SMTP_AUTH_CODE = ''
ALERT_TO = '305488483@qq.com'

# 角色判断
HOST = os.uname().nodename
IS_OBSERVER = 'observer' in HOST or '38.55' in HOST
ROLE = '观察端' if IS_OBSERVER else '生产端'

# 关键文件路径
TRADE_LOG = os.path.expanduser('~/.local/share/auto_trade/trade.log')
STATE_FILE = os.path.expanduser('~/.local/share/auto_trade/state.json')
DATA_FILES = {
    'K线缓存': '/home/myuser/backtester/data_cache/notusdt_1d_full.json',
    'OI缓存': '/home/myuser/backtester/data_cache/oi_daily.json',
    '恐慌贪婪': '/home/myuser/websocket_new/data/fear_greed_history.json',
    '宏观资产': '/home/myuser/websocket_new/data/macro_assets.json',
    '清算数据': '/home/myuser/websocket_new/data/liq_daily.json',
    'BTC市值': '/home/myuser/coingecko_data/btc_mcap.json',
    'BTC市占率': '/home/myuser/coingecko_data/btc_dominance.json',
    '算力': '/home/myuser/hashrate_data/hashrate_history.json',
    'TVL(ETH)': '/home/myuser/defillama_data/ethereum_tvl.json',
    # crypto_sectors.json是静态板块映射，不需要每天更新，从新鲜度检查中移除
    # (也不做MD5一致性检查: 两端各自采集分类, 新币归类时序不同, 内容合法不一致)
}

# 异常关键词
ERROR_KEYWORDS = ['ERROR', 'CRITICAL', '失败', '裸奔', 'Traceback', '不匹配', '异常退出']
# 忽略的关键词（已知且已修复的旧记录）
IGNORE_KEYWORDS = ['特征维度不匹配! 实际=942 期望=936']  # 已修复，但旧日志可能还有


def log(msg):
    ts = NOW.strftime('%H:%M:%S')
    print(f'[{ts}] {msg}')


def send_email(subject, body):
    """发送邮件（观察端不发）"""
    if IS_OBSERVER:
        log(f'[观察端] 跳过邮件发送: {subject}')
        return False
    if not SMTP_USER or not SMTP_AUTH_CODE:
        log('[邮件] SMTP未配置, 跳过')
        return False
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = subject
    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=15) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        log(f'[邮件] 已发送: {subject}')
        return True
    except Exception as e:
        log(f'[邮件] 发送失败: {e}')
        return False


def check_trade_log():
    """检查trade.log中的异常（只看最后一次"交易启动"之后的日志）"""
    if not os.path.exists(TRADE_LOG):
        return False, 'trade.log不存在'

    try:
        with open(TRADE_LOG, 'rb') as f:
            f.seek(0, 2); size = f.tell()
            # 最多读最后256KB
            f.seek(max(0, size - 262144))
            content = f.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return False, f'读取trade.log失败: {e}'

    lines = content.split('\n')

    # 找最后一次"交易启动"的位置
    marker = '自动多空二选一交易启动'
    last_start_idx = -1
    for i, line in enumerate(lines):
        if marker in line:
            last_start_idx = i

    if last_start_idx < 0:
        # 没找到"交易启动"，看最后100行
        last_start_idx = max(0, len(lines) - 100)

    recent_lines = lines[last_start_idx:]

    # 检查日志新鲜度
    if recent_lines:
        first_line = recent_lines[0]
        # 提取时间戳 [2026-07-12 08:05:00]
        ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', first_line)
        if ts_match:
            log_time = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
            age_h = (NOW - log_time).total_seconds() / 3600
            if age_h > 30:
                return False, f'最后一次运行在{age_h:.1f}h前（Cron可能未运行）'

    # 搜索异常关键词
    errors = []
    for line in recent_lines:
        if any(kw in line for kw in ERROR_KEYWORDS):
            # 跳过已知忽略的关键词
            if any(ign in line for ign in IGNORE_KEYWORDS):
                continue
            errors.append(line.strip()[:150])

    if errors:
        error_str = '\n'.join(errors[:20])  # 最多显示20条
        return False, f'发现{len(errors)}条异常:\n{error_str}'

    return True, '最后一次运行无异常'


def check_feature_nan():
    """检查特征矩阵NaN/Inf（从trade.log中提取，只看最后一次运行）"""
    if not os.path.exists(TRADE_LOG):
        return True, 'trade.log不存在, 跳过'

    try:
        with open(TRADE_LOG, 'rb') as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - 262144))
            content = f.read().decode('utf-8', errors='ignore')
    except Exception:
        return True, '读取trade.log失败, 跳过'

    lines = content.split('\n')

    # 只看最后一次"交易启动"之后的日志
    marker = '自动多空二选一交易启动'
    last_start_idx = -1
    for i, line in enumerate(lines):
        if marker in line:
            last_start_idx = i
    if last_start_idx < 0:
        last_start_idx = max(0, len(lines) - 100)
    recent_lines = lines[last_start_idx:]

    # 搜索NaN/Inf相关日志
    nan_lines = []
    for line in recent_lines:
        if 'NaN' in line or 'Inf' in line or 'nan' in line.lower():
            if '训练数据有' in line or 'WARNING' in line or 'CRITICAL' in line:
                nan_lines.append(line.strip()[:150])

    if nan_lines:
        recent_nan = nan_lines[-5:]
        return False, f'发现{len(nan_lines)}条NaN/Inf警告:\n' + '\n'.join(recent_nan)

    # 检查特征维度不匹配（只看最后一次运行）
    for line in recent_lines:
        if '特征维度不匹配' in line:
            return False, f'特征维度不匹配: {line.strip()[:100]}'

    return True, '特征矩阵无NaN/Inf'


def check_data_freshness():
    """检查数据文件新鲜度"""
    lines = []
    all_ok = True
    for name, path in DATA_FILES.items():
        if not os.path.exists(path):
            lines.append(f'  {name}: 不存在')
            all_ok = False
        else:
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            if age_h > 26:
                status = f'STALE({age_h:.0f}h前)'
                all_ok = False
            else:
                status = f'OK({age_h:.1f}h前)'
            lines.append(f'  {name}: {status}')
    return all_ok, '\n'.join(lines)


def check_positions():
    """检查持仓状态一致性"""
    if not os.path.exists(STATE_FILE):
        return True, 'state.json不存在(无持仓)'

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception as e:
        return False, f'state.json读取失败: {e}'

    positions = state.get('positions', {})
    if not positions:
        return True, '无持仓'

    lines = []
    has_issue = False
    for sym, pos in positions.items():
        side = pos.get('direction', '?')
        entry = pos.get('open_price', None)
        entry_time = pos.get('open_ts', None)

        issues = []
        if entry is None or entry == 0:
            issues.append('open_price缺失')
            has_issue = True
        if entry_time is None:
            issues.append('open_ts缺失')
            has_issue = True

        issue_str = f' [{",".join(issues)}]' if issues else ''
        lines.append(f'  {sym}: {side} entry={entry}{issue_str}')

    status = '有问题' if has_issue else '正常'
    return not has_issue, f'{status}({len(positions)}个持仓)\n' + '\n'.join(lines)


def check_md5_sync():
    """检查三端MD5同步（仅生产端执行）。观察端已于2026-07-29下线, 跳过。"""
    if True or IS_OBSERVER:
        return True, '观察端已下线(2026-07-29), 跳过MD5同步检查'

    import subprocess
    files_to_check = [
        ('auto_dual_trade.py', '/home/myuser/websocket_new/auto_dual_trade.py'),
        ('daily_predictor.py', '/home/myuser/websocket_new/daily_predictor.py'),
        ('daily_digest_email.py', '/home/myuser/websocket_new/daily_digest_email.py'),
        ('daily_momentum_email.py', '/home/myuser/websocket_new/daily_momentum_email.py'),
    ]

    lines = []
    all_ok = True
    for name, local_path in files_to_check:
        if not os.path.exists(local_path):
            lines.append(f'  {name}: 本地不存在')
            all_ok = False
            continue

        local_md5 = hashlib.md5(open(local_path, 'rb').read()).hexdigest()[:8]

        # 检查观察端
        try:
            r = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
                 'myuser@38.55.252.66', f'md5sum {local_path}'],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                remote_md5 = r.stdout.strip().split()[0][:8]
                match = '✅' if local_md5 == remote_md5 else '❌'
                if local_md5 != remote_md5:
                    all_ok = False
                lines.append(f'  {name}: 本地={local_md5} 观察端={remote_md5} {match}')
            else:
                lines.append(f'  {name}: 本地={local_md5} 观察端=连接失败')
        except Exception as e:
            lines.append(f'  {name}: 本地={local_md5} 观察端=SSH失败({e})')

    return all_ok, '\n'.join(lines)


def main():
    log(f'=== {ROLE} 每日健康检查 {TODAY} {NOW.strftime("%H:%M")} CST ===')

    # 执行所有检查
    checks = []

    log('[1/5] 检查trade.log异常...')
    ok, msg = check_trade_log()
    checks.append(('日志异常检查', ok, msg))

    log('[2/5] 检查特征NaN/Inf...')
    ok, msg = check_feature_nan()
    checks.append(('特征NaN/Inf检查', ok, msg))

    log('[3/5] 检查数据新鲜度...')
    ok, msg = check_data_freshness()
    checks.append(('数据新鲜度', ok, msg))

    log('[4/5] 检查持仓状态...')
    ok, msg = check_positions()
    checks.append(('持仓状态', ok, msg))

    log('[5/5] 检查三端MD5同步...')
    ok, msg = check_md5_sync()
    checks.append(('MD5同步', ok, msg))

    # 汇总
    all_ok = all(ok for _, ok, _ in checks)
    icon = 'OK' if all_ok else 'ALERT'
    subject = f'[{ROLE}] {TODAY} 健康检查 - {icon}'

    body_lines = [
        f'{"=" * 50}',
        f'{ROLE} 每日健康检查 {TODAY} {NOW.strftime("%H:%M")} CST',
        f'{"=" * 50}',
        f'总状态: {icon}',
        '',
    ]

    for name, ok, msg in checks:
        status = '✅' if ok else '❌'
        body_lines.append(f'## {status} {name}')
        body_lines.append(msg)
        body_lines.append('')

    body_lines.append(f'{"=" * 50}')
    body_lines.append(f'服务器: {HOST}')

    body = '\n'.join(body_lines)
    print(body)

    # 发送邮件（观察端不发）
    send_email(subject, body)

    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
