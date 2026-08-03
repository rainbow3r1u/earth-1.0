# AGENTS.md — 系统接手总入口(For Any AI, 不依赖对话上下文)

> 最后更新: 2026-08-03(幽灵问题已修复) | 适用目录: `/home/myuser/websocket_new/`(earth-1.0 仓库)
> 本文件是接手本系统的**第一份必读文档**。读完本文件后按「接手顺序」逐份阅读即可独立工作。

---

## 0. 系统一句话

**地球版量化交易系统**:XGBoost 双模型(LONG/SHORT)每日全量重训,基于 946 维特征(主传感器=对BTC残差)预测币安 U-M 合约 2 日涨跌,aligned 时序(预测日=入场日),止盈+10%/止损-5%/48h 退出。三端架构:生产端(本机,每日训练+预测+公证)/ GPU 回溯端(175.155.64.171,回测实验)/ 观察端(已下线)。

**当前状态(2026-08-03)**:完全体·前向观察期(7/30~8/6 原窗口, 含 7/29~8/2 脏数据期),`TRADING_ENABLED=false`(只预测不开仓)。**前向评审改期: 8/3(修复日)起 + 7 天 = 8/10 评审**(7/29~8/2 为错位模型脏数据期, 单独标注不纳入)。**幽灵问题已修复(8/3)**:`_fast_winsor_bounds` 的 `col.partition()` 原地重排 X_train 每列(6/12 引入,生产一直在错位数据上训练)→ 已修复并验证(生产 prob 恢复 90%+ 级)。

## 1. 知识权威源(按优先级)

| 源 | 路径 | 内容 |
|---|---|---|
| **Obsidian 知识库** | `/home/myuser/Sync/rainbow/` — git: `github.com/rainbow3r1u/rainbow-vault`(私有, 2026-08-02 入版本管理) | 权威: 系统/ 7篇(总览/参数卡/特征工程/标签规则/数据管道/部署运维/版本历史) + 研究/ 14篇(审计/实验/调查) + 回溯日志/ |
| **调查文档(8/3, 必读)** | `Sync/rainbow/研究/幽灵问题-生产训练特征矩阵列错位.md` | 幽灵问题(行错位)全链路调查:根因/修复/验证/回测=生产一致性实验 + 接手指引 |
| **8/2 调查文档** | `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md` | 前向4连止损→回测生产矩阵差异调查(已被幽灵文档取代,保留作背景) |
| **回测日志** | `Sync/rainbow/回溯日志/001_946D_180d_GPU.md` | 180d 真实水位 17.29(修复版待重跑复核) |
| 代码仓库文档 | 本仓库 `SYSTEM_OVERVIEW.md` / `DEPLOY.md` / `docs/gpu_server_connection.md` | 全景/部署/GPU(注意: SYSTEM_OVERVIEW 停在 7/18 地球版 1.1, 现状以 Obsidian 为准) |
| 背景记忆 | Reasonix memory(project scope, 仅本机 Reasonix 实例可见) | 前向评估口径 v2 / npz 偏差 / 矩阵差异 / GPU 连接 / 铁律 |

## 2. 当前状态快照(2026-08-03)

- 前向观察期 **8/3~8/10**(修复后 7 天, 评审 8/10): 修复前(7/29~8/2)4 连止损(-20%)为错位模型产物, 单独标注不纳入评审; **修复后首日(8/3): SHORT HOMEUSDT 93.1% → 止盈+10% 命中(8/3 11:36 北京), LONG BROCCOLIF3BUSDT 80.7% 持仓中(8/5 08:21 到期)**
- 回测 180d 真实水位(数据全同步+生产同源构建): **Sharpe 17.29 / +1050% / MaxDD 15.6% / 胜率 76%**(prob 全 90~100% 饱和是干净模型固有分布, 非 bug)
- **幽灵已修复(8/3)**: 生产训练矩阵行错位根因 = `_fast_winsor_bounds` 的 `col.partition()` 原地重排; 修复后 npz 干净、生产 prob 与回测一致(90%+)
- **回测=生产逐日一致性已验证**(GPU 重放): 7/28/7/30 完全同币, 7/29 差 0.1pp 浮点噪声
- 生产钱包 0u; TRADING_ENABLED=false; 每日训练/预测/公证/晨报自动运行
- 错位模型副本(7/31~8/2)已隔离至 `/tmp/poison_models/`; SOUP 自 8/3 起只用干净模型

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

1. ~~**生产 npz 特征偏差**(8/1 起)~~ → **已修复 2026-08-03**: 根因 `_fast_winsor_bounds` 的 `col.partition()` 原地重排(commit 3ef51c5 修复); 详见幽灵文档。**遗留动作**: ① 8/4 08:05 验证 [SAMPLECHK]=构建真值 + prob 90%+; ② 用修复版重跑 180d 回测复核水位(17.29 → 预期 ~17~18); ③ 错位模型副本 7 天后可删(保留作审计)
2. **verify_yesterday 早盘快照偏置**: 邮件/tracker 收益数字乐观偏置, 结算需改完整 K 线(生产文件, 走铁律 1)
3. **前向 4 连止损(7/29~8/1)**: 错位模型的输出, 不代表修复后表现; 修复后(8/3 起)重新前向观察, **评审 8/10**(8/3+7天); 7/29~8/2 脏数据期单独标注
4. **SOUP 历史模型边界**: 8/3 已隔离 7/31~8/2 错位模型副本; 若从旧备份恢复模型目录, 需先甄别错位模型
5. SYSTEM_OVERVIEW.md 内容滞后(7/18 版), 现状以 Obsidian 为准
6. GPU SSH 端口已变更为 **24090**(旧 22160/22183/22156 全失效)

## 8. 常用操作

- 看系统健康: `crontab -l` + `tail logs/auto_dual.log` + `~/.local/share/auto_trade/trade.log`
- 手动触发训练: `cd /home/myuser/websocket_new && python3 auto_dual_trade.py`(会拿锁, 勿重复跑)
- GPU 回测: `ssh -p 24090 linux@175.155.64.171` → `cd ~/websocket_new && env NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=0 SL_PCT=5 WINSOR_Q=0.001 python3 gpu_backtest_exp.py 180 1`
- 生产=回测一致性重放校验: GPU 上 `python3 gpu_replay_prod.py`(见仓库, 对比当日 pred 存档)
- 审计日志: `tail -20 /home/myuser/websocket_new/logs/audit.log`
- 数据同步生产→GPU: `rsync -az --partial -e "sshpass -p '<密码>' ssh -p 24090" /home/myuser/backtester/data_cache/{notusdt_1d_full.json,oi_daily.json,funding_hist.json} linux@175.155.64.171:~/backtester/data_cache/` + 外部数据目录(见 DEPLOY.md §9)
