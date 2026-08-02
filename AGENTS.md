# AGENTS.md — 系统接手总入口(For Any AI, 不依赖对话上下文)

> 最后更新: 2026-08-02 | 适用目录: `/home/myuser/websocket_new/`(earth-1.0 仓库)
> 本文件是接手本系统的**第一份必读文档**。读完本文件后按「接手顺序」逐份阅读即可独立工作。

---

## 0. 系统一句话

**地球版量化交易系统**:XGBoost 双模型(LONG/SHORT)每日全量重训,基于 946 维特征(主传感器=对BTC残差)预测币安 U-M 合约 2 日涨跌,aligned 时序(预测日=入场日),止盈+10%/止损-5%/48h 退出。三端架构:生产端(本机,每日训练+预测+公证)/ GPU 回溯端(175.155.64.171,回测实验)/ 观察端(已下线)。

**当前状态(2026-08-02)**:完全体·前向观察期(7/30~8/6),`TRADING_ENABLED=false`(只预测不开仓),8/6 评审定版「地球版 1.9」。**研究中:生产训练数据特征偏差(见「已知问题」)。**

## 1. 知识权威源(按优先级)

| 源 | 路径 | 内容 |
|---|---|---|
| **Obsidian 知识库** | `/home/myuser/Sync/rainbow/` — git: `github.com/rainbow3r1u/rainbow-vault`(私有, 2026-08-02 入版本管理) | 权威: 系统/ 7篇(总览/参数卡/特征工程/标签规则/数据管道/部署运维/版本历史) + 研究/ 14篇(审计/实验/调查) + 回溯日志/ |
| **调查文档(8/2, 必读)** | `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md` | 前向4连止损→生产npz特征偏差全链路调查 + 接手指引 |
| **回测日志** | `Sync/rainbow/回溯日志/001_946D_180d_GPU.md` | 180d 真实水位 17.29 |
| 代码仓库文档 | 本仓库 `SYSTEM_OVERVIEW.md` / `DEPLOY.md` / `docs/gpu_server_connection.md` | 全景/部署/GPU(注意: SYSTEM_OVERVIEW 停在 7/18 地球版 1.1, 现状以 Obsidian 为准) |
| 背景记忆 | Reasonix memory(project scope, 仅本机 Reasonix 实例可见) | 前向评估口径 v2 / npz 偏差 / 矩阵差异 / GPU 连接 / 铁律 |

## 2. 当前状态快照(2026-08-02)

- 前向观察期 7/30~8/6: TOP1 口径 4 连止损(-20%, 修正口径); Top10 口径 LONG +2.6% / SHORT -20%(1m 修正结算)
- 回测 180d 真实水位(数据全同步+生产同源构建): **Sharpe 17.29 / +1050% / MaxDD 15.6% / 胜率 76%**(prob 全 90~100% 饱和是回测自训模型固有分布, 非 bug)
- **市场存在可统计 alpha 已获实证**(脏/净两次回测几乎一致); 生产能否拿到待偏差修复+前向验证
- 生产钱包 0u; TRADING_ENABLED=false; 每日训练/预测/公证/晨报自动运行

## 3. 关键文件地图

| 文件 | 用途 | 分类 |
|---|---|---|
| `auto_dual_trade.py` | 主交易: 特征构建/训练/预测/执行/日报 | 🔴 核心生产 |
| `daily_predictor.py` | 特征工程库(宏观/RSI/回归/2日验证 verify_yesterday) | 🔴 核心生产 |
| `utils/feature_builder.py` | 特征向量组装(946 维) | 🔴 核心生产 |
| `guardian.py` / `daily_data_collection.py` | 进程守护 / 数据采集 | 🔴 核心生产 |
| `gpu_backtest_exp.py` | GPU 回测引擎(样本构建已改 adt 同源) | 🟢 实验 |
| `audit/{audit_snapshot,audit_verify}.py` | 生产训练审计(8/2 挂) | 🟢 实验 |
| `~/.local/share/auto_trade/train_data_latest.npz` | 生产训练数据(疑似有特征偏差) | 数据 |

## 4. 每日流水线(cron, 北京时间)

```
每分钟  guardian.py 守护
6:00   daily_data_collection.py 采集
7:30   K线+OI 补采
8:04   audit_snapshot.py 审计快照(只读) ← 8/2 新增
8:05   auto_dual_trade.py 训练+预测(+交易, 当前关闭)
8:20   预测公证(git push GitHub, 预测先于结果)
8:25   audit_verify.py 审计校验(只读) ← 8/2 新增
8:40   cron_monitor 失败重试
9:00   晨报(注意: 2日验证结算为早盘快照口径, 收益偏乐观, 不可信!)
```

## 5. 铁律(必须遵守)

1. **生产改动门槛**: 任何 🔴 生产文件改动必须先 180d 回测对比(Before vs After: Sharpe/收益/回撤/胜率)+ 用户审核同意
2. **不再优化模型本身**(2026-08-01 起): 任何新特征/参数变更须先过前向数据; 前向数据是最终裁判
3. **git 提交必须经用户明确同意**: 禁止自行 commit/push
4. **回测绝对水位不可作为生产预期**: 回测只用于管道内 A/B 相对排序
5. **前向结算口径 v2**: 8:21 市价入场 + 1m 粒度 + 成交即盯盘(先查止损后止盈, 48h 到期); 不豁免入场日

## 6. 接手顺序(建议)

1. 读本文件 + `Sync/rainbow/首页.md` + `Sync/rainbow/系统/地球版-总览.md`
2. 读 `Sync/rainbow/系统/参数卡.md`(完全体配置)+ `系统/标签与交易规则.md`(aligned 时序)
3. 读调查文档第八节(接手指引)→ 处理「已知问题」
4. 熟悉代码: `auto_dual_trade.py` 主流程(main → 训练 → SOUP → perm test → 交易), `gpu_backtest_exp.py` 回测流程

## 7. 已知问题(接手时开放)

1. **生产 npz 特征偏差**(8/1 起): `train_data_latest.npz` 训练特征与代码重建不一致(0GUSDT 列0: 理论 -1.9970 vs npz -0.2601); 审计 cron 已挂, **8/3 08:25 后看 `logs/audit.log` 判定**(MATCH_A=内存旧版K线实锤 / MATCH_B / NO_MATCH), 按调查文档第八节决策树处理
2. **verify_yesterday 早盘快照偏置**: 邮件/tracker 收益数字乐观偏置, 结算需改完整 K 线(生产文件, 走铁律 1)
3. **前向 TOP1 4 连止损**: 是偏差特征模型的输出, 不代表修复后表现; 修复后需重新前向观察
4. SYSTEM_OVERVIEW.md 内容滞后(7/18 版), 现状以 Obsidian 为准

## 8. 常用操作

- 看系统健康: `crontab -l` + `tail logs/auto_dual.log` + `~/.local/share/auto_trade/trade.log`
- 手动触发训练: `cd /home/myuser/websocket_new && python3 auto_dual_trade.py`(会拿锁, 勿重复跑)
- GPU 回测: `ssh -p 22160 linux@175.155.64.171` → `cd ~/websocket_new && env NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=0 SL_PCT=5 WINSOR_Q=0.001 python3 gpu_backtest_exp.py 180 1`
- 审计判定: `tail -20 /home/myuser/websocket_new/logs/audit.log`
