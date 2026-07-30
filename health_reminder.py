#!/usr/bin/env python3
"""
系统健康提醒脚本 — 每15天发送一次系统状态摘要邮件
检查项: 三端同步状态、最近交易、数据更新、模型训练、回测性能
"""
import os, sys, json, hashlib, subprocess
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ============ 配置 ============
load_dotenv('/home/myuser/websocket_new/.env')

SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
ALERT_TO = '305488483@qq.com'
SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465

CST = timezone(timedelta(hours=8))
REMINDER_INTERVAL_DAYS = 15
STATE_FILE = '/home/myuser/websocket_new/logs/health_reminder_state.json'

PROD_AUTO_DUAL = '/home/myuser/websocket_new/auto_dual_trade.py'
OBSERVER_HOST = 'myuser@38.55.252.66'
OBSERVER_PATH = '/home/myuser/websocket_new/auto_dual_trade.py'
GPU_HOST = os.environ.get('GPU_HOST', 'linux@175.155.64.171')   # GPU按天租, 每次地址/端口会变
GPU_PORT = os.environ.get('GPU_PORT', '24048')
GPU_PASS = os.environ.get('GPU_PASS', '')   # 密码从环境变量读, 绝不硬编码(脚本当前已在cron禁用)
GPU_BACKTEST = '/home/linux/websocket_new/gpu_backtest.py'

TRADE_LOG = '/home/myuser/websocket_new/logs/auto_dual.log'
KLINE_CACHE = '/home/myuser/backtester/data_cache/notusdt_1d_full.json'
PRED_ARCHIVE_DIR = '/home/myuser/websocket_new/data/predictions'


def now_cst():
    return datetime.now(CST)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def md5_file(path):
    """本地文件MD5"""
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


def ssh_md5(host, path, port=None, password=None):
    """通过SSH获取远程文件MD5"""
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
    if port:
        cmd += ['-p', port]
    cmd += [host, f'md5sum {path}']
    try:
        if password:
            full_cmd = f"sshpass -p '{password}' " + ' '.join(cmd)
            r = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=20)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split()[0]
    except Exception:
        pass
    return None


def check_three_way_sync():
    """检查三端代码同步状态"""
    results = []
    prod_md5 = md5_file(PROD_AUTO_DUAL)
    results.append(f'生产端 auto_dual_trade.py: {prod_md5}')

    obs_md5 = ssh_md5(OBSERVER_HOST, OBSERVER_PATH)
    sync_obs = '✓ 一致' if obs_md5 == prod_md5 else '✗ 不一致'
    results.append(f'观察端 auto_dual_trade.py: {obs_md5} [{sync_obs}]')

    # 回溯端检查gpu_backtest.py的关键配置
    gpu_config_cmd = (
        f"sshpass -p '{GPU_PASS}' ssh -o StrictHostKeyChecking=no -p {GPU_PORT} "
        f"{GPU_HOST} \"grep -c \\\"device='cuda'\\\" {GPU_BACKTEST}; "
        f"grep 'TRAIN_WINDOW_OVERRIDE' {GPU_BACKTEST}\""
    )
    try:
        r = subprocess.run(gpu_config_cmd, shell=True, capture_output=True, text=True, timeout=20)
        lines = r.stdout.strip().split('\n') if r.stdout.strip() else []
        cuda_count = lines[0] if lines else '?'
        tw_line = lines[1] if len(lines) > 1 else '?'
        has_cuda = cuda_count != '0'
        sync_gpu = '✗ 仍有cuda' if has_cuda else '✓ CPU模式'
        results.append(f'回溯端 gpu_backtest.py: {sync_gpu} | {tw_line.strip()}')
    except Exception as e:
        results.append(f'回溯端 gpu_backtest.py: 检查失败 ({e})')

    all_sync = (obs_md5 == prod_md5) and not has_cuda if 'has_cuda' in dir() else (obs_md5 == prod_md5)
    return all_sync, results


def check_recent_trades():
    """检查最近交易记录"""
    results = []
    try:
        if not os.path.exists(TRADE_LOG):
            results.append('交易日志不存在')
            return results
        # 读取最近50行
        with open(TRADE_LOG) as f:
            lines = f.readlines()[-50:]
        # 找最近的交易相关行
        trade_lines = [l.strip() for l in lines if any(k in l for k in ['开仓', '平仓', '止盈', '止损', '训练完成', 'best_long', 'best_short'])]
        for line in trade_lines[-10:]:
            results.append(line)
        if not trade_lines:
            results.append('最近无交易记录')
    except Exception as e:
        results.append(f'读取交易日志失败: {e}')
    return results


def check_data_freshness():
    """检查数据更新状态"""
    results = []
    now = now_cst()
    files_to_check = [
        ('K线缓存', KLINE_CACHE),
        ('OI缓存', '/home/myuser/backtester/data_cache/oi_daily.json'),
        ('恐慌贪婪', '/home/myuser/websocket_new/data/fear_greed_history.json'),
    ]
    for name, path in files_to_check:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=CST)
            age_hours = (now - mtime).total_seconds() / 3600
            status = '✓ 新鲜' if age_hours < 30 else f'⚠ {age_hours:.0f}h前'
            results.append(f'{name}: {mtime.strftime("%m-%d %H:%M")} [{status}]')
        except Exception:
            results.append(f'{name}: 文件不存在')
    return results


def check_predictions():
    """检查预测存档"""
    results = []
    try:
        if not os.path.exists(PRED_ARCHIVE_DIR):
            results.append('预测存档目录不存在')
            return results
        files = sorted(os.listdir(PRED_ARCHIVE_DIR), reverse=True)[:5]
        for f in files:
            results.append(f)
        if not files:
            results.append('无预测存档')
    except Exception as e:
        results.append(f'检查预测存档失败: {e}')
    return results


def build_email_body():
    """构建邮件内容"""
    now = now_cst()
    sections = []

    sections.append(f'检查时间: {now.strftime("%Y-%m-%d %H:%M:%S")} CST')
    sections.append('')

    # 1. 三端同步
    sections.append('===== 三端代码同步状态 =====')
    all_sync, sync_results = check_three_way_sync()
    for r in sync_results:
        sections.append(r)
    sections.append(f'总体: {"✓ 三端完全同步" if all_sync else "⚠ 存在不一致"}')
    sections.append('')

    # 2. 最近交易
    sections.append('===== 最近交易/训练记录 =====')
    trade_results = check_recent_trades()
    for r in trade_results:
        sections.append(r)
    sections.append('')

    # 3. 数据更新
    sections.append('===== 数据更新状态 =====')
    data_results = check_data_freshness()
    for r in data_results:
        sections.append(r)
    sections.append('')

    # 4. 预测存档
    sections.append('===== 最近预测存档 =====')
    pred_results = check_predictions()
    for r in pred_results:
        sections.append(r)
    sections.append('')

    sections.append('---')
    sections.append('此邮件由生产系统每15天自动发送')

    return '\n'.join(sections)


def send_email(subject, body):
    """发送邮件"""
    if not SMTP_USER or not SMTP_AUTH_CODE:
        print('[REMINDER] SMTP未配置, 跳过邮件发送')
        return False

    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = f'[系统健康提醒] {subject}'

    color = '#17a2b8'
    html = f"""
    <html><body style="font-family: 'Microsoft YaHei', Arial; color: #333;">
    <div style="background: {color}; color: white; padding: 12px; border-radius: 5px;">
        <h2 style="margin: 0;">📋 {subject}</h2>
    </div>
    <div style="padding: 15px; border: 1px solid #ddd; margin-top: 10px;">
        <pre style="white-space: pre-wrap; font-size: 14px;">{body}</pre>
    </div>
    <hr style="border: 1px solid #eee;">
    <p style="color: #999; font-size: 12px;">
        发送时间: {now_cst().strftime('%Y-%m-%d %H:%M:%S')} CST<br>
        服务器: {os.uname().nodename}<br>
        每15天自动发送, 请勿回复
    </p>
    </body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        print(f'[REMINDER] 邮件已发送: {subject} → {ALERT_TO}')
        return True
    except Exception as e:
        print(f'[REMINDER] 邮件发送失败: {e}')
        return False


def main():
    now = now_cst()
    state = load_state()

    last_sent = state.get('last_sent')
    if last_sent:
        last_dt = datetime.fromisoformat(last_sent)
        days_since = (now - last_dt).total_seconds() / 86400
        if days_since < REMINDER_INTERVAL_DAYS:
            print(f'[REMINDER] 距上次发送仅{days_since:.1f}天, 不足{REMINDER_INTERVAL_DAYS}天, 跳过')
            return

    print('[REMINDER] 开始构建系统健康报告...')
    body = build_email_body()
    subject = f'15天系统健康报告 ({now.strftime("%Y-%m-%d")})'

    if send_email(subject, body):
        state['last_sent'] = now.isoformat()
        state['last_subject'] = subject
        save_state(state)
        print('[REMINDER] 状态已更新')
    else:
        print('[REMINDER] 邮件发送失败, 状态未更新')


if __name__ == '__main__':
    main()
