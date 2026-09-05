# AGENTS.md — 系统接手总入口(For Any AI, 不依赖对话上下文)

> 最后更新: 2026-09-06(全面校准至当日实况: §0/§2快照·§3文件地图·§4流水线·§7问题清单; 公证v2+体检SKILL上线) | 适用目录: `/home/myuser/websocket_new/`(earth-1.0 仓库)
> 本文件是接手本系统的**第一份必读文档**。读完本文件后按「接手顺序」逐份阅读即可独立工作。

---

## 0.5 系统运作底层逻辑(宪法级, 2026-09-04 用户与AI对话确立)

> **本节是系统一切设计与优化的唯一最高指引。任何优化提案、参数变更、用户直觉, 都先回到这里对照: 该改动是强化了哪一条, 还是破坏了哪一条? 两者冲突时以本节为准。**

### 一句话: 本系统 = 在正期望条件下, 用足够数量和存活能力, 等/接住肥右尾

### 四条底层公理

**公理1: 正期望来自赔率不对称, 不来自胜率。**
实证(33天影子臂313笔LONG): 65%的单止损(每笔-15.5U, 合计-3142U) vs 35%的单到期(均值+53.7U, 合计+5905U) → E=+8.8U/笔。**大部分单会"死", 系统靠"活着的单平均赚死掉的单3.5倍"盈利。** 到期单均值(+53.7)远大于中位(+17.4) = 右偏分布的数学签名。
推论: 任何提高胜率但压缩赔率的改动(如加止盈)都在破坏引擎 — SHORT TP10/SL5 是主动砍右尾的反例结构, memory已有教训(SHORT全裸-72%, 因空头MAE P90=52.7%)。

**公理2: 滚动/复利是放大器, 不产生期望。**
72h滚动只是资金回收器: 期望为正时滚动复利化, 期望为负时滚动加速亏。"每天开新批→平最老批"机制本身零期望, 期望唯一来源 = 选币alpha + 离场结构(公理1)。
推论: 不要把盈利归因于"滚动机制", 也不要试图靠"更勤换仓"救期望。

**公理3: 肥右尾只在"永不被迫离场"时才能兑现(孙正义卖英伟达教训)。**
2019年孙卖英伟达缺的不是眼光, 是流动性(WeWork巨亏被迫变现没长完的尾巴)。系统全部风控设计服务于同一件事 — **活到尾巴来**: 40U小仓/85%资金守卫/-62U承受线/逐仓隔离。
推论: 任何会引发"被迫批量平仓"的改动(加杠杆/放大单笔/撤资金守卫)都是宪法级违宪, 无论当时多赚钱。

**公理4: 正期望是经验统计, 不是数学公理 — 会被行情打断, 也会自愈。**
E=+8.8U/笔来自样本, 震荡期临时转负(8/28~9/3六连亏-701U)是方差本身, 不是引擎坏了; 转折税(起涨/冲顶首日亏损)结构性不可消除。模型自愈实测周期4~6天(两样本验证); 自愈失效条件 = 山寨翻转速度(1-2天) > 自愈速度。
推论: 判"模型失效"的唯一标准是结构证据(到期胜率<25%持续一周/IC_L周线持续负/趋势确认日LONG侧仍亏), 不是连亏天数本身。

### 三个条件缺一, 尾巴来了也接不住(优化系统时的检查清单)

1. **结构性赔率不对称**(公理1) — 改的是赔率还是胜率?
2. **数量**(大数定律, E只在渐近意义下实现) — 该改动是否无理由减少开仓数量?
3. **存活**(公理3) — 该改动是否增加被迫离场风险?

### 站位声明: 本系统 LONG 无TP = 买右尾; SHORT TP10/SL5 = 卖右尾(砍掉空头尾部敞口)
不同投资风格是选择站在尾部哪一侧(做市/卖期权=卖右尾赚"尾巴不出现"的钱)。本系统的选择已由数据锁定, 不要在对侧加仓。

---

## 0. 系统一句话

**地球版量化交易系统**:XGBoost 双模型(LONG/SHORT)每日全量重训,基于 946 维特征(主传感器=对BTC残差)预测币安 U-M 合约 2 日涨跌,aligned 时序(预测日=入场日),止盈+10%/止损-5%/48h 退出。三端架构:生产端(本机,每日训练+预测+公证)/ GPU 回溯端(175.155.64.171,回测实验)/ 观察端(已下线)。

**当前状态(2026-09-06)**: 生产流水线完全体运行中(采集→训练→预测→公证→实盘→影子结算→晨报→体检, 全表见§4)。**交易分两层, 勿混淆**: ① 老双模型自动交易**关闭**(`backtester/config/current_params.json` 的 `_live_trading.TRADING_ENABLED=false` 覆盖代码默认值, 且本金<10u 跳过); ② **残差实盘 LONG 臂运行中**(`audit/residual_live.py`, 9/2 起正式批: 40U 名义/5x 逐仓/SL-5%/72h/每日≤10笔, 交易所真实挂 SL algo 单)。幽灵问题已修复(8/3, §7.1); 公证 stash-pop 冲突已根治(9/6, §7.10); 全链路体检 SKILL 上线(9/6, §4/§8)。

## 1. 知识权威源(按优先级)

| 源 | 路径 | 内容 |
|---|---|---|
| **Obsidian 知识库** | `/home/myuser/Sync/rainbow/` — git: `github.com/rainbow3r1u/rainbow-vault`(私有, 2026-08-02 入版本管理) | 权威: 系统/ 8篇(总览/参数卡/特征工程/标签规则/数据管道/部署运维/版本历史/Rust48h接手指引) + 研究/ 14篇(审计/实验/调查) + 回溯日志/ |
| **调查文档(8/3, 必读)** | `Sync/rainbow/研究/幽灵问题-生产训练特征矩阵列错位.md` | 幽灵问题(行错位)全链路调查:根因/修复/验证/回测=生产一致性实验 + 接手指引 |
| **8/2 调查文档** | `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md` | 前向4连止损→回测生产矩阵差异调查(已被幽灵文档取代,保留作背景) |
| **回测日志** | `Sync/rainbow/回溯日志/` **001~008**(001/002=180d水位17.29, 003=低点抬高, 004=A100复核, 005=超参, 006=A100扫荡, 007=liq启用, 008=ETF数据审计) | 180d 真实水位 17.29(002号修复版复核一致) |
| 代码仓库文档 | 本仓库 `SYSTEM_OVERVIEW.md` / `DEPLOY.md` / `docs/gpu_server_connection.md` | 全景/部署/GPU(注意: SYSTEM_OVERVIEW 停在 7/18 地球版 1.1, 现状以 Obsidian 为准) |
| 背景记忆 | Reasonix memory(project scope, 仅本机 Reasonix 实例可见) | 前向评估口径 v2 / npz 偏差 / 矩阵差异 / GPU 连接 / 铁律 |

## 2. 当前状态快照(2026-09-06)

- **实盘**: 残差 LONG 臂 9/2 起真实交易(币安 U 本位逐仓), 9/5 在持 20 笔/权益≈306.9U; 每日 08:21 对账→平到期→开新批(≤10笔), 72h 滚动; SL 交易所 algo 单 + 每小时 :31 对账重挂
- **影子臂三轨**(晨报 3.8/3.9, 纯记账不影响实盘): hybrid 主臂(8/24, LONG 无TP + SHORT TP10/SL5, 300U 名义) + S5 对照臂(8/25, SHORT 仅前5) + residual 影子(9/1, 残差标签 LONG); **终审节点 10/23**(48h v 72h + 残差 60 天评审)
- **晨报**: 09:00 `digest_guard.sh` 单封合并 13 节(交易摘要/前向结算/影子臂/批次生存表/健康/四灯IC驾驶舱/BTC-山寨图/GitHub同步), 编译损坏自动用备份版; **09:15 体检SKILL 自动巡检**(12项, FAIL 邮件告警)
- **公证**: notarize **v2**(9/6 重写, 无 stash), 预测先于结果 push `xgboot/main`(earth-1.0); 每日 08:50 GitHub 同步(Contents API)
- **回测水位**: 180d 修复版 Sharpe 17.29(+1050%/MaxDD 15.6%, 002号日志); 回溯日志已积累 **001~008**(低点抬高/A100复核/超参搜索/liq启用/ETF审计)
- 幽灵修复后生产 prob 恢复 90%+ 级, 每日 [SAMPLECHK] 3/3 探针在报(幽灵防复发哨兵)
- 模型自愈实证周期 **4~6 天**(两次样本); 震荡期临时负期望是方差不是引擎坏(§0.5 公理4)

## 3. 关键文件地图

| 文件 | 用途 | 分类 |
|---|---|---|
| `auto_dual_trade.py` | 主交易: 特征构建/训练/预测/(老交易通道, 已关)/日报 | 🔴 核心生产 |
| `daily_predictor.py` | 特征工程库(宏观/RSI/回归/2日验证 verify_yesterday) | 🔴 核心生产 |
| `utils/feature_builder.py` | 特征向量组装(946 维) | 🔴 核心生产 |
| `guardian.py` / `daily_data_collection.py` | 进程守护 / 数据采集 | 🔴 核心生产 |
| `audit/residual_live.py` | **实盘执行器**: 对账/到期平仓/开仓/交易所SL挂单/每小时reconcile | 🔴 核心生产 |
| `daily_digest_email.py` + `scripts/digest_guard.sh` | 晨报(09:00, 13节) + 保险丝 | 🔴 核心生产 |
| `scripts/notarize_pred.sh` | 预测公证 v2(08:30, 无stash) | 🔴 核心生产 |
| `backtester/config/current_params.json` | 实盘参数(`_live_trading` 覆盖代码默认, 含 TRADING_ENABLED/ALLOW_SHORT) | 🔴 核心配置 |
| `audit/{hybrid_tracker,hybrid_s5,residual_tracker,forward_tracker,top10_forward}.py` | 影子臂/前向结算(纯记账) | 🟡 影子 |
| `audit/{audit_snapshot,audit_verify,data_versions_snapshot,replay_verify}.py` | 生产审计链(只读) | 🟢 审计 |
| `scripts/trading_system_github_sync.py` | 每日 GitHub 同步(08:50, Contents API 直推远程) | 🟢 基础设施 |
| `scripts/system_health_check.py` | 体检SKILL脚本(09:15 全链路巡检) | 🟢 运维 |
| `gpu_backtest_exp.py` | GPU 回测引擎(样本构建已改 adt 同源) | 🟢 实验 |
| `~/.local/share/auto_trade/train_data_latest.npz` | 生产训练数据(幽灵已修复 8/3, 现为干净数据) | 数据 |
| `~/.local/share/auto_trade/models/` | 每日全量重训模型(xgb_daily_{long,short}_YYYYMMDD.pkl) | 数据 |

## 4. 每日流水线(cron, 北京时间; 2026-09-06 与实际 crontab 对齐)

```
每分钟  guardian.py 守护(进程拉起 + 热力图/宇宙快照等周期任务, 日志 /tmp/guardian.log)
04:10  sector_fetcher.py 板块数据
06:00  daily_data_collection.py 主采集(K线/宏观/链上/情绪/稳定币)
06:05  fetch_etf.py ETF资金流 | 06:10 coingecko_mcap 市值 | 06:12 exchange_info | 06:20 oi_snapshot
07:10  daily_universe_snapshot.py 宇宙快照(data/universe/)
07:30  update_klines_oi K线+OI 补采
08:02  data_versions_snapshot.py 数据版本快照(只读)
08:04  audit_snapshot.py 审计快照(只读)
08:05  auto_dual_trade.py 训练+预测(SOUP+置换检验; 交易由 residual_live 执行)
08:20  telegram_group_bot.py signal
08:21  residual_live.py trade 实盘: 对账/平到期 → 开新批(≤10笔, 40U名义/5x逐仓/SL-5%/72h)
08:25  audit_verify.py 审计校验(只读)
08:30  notarize_pred.sh 预测公证(v2: 快照提交+rebase -X theirs, 预测先于结果)
08:30  data_drift_monitor.py --check 数据漂移 | replay_verify.py 重放校验 | oi_180d_ready.py
08:40  cron_monitor.py --task 交易预测 失败重试
08:45  hybrid_tracker.py 主臂影子结算
08:50  trading_system_github_sync.py GitHub同步(Contents API) + forward_ic_check.py 前向IC + hybrid_s5.py S5对照臂
08:55  residual_tracker.py 残差影子结算
09:00  digest_guard.sh 晨报(保险丝: 脚本编译损坏时自动用备份版发送; 2日验证为完整K线口径)
09:02  telegram_group_bot.py pnl | 09:05 altcoin_volume_alert.py 放量警报 | 09:10 forward_tracker.py
09:15  system_health_check.py --notify 全链路体检(缺失/失效即邮件告警) ← 9/6 新增
每小时:31  residual_live.py reconcile 对账/SL丢失重挂
每周日 03:00  obsidian_github_sync.py
```

> **体检SKILL**: `.agents/skills/health-check/`(触发词"体检SKILL"; 手动 `python3 scripts/system_health_check.py`)。
> 流水线任一环节"有没有跑/产物在不在"以体检SKILL检查矩阵为准。

## 5. 铁律(必须遵守)

1. **生产改动门槛**: 任何 🔴 生产文件改动必须先 180d 回测对比(Before vs After: Sharpe/收益/回撤/胜率)+ 用户审核同意
2. **不再优化模型本身**(2026-08-01 起): 任何新特征/参数变更须先过前向数据; 前向数据是最终裁判
3. **git 提交必须经用户明确同意**: 禁止自行 commit/push
4. **回测绝对水位不可作为生产预期**: 回测只用于管道内 A/B 相对排序
5. **前向结算口径 v2**: 8:21 市价入场 + 1m 粒度 + 成交即盯盘(先查止损后止盈, 48h 到期); 不豁免入场日
6. **批判优先(用户 8/7 明确指令)**: 用户提出任何意见/方向/优化时, 一律以数据和事实辩证处理, 不顺从情绪:
   ① 先查已有证据(证伪台账/实测基线/底率), 再表态;
   ② 区分"已验证事实"与"个案/直觉外推", 幸存者偏差案例(SKYAI 式)必须点破;
   ③ 结构化输出: 证据 → 正反两面 → 结论 → 证伪线(什么数据会推翻结论);
   ④ 用事实浇灭幻想是职责, 不是冒犯; 同样, 用户意见有数据支持时也要明确支持, 不为批判而批判

## 6. 接手顺序(建议)

1. 读本文件 + `Sync/rainbow/首页.md` + `Sync/rainbow/系统/地球版-总览.md`
2. 读 `Sync/rainbow/系统/参数卡.md`(完全体配置)+ `系统/标签与交易规则.md`(aligned 时序)
3. 读调查文档第八节(接手指引)→ 处理「已知问题」
4. 熟悉代码: `auto_dual_trade.py` 主流程(main → 训练 → SOUP → perm test → 交易), `gpu_backtest_exp.py` 回测流程

## 7. 已知问题(接手时开放)

1. ~~**生产 npz 特征偏差**(8/1 起)~~ → **已修复 2026-08-03**: 根因 `_fast_winsor_bounds` 的 `col.partition()` 原地重排(commit 3ef51c5 修复); 详见幽灵文档。**遗留动作**: ① 8/4 08:05 验证 [SAMPLECHK]=构建真值 + prob 90%+; ② ~~用修复版重跑 180d 回测复核~~ → ✅ 17.29 完全一致(002号回溯日志); ③ 错位模型副本 7 天后可删(保留作审计)
2. ~~**verify_yesterday 早盘快照偏置**~~ → **已修复 2026-08-06**(用户批准): 结算推迟到 T+2 收盘后(T+3 结算) + 入场日纳入止损扫描(口径v2不豁免) + tracker 近7天每日滚动重算覆盖历史快照值; 备份 `.bak_verify_20260806`
3. ~~**前向 4 连止损(7/29~8/1)**~~ → **已关闭(2026-09-06)**: 评审期(8/10)已过, 修复后系统转入完全体长跑(§2), 7/29~8/2 脏数据期仅作历史标注
4. **SOUP 历史模型边界**: 8/3 已隔离 7/31~8/2 错位模型副本至 `/tmp/poison_models/`(仍在, 重启即清, 无需处理); 若从旧备份恢复模型目录, 需先甄别错位模型
5. SYSTEM_OVERVIEW.md 内容滞后(7/18 版), 现状以 Obsidian 为准
6. GPU SSH 端口已变更为 **24090**(旧 22160/22183/22156 全失效)
7. **待研究队列(8/10 评审后启动, 用户 8/6 排期; 均为生产变更, 走铁律 1)**:
   ① 深度树陡峭边界+数据微差 敏感性实验(8/6 用户拍板挂起);
   ② **核心**: 路径感知标签族(MAE/MFE 回归为核心 — 用户 8/6 实测观察"方向对被止损"是主要损耗; 干净期实测: 止损单 35% 方向最终正确, MAE 中位 9% > 止损线 5%) — 任务卡 `Sync/rainbow/想法箱/路径标签实验-排期8-10后.md`
   ③ 远期(用户 8/6 拍板): 双机对比实验验证早入场盈利 → Rust 最终迭代 → 地球版固化转新方向 — 路线图 `Sync/rainbow/研究/地球版最终迭代路线图-20260806.md`; 原则: Python 实验, Rust 只移植已验证设计
   ④ 独立方向(8/10 后, 用户 8/7 立项): 持续放量趋势延续研究(SKYAI 案例; 独立模型, 不动方向模型; 先群组统计验证"量能持续性"判别力) — 任务卡 `Sync/rainbow/想法箱/持续放量趋势延续研究-排期8-10后.md`
8. ~~**8/10 评审及之后的研究核心主题(用户 8/7 定调, 四个字): 止损位置**~~ → 评审期已过, 该主题由 §7.7② 路径标签实验(任务卡在想法箱)承接推进
9. **待复测约定(用户 8/7 嘱托, 勿忘)**: 用户假设 — 止损 1-15% 区间存在择优档位拐点, 与距前低距离、量能形态相关(用户 10 年交易直觉, 默会知识, 需代码扫描逼近)。当前干净样本(8/3~8/6, ~60 个 LONG 候选)不足为据: "6-10% 甜区"等切片结论**只是线索, 非证伪非证实**, 按用户指示暂不入台账。**触发条件: 干净 LONG 候选样本累积 ≥200 笔(约每日+15~20, 两三周后)时重跑网格: 止损档位(1-15%逐档) × 距前低(细分桶) × 量能(分位)**。方法论约定: 用户直觉提假设 → 数据裁决 → 代码逼近拐点; 直觉与数据互纠(8/7 用户曾纠正小样本草率证伪)。 **状态(2026-09-06)**: 按 8/3 起每日+15~20 估算, 干净 LONG 候选累计已远超 200 → 触发条件已满足, 待用户拍板重跑网格。
10. ~~**公证 stash-pop 冲突**(8/26、8/27、9/5 三次复发)~~ → **已根治 2026-09-06**: 根因是 08:50 GitHub 同步(Contents API)在远程生成提交, 次日 08:30 公证的 `stash pop` 与其在相同文件上三方合并 → UU; 9/4 残差双轨上线后 `residual_live_state.json` 每日被同步上传+每日被改写, 冲突已成必然。公证脚本重写为 **v2**(临时快照提交 + `rebase -X theirs` 取本地 + `reset --mixed` 解包, 全程无 stash, 失败完整回滚); commit 7973118。9/5 遗留 stash@{0} 待用户确认后 drop。
11. **晨报代码已知小坑(2026-09-06 体检发现, 大部分已修)**: pred 取档已改"优先今日+缺失明示旧数据"; S5 对照臂改同窗口对照; 重合度分母改实际榜单长度; 发送加一次重试; `_refresh_tracker` 副作用已移出格式化函数。**digest_guard 保险丝只兜编译损坏, 运行时错误无保险丝**(09:15 体检兜底告警)。

## 8. 常用操作

- 看系统健康: `crontab -l` + `tail logs/auto_dual.log` + `~/.local/share/auto_trade/trade.log`
- **全链路体检**: `python3 scripts/system_health_check.py`(09:15 cron 自动跑, `--notify` 失败邮件; 日志 logs/health_check.log; 详见体检SKILL `.agents/skills/health-check/`)
- **实盘状态**: `cd /home/myuser/websocket_new && python3 audit/residual_live.py status`(权益/在持/当日开仓/SL)
- 手动触发训练: `cd /home/myuser/websocket_new && python3 auto_dual_trade.py`(会拿锁, 勿重复跑)
- GPU 回测: `ssh -p 24090 linux@175.155.64.171` → `cd ~/websocket_new && env NOLAG_MODE=aligned VOLRAW_FEATS=1 FUND_FEATS=1 LONG_MOM_FILTER=0 SL_PCT=5 WINSOR_Q=0.001 python3 gpu_backtest_exp.py 180 1`
- 生产=回测一致性重放校验: GPU 上 `python3 gpu_replay_prod.py`(见仓库, 对比当日 pred 存档)
- 审计日志: `tail -20 /home/myuser/websocket_new/logs/audit.log`
- 数据同步生产→GPU: `rsync -az --partial -e "sshpass -p '<密码>' ssh -p 24090" /home/myuser/backtester/data_cache/{notusdt_1d_full.json,oi_daily.json,funding_hist.json} linux@175.155.64.171:~/backtester/data_cache/` + 外部数据目录(见 DEPLOY.md §9)
