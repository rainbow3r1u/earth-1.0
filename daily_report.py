#!/usr/bin/env python3
"""每日简报 — 每天9点CST发送，只含3项：数据采集/多空列表/系统状态"""
import os, json, time, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv('/home/myuser/websocket_new/.env')
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
ALERT_TO = '305488483@qq.com'

CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY = NOW.strftime('%Y-%m-%d')
TODAY_START = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

# 数据文件
DATA_FILES = {
    'K线缓存': '/home/myuser/backtester/data_cache/notusdt_1d_full.json',
    'OI缓存': '/home/myuser/backtester/data_cache/oi_daily.json',
    '恐慌贪婪': '/home/myuser/websocket_new/data/fear_greed_history.json',
    '宏观资产': '/home/myuser/websocket_new/data/macro_assets.json',
    '清算数据': '/home/myuser/websocket_new/data/liq_daily.json',
}
LOG_FILE = '/home/myuser/websocket_new/logs/auto_dual.log'
PRED_FILE = '/home/myuser/websocket_new/data/daily_predictions.json'

def data_status():
    """检查数据文件是否今日更新"""
    lines = []
    all_ok = True
    for name, path in DATA_FILES.items():
        if not os.path.exists(path):
            lines.append(f'  {name}: 不存在')
            all_ok = False
        else:
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            ok = age_h < 26
            if not ok: all_ok = False
            lines.append(f'  {name}: {"OK" if ok else "STALE"} ({age_h:.1f}h前)')
    return all_ok, '\n'.join(lines)

def pred_list():
    """读取当天多空列表"""
    if not os.path.exists(PRED_FILE):
        return False, '预测文件不存在'
    try:
        with open(PRED_FILE) as f:
            d = json.load(f)
        updated = d.get('updated', 0)
        if updated < TODAY_START.timestamp():
            return False, f'预测未更新 (date={d.get("date")})'
        bl = d.get('best_long')
        bs = d.get('best_short')
        tl = d.get('top10_long', [])
        ts = d.get('top10_short', [])
        lines = []
        if bl:
            lines.append(f'  最佳多: {bl["symbol"]} ({bl["prob"]}%)')
        else:
            lines.append('  最佳多: 无')
        if bs:
            lines.append(f'  最佳空: {bs["symbol"]} ({bs["prob"]}%)')
        else:
            lines.append('  最佳空: 无')
        if tl:
            top5_l = ', '.join(f"{x['symbol']}({x['prob']}%)" for x in tl[:5])
            lines.append(f'  多TOP5: {top5_l}')
        if ts:
            top5_s = ', '.join(f"{x['symbol']}({x['prob']}%)" for x in ts[:5])
            lines.append(f'  空TOP5: {top5_s}')
        return True, '\n'.join(lines)
    except Exception as e:
        return False, f'读取失败: {e}'

def system_status():
    """检查系统是否正常运行"""
    if not os.path.exists(LOG_FILE):
        return False, '交易日志不存在'
    age_h = (time.time() - os.path.getmtime(LOG_FILE)) / 3600
    if age_h > 30:
        return False, f'日志{age_h:.1f}h未更新 (Cron可能未运行)'
    # 读最后几行看是否有报错
    try:
        with open(LOG_FILE, 'rb') as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 4096))
            content = f.read().decode('utf-8', errors='ignore')
        if 'Traceback' in content or 'ERROR' in content:
            # 找最后一条错误
            for line in reversed(content.split('\n')):
                if 'Traceback' in line or 'ERROR' in line:
                    return True, f'运行中(有报错): {line[:80]}'
            return True, '运行中(有报错)'
        return True, '运行正常'
    except:
        return True, '运行中'

def send_email(subject, body):
    if not SMTP_USER or not SMTP_AUTH_CODE:
        print('[REPORT] SMTP未配置, 跳过')
        return False
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = ALERT_TO
    msg['Subject'] = subject
    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=15) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        print(f'[REPORT] 邮件已发送: {subject}')
        return True
    except Exception as e:
        print(f'[REPORT] 发送失败: {e}')
        return False

def main():
    host = os.uname().nodename
    is_observer = 'observer' in host or '38.55' in host
    role = '观察端' if is_observer else '生产端'

    data_ok, data_lines = data_status()
    pred_ok, pred_lines = pred_list()
    sys_ok, sys_lines = system_status()

    all_ok = data_ok and pred_ok and sys_ok
    icon = 'OK' if all_ok else 'ALERT'
    subject = f'[{role}] {TODAY} 简报 - {icon}'

    body = f"""{'='*40}
{role} 每日简报 {TODAY} {NOW.strftime('%H:%M')} CST
{'='*40}

1. 数据采集
{data_lines}

2. 当天多空列表
{pred_lines}

3. 系统状态
  {sys_lines}

{'='*40}
服务器: {host}
"""
    print(body)
    send_email(subject, body)

if __name__ == '__main__':
    main()
