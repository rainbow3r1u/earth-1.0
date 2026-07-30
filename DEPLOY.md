# 新机器部署指南 (DEPLOY) — 地球版完全体

加密货币 ML 自动交易系统(**地球版完全体**: XGBoost 946维 / aligned 标签 / 180天训练窗口 / winsor 0.1%-99.9% 温和截尾 / 无动量闸门 / SOUP 时间集成)完整部署流程。
数据全部托管在腾讯云 COS,新机器无需手动拷贝任何数据目录。

> 系统全景与运维手册见 `SYSTEM_OVERVIEW.md`;COS 数据路径清单见 `deploy/cos_paths.json`;实验台账见 Obsidian vault(不入库)。
> 当前生产配置: `TRADING_ENABLED=false`(只训练预测不发单, 观察期中)。要开真实交易再改 `deploy/current_params.json`。

---

## 0. 前置要求

| 项 | 要求 |
|----|------|
| 操作系统 | Linux(Ubuntu 验证过) |
| 用户 | **必须是 `myuser`**(脚本硬编码 `/home/myuser` 路径;换用户名需全局 sed) |
| Python | 3.10+(用 /usr/bin/python3) |
| GPU | **不需要**(CPU 训练,4 核足够,每日 ~15 分钟) |
| 内存 | ≥4GB(训练峰值 ~2GB) |
| 密钥 | 币安 API Key/Secret + 腾讯云 COS SecretId/SecretKey + QQ邮箱SMTP授权码 |

---

## 1. 克隆代码

```bash
git clone https://github.com/rainbow3r1u/earth-1.0.git /home/myuser/websocket_new
cd /home/myuser/websocket_new && mkdir -p logs
```

## 2. 配置 .env

```bash
cp .env.example .env   # 然后编辑填入真实密钥
```

必填(系统核心):

```ini
# 腾讯云COS(数据拉取/模型备份都靠它)
COS_SECRET_ID=...
COS_SECRET_KEY=...
COS_REGION=ap-seoul
COS_BUCKET=lhsj-1h-1314017643
COS_ENDPOINT=cos.ap-seoul.myqcloud.com

# 币安(开仓需要交易权限;只跑训练预测可给只读Key)
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...

# QQ邮箱SMTP(晨报/健康/告警邮件; 授权码非登录密码)
SMTP_USER=...@qq.com
SMTP_AUTH_CODE=...
```

可选(网站功能):`FEISHU_*`、`DEEPSEEK_*`、`WEB_HOST/WEB_PORT`。

## 3. 拉取数据(~1700 个文件 + 费率历史,几分钟)

```bash
python3 deploy/bootstrap_from_cos.py            # 正式拉取
python3 deploy/bootstrap_from_cos.py --dry-run  # 先预览不落盘
```

拉取内容:K线缓存(532币全量日线,含 n/tbq 修复版)、OI 缓存、**资金费率历史 funding_hist.json(fund_raw 特征源, ~29MB)**、情绪数据小时文件、板块标签、稳定币/宏观/TVL/算力/恐慌贪婪、链上 CSV 种子。

> 之后每天由 cron 采集器自行累积更新,无需再跑本脚本。

## 4. 安装依赖

```bash
pip3 install -r requirements.txt
# xgboost 需 ≥3.0 (tree_method='hist'); 若 requirements 未含: pip3 install xgboost
```

## 5. 生产配置软链(关键一步)

策略参数文件在仓库 `deploy/current_params.json`,运行路径在仓外,用软链对接:

```bash
mkdir -p /home/myuser/backtester/config /home/myuser/backtester/data_cache
ln -sf /home/myuser/websocket_new/deploy/current_params.json /home/myuser/backtester/config/current_params.json
```

`_live_trading` 当前值(完全体):

```json
"LONG_MOM_FILTER": false,   // 动量闸门已摘除(7/30证据: 闸门选的是止损画像币)
"SOUP_ON": true,            // 时间集成: 今日+最近2日模型概率平均
"TRADING_ENABLED": false,   // 只预测不开仓(观察期); 开真实交易改 true
"STOP_LOSS_PCT": 5.0, "LEVERAGE": 10, "PROB_THRESHOLD": 60.0, "TRAIN_DAYS": 180
```

> SOUP 说明: 部署后第1天只有1个模型(日志"SOUP时间集成: 1个模型"属正常),第3天起满编3个模型平均。

## 6. 配置 cron

`crontab -e` 写入(生产端当前完整调度):

```cron
# guardian进程守护(每分钟: 进程存活+文件新鲜度+自动重启)
* * * * * cd /home/myuser/websocket_new && /usr/bin/python3 guardian.py >> /tmp/guardian.log 2>&1
# 每日数据采集 6:00
0 6 * * * cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 daily_data_collection.py >> /home/myuser/websocket_new/logs/collect.log 2>&1
# K线+OI 补采 7:30(冗余)
30 7 * * * cd /home/myuser/websocket_new && /usr/bin/python3 -c "import daily_data_collection as ddc; ddc.update_klines_oi()" >> /home/myuser/websocket_new/logs/collect.log 2>&1
# 自动交易 8:05(训练→预测→[开仓])
5 8 * * * cd /home/myuser/websocket_new && PYTHONUNBUFFERED=1 /usr/bin/python3 auto_dual_trade.py >> /home/myuser/websocket_new/logs/auto_dual.log 2>&1
# 预测公证 8:20(预测先于结果, GitHub时间戳)
20 8 * * * cd /home/myuser/websocket_new && git add data/pred_*.json data/daily_predictions.json && git commit -m "pred: $(date +\%F) 每日预测公证" >> logs/notarize.log 2>&1 && git push xgboot HEAD:main >> logs/notarize.log 2>&1
# 交易失败自动重试 8:40(确认失败才重试一次)
40 8 * * * cd /home/myuser/websocket_new && /usr/bin/python3 cron_monitor.py --task 交易预测 >> /home/myuser/websocket_new/logs/cron_monitor.log 2>&1
# 晨报总览 9:00(交易摘要+2日验证+强势股资金榜+健康, 一天只此一封)
0 9 * * * cd /home/myuser/websocket_new && /usr/bin/python3 daily_digest_email.py >> /home/myuser/websocket_new/logs/digest.log 2>&1
```

## 7. 验证部署

```bash
# 1) guardian 拉起常驻服务(等1分钟看状态)
python3 guardian.py && cat /tmp/guardian_status.json | python3 -m json.tool | head -20

# 2) 手动跑一次采集(确认各数据源成功)
cd /home/myuser/websocket_new && python3 daily_data_collection.py 2>&1 | tail -15

# 3) 确认特征构建正常(946维)
python3 - << 'EOF'
import os, sys, json
sys.path.insert(0, '/home/myuser/websocket_new'); os.chdir('/home/myuser/websocket_new')
import auto_dual_trade as adt, daily_predictor as dp
cache = json.load(open(adt.KLINE_CACHE_FILE))['klines']
res = adt._build_feat_impl('BTCUSDT', cache['BTCUSDT'], {}, dp._compute_returns([k['c'] for k in cache['BTCUSDT']]), {}, {})
print('样本数:', len(res), '维度:', len(res[-1][2]))   # 预期维度 946
EOF
```

## 8. 首跑验证(次日 8:05 后)

```bash
# 训练日志: 应见 "特征维度验证: 946 == 946 OK"、"SOUP时间集成: LONG 1个模型"
grep "$(date +%F)" ~/.local/share/auto_trade/trade.log | grep -E "训练:|SOUP|PERM-TEST|特征维度"

# 预测存档: 含 all_long/all_short 全量概率
python3 -c "import json; d=json.load(open('/home/myuser/websocket_new/data/daily_predictions.json')); print(d['date'], len(d.get('all_long',[])))"
```

---

## 注意事项

- **首次交易运行**: cron 每天 8:05 自动跑。`TRADING_ENABLED=false` 时只训练预测不发单;改 true 才开仓。
- **止损/止盈**: 系统开仓后挂 Algo 条件单;遇 `-4130`(同方向已有 closePosition 单)会记录裸仓并告警,需人工补挂。
- **邮件接收**: 晨报发到 alert_monitor.py 里配置的收件箱;发件 SMTP 在 `.env`。
- **大文件**: 仓库含 `obscura`/`obscura-worker`(~75MB 二进制,GitHub 警告但可正常 clone)。
- **观察端已下线(7/29)**: 现为单端运行,无多机同步问题;代码以 GitHub 为准,数据以 COS 为准。
- **GPU 实验**: 回测框架 `gpu_backtest_exp.py`(环境变量开关见文件头注释),需另租 GPU 机;生产端不需要。
