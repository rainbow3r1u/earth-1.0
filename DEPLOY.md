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
# 私有仓库, 先配好读取权限的 git 凭证(token 或 ssh key)
git clone https://github.com/rainbow3r1u/earth-1.0.git /home/myuser/websocket_new
cd /home/myuser/websocket_new && mkdir -p logs
# 预测公证 cron 需要推送权限: git remote set-url 配上带写权限的 token
git remote set-url xgboot https://<TOKEN>@github.com/rainbow3r1u/earth-1.0.git
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

---

## 9. 2026-08-02 Ops Log (audit & maintenance)

> 详见 Obsidian: `Sync/rainbow/研究/幽灵问题-生产训练特征矩阵列错位.md`(8/3, 幽灵已修复) + `Sync/rainbow/回溯日志/001_946D_180d_GPU.md`

### GPU connection (2026-08-03)
- `ssh -p 24090 linux@175.155.64.171` (RTX 3080 20GB; 端口 8/3 变更, 旧 22160/22183/22156 全失效)
- GPU 无法访问币安 API, 数据一律从生产端 rsync(见 AGENTS.md §8)

### Data sync (prod → GPU, all MD5-verified)
- klines/OI/funding/sector(+overrides)/sentiment/coingecko/hashrate/stablecoin/defillama/blockchair
- 8/2 修复: GPU external data had stalled at 07-17/18

### Backtest engine change
- `gpu_backtest_exp.py::_build_coin_samples` now calls `auto_dual_trade._build_feat_impl` (prod-sourced features)
- Unsupported experiment arms (KRONOS/BB/EXT/DIV/RAWR/LABEL_1D/FEAT_SHIFT) raise explicitly

### 幽灵问题修复 (2026-08-03, commit 3ef51c5) ⭐
- **根因**: `daily_predictor.py::_fast_winsor_bounds` 里 `col.partition([k1,k99])` 在 X_train 列视图上**原地分区**, 每列独立重排 → 破坏行完整性, 训练标签与特征错位(6/12 引入; 生产 8/1 起 npz 特征偏差、回测vs生产不一致、前向4连止损的根因)
- **修复**: `col = X[:, j].copy()` 后再 partition
- **验证**: 全量复刻 npz 0GUSDT 7/30=-1.8388 / 7/31=-0.2000 与构建真值一致; 生产手动运行 [SAMPLECHK] 全部正确; prob 恢复 90%+ 级(LONG BROCCOLIF3BUSDT 80.7% / SHORT HOMEUSDT 93.1%)
- **回测=生产逐日一致性**: GPU 重放实验 7/28/7/30 完全同币, 7/29 差 0.1pp 浮点噪声(工具: `gpu_replay_prod.py`)
- **SOUP 错位模型**: 7/31~8/2 模型副本已隔离至 `/tmp/poison_models/`(SOUP 自 8/3 只用干净模型)

### Audit cron (read-only, added 08-02)
```
4 8 * * *  websocket_new/audit/audit_snapshot.py   # pre-run snapshot (data MD5 + 3 probe coins 40 klines)
25 8 * * * websocket_new/audit/audit_verify.py     # post-run verify (npz probe vs snapshot/disk theory)
```

### Known issues (open)
- ~~Prod npz feature drift~~ → **已修复 8/3**(见上); 遗留: 8/4 08:05 验证 / 180d 回测复核 / 错位模型副本 7 天后清理
- **verify_yesterday early-snapshot bias**: settles with unclosed kline at 08:21; email/tracker returns are optimistic; needs full-kline settle (pending review)
- **前向 4 连止损(7/29~8/1)** = 错位模型产物; 修复后(8/3 起)重新前向观察, 8/6 评审分开统计

### 180d true watermark (08-02, full sync + prod-sourced build)
Sharpe 17.29 / +1050% / MaxDD 15.6% / win 76% — 干净数据训练(回测走安全 winsor 路径), 修复后预期保持 ~17~18(待修复版 180d 复核)

---

## 10. 2026-08-02 完整工作总结(与 Obsidian 研究文档同步)

> 全链路调查见 `Sync/rainbow/研究/前向观察与回测生产差异调查-20260802.md`

### 背景与触发
前向观察期(7/30~8/6)TOP1 口径 4 连止损(-20%),与回测水位(Sharpe 17~20/胜率76~79%)严重背离,启动全链路调查。

### 一、前向评估口径修正史

| 版本 | 口径 | 结果 | 教训 |
|---|---|---|---|
| v1(7/31) | 日线open入场+豁免入场日+15m | GIGGLE"浮盈+7.63%" | 漏开仓当天止损 |
| **v2(8/1定)** | **8:21市价入场+1m粒度+成交即盯盘,先查止损后止盈,48h到期** | TOP1 4单全止损 -20% | 见 forward-eval-rule |
| tracker/邮件 | verify_yesterday 早盘快照(结算日08:21未收盘) | 乐观偏置(EDGE记+2.59%实-9.8%) | 结算须用完整K线 |

**TOP1 前向(修正口径)**: 7/29 EDGEUSDT -5%｜7/30 PLAYUSDT -5%｜7/31 ONUSDT -5%(当日02:36 UTC)｜8/1 GIGGLEUSDT -5%(开仓6分钟)

**观察期 Top10 修正结算(7/30、7/31, 1m)**: LONG 20笔 平均+0.13% 胜率45% 累计**+2.6%**(止盈4/止损11); SHORT 10笔 平均-2.00% 胜率20% 累计**-20.0%**(止盈2/止损8)。邮件显示 SHORT +20% 是早盘快照假象。

### 二、回测 vs 生产差异调查(15+ 交叉实验)

**已排除**: 代码MD5 / K线OI费率外部数据(全同步全一致, 修了GPU外部数据7/17-18停更+缺sector_overrides) / 标签公式 / 训练截止d-3 / 超参 / winsor / xgboost版本 / cuda-hist(~3pp) / 宇宙(529vs540) / 训练窗。

**决定性对照(同机同环境)**: 生产npz训练→同一X_pred输出63~68%(正常); GPU缓存(adt构建)训练→95~96%(饱和)。生产原装模型→GPU缓存X_pred=68.6%正常 ⇒ X_pred无问题, 差异在训练矩阵。

**关键转折(8/2)**: 0GUSDT特征日7/28列0(ret_1d_norm): K线理论=-1.9970; GPU缓存(任何K线版本)=-1.9970; **生产主流程完整复刻=-1.9970**; **生产npz(8/1、8/2)=-0.2601(对不上任何构建!)**。生产npz含569个vol_raw=0异常行(GPU仅1个)。**⇒ 偏差在生产端每日构建环节, 不是GPU/回测的问题; 回测(正确构建)反而是无偏差参照系。**

**最可能机制(待8/3证实)**: 08:05启动时内存加载旧K线, API更新(08:05:17写盘)在加载之后, 构建用内存旧版。

### 三、审计安排(8/2已挂cron, 只读)
```
4 8 * * *  audit/audit_snapshot.py   # 运行前快照(数据MD5+3币40根K线)
25 8 * * * audit/audit_verify.py     # 运行后校验(npz抽查 vs 快照/磁盘理论值)
```
判定: MATCH_A=内存旧版K线实锤 / MATCH_B=运行后磁盘 / NO_MATCH=另有来源。日志 logs/audit.log。crontab备份 /tmp/crontab_backup_20260802.txt。

### 四、180d 真实水位(8/2, 数据全同步+adt同源构建)
**Sharpe 17.29 / +1050% / MaxDD 15.6% / 胜率 76%**(prob全90~100%饱和; LONG 118笔84% / SHORT 62笔60%; 耗时22min)。脏数据版18.68 vs 干净版17.29几乎一致 ⇒ **回测17~18是"生产同源特征+正确数据"的稳定水位**, 8/1"两套构建差异"结论作废; 生产修好npz偏差后为可复现预期(待前向验证)。

### 五、统计 alpha 理解(用户8/2)
- **"市场存在统计学的Alpha" ✅ 有实证支撑** — XGBoost是统计学产物: 过去180天9万+样本中"特征→未来2日收益"统计关系真实存在, walk-forward下Sharpe 17.29/76%, 脏/净两次独立验证几乎一致
- **"我们生产现在能拿到" ❌ 未兑现** — 生产npz有偏差(8/1起), 前向4连止损=alpha还没流到生产; 8/3修偏差→生产与回测同构→前向验证, 兑现才算拿到

### 六、待办
- [ ] 8/3 08:25 看 logs/audit.log 判定(MATCH_A/NO_MATCH), 锁定偏差环节
- [ ] 修生产构建偏差后, 前向重新观察(修复前4连止损不代表修复后)
- [ ] 修 verify_yesterday 结算口径(早盘快照→完整K线), 邮件/tracker数字才可信(生产文件, 需180d回测+用户同意)
- [ ] 8/6 前向评审: 观察期7天凑满, 以修正口径(1m)汇总
- [ ] 生产npz增加构建校验(保存后抽查vs理论值, 偏差即告警)

---

## 11. 接手指引 (For Next AI, 2026-08-03 更新)

> 一句话: 前向 4 连止损 vs 回测 17~20 Sharpe 背离 → 追查发现**生产训练特征矩阵行错位**(幽灵, 8/3 已修复): 根因 `_fast_winsor_bounds` 的 `col.partition()` 原地重排 X_train 每列。修复后生产 prob 恢复 90%+ 级, **回测=生产逐日一致性已由 GPU 重放实验验证**(7/28/7/30 同币, 7/29 差 0.1pp 浮点噪声)。完整依据与实验过程见 Obsidian `Sync/rainbow/研究/幽灵问题-生产训练特征矩阵列错位.md`。

### 术语速查
| 术语 | 含义 |
|---|---|
| `npz` | `~/.local/share/auto_trade/train_data_latest.npz`, 生产每天保存的训练数据(X_train 为 winsor 后特征; 8/3 起干净) |
| `adt` | `auto_dual_trade.py`(生产主程序); `adt._build_feat_impl` = 生产特征构建函数(唯一正确实现) |
| `回测引擎` | `gpu_backtest_exp.py`(GPU 端), 样本构建调用 `adt._build_feat_impl`(生产同源) |
| `幽灵` | 已修复 bug: `_fast_winsor_bounds` 的 `col.partition()` 原地重排(commit 3ef51c5, 2026-08-03) |
| `饱和` | 模型输出概率 90~100%(干净模型固有分布); 错位模型输出 54~71% |
| `早盘快照` | `verify_yesterday`(daily_predictor.py)在结算日 08:21 用未收盘 K 线结算 → 收益乐观偏置 |
| `特征日/样本日` | 样本日 ts 的特征用 ts-1 蜡烛; 回测缓存文件名 = 样本日 |
| `重放` | `gpu_replay_prod.py`: 复刻生产每天训练+SOUP+预测, 与 pred 存档对比 TOP1(回测=生产校验工具) |

### 关键路径
- 生产代码: `/home/myuser/websocket_new/`(`auto_dual_trade.py` / `daily_predictor.py`)
- 生产训练数据: `/home/myuser/.local/share/auto_trade/train_data_latest.npz`
- 审计脚本: `/home/myuser/websocket_new/audit/{audit_snapshot,audit_verify}.py`; 日志 `logs/audit.log`
- GPU: `ssh -p 24090 linux@175.155.64.171`, 代码 `/home/linux/websocket_new/`
- 回测缓存: `~/backtester/data_cache/by_day_cache_v5_aligned_volraw_fund{,_prod,_replay}`(`_replay`=MIN_KLINES=35 生产宇宙版)
- 错位模型副本(7/31~8/2): `/tmp/poison_models/`(SOUP 已不用, 7 天后可删)

### 接手第一步
1. 读 `logs/audit.log` 尾部(8/3 起应 MATCH 干净) + `tail trade.log` 看 [SAMPLECHK] 是否=构建真值(0GUSDT 7/31=-0.2000)
2. 读 Obsidian 幽灵文档(依据+实验过程全记录) → 处理「遗留动作」(180d 回测复核 / 8/6 评审口径)
3. 回测=生产校验: GPU 跑 `gpu_replay_prod.py` 对比当日 pred 存档
4. 更新本档 + Obsidian 幽灵文档

### 已知问题(接手时仍开放)
1. 生产 npz 特征偏差(待 8/3 判定+修复)
2. `verify_yesterday` 早盘快照偏置 → 邮件/tracker 收益数字不可信, 需改完整 K 线结算(生产文件, 需 180d 回测对比+用户同意)
3. 前向 TOP1 4 连止损 = 偏差特征模型的输出, 不代表修复后表现

---

## 12. 换服务器/迁移接手 Checklist(2026-08-02)

> 目标: 新服务器上 30 分钟内恢复"研究+运行"能力, 不依赖旧服务器。两个仓库:
> - 代码/文档: `github.com/rainbow3r1u/earth-1.0`(公开)
> - 知识库: `github.com/rainbow3r1u/rainbow-vault`(**私有**, 需 git 凭证)

### 步骤
```bash
# 1. 克隆代码 + 知识库
git clone https://github.com/rainbow3r1u/earth-1.0.git /home/myuser/websocket_new
cd /home/myuser/websocket_new && mkdir -p logs
git clone https://github.com/rainbow3r1u/rainbow-vault.git /home/myuser/Sync/rainbow   # 私有, 配好凭证

# 2. 配 .env(照 .env.example): COS_SECRET_ID/KEY、BINANCE_API/KEY、SMTP 必填
cp .env.example .env

# 3. 依赖 + git 身份
pip3 install -r requirements.txt
git config user.name "rainbow3r1u" && git config user.email "rainbow3r1u@users.noreply.github.com"

# 4. 从 COS 一键拉取全部数据(~1700 文件, K线/OI/费率/外部数据)
python3 deploy/bootstrap_from_cos.py --dry-run   # 先预览
python3 deploy/bootstrap_from_cos.py             # 实际拉取

# 5. 挂 cron(含 8/2 新增审计两条)
# 照 §6 cron 全表 + 审计:
#   4 8 * * * audit/audit_snapshot.py
#   25 8 * * * audit/audit_verify.py
```

### 验证清单(换机后必做)
- [ ] `ls backtester/data_cache/notusdt_1d_full.json` 存在且 MD5 与 COS 一致(或 >90天K线数 ~532币)
- [ ] 手动跑一次 `python3 auto_dual_trade.py`(观察训练+预测正常, 输出 pred_YYYY-MM-DD.json)
- [ ] `python3 audit/audit_verify.py` 干跑通过(或看 logs/audit.log 有正常判定)
- [ ] guardian 启动后 `/tmp/guardian_status.json` 正常
- [ ] 模型目录 `~/.local/share/auto_trade/models/` 存在(首次由 auto_dual_trade 自动生成)
- [ ] 知识库: 读 `AGENTS.md` → Obsidian `首页.md` → `系统/参数卡.md` 确认现状

### 注意事项
- `.env` 含密钥绝不入库; 换机后手动配置
- 数据以 COS 为准(每日自动备份); 代码/文档以 GitHub 为准
- GPU 回测机独立(175.155.64.171:22160), 不随生产迁移; 数据从生产 scp
