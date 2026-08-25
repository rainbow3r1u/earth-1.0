---
name: obsidian-github-sync
description: |
  每周自动扫描本服务器 Obsidian 和系统维护文档，用 MD5 对比检测变化，
  有差异就自动上传 GitHub（Obsidian -> rainbow-vault；系统文档/主线 -> trading-system-docs-backup），
  并重新打包 tar.gz 备份上传。当用户提到 Obsidian 同步、文档备份、GitHub 推送、MD5 差异时使用。
---

# Obsidian / 文档自动同步 GitHub Skill

## 一句话

> 每周扫描本地 Obsidian 与系统文档，MD5 变化就自动推 GitHub，保证云端备份始终跟上本地。

## 脚本

- 路径：`/home/myuser/websocket_new/scripts/obsidian_github_sync.py`
- 日志：`/home/myuser/logs/obsidian_github_sync.log`
- 状态文件：`/home/myuser/.cache/obsidian_github_sync/`

## 同步目标

| 内容 | 目标 GitHub 仓库 |
|------|------------------|
| Obsidian 库（`~/Sync/rainbow`，排除 `.git/.obsidian`） | `rainbow3r1u/rainbow-vault` |
| 系统维护文档、AGENTS/DEPLOY/SYSTEM_OVERVIEW/EXTERNAL_FILES、主线地图 | `rainbow3r1u/trading-system-docs-backup` |
| 重新打包的 tar.gz 备份 | `rainbow3r1u/trading-system-docs-backup` |

## 工作机制

1. 扫描本地文件并计算 MD5；
2. 与上次运行保存的 manifest 对比；
3. 新增/变化的文件通过 GitHub Contents API 上传；
4. 本地已删除的文件从 GitHub 删除；
5. 更新本地 manifest；
6. 重新打包 `交易系统文档备份_YYYYMMDD.tar.gz` 并上传 backup 仓库。


## 首次运行

首次运行没有历史 manifest，脚本会把当前所有文件视为新增并全量上传；之后每周只上传 MD5 变化的文件。

## 手动运行

```bash
# 试运行（只显示将变更，不实际上传）
python3 /home/myuser/websocket_new/scripts/obsidian_github_sync.py --dry-run

# 正式同步
python3 /home/myuser/websocket_new/scripts/obsidian_github_sync.py
```

## Cron

已配置每周日凌晨 03:00 运行：

```cron
0 3 * * 0 /usr/bin/python3 /home/myuser/websocket_new/scripts/obsidian_github_sync.py >> /home/myuser/logs/obsidian_github_sync.log 2>&1
```

## 注意

- 脚本使用 GitHub API 上传，不依赖 git push，避免凭据/网络问题。
- 上传前请用 `--dry-run` 确认差异。
- 若 GitHub token 失效，需要更新 `~/.config/gh/hosts.yml` 或重新登录 gh。
