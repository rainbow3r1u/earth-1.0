# 外部系统文件 (不在本仓库根目录下)

| 文件 | 用途 | Git追踪方案 |
|------|------|------------|
| `../backtester/config/current_params.json` | 策略参数 | **已入库**: 正本=`deploy/current_params.json`, 此路径为软链(7/30起, 见DEPLOY.md第5步) |
| `../backtester/data_cache/` | K线/OI/费率缓存 | COS 拉取 (bootstrap_from_cos.py) |
| `../stablecoin_data/monitor.py` | 稳定币采集 | 独立目录, 数据经COS同步 |
| `../openclaw-.../etf_data/fetch_etf.py` | ETF采集(已禁用) | OpenClaw 管理 |
| `~/.local/share/auto_trade/` | 模型/状态/日志 | cron 产出, 不入库; 模型每日自动备份COS |

## 恢复流程
```bash
# 策略参数: 建仓即恢复(软链)
ln -sf /home/myuser/websocket_new/deploy/current_params.json /home/myuser/backtester/config/current_params.json
# 数据: COS 拉取
python3 deploy/bootstrap_from_cos.py
```
