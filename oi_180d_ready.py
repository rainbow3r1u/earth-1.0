#!/usr/bin/env python3
"""OI 攒满 180 天检测 (2026-08-10): 一次性任务, 达到即通知+自毁

Why: OI 历史从 2026-05-10 开始积累, 训练窗=滚动180天。在 OI 覆盖 <180 天时,
     训练样本中前段 oi_chg=0 (占 ~47%), 模型学到的 OI 信号不完整。
     当 OI 覆盖 >= 180 天时, 训练窗内 OI 全量有效 → 发邮件通知用户,
     并写标志文件 data/oi_180d_ready.flag, 然后从 crontab 删除本任务 (一次性)。

运行时机: 每日 08:30 (训练完成后, OI 已是最新)
"""
import os, sys, json, subprocess, datetime as dt
from email.mime.text import MIMEText
import smtplib

BASE = os.path.dirname(os.path.abspath(__file__))
CST = dt.timezone(dt.timedelta(hours=8))
LOG_FILE = os.path.join(BASE, 'logs', 'oi_180d.log')
OI_CACHE = '/home/myuser/backtester/data_cache/oi_daily.json'
FLAG = os.path.join(BASE, 'data', 'oi_180d_ready.flag')
TARGET = 180

def log(msg):
    line = f'[{dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def send_mail(subject, body):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE, '.env'))
        SMTP_USER = os.environ.get('SMTP_USER', '')
        SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
        if not SMTP_USER or not SMTP_AUTH_CODE:
            log('[邮件] SMTP未配置, 跳过')
            return False
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = SMTP_USER
        msg['To'] = '305488483@qq.com'
        msg['Subject'] = subject
        with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=15) as s:
            s.login(SMTP_USER, SMTP_AUTH_CODE)
            s.sendmail(SMTP_USER, ['305488483@qq.com'], msg.as_string())
        log(f'[邮件] 已发送: {subject}')
        return True
    except Exception as e:
        log(f'[邮件] 发送失败: {e}')
        return False

def oi_coverage_days():
    """OI 覆盖天数 = 最早~最新跨度 (天), 用 BTCUSDT (全币种同起点)"""
    try:
        d = json.load(open(OI_CACHE))
        recs = d.get('BTCUSDT', {})
        ks = sorted(int(k) for k in recs.keys())
        if len(ks) < 2:
            return len(ks), None
        span = (ks[-1] - ks[0]) / 86400
        return span + 1, (ks[0], ks[-1])
    except Exception as e:
        log(f'[警告] OI 读取失败: {e}')
        return 0, None

def remove_self_from_crontab():
    """从 crontab 删除本任务 (一次性自毁)"""
    try:
        cur = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
        lines = [l for l in cur.splitlines() if 'oi_180d_ready.py' not in l]
        subprocess.run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True, check=True)
        log('✅ 已从 crontab 移除本任务 (一次性完成)')
    except Exception as e:
        log(f'[警告] crontab 自移除失败: {e} (请手动清理)')

def main():
    # 已触发过则直接退出 (防重复)
    if os.path.exists(FLAG):
        log('已触发过 (flag 存在), 退出')
        remove_self_from_crontab()
        return

    days, span = oi_coverage_days()
    log(f'OI 覆盖: {days:.0f} 天 (目标 {TARGET} 天)')

    if days >= TARGET:
        # 触发: 写 flag + 发邮件 + 自毁
        with open(FLAG, 'w') as f:
            f.write(f'oi_180d_ready at {dt.datetime.now(CST).isoformat()}\n')
        body = (f'OI 数据已攒满 {TARGET} 天 (覆盖 {span[0]} → {span[1]})\n\n'
                f'从今天起, 180 天训练窗内 oi_chg 特征全量有效 (不再有 ~47% 零值)。\n'
                f'XGBoost 今日重训将自动吃到完整 OI 信号。\n\n'
                f'本一次性任务已自毁。如需验证: 训练后检查 oi_chg 非零比例。')
        send_mail(f'✅ OI 已攒满 {TARGET} 天, 自动纳入 XGBoost 训练', body)
        remove_self_from_crontab()
    else:
        remaining = TARGET - int(days)
        log(f'未满 {TARGET} 天, 还需 {remaining} 天 (~{dt.date.today() + dt.timedelta(days=remaining)})')

if __name__ == '__main__':
    main()
