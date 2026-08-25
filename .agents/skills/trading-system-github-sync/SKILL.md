---
name: trading-system-github-sync
description: |
  每周自动扫描交易系统代码/配置/文档（websocket_new），用 MD5 对比检测变化，
  有差异就自动上传 GitHub（rainbow3r1u/earth-1.0）。用于代码备份、多机同步、
  GitHub 自动公证、防本地代码丢失。
---

# 交易系统自动同步 GitHub Skill

## 一句话

> 每周扫描 websocket_new 的关键代码/配置/文档，MD5 有变化就自动上传到 Earth-1.0 GitHub 仓库。

## 脚本

- 路径：`/home/myuser/websocket_new/scripts/trading_system_github_sync.py`
- 日志：`/home/myuser/logs/trading_system_github_sync.log`
- 状态：`/home/myuser/.cache/trading_system_github_sync/manifest.json`
- 目标仓库：`rainbow3r1u/earth-1.0`

## 包含内容

自动扫描并同步：

- `.py` 主脚本、采集器、监控、回测、审计等代码
- 配置/部署：`configs/`、`deploy/`、`requirements.txt`、`Dockerfile` 等
- `scripts/` 脚本、`audit/`、`core/`、`utils/` 等
- 文档：`AGENTS.md`、`DEPLOY.md`、`SYSTEM_OVERVIEW.md`、`EXTERNAL_FILES.md`、`docs/`
- Shell 脚本、cron 相关脚本

## 排除内容

- `.git`、`.env` 及密钥文件
- `data/`、`logs/`、`__pycache__`、缓存/数据库/二进制
- `*.bak*`、临时备份、大文件
- `archive/`、`kronos_finetune/`、`kronos_model/`、`experiments/` 等非核心或大目录

## 工作机制

1. 扫描选定文件并计算 MD5；
2. 和上次 manifest 比较；
3. 新增/变化文件用 GitHub Contents API 上传；
4. 本地删除的文件从 GitHub 删除；
5. 更新 manifest。

首次运行没有 manifest，会全量上传；之后只传变化。

## 手动运行

```bash
# 试运行
python3 /home/myuser/websocket_new/scripts/trading_system_github_sync.py --dry-run

# 正式同步
python3 /home/myuser/websocket_new/scripts/trading_system_github_sync.py
```

## Cron

每天 08:50 自动运行（结果写入 9:00 晨报邮件）：

```cron
50 8 * * * /usr/bin/python3 /home/myuser/websocket_new/scripts/trading_system_github_sync.py >> /home/myuser/logs/trading_system_github_sync.log 2>&1
```

## 注意

- 使用 GitHub API 上传，不依赖 git push，避免凭据/网络问题；
- 如果 GitHub token 失效，需要更新 `~/.config/gh/hosts.yml` 或重新登录 gh；
- 上传前建议先 `--dry-run`。
