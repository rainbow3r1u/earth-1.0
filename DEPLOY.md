# 新机器部署指南 (DEPLOY) — 地球版 1.0

加密货币 ML 自动交易系统（**地球版 1.0**: XGBoost 944维 / aligned 标签 / 180天训练窗口）完整部署流程。
数据全部托管在腾讯云 COS，新机器无需手动拷贝任何数据目录。

> 系统全景与运维手册见 `SYSTEM_OVERVIEW.md`；COS 数据路径清单见 `deploy/cos_paths.json`。

---

## 0. 前置要求

| 项 | 要求 |
|----|------|
| 操作系统 | Linux（生产/观察端均为 Ubuntu） |
| 用户 | **必须是 `myuser`**（29 个脚本硬编码 `/home/myuser` 路径；换用户名需全局 sed） |
| Python | 3.10+（生产端用 /usr/bin/python3） |
| GPU | **不需要**（生产端 CPU 训练，4 核足够，每日 ~15 分钟） |
| 密钥 | 币安 API Key/Secret + 腾讯云 COS SecretId/SecretKey |

---

## 1. 克隆代码

```bash
git clone https://github.com/rainbow3r1u/earth-1.0.git /home/myuser/websocket_new
cd /home/myuser/websocket_new && mkdir -p logs
```

## 2. 配置 .env

```bash
cp .env.example .env
```

必填（交易系统核心）：

```ini
# 腾讯云COS（数据拉取/备份都靠它）
COS_SECRET_ID=...
COS_SECRET_KEY=...
COS_REGION=ap-seoul
COS_BUCKET=lhsj-1h-1314017643
COS_ENDPOINT=cos.ap-seoul.myqcloud.com

# 币安（开仓需要交易权限；只跑训练验证可给只读Key）
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
```

可选（网站功能）：`FEISHU_*`（告警推送）、`DEEPSEEK_*`（新闻页 AI 聊天）、`WEB_HOST/WEB_PORT`。

## 3. 拉取数据（~1700 个文件，几分钟）

```bash
python3 deploy/bootstrap_from_cos.py            # 正式拉取
python3 deploy/bootstrap_from_cos.py --dry-run  # 先预览不落盘
```

拉取内容：K线缓存（532币全量，含 n/tbq 修复版）、OI 缓存、情绪数据 1682 个小时文件（含 25 天回填）、板块标签（含 BSC/ARB 手工标签）、稳定币/宏观/TVL/算力/恐慌贪婪、策略参数与链上 CSV 种子快照。

> 之后的每天，数据由 cron 采集器自行累积更新，无需再跑本脚本。

## 4. 安装依赖

```bash
pip3 install -r requirements.txt
```

## 5. 配置 cron

`crontab -e` 写入（生产端完整调度）：

```cron
# guardian进程守护（每分钟: 进程存活+文件新鲜度+自动重启）
* * * * * cd /home/myuser/websocket_new && /usr/bin/python3 guardian.py >> /tmp/guardian.log 2>&1
# 每日数据采集 6:00
0 6 * * * cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 daily_data_collection.py >> /home/myuser/websocket_new/logs/collect.log 2>&1
# K线+OI 补采 7:30（冗余，确保交易前数据最新）
30 7 * * * cd /home/myuser/websocket_new && /usr/bin/python3 -c "import daily_data_collection as ddc; ddc.update_klines_oi()" >> /home/myuser/websocket_new/logs/collect.log 2>&1
# 自动交易 8:05（训练→预测→开仓）
5 8 * * * cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 auto_dual_trade.py >> /home/myuser/websocket_new/logs/auto_dual.log 2>&1
# 健康检查 8:30 / 健康报告 9:00（邮件）
30 8 * * * /usr/bin/python3 /home/myuser/websocket_new/daily_health_check.py >> /home/myuser/websocket_new/logs/health_check.log 2>&1
0 9 * * * cd /home/myuser/websocket_new && /usr/bin/python3 alert_monitor.py --report >> /home/myuser/websocket_new/logs/alert.log 2>&1
```

观察端（只训练不交易）只需保留：guardian、6:00 采集、7:30 补采、8:05 auto_dual_trade（余额不足会自动跳过交易）、8:30 健康检查。

## 6. 验证部署

```bash
# 1) guardian 拉起常驻服务（等1分钟后看状态）
python3 guardian.py && cat /tmp/guardian_status.json | python3 -m json.tool

# 2) 手动跑一次采集（确认9项数据全部成功）
cd /home/myuser/websocket_new && python3 daily_data_collection.py 2>&1 | tail -15

# 3) 确认特征构建正常（944维）
python3 - << 'EOF'
import os, sys, json
sys.path.insert(0, '/home/myuser/websocket_new'); os.chdir('/home/myuser/websocket_new')
import auto_dual_trade as adt, daily_predictor as dp
cache = json.load(open(adt.KLINE_CACHE_FILE))['klines']
res = adt._build_feat_impl('BTCUSDT', cache['BTCUSDT'], {}, dp._compute_returns([k['c'] for k in cache['BTCUSDT']]), {}, {})
print('样本数:', len(res), '维度:', len(res[-1][2]))
EOF
```

预期：guardian 15 项服务全绿；采集 "9成功"；特征维度 944。

---

## 注意事项

- **首次交易运行**：cron 每天 8:05 自动跑。不要手动提前跑 `auto_dual_trade.py` 开仓，先确认数据新鲜度（`logs/collect.log` 全绿）。
- **止损/止盈**：系统开仓后挂 Algo 条件单，若遇 `-4130`（同方向已有 closePosition 单）会记录裸仓并日志告警，需在币安手动补挂（当前运维模式：人工管理止损）。
- **邮箱接收**：日报/健康检查/过拟合告警发到 `305488483@qq.com`（在 alert_monitor.py / daily_health_check.py 里配置）。
- **大文件**：仓库含 `obscura`/`obscura-worker`（~75MB 二进制，GitHub 警告但可正常 clone）。
- **多机一致性**：以生产端为准——代码用 git 同步，数据用 COS 同步，不要手工改副本上的脚本。
