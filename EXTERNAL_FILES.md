# 外部系统文件 (不在本仓库根目录下)

这些文件通过 git 的 `--work-tree` 或手动备份管理：

| 文件 | 用途 | Git追踪方案 |
|------|------|------------|
| `../backtester/config/current_params.json` | 策略参数 | 手动备份到 `config/` 下 |
| `../stablecoin_data/monitor.py` | 稳定币采集 | 已有独立目录 |
| `../openclaw-.../etf_data/fetch_etf.py` | ETF采集 | OpenClaw 管理 |
| `../gpu_mcp_proxy.py` | GPU代理 | 本目录有 `gpu_mcp.py` 正本 |
| `../backtester/cos_service/oi_collector.py` | OI采集 | screen 直接运行 |
| `~/.local/share/auto_trade/` | 模型/状态/日志 | cron 产出，不入库 |

## 恢复流程
```bash
# 从备份恢复策略参数
cp config/current_params.json ../backtester/config/
```
