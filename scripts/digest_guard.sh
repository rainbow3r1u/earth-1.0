#!/bin/bash
# 晨报保险丝 (2026-08-27 建, 2026-09-12 加固): daily_digest_email.py 损坏
# (git冲突标记/语法错误)时, 用最近正常备份版发送晨报, 保证 09:00 报告永不静默。
# 备份存放于仓库外(~/.local/share/auto_trade/), 不受 stash/pop/rebase 影响。
# 触发背景: 8/26、8/27 连续两天 notarize_pred.sh 的 stash-pop 冲突把
# daily_digest_email.py 打出冲突标记 → 09:00 cron IndentationError → 晨报停发。
#
# 2026-09-12 加固(原两处缺陷):
#   ① 原在"编译通过"后无条件 cp 刷新备份、不看 python 退出码 → **运行期崩溃的版本也会被
#      存成 lastgood**, 真正需要保险丝时备份可能同样是坏的(运行期故障比语法错误更常见)。
#      现: 只有发送成功才刷新备份。
#   ② 原只兜编译失败, 运行期失败(非0退出)完全无兜底。现: 运行期失败改用备份版补发 + 告警。
cd /home/myuser/websocket_new
BK=/home/myuser/.local/share/auto_trade/.digest_lastgood.py
LOG=logs/digest.log
ERR=/tmp/digest_guard.err

notify() {
    /usr/bin/python3 - "$1" "$2" >> "$LOG" 2>&1 <<'PYEOF' || true
import sys
from alert_monitor import send_email
send_email(sys.argv[1], sys.argv[2])
PYEOF
}

run_backup() {
    if [ -f "$BK" ]; then
        cp "$BK" /tmp/digest_backup_run.py
        /usr/bin/python3 /tmp/digest_backup_run.py >> "$LOG" 2>&1
    else
        echo "$(date +%F-%T) GUARD: 备份版不存在, 无法补发/代发" >> "$LOG"
    fi
}

if /usr/bin/python3 -m py_compile daily_digest_email.py 2>"$ERR"; then
    # 编译通过: 发送; 仅发送成功才刷新备份
    if /usr/bin/python3 daily_digest_email.py >> "$LOG" 2>&1; then
        cp daily_digest_email.py "$BK"
    else
        echo "$(date +%F-%T) GUARD: daily_digest_email.py 运行失败(非0退出), 未刷新备份, 尝试备份版补发" >> "$LOG"
        notify '晨报运行失败-已启用备份版补发' "daily_digest_email.py 编译通过但运行时失败(详见 logs/digest.log 末尾)。
今晨晨报改用最近正常备份版补发; 备份未被本次失败版本覆盖。请尽快人工检查主脚本。"
        run_backup
    fi
else
    echo "$(date +%F-%T) GUARD: daily_digest_email.py 编译失败, 启用备份版发送" >> "$LOG"
    # 先告警(无论备份是否存在)
    notify '晨报脚本损坏-已启用备份版' "daily_digest_email.py 编译失败(疑似git冲突标记未解决)。
今晨晨报改用最近正常备份版发送, 原文件请尽快人工修复:

$(head -c 1500 "$ERR" 2>/dev/null)"
    run_backup
fi
