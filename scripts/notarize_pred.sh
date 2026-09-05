#!/bin/bash
# 08:30 每日预测公证 v2 — 2026-09-06 重构(结构性根治 stash-pop UU 冲突)
#
# v1 缺陷: `git add -A && git stash --include-untracked` + rebase + `git stash pop`。
#   08:50 每日同步(Contents API)直接在远程生成 "sync trading system:" 提交, 次日
#   08:30 rebase 后 stash pop 与远程改动在相同文件上三方合并 → UU 冲突。
#   实录: 8/26、8/27(daily_digest_email.py), 9/5(residual_live_state.json);
#   9/4 残差双轨上线后 residual_live_state.json 每天被同步上传 → 冲突成必然。
#
# v2 原理: 全程不用 stash。
#   1) 工作区全部改动(含未跟踪)做成一个"临时快照提交";
#   2) git rebase -X theirs 到远程 — 内容冲突一律取本地: 远程 sync 内容本源自
#      本机(旧副本), 本地工作区永远是最新真值;
#   3) git reset --mixed HEAD~1 解包: 快照还原为未提交改动, 工作区内容不变;
#   4) 只提交 pred 相关文件并 push(与 v1 相同)。
#   任何一步失败都把仓库完整还原到运行前状态, 不再产生 UU 残留。
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

alert() {
  /usr/bin/python3 - "$1" >> "$LOG" 2>&1 <<'PYEOF' || true
import sys
from alert_monitor import send_email
send_email('公证失败-需人工关注', sys.argv[1])
PYEOF
}

# 1) fetch 远程(Contents API 可能已写入新 commit)
git fetch "$REMOTE" "$BRANCH" >> "$LOG" 2>&1

# 2) 全量快照提交(替代 v1 的 stash, 含未跟踪文件)
SNAP=0
git add -A >> "$LOG" 2>&1
if ! git diff --cached --quiet; then
  git commit -m "tmp: notarize pre-snapshot (auto unwrap)" >> "$LOG" 2>&1
  SNAP=1
fi

# 3) rebase 到远程最新; 内容冲突 -X theirs 一律取本地(最新真值)
if ! git rebase -X theirs "$REMOTE/$BRANCH" >> "$LOG" 2>&1; then
  git rebase --abort >> "$LOG" 2>&1 || true
  if [ "$SNAP" -eq 1 ]; then
    git reset --mixed HEAD~1 >> "$LOG" 2>&1
  fi
  echo "$(date +%F-%T) ERROR: rebase 失败(已完整回滚, 工作区无残留), 放弃推送" >> "$LOG"
  alert "notarize rebase 失败已回滚, 今日公证未完成, 请检查 logs/notarize.log"
  exit 1
fi

# 4) 解包: 快照提交还原为未提交改动(工作区文件内容不变)
if [ "$SNAP" -eq 1 ]; then
  git reset --mixed HEAD~1 >> "$LOG" 2>&1
fi

# 5) 只提交预测相关文件
git add data/pred_*.json data/daily_predictions.json \
        data/universe/ data/forward_ic_history.json \
        data/forward_ic_history_48h.json >> "$LOG" 2>&1
if ! git diff --cached --quiet; then
  git commit -m "pred: $(date +%F) 每日预测公证(预测先于结果)" >> "$LOG" 2>&1
fi

# 6) push
if git push "$REMOTE" "HEAD:$BRANCH" >> "$LOG" 2>&1; then
  echo "$(date +%F-%T) === notarize done (push OK) ===" >> "$LOG"
else
  echo "$(date +%F-%T) ERROR: push 失败(公证提交保留在本地, 无 UU 残留)" >> "$LOG"
  alert "notarize push 失败, 公证提交保留在本地待手动 push, 请检查 logs/notarize.log"
  exit 1
fi
