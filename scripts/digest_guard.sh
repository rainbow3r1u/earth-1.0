#!/bin/bash
# 晨报保险丝 (2026-08-27): daily_digest_email.py 损坏(git冲突标记/语法错误)时
# 用最近正常备份版发送晨报, 保证 09:00 报告永不静默。
# 备份存放于仓库外(~/.local/share/auto_trade/), 不受 stash/pop/rebase 影响。
# 触发背景: 8/26、8/27 连续两天 notarize_pred.sh 的 stash-pop 冲突把
# daily_digest_email.py 打出冲突标记 → 09:00 cron IndentationError → 晨报停发。
cd /home/myuser/websocket_new
BK=/home/myuser/.local/share/auto_trade/.digest_lastgood.py
LOG=logs/digest.log
ERR=/tmp/digest_guard.err

if /usr/bin/python3 -m py_compile daily_digest_email.py 2>"$ERR"; then
    # 正常路径: 发送并刷新备份
    /usr/bin/python3 daily_digest_email.py >> "$LOG" 2>&1
    cp daily_digest_email.py "$BK"
else
    echo "$(date +%F-%T) GUARD: daily_digest_email.py 编译失败, 启用备份版发送" >> "$LOG"
    # 先告警(无论备份是否存在)
    /usr/bin/python3 - >> "$LOG" 2>&1 <<'PYEOF' || true
from alert_monitor import send_email
err = open('/tmp/digest_guard.err').read()[:1500]
send_email('晨报脚本损坏-已启用备份版',
           'daily_digest_email.py 编译失败(疑似git冲突标记未解决)。\n'
           '今晨晨报改用最近正常备份版发送, 原文件请尽快人工修复:\n\n' + err)
PYEOF
    if [ -f "$BK" ]; then
        cp "$BK" /tmp/digest_backup_run.py
        /usr/bin/python3 /tmp/digest_backup_run.py >> "$LOG" 2>&1
    fi
fi
