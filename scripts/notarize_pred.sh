#!/bin/bash
# 08:30 每日预测公证：commit + push 前先 fetch+rebase，
# 防止 08:50 Contents API 同步产生的远程 commit 导致 push 被拒。
# 2026-08-24: 因 8/23、8/24 连续 push rejected 而创建。
# 2026-08-27: 加 4b) pop 冲突检测告警 — 8/26、8/27 连续两天 stash pop
#             冲突留下 UU 状态, 09:00 晨报编译崩溃静默停发。
set -euo pipefail
cd /home/myuser/websocket_new
LOG=logs/notarize.log
REMOTE=xgboot
BRANCH=main

f="data/pred_$(date +%F).json"
if [ ! -f "$f" ]; then
  echo "$(date +%F-%T) SKIP公证: $f 未生成" >> "$LOG"
  exit 0
fi

echo "$(date +%F-%T) === notarize start ===" >> "$LOG"

# 1) fetch 远程（Contents API 可能已写入新 commit）
git fetch "$REMOTE" "$BRANCH" >> "$LOG" 2>&1

# 2) 暂存所有改动（含未跟踪文件），给 rebase 腾出干净工作区
git add -A >> "$LOG" 2>&1
STASHED=0
if ! git diff --cached --quiet; then
  git stash --include-untracked >> "$LOG" 2>&1
  STASHED=1
fi

# 3) rebase 到远程最新
if ! git rebase "$REMOTE/$BRANCH" >> "$LOG" 2>&1; then
  git rebase --abort >> "$LOG" 2>&1
  echo "$(date +%F-%T) ERROR: rebase 失败，放弃推送" >> "$LOG"
  [ $STASHED -eq 1 ] && git stash pop >> "$LOG" 2>&1
  exit 1
fi

# 4) 恢复暂存的本地改动
if [ $STASHED -eq 1 ]; then
  git stash pop >> "$LOG" 2>&1 || true
fi

# 4b) pop 冲突检测 (2026-08-27 加): UU 状态会让 09:00 晨报编译崩溃,
#     必须在 08:30 就告警, 不能等到晨报静默失败。
UU_FILES=$(git status --short | grep -E '^(UU|AA|DD|AU|UA|DU|UD)' || true)
if [ -n "$UU_FILES" ]; then
  echo "$(date +%F-%T) ERROR: stash pop 产生冲突, 未解决文件:" >> "$LOG"
  echo "$UU_FILES" >> "$LOG"
  echo "$UU_FILES" > /tmp/notarize_uu.txt
  /usr/bin/python3 - >> "$LOG" 2>&1 <<'PYEOF' || true
from alert_monitor import send_email
uu = open('/tmp/notarize_uu.txt').read()
send_email('公证stash-pop冲突-需人工解决',
           'websocket_new 出现未解决 git 冲突(UU):\n' + uu +
           '\n09:00 晨报将自动用备份版发送, 但请尽快人工解决:\n'
           'git add <文件> 标记 resolution 后 commit, 否则每天公证都会重演冲突。')
PYEOF
fi

# 5) 只提交预测相关文件
git add data/pred_*.json data/daily_predictions.json \
        data/universe/ data/forward_ic_history.json \
        data/forward_ic_history_48h.json >> "$LOG" 2>&1

git commit -m "pred: $(date +%F) 每日预测公证(预测先于结果)" >> "$LOG" 2>&1

# 6) push
if git push "$REMOTE" "HEAD:$BRANCH" >> "$LOG" 2>&1; then
  echo "$(date +%F-%T) === notarize done (push OK) ===" >> "$LOG"
else
  echo "$(date +%F-%T) ERROR: push 仍然失败" >> "$LOG"
  exit 1
fi
