---
name: "health-check"
description: "交易系统全链路健康体检SKILL。当用户说'系统体检'/'健康检查'/'系统是否正常'/'检查系统'/'体检SKILL'时触发。覆盖: CRON注册表→进程守护→数据采集→特征→XGBoost训练→预测→公证→实盘开仓/挂止损→影子结算→GitHub同步→晨报发送, 逐环节判定 OK/WARN/FAIL, 支持定期(09:15 cron)自动运行+失败邮件告警。"
---

# 体检SKILL

> 创建: 2026-09-06 (notarize stash-pop 三次复发后, 用户要求"定期检查系统是否没坏"); 名称: 体检SKILL
> 适用目录: `/home/myuser/websocket_new/` (earth-1.0 仓库)
> 定位: **流水线结构体检**(每个环节有没有跑、产物在不在、状态对不对) — 与
> `morning-report-analyst` SKILL 互补(那个管"怎么解读数据", 这个管"机器还活着吗")。

---

## 0. 一键执行

```bash
cd /home/myuser/websocket_new && python3 scripts/system_health_check.py
```

- 每行一个环节: ✅OK / ⚠️WARN / ❌FAIL / ⏳NOT_DUE(未到判定时间), 末行给总判定
- 退出码: 0=无FAIL(可有WARN), 1=有FAIL → 适合脚本/CI调用
- `--notify`: 有 WARN/FAIL 时调用 alert_monitor 发邮件(生产 cron 用这个)
- `--json`: 追加机器可读输出

**时间感知(核心设计)**: 基准日 = 08:00 后=今天, 凌晨跑=昨天。凌晨跑时昨日全流水线视为已到期、全链路可检; 白天跑时未到"预期完成时间+缓冲"的环节标 NOT_DUE 不误报。

## 1. 定期运行(已部署 cron)

```
15 9 * * *  python3 scripts/system_health_check.py --notify >> logs/health_check.log 2>&1
```

选 09:15 的原因: 全天流水线(06:00采集→09:00晨报)已全部跑完, 任一环节失效当天 09:15 即邮件告警, 不用等用户看晨报才发现。历史体检日志: `logs/health_check.log`。

## 2. 检查矩阵(环节 → 判定时间 → 依据)

| 环节 | 判定时间 | 检查内容(脚本内实现) | 失效时深挖命令 |
|---|---|---|---|
| CRON注册表 | 任何时刻 | 29条关键任务在 crontab(基线比对, 缺失=FAIL) | `crontab -l` |
| 进程/服务 | 任何时刻 | guardian.log<90min; signal_api:8080 监听 | `tail /tmp/guardian.log`; `ss -tlnp \| grep 8080` |
| 磁盘空间 | 任何时刻 | / 剩余 <1GB FAIL, <2GB WARN | `df -h /` |
| 数据采集 | 07:45 | K线缓存(notusdt_1d_full.json)<26h; universe/{基准日}.json 在; etf/恐慌/宏观/稳定币/清算 26h内 | `tail logs/collect.log` |
| 训练预测 | 08:30 | pred_{基准日}.json 存在+三榜非空; npz<26h; 当日模型pkl在; trade.log SAMPLECHK≥1(满配3) | `tail logs/auto_dual.log`; grep SAMPLECHK trade.log |
| 实盘开仓 | 08:36 | residual_live_state days[基准日].opened; 在持≤30; 每笔有 sl_algo_id/sl_price | `tail logs/residual_live.log`; `python3 audit/residual_live.py status` |
| 公证/git | 08:45 | 无UU未解决文件; notarize.log 当日 push OK(ERROR但已有pred补提交→降级WARN); 远程一致性fetch | `tail logs/notarize.log`; `git status --short` |
| 数据漂移 | 08:45 | drift_report.json 日期=基准日; ALERT→WARN(历史修订是数据源正常行为, 不阻断) | `tail logs/data_drift.log` |
| 影子结算 | 09:10 | hybrid_tracker / residual_tracker / hybrid_s5 均含基准日条目 | `tail logs/hybrid_tracker.log` |
| GitHub同步 | 09:05 | sync_status 日期=基准日, 状态 CHANGED/NO_CHANGE, failed=0 | `tail /home/myuser/logs/trading_system_github_sync.log` |
| 晨报发送 | 09:15 | digest.log "晨报总览 {基准日}"+已发送; 经保险丝(GUARD)发送→WARN | `tail logs/digest.log` |
| 对账活性 | 任何时刻 | residual_live_state.json <26h 有落笔 | `tail logs/residual_live.log` |

## 3. 已知问题手册(体检命中后怎么办)

| 症状 | 定性 | 处置 |
|---|---|---|
| git UU 冲突 | 公证 stash-pop 遗留(2026-09-06 起已结构性根治: notarize v2 用快照提交+rebase -X theirs, 无stash) | 理论不再发生; 若发生: `git status` 看冲突文件 → 取本地版本 → `git add` → 通知用户 |
| 公证失败但已补提交(WARN) | 08:30 失败后人工 24e7b46 式补救 | 无需动作, 观察次日是否复发 |
| stash 残留(WARN info) | 冲突遗留快照 | 确认内容已恢复后 `git stash drop` |
| etf_flow 历史修订(数据漂移ALERT) | 数据源(交易所官网)回改历史, 正常现象 | 无需动作; 若伴随重放探针不一致才升级 |
| pred 缺失/为空 | 08:05 训练失败 | `tail -50 logs/auto_dual.log`; cron_monitor 08:40 会自动重试一次 |
| SAMPLECHK 少于3 | 特征构建探针缺失, 幽灵问题哨兵 | 立即查 auto_dual.log 构建段, 对照幽灵文档 |
| 晨报未发送 | 09:00 任务失效或 SMTP 故障 | digest_guard 只兜"编译损坏"; 运行时错误无保险丝, 查 digest.log 末尾 |
| 实盘在持无SL | 交易所 algo 单丢失 | residual_live reconcile(每小时:31)会自动重挂; 连续出现才人工介入 |

## 4. 边界与不覆盖项(诚实声明)

- **不检查账户资金安全**(权益/占用率): 需要 API 签名, 属于交易侧; 用 `python3 audit/residual_live.py status` 人工看
- **不检查回测/GPU端**(175.155.64.171): 那是实验端, 坏了不影响生产
- **不验证数据"内容正确性"**(只查"新鲜+在不在"): 内容对错由 drift monitor 重放探针 + 审计链(08:04/08:25)负责
- **CRON 注册表是基线比对**: 新增/删除 CRON 后需同步更新 `chk_cron()` 的 expected 列表(脚本内注释标了每条的职责)

## 5. 与其他 SKILL/文档的关系

- `AGENTS.md` §4 每日流水线 = 本 SKILL 检查矩阵的时间表来源(注意: AGENTS.md §4 写的是 8:20 公证/8:25 审计, 实际 crontab 已演化为 08:30 公证 + 更多任务, 以本 SKILL 表为准)
- `morning-report-analyst` SKILL 的 Step 0(流水线核验)= 本 SKILL 的手工迷你版; 诊断数据含义用那个, 检查机器活性用这个
