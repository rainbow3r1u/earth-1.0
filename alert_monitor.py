#!/usr/bin/env python3
"""
生产系统邮件告警脚本
功能: 监控余额不足/数据陈旧/Permutation Test失败等异常, 发送邮件到305488483@qq.com

使用方式:
  1. 自动模式 (Cron每小时检查): python3 alert_monitor.py
  2. 手动触发: python3 alert_monitor.py --check
  3. 发送测试邮件: python3 alert_monitor.py --test

配置:
  需要在 /home/myuser/websocket_new/.env 中添加:
    SMTP_USER=your_qq@qq.com        (发件QQ邮箱)
    SMTP_AUTH_CODE=your_auth_code   (QQ邮箱SMTP授权码, 非登录密码)
"""
import os
import sys
import json
import time
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# ============ 配置 ============
load_dotenv('/home/myuser/websocket_new/.env')

SMTP_USER = os.environ.get('SMTP_USER', '')           # 发件QQ邮箱
SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '') # SMTP授权码
ALERT_TO = '305488483@qq.com'                          # 收件邮箱 (固定)
SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465

# 告警阈值
BALANCE_MIN_USDT = 10          # 余额低于此值告警
DATA_STALE_HOURS = 26          # 数据陈旧阈值(小时)
LOG_STALE_HOURS = 30           # 日志未更新阈值(小时, Cron未运行)

# 关键文件路径
DATA_DIR = '/home/myuser/websocket_new/data'
LOG_DIR = '/home/myuser/websocket_new/logs'
KEY_DATA_FILES = {
    'K线缓存': '/home/myuser/backtester/data_cache/notusdt_1d_full.json',
    'OI缓存': '/home/myuser/backtester/data_cache/oi_daily.json',
    '恐慌贪婪': f'{DATA_DIR}/fear_greed_history.json',
    '宏观资产': f'{DATA_DIR}/macro_assets.json',
    '清算数据': f'{DATA_DIR}/liq_daily.json',
    '算力数据': '/home/myuser/hashrate_data/hashrate_history.json',
    'BTC市值': '/home/myuser/coingecko_data/btc_mcap.json',
    'BTC市占率': '/home/myuser/coingecko_data/btc_dominance.json',
}
LOG_FILES = {
    '交易日志': f'{LOG_DIR}/auto_dual.log',
    '采集日志': f'{LOG_DIR}/collect.log',
}

# 告警状态文件 (避免重复告警)
ALERT_STATE = '/home/myuser/websocket_new/logs/alert_state.json'
ALERT_COOLDOWN_HOURS = 6  # 同一告警6小时冷却

# ============ 邮件发送 ============
def send_email(subject, body, priority='normal'):
    """发送邮件到QQ邮箱"""
    if not SMTP_USER or not SMTP_AUTH_CODE:
        print('[ALERT] SMTP未配置, 跳过邮件发送. 请在.env中设置 SMTP_USER 和 SMTP_AUTH_CODE')
        return False

    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = f'[加密系统告警] {subject}'

    # HTML邮件体
    color = {'high': '#dc3545', 'normal': '#ffc107', 'info': '#17a2b8'}.get(priority, '#17a2b8')
    html = f"""
    <html><body style="font-family: 'Microsoft YaHei', Arial; color: #333;">
    <div style="background: {color}; color: white; padding: 12px; border-radius: 5px;">
        <h2 style="margin: 0;">⚠️ {subject}</h2>
    </div>
    <div style="padding: 15px; border: 1px solid #ddd; margin-top: 10px;">
        <pre style="white-space: pre-wrap; font-size: 13px; font-family: Consolas, Menlo, 'Courier New', monospace; line-height: 1.5;">{body}</pre>
    </div>
    <hr style="border: 1px solid #eee;">
    <p style="color: #999; font-size: 12px;">
        告警时间: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')} CST<br>
        服务器: {os.uname().nodename}<br>
        此邮件由生产系统自动发送, 请勿回复
    </p>
    </body></html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        print(f'[ALERT] 邮件已发送: {subject} → {ALERT_TO}')
        return True
    except Exception as e:
        print(f'[ALERT] 邮件发送失败: {e}')
        return False


# ============ 告警检查 ============
def load_alert_state():
    """加载告警状态(用于冷却)"""
    if os.path.exists(ALERT_STATE):
        try:
            with open(ALERT_STATE) as f:
                return json.load(f)
        except:
            pass
    return {}


def save_alert_state(state):
    """保存告警状态"""
    os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
    with open(ALERT_STATE, 'w') as f:
        json.dump(state, f, indent=2)


def should_alert(alert_key, state):
    """检查是否应该告警(冷却期内不重复)"""
    last = state.get(alert_key, 0)
    return (time.time() - last) > ALERT_COOLDOWN_HOURS * 3600


def check_balance():
    """检查交易余额 (从最新交易日志解析)"""
    alerts = []
    log_file = LOG_FILES['交易日志']
    if not os.path.exists(log_file):
        return alerts

    # 读取最近100行日志
    try:
        with open(log_file, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            content = f.read().decode('utf-8', errors='ignore')
    except:
        return alerts

    # 检查余额不足关键词
    if '本金不足' in content or '余额不足' in content:
        # 提取最新一次
        lines = [l for l in content.split('\n') if '本金不足' in l or '余额不足' in l]
        if lines:
            alerts.append({
                'key': 'balance_low',
                'subject': '交易余额不足',
                'body': f'检测到余额不足, 系统已跳过交易!\n\n最新日志:\n{lines[-1]}\n\n请尽快充值USDT到交易账户.',
                'priority': 'high',
            })
    return alerts


def check_data_stale():
    """检查数据文件是否陈旧"""
    alerts = []
    now = time.time()
    stale_files = []

    for name, path in KEY_DATA_FILES.items():
        if not os.path.exists(path):
            stale_files.append(f'❌ {name}: 文件不存在 ({path})')
        else:
            age_hours = (now - os.path.getmtime(path)) / 3600
            if age_hours > DATA_STALE_HOURS:
                stale_files.append(f'⚠️ {name}: {age_hours:.1f}h前 ({os.path.basename(path)})')

    if stale_files:
        alerts.append({
            'key': 'data_stale',
            'subject': '数据文件陈旧',
            'body': f'以下数据文件已超过{DATA_STALE_HOURS}小时未更新:\n\n' + '\n'.join(stale_files) +
                    f'\n\n请检查 daily_data_collection.py 是否正常运行 (Cron 06:00 CST)',
            'priority': 'high',
        })
    return alerts


def check_cron_running():
    """检查Cron任务是否正常运行 (通过日志更新时间)"""
    alerts = []
    now = time.time()

    for name, path in LOG_FILES.items():
        if not os.path.exists(path):
            alerts.append({
                'key': f'cron_{name}_missing',
                'subject': f'{name}不存在 - Cron可能未运行',
                'body': f'日志文件不存在: {path}\n\n请检查crontab -l 确认Cron任务配置.',
                'priority': 'high',
            })
        else:
            age_hours = (now - os.path.getmtime(path)) / 3600
            if age_hours > LOG_STALE_HOURS:
                alerts.append({
                    'key': f'cron_{name}_stale',
                    'subject': f'{name} {age_hours:.1f}h未更新 - Cron可能停止',
                    'body': f'{name} 已 {age_hours:.1f} 小时未更新\n路径: {path}\n\n请检查:\n1. crontab -l 确认任务存在\n2. systemctl status cron 确认服务运行\n3. 手动运行测试: python3 auto_dual_trade.py',
                    'priority': 'high',
                })
    return alerts


def check_perm_test_fail():
    """检查Permutation Test是否失败 (overfit)

    FIX: 只检查最后一次"交易启动"之后的日志行，避免旧overfit记录导致重复告警。
    读取 trade.log (auto_dual_trade.py内部写入，cron和手动运行都会更新)。
    """
    import re
    alerts = []
    # 使用 auto_dual_trade.py 内部写入的日志文件（无论cron还是手动运行都会更新）
    log_file = os.path.expanduser('~/.local/share/auto_trade/trade.log')
    if not os.path.exists(log_file):
        log_file = LOG_FILES['交易日志']  # 回退
        if not os.path.exists(log_file):
            return alerts

    try:
        with open(log_file, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))  # 读最后64KB，确保包含至少一次完整运行
            content = f.read().decode('utf-8', errors='ignore')
    except:
        return alerts

    # 找到最后一次"交易启动"的位置，只检查该位置之后的日志
    marker = '自动多空二选一交易启动'
    last_start = content.rfind(marker)
    if last_start < 0:
        return alerts  # 没找到启动标记，不告警

    recent = content[last_start:]

    # 提取运行时间戳（用于告警key，避免同一运行结果重复告警）
    # marker 位置可能截断了时间戳行，往前找换行符以包含完整时间戳
    line_start = content.rfind('\n', 0, last_start)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    recent_with_ts = content[line_start:]
    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', recent_with_ts)
    run_ts = ts_match.group(1) if ts_match else 'unknown'

    # 只在最后一次运行中检查 overfit 关键词
    if '→ overfit' in recent:
        lines = [l for l in recent.split('\n') if 'PERM-TEST' in l and 'overfit' in l]
        if lines:
            alerts.append({
                'key': f'perm_test_overfit_{run_ts}',  # 含运行时间戳，同一运行只告警一次
                'subject': 'Permutation Test失败 - 模型过拟合',
                'body': f'检测到模型过拟合信号, 已禁止交易!\n\n运行时间: {run_ts}\n最新日志:\n{lines[-1]}\n\n可能原因:\n1. 特征无真实alpha\n2. 训练数据质量问题\n3. 市场环境突变',
                'priority': 'high',
            })
    return alerts


def _is_pred_updated_today():
    """检查 daily_predictions.json 是否今日已更新
    返回 (是否今日更新, 描述信息)
    注: 存档文件名基于最新K线数据日期(UTC), 可能是昨天, 故按 updated 时间戳判定.
    """
    cache_file = f'{DATA_DIR}/daily_predictions.json'
    if not os.path.exists(cache_file):
        return False, 'daily_predictions.json 不存在'
    try:
        with open(cache_file) as f:
            data = json.load(f)
        updated = data.get('updated', 0)
        if not updated:
            return False, '无 updated 字段'
        cst = timezone(timedelta(hours=8))
        today_start = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
        if updated >= today_start.timestamp():
            return True, f"date={data.get('date', '?')}"
        age_h = (time.time() - updated) / 3600
        return False, f'updated过期 ({age_h:.1f}h前, date={data.get("date")})'
    except Exception as e:
        return False, f'读取失败: {e}'


def check_prediction_fresh():
    """检查预测结果是否今日已生成 (按 daily_predictions.json 的 updated 时间戳判定)"""
    alerts = []
    today = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

    now = time.time()
    # 只在10:00 CST之后检查 (08:05应已完成)
    cst_hour = datetime.now(timezone(timedelta(hours=8))).hour
    if cst_hour >= 10:
        ok, msg = _is_pred_updated_today()
        if not ok:
            alerts.append({
                'key': f'pred_missing_{today}',
                'subject': f'今日({today})预测存档未生成',
                'body': f'检查结果: {msg}\n\n今日08:05 CST的auto_dual_trade.py可能运行失败.\n请检查日志: {LOG_FILES["交易日志"]}',
                'priority': 'high',
            })
    return alerts


def check_model_health():
    """检查模型训练数据完整性 (从最新训练数据npz)"""
    alerts = []
    train_data = '/home/myuser/.local/share/auto_trade/train_data_latest.npz'
    if not os.path.exists(train_data):
        return alerts

    try:
        import numpy as np
        data = np.load(train_data, allow_pickle=True)
        if 'X' in data:
            X = data['X']
            # NaN检查
            nan_count = int(np.isnan(X).sum())
            if nan_count > 0:
                alerts.append({
                    'key': 'model_nan_features',
                    'subject': f'训练数据有NaN ({nan_count}个)',
                    'body': f'训练数据路径: {train_data}\nNaN总数: {nan_count}\n\n可能原因:\n1. 数据采集缺失\n2. 特征计算异常\n请检查数据采集日志.',
                    'priority': 'high',
                })
            # 全零列检查 (排除Kronos 100:932 和 liq 72:91 置零区域)
            zero_cols = np.where(np.all(X == 0, axis=0))[0]
            expected_zero = set(range(100, 932)) | set(range(72, 91))
            unexpected_zero = [c for c in zero_cols if c not in expected_zero]
            if len(unexpected_zero) > 5:
                alerts.append({
                    'key': 'model_zero_cols',
                    'subject': f'训练数据有{len(unexpected_zero)}个异常全零列',
                    'body': f'训练数据路径: {train_data}\n异常全零列(前20): {unexpected_zero[:20]}\n\n排除置零区域(Kronos 100:932, liq 72:91)后仍有全零列, 可能特征计算异常.',
                    'priority': 'normal',
                })
            # 样本数检查
            if 'y' in data:
                y = data['y']
                if len(y) < 100:
                    alerts.append({
                        'key': 'model_low_samples',
                        'subject': f'训练样本数过少 ({len(y)})',
                        'body': f'训练样本数: {len(y)}\n阈值: 100\n\n样本过少会导致模型不稳定, 请检查训练窗口配置.',
                        'priority': 'high',
                    })
    except Exception as e:
        print(f'[CHECK] 模型健康检查失败: {e}')
    return alerts


def generate_health_report():
    """生成系统健康报告 (用于每日早报)"""
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime('%Y-%m-%d')

    # 1. 余额
    balance_info = '未知'
    log_file = LOG_FILES['交易日志']
    if os.path.exists(log_file):
        try:
            with open(log_file, 'rb') as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 8192))
                content = f.read().decode('utf-8', errors='ignore')
            if '本金不足' in content:
                balance_info = '不足10U (需充值)'
            elif '开仓' in content:
                balance_info = '已开仓'
            else:
                balance_info = '正常'
        except:
            pass

    # 2. 数据新鲜度
    data_status = []
    for name, path in KEY_DATA_FILES.items():
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            icon = 'OK' if age_h < 26 else 'STALE'
            data_status.append(f'  [{icon}] {name}: {age_h:.1f}h前')
        else:
            data_status.append(f'  [MISS] {name}: 不存在')

    # 3. 今日预测 (按 daily_predictions.json 的 updated 时间戳判定, 文件名日期可能是昨天UTC)
    ok, pred_msg = _is_pred_updated_today()
    pred_info = f'已生成 ({pred_msg})' if ok else f'未生成 ({pred_msg})'

    # 4. Permutation Test (只看最后一次运行的结果)
    perm_info = '未知'
    trade_log = os.path.expanduser('~/.local/share/auto_trade/trade.log')
    if not os.path.exists(trade_log):
        trade_log = log_file  # 回退到 auto_dual.log
    if os.path.exists(trade_log):
        try:
            with open(trade_log, 'rb') as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 65536))
                content = f.read().decode('utf-8', errors='ignore')
            import re
            # 只看最后一次"交易启动"之后的内容
            marker = '自动多空二选一交易启动'
            last_start = content.rfind(marker)
            recent = content[last_start:] if last_start >= 0 else content
            long_match = re.search(r'\[LONG\].*?drop=([+\-]?\d+\.\d+)%.*?(real|partial|overfit)', recent)
            short_match = re.search(r'\[SHORT\].*?drop=([+\-]?\d+\.\d+)%.*?(real|partial|overfit)', recent)
            if long_match and short_match:
                perm_info = f'LONG={long_match.group(2)}({long_match.group(1)}%), SHORT={short_match.group(2)}({short_match.group(1)}%)'
        except:
            pass

    # 5. Cron状态
    cron_status = []
    for name, path in LOG_FILES.items():
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            icon = 'OK' if age_h < 30 else 'STALE'
            cron_status.append(f'  [{icon}] {name}: {age_h:.1f}h前')
        else:
            cron_status.append(f'  [MISS] {name}: 不存在')

    report = f"""生产系统健康报告 - {today}

==============================
【系统概览】
  服务器: {os.uname().nodename}
  报告时间: {now.strftime('%Y-%m-%d %H:%M:%S')} CST
  Python: {sys.version.split()[0]}

==============================
【交易状态】
  余额: {balance_info}
  今日预测: {pred_info}
  Permutation Test: {perm_info}

==============================
【数据新鲜度】
{chr(10).join(data_status)}

==============================
【Cron任务状态】
{chr(10).join(cron_status)}

==============================
【Cron任务清单】
  06:00 - 数据采集 (daily_data_collection.py)
  06:30 - Cron监控 (cron_monitor.py)
  08:05 - 交易预测 (auto_dual_trade.py)
  08:40 - Cron监控 (cron_monitor.py)
  每小时 - 告警检查 (alert_monitor.py)

==============================
如发现STALE或MISS, 请及时处理.
此邮件由生产系统自动发送.
"""
    return report


# ============ 主流程 ============
def run_all_checks():
    """运行所有检查, 返回告警列表"""
    alerts = []
    alerts.extend(check_balance())
    alerts.extend(check_data_stale())
    alerts.extend(check_cron_running())
    alerts.extend(check_perm_test_fail())
    alerts.extend(check_prediction_fresh())
    alerts.extend(check_model_health())
    return alerts


def main():
    # 测试邮件
    if '--test' in sys.argv:
        print('发送测试邮件...')
        ok = send_email(
            '测试邮件 - 生产系统告警',
            '这是一封测试邮件, 确认邮件告警功能正常.\n\n如果你收到了这封邮件, 说明SMTP配置正确.',
            priority='info'
        )
        sys.exit(0 if ok else 1)

    # 健康报告 (每日早报)
    if '--report' in sys.argv:
        print('生成健康报告...')
        report = generate_health_report()
        print(report)
        ok = send_email('每日健康报告', report, priority='info')
        sys.exit(0 if ok else 1)

    # 手动检查
    if '--check' in sys.argv:
        print('=== 手动检查模式 ===')
        alerts = run_all_checks()
        if not alerts:
            print('所有检查通过, 无告警')
        else:
            print(f'发现 {len(alerts)} 个告警:')
            for a in alerts:
                print(f'  [{a["priority"]}] {a["subject"]}')
                print(f'    {a["body"][:100]}...')
                print()
        sys.exit(0 if not alerts else 1)

    # 自动模式 (Cron调用)
    alerts = run_all_checks()
    if not alerts:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] 所有检查通过')
        sys.exit(0)

    # 发送告警 (带冷却)
    state = load_alert_state()
    sent_count = 0
    for alert in alerts:
        if should_alert(alert['key'], state):
            ok = send_email(alert['subject'], alert['body'], alert['priority'])
            if ok:
                state[alert['key']] = time.time()
                sent_count += 1
        else:
            print(f'[ALERT] {alert["subject"]} 在冷却期内, 跳过')

    save_alert_state(state)
    print(f'[{datetime.now().strftime("%H:%M:%S")}] 检查完成: {len(alerts)}个告警, 发送{sent_count}封邮件')
    sys.exit(0)


if __name__ == '__main__':
    main()
