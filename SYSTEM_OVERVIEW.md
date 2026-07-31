# 加密货币ML自动交易系统 — 系统全景文档

> 最后更新: 2026-07-18 (**地球版 1.1**, vol_raw 945维)
> 适用目录: `/home/myuser/websocket_new/`
> 版本代号规则: 行星系中文代号 (地球→火星→...), 每版需 180d 回测验证 + 用户审核

---

## 0. 版本参数卡 — 地球版 1.0 (Earth v1.0)

> 定版日期: 2026-07-18 | 基准提交: `5668355` | 定义: v3 特征体系 + volfeat 量能特征 + aligned 时序与标签

| 维度 | 参数 |
|------|------|
| **模型** | XGBoost 双模型(多/空): n_estimators=200, max_depth=6, lr=0.05, min_child_weight=1, reg_lambda=10, reg_alpha=10, subsample=0.8, colsample_bytree=0.6, scale_pos_weight=auto, CPU(hist) |
| **特征** | 944维: K线17 + 波动聚类3 + 回归4 + RSI/背离7 + 90天回看6 + volfeat量能2(tr_ratio/tbr) + 板块22 + 宏观~56 + Kronos832(置零) + 跨资产4 |
| **标签** | aligned: `(close[T+2]-open[T])/open[T] > ±5% = 1` (与48h持仓窗口对齐) |
| **训练** | 每日8:05重训, 窗口180天, 门槛90天K线(~525/532币), Permutation Test按边阻断 |
| **预测** | 每日8:05, 预测日=入场日, TOP1多空二选一, 基础阈值60% + 动态自适应 |
| **入场** | 市价单, 保证金阶梯 [5,8,10,15,20,30]U (钱包分档 [25,50,100,200,400]), **杠杆 20x** |
| **出场** | 止盈 +10% / 止损 -10% (Algo条件单 MARK_PRICE), 48h时间退出 |
| **风控** | 余额<10U停交易, 24h成交额>50万U过滤, 同币反向先平后开 |
| **回测成绩** | 180d: Sharpe 6.33 / Cum +469.6% / MaxDD 46.5% / 胜率65%; 90d: Sharpe 8.78 / +321.8% / 28.3% / 71% |

**版本演进**: 地球版 1.0 = 首个定版 (修复入场滞后24h + 标签对齐 + 数据链路清理后). 后续版本按行星序命名, 变更须过 production-change-gate (180d回测+用户审核).

### 地球版 1.3 (2026-07-20 定版)

| 变更 | 内容 |
|------|------|
| 规则 | **LONG 动量过滤**: LONG 候选必须满足 特征蜡烛连涨≥2天 + 20日价格位置>0.7 (修复"LONG只选底部徘徊币"问题); SHORT 完全不受影响 |
| 回测(180d vs 1.2基线) | Sharpe 8.62→**9.00**, Cum +636.5%→+633.8%, MaxDD 40.3%→**28.3%**, 胜率 69%→72%, T/ST 103/32→100/33 |
| 生效 | 2026-07-20 08:05 首次运行 (用户观察驱动: "LONG找不到上涨途中的币" → 动量过滤验证有效) |

### 配置改进 (2026-07-23)

| 变更 | 内容 |
|------|------|
| **TRADING_ENABLED 开关** | 新增 `TRADING_ENABLED` 配置项 (`current_params.json` → `_live_trading`) — `false` 时训练+预测照常运行但跳过开仓，日志显示"交易已禁用"；`true` 恢复正常。用于暂停实盘期间继续更新模型和发日报 |
| **early_stopping 早停** | 训练数据按时间切分 train/val (最后10%天数作验证集)，`XGBClassifier.fit()` 加 `eval_set` + `early_stopping_rounds=15`。验证集过同一套预处理(NaN填充→Kronos置零→winsor)。日志输出早停轮次。置换检验的shuf模型也复用同一验证集早停，保证对比公平 |
| **目的** | 当前 7天命中率仅 LONG 13% / SHORT 23%，模型过自信(预测概率 57-71%)——是过拟合症状。早停直接解决"无验证集硬跑200轮到过拟合"问题 |
| **影响范围** | `auto_dual_trade.py`: `train_and_predict()` 数据切分+fit参数, `_run_permutation_test()` 签名扩展; `current_params.json`: +`TRAIN_VAL_SPLIT`/`EARLY_STOPPING_ROUNDS`/`TRADING_ENABLED` |
| **回退** | `TRADING_ENABLED=true` + `TRAIN_VAL_SPLIT=0` 恢复原始行为 |
| **后注(7/24)** | early_stopping 次日即引爆: XGBoost 3.x 已移除 fit 的 `early_stopping_rounds` kwarg, 7/24 08:05 全盘崩溃(TypeError), 恢复1.3时**早停代码已整体删除**, 当前代码无早停/TRAIN_VAL_SPLIT; TRADING_ENABLED 开关保留至今 |

### 地球版 1.2 (2026-07-20 定版)

| 变更 | 内容 |
|------|------|
| 特征 | 945 → 946维: +`fund_raw` 单币种资金费率原值 (样本日前最近一次8h结算, 不平均/无前视; 数据 `backtester/data_cache/funding_hist.json`, 每日ddc增量更新+新币全历史) |
| 回测(180d vs 1.1基线) | Sharpe 8.13→**8.62**, Cum +610.8%→+636.5%, MaxDD 36.1%→40.3%, 胜率69%→69%, T/ST 104/36→103/32 |
| 生效 | 2026-07-20 08:05 首次运行 (用户提案; 小币费率有真实信息量: 541币仅1币不动, LAB/GUA波动达0.248%) |

### 地球版 1.1 (2026-07-18 定版)

| 变更 | 内容 |
|------|------|
| 特征 | 944 → 945维: +`vol_raw` 原始成交额q(USDT), 不平均/不归一 — 提供大币小币尺度信息 (vol_ratio 相对量丢失的部分) |
| 回测(180d vs 1.0基线) | Sharpe 6.33→**8.13** (+28%), Cum +469.6%→+610.8%, MaxDD 46.5%→**36.1%**, 胜率 65%→69%, T/ST 88/34→104/36 |
| 生效 | 2026-07-18 08:05 首次运行 (用户提案, 当日回测通过后接入) |

---

## 1. 系统架构概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        数据采集层 (Data Collection)                       │
├─────────────────────────────────────────────────────────────────────────┤
│  实时采集 (guardian.py 守护)        │  每日采集 (6:00 cron)              │
│  ─────────────────────────────────  │  ────────────────────────────────  │
│  • OI多空比采集器  (oi_collector)   │  • 恐慌贪婪指数                     │
│  • 情绪数据采集器  (sentiment)      │  • 情绪数据补充                     │
│  • 链上数据       (blockchair)      │  • BTC市值/市占率                  │
│  • 板块分类       (sector_fetcher)  │  • 宏观资产(SPX/DXY/黄金)           │
│  • 板块热力图     (sector_heatmap)  │  • DeFi TVL (多链)                 │
│  • 算力采集器     (hashrate)        │  • 清算热力图                       │
│  • 稳定币监控     (stablecoin)      │  • ETF资金流                        │
│                                     │  • K线缓存更新 (528币日线)          │
│                                     │  • OI缓存更新 (531币日级)           │
│                                     │  OI日级缓存重建 (oi_history_builder)│
└────────────────────┬────────────────┴────────────────┬───────────────────┘
                     │                                   │
                     ▼                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        数据存储层 (Data Storage)                          │
├─────────────────────────────────────────────────────────────────────────┤
│  本地文件                              │  腾讯云COS (云端备份)              │
│  ────────────────────────────────────  │  ───────────────────────────────  │
│  data/fear_greed_history.json          │  klines/sentiment_data/           │
│  data/macro_assets.json                │  klines/oi_data/                  │
│  data/liq_daily.json                   │  klines/long_short_data/          │
│  data/crypto_sectors.json              │                                   │
│  backtester/data_cache/oi_daily.json   │  klines/cache/notusdt_1d_full.json  │
│  backtester/data_cache/notusdt_1d_full.json│ klines/cache/oi_daily.json         │
│  hashrate_data/hashrate_history.json   │                                   │
│  stablecoin_data/*.json                │                                   │
│  coingecko_data/*.json                 │                                   │
│  defillama_data/*.json                 │                                   │
│  sentiment_data/*.json                 │                                   │
│  etf_data/etf_flow.json                │                                   │
│  blockchair_data/btc_chain.csv         │                                   │
└────────────────────┬──────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        模型训练层 (Model Training)                        │
├─────────────────────────────────────────────────────────────────────────┤
│  auto_dual_trade.py → train_and_predict()                               │
│                                                                         │
│  1. 拉取K线 (fetch_klines_full)    → 直接全量加载528币, 缓存+API补充   │
│  2. 加载OI数据 (fetch_oi)          → 531币, 30天历史                    │
│  3. 加载宏观特征 (daily_predictor) → 35维外部数据                        │
│  4. 预计算板块热度                  → 22维 (ts-86400防泄露)              │
│  5. 预计算Kronos嵌入                → 832维 (CPU推理, Kronos-base)      │
│  6. 构建944维特征 (build_features_78d)                                  │
│       • K线特征: 17维 (归一化收益/波动率/位置/振幅/ streak/背离/OI变化)  │
│       • 波动聚类: 3维 (regime/momentum/persist)                         │
│       • 回归特征: 4维  (β/α/R²/残差 vs BTC)                             │
│       • RSI+背离: 7维  (RSI7/14/30 + 背离4维)                           │
│       • 90天回看: 6维  (rsi90/vol_90d/pp_90/ret_30d/60d/90d, v3)        │
│       • 量能特征: 2维  (tr_ratio笔数量比+tbr主动买卖比, volfeat 7/18)    │
│       • 板块热度: 22维 (ts-86400, 避免当日收益泄露; 手工覆盖层防回退)    │
│       • 宏观特征: ~56维 (链上/情绪/恐慌/稳定币/溢价/算力/清算/TVL)      │
│         (ETF 2维 7/12起禁用置零; 清算26维中19维置零; ab已改prev_date防前视)│
│       • Kronos:  832维 (已禁用, 训练/预测一致置零)                       │
│       • 跨资产:   4维  (SP500/DXY/黄金 + 山寨BTC溢价)                   │
│  7. 标签: 对齐入场点 2日收益 > ±5% = 1 (aligned, 7/18)                   │
│       next_ret = (close[i+2] - open[i]) / open[i]  (≈48h持仓窗口)       │
│       i=n-1为预测样本(特征=最新收盘蜡烛D-1); i∈{n-3,n-2}标签未实现跳过   │
│  8. Walk-forward训练 XGBoost (做多模型 + 做空模型)                       │
│       • 训练窗口: 最近180天 (v3验证最优)                                 │
│       • 门槛: ≥90天K线历史 (当前~525/532)                               │
│       • 正样本权重: scale_pos_weight 自动平衡                          │
│       • Permutation Test 过拟合检测 (每天执行)                          │
│  9. 预测今日所有币种 → 输出Top10多空概率                                │
└────────────────────┬──────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        交易执行层 (Trade Execution)                       │
├─────────────────────────────────────────────────────────────────────────┤
│  auto_dual_trade.py → main() (每天8:00 cron运行)                        │
│                                                                         │
│  步骤1: 检查现有持仓                                                    │
│    • 止损检查: -10% → 市价平仓                                          │
│    • 时间退出: 48小时 → 市价平仓                                        │
│    • 平仓前 cancel_all_orders(symbol) 防止挂单冲突                      │
│                                                                         │
│  步骤2: 训练+预测 (见上文)                                              │
│                                                                         │
│  步骤3: 开仓决策                                                        │
│    • prob < 60% → 空仓跳过                                              │
│    • 做多prob >= 做空prob → 开多仓                                      │
│    • 做空prob > 做多prob  → 开空仓                                      │
│    • 同币种已有同向持仓 → 跳过                                          │
│    • 同币种已有反向持仓 → 先平仓再开仓                                  │
│                                                                         │
│  步骤4: 仓位计算                                                        │
│    • 杠杆: 2x (配置可调)                                                │
│    • 保证金: 固定金额 (wallet × 配置比例)                               │
│    • 预留0.5%资金费缓冲                                                 │
│                                                                         │
│  步骤5: 下单                                                            │
│    • 市价单开仓 (MARKET)                                                │
│    • 设置杠杆 (set_leverage)                                            │
│    • 尝试挂止损单 (STOP_MARKET, -10%, MARK_PRICE触发)                  │
│    • 尝试挂止盈单 (TAKE_PROFIT_MARKET, +10%, MARK_PRICE触发)           │
│    ⚠️ 已知限制: Binance API -4120, STOP_MARKET标准接口暂不支持          │
│       → 遇-4120不回滚, 记录裸仓状态, 需手动补止损                        │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 文件依赖关系图

```
auto_dual_trade.py (主交易脚本)
    ├── .env (API密钥)
    ├── daily_predictor.py (特征工程+模型)
    │     ├── data/crypto_sectors.json (板块分类)
    │     ├── data/fear_greed_history.json (恐慌贪婪)
    │     ├── etf_data/etf_flow.json (ETF)
    │     ├── blockchair_data/btc_chain.csv (链上)
    │     ├── sentiment_data/*.json (情绪)
    │     ├── stablecoin_data/*.json (稳定币)
    │     ├── coingecko_data/*.json (市值/市占率)
    │     ├── defillama_data/*.json (TVL)
    │     ├── hashrate_data/hashrate_history.json (算力)
    │     ├── data/liq_daily.json (清算)
    │     ├── data/macro_assets.json (宏观)
    │     └── kronos_features.py (Kronos嵌入)
    ├── backtester/data_cache/notusdt_1d_full.json (K线缓存)
    ├── backtester/data_cache/oi_daily.json (OI缓存)
    ├── backtester/config/current_params.json (策略参数)
    └── utils/feature_builder.py (特征组装)

daily_data_collection.py (每日采集, 6:00 cron)
    ├── update_klines_oi()        → K线缓存+OI缓存刷新 (NEW)
    ├── fear_greed_collector.py   → data/fear_greed_history.json
    ├── sentiment_collector.py    → sentiment_data/
    ├── collect_btc_mcap.py       → coingecko_data/
    ├── collect_btc_dominance.py  → coingecko_data/
    ├── collect_macro_assets.py   → data/macro_assets.json
    ├── collect_tvl.py            → defillama_data/
    ├── liquidation_heatmap.py    → data/liq_daily.json
    ├── fetch_etf.py              → etf_data/etf_flow.json
    └── monitor.py                → stablecoin_data/

guardian.py (进程守护, cron每分钟)
    ├── screen会话: oi_collector, blockchair_collector, sentiment_collector
    │     • 重启前先 kill 旧 screen session, 防止孤儿进程泄漏
    │     • 所有命令自动注入 .env 环境变量
    ├── 文件新鲜度: 清算热力图(2h), 恐慌贪婪(25h), 稳定币(25h), 算力(25h), ETF(20h), TVL(25h), 板块(25h), BTC市值(25h)
    ├── 过载保护: 每小时最多重启5次, 超出则放弃
    └── 状态输出: /tmp/guardian_status.json
```

---

## 3. 核心配置参数

位置: `backtester/config/current_params.json` (从 shared_params.json 读取)

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `STOP_LOSS_PCT` | 10.0 | 止损百分比 |
| `TAKE_PROFIT_PCT` | 10.0 | 止盈百分比 |
| `PROB_THRESHOLD` | 60.0 | 最低置信度阈值 |
| `LEVERAGE` | 20 | 杠杆倍数 (实盘 20x, 名义=保证金×20) |
| `MIN_VOLUME_24H` | 500000 | 最小24h成交量 (仅回退时使用) |
| `TRAIN_DAYS` | 180 | 训练历史窗口天数 (v3验证最优; min_required=90天K线) |
| `MARGIN_STEPS` | [5,8,10,15,20,30] | 保证金阶梯 (USDT) |
| `CAPITAL_BREAKPOINTS` | [25,50,100,200,400] | 钱包余额分档 |

---

## 4. Cron 调度配置

```
* * * * *  cd /home/myuser/websocket_new && /usr/bin/python3 guardian.py >> /tmp/guardian.log 2>&1
0 6 * * *  cd /home/myuser/websocket_new && /usr/bin/python3 daily_data_collection.py >> logs/collect.log 2>&1
30 7 * * * K线+OI缓存补采 (update_klines_oi) >> logs/collect.log 2>&1
5 8 * * *  cd /home/myuser/websocket_new && /usr/bin/python3 auto_dual_trade.py >> logs/auto_dual.log 2>&1
40 8 * * * cron_monitor.py --task 交易预测 (确认失败才自动重试一次, 7/27启用) >> logs/cron_monitor.log 2>&1
0 9 * * *  cd /home/myuser/websocket_new && /usr/bin/python3 daily_digest_email.py >> logs/digest.log 2>&1
```

> 观察端(38.55.252.66)已于 2026-07-29 下线: 三条同步cron(scp K线/OI、rsync情绪、scp预测存档)已停用,
> 健康检查中的两端MD5一致性检查同步移除。现为生产端单端运行。

| 时间 | 脚本 | 作用 |
|------|------|------|
| 每分钟 | guardian.py | 进程存活+文件新鲜度+自动重启 (7/18重新启用) |
| 每天6:00 | daily_data_collection.py | 统一采集所有外部数据源 (含K线/OI缓存刷新) |
| 每天7:30 | update_klines_oi | 交易前K线/OI补采(冗余) |
| 每天8:05 | auto_dual_trade.py | 检查持仓→训练(946维/180窗/aligned)→预测(入场日)→交易 |
| 每天8:40 | cron_monitor.py --task 交易预测 | 检查8:05运行是否成功, 确认失败才重试一次 (7/27启用) |
| 每天9:00 | daily_digest_email.py | 晨报总览一封: 交易摘要+2日验证+强势股资金榜+健康检查(7/27合并, 原8:19日报/8:27强势股/8:30健康检查/9:00健康报告已停) |

---

## 5. 日志文件位置

| 日志 | 路径 | 内容 |
|------|------|------|
| 交易日志 | `~/.local/share/auto_trade/trade.log` | auto_dual_trade 所有操作记录 |
| cron交易日志 | `/home/myuser/websocket_new/logs/auto_dual.log` | cron运行的stdout/stderr |
| 采集日志 | `/home/myuser/websocket_new/logs/collect.log` | daily_data_collection 输出 |
| 守护日志 | `/tmp/guardian.log` | guardian 每分钟检查记录 |
| OI采集 | `/tmp/oi_collector.log` | OI实时采集记录 |
| 情绪采集 | `/tmp/sentiment.log` | 情绪数据实时采集 |
| Web服务 | `/tmp/web_server.log` | Flask app 日志 |

---

## 6. 已知限制与风险

### 6.1 API限制 (已修复 ✅)
- ~~Binance API -4120~~: 已切换至 Algo Order API (`POST /fapi/v1/algoOrder`)
- `algoType=CONDITIONAL` + `type=STOP_MARKET/TAKE_PROFIT_MARKET` + `triggerPrice`
- `closePosition=true` 全仓止损, `workingType=MARK_PRICE` 按标记价格触发, `priceProtect=true`
- ~~algoId 检查 bug~~ (6/2 修复): Algo Order 返回 `algoId` 但代码检查 `orderId`, 导致止损止盈永远判失败
- ~~自动回滚 bug~~ (6/2 修复): 止损止盈判失败后自动平仓, 白烧手续费; 改为标记裸仓不自动回滚
- ~~cancel_all_orders 漏 Algo 单~~ (6/2 修复): 平仓时不同步取消 Algo 条件单, 新增 `algoOpenOrders` 调用
- 不再需要手动补止损

### 6.2 模型偏差
- ~~标签时间错配~~: 7/18 aligned 已对齐 — 标签 = open[T]→close[T+2] ≈ 48h持仓窗口
- ~~入场滞后24h~~: 7/18 修复 — 预测样本 i=n-2→i=n-1 (特征=最新收盘蜡烛), 生产时序=回测时序 (180d回测: lag Sharpe -2.64 vs aligned +6.33)
- 训练数据历史窗口180天，市场结构变化可能导致模型失效
- Permutation Test 每天检测过拟合，按边阻断 (LONG/SHORT 独立)

### 6.3 资金门槛
- 最低余额 10 USDT 以下跳过交易（但仍更新全部数据缓存）
- 保证金阶梯从 5 USDT (钱包<25) 到 30 USDT (钱包≥400)
- 预留0.5%资金费缓冲

### 6.4 数据延迟
- 宏观数据（ETF/恐慌贪婪）通常T-1更新
- 链上数据依赖blockchair API，偶有延迟
- 清算数据为日级聚合，非实时
- Kronos特征为CPU推理(832维)，首次运行需10-20分钟

### 6.5 币种覆盖率
- 训练门槛 ≥400天日线历史，当前 ~341/528 币种达标 (65%)
- 新币种需要约13个月才能进入训练池

---

## 7. 日常运维检查清单

### 每天开盘前 (8:00前)
- [ ] screen 会话存活性: `screen -ls` (应有 oi_collector, sentiment, bc_collector)
- [ ] 数据文件新鲜度: `python3 daily_data_collection.py` 最后几行可查看
- [ ] K线缓存: `ls -lt backtester/data_cache/notusdt_1d_full.json` (应<26h)
- [ ] OI缓存: `ls -lt backtester/data_cache/oi_daily.json` (应<26h)
- [ ] COS上传: 检查 `klines/blockchair_data/` 等目录最新文件时间

### 交易后 (8:00后)
- [ ] 检查交易日志: `tail -50 ~/.local/share/auto_trade/trade.log`
- [ ] 检查cron日志: `tail -50 /tmp/auto_dual_cron.log`
- [ ] 检查Permutation Test: `cat ~/.local/share/auto_trade/permutation_test_log.json`
- [ ] 确认持仓: 查看Binance App持仓列表
- [ ] ~~手动补止损~~ (已自动化: Algo Order 止损止盈单自动挂载, 6/2 修复 algoId 检查)

### 每周
- [ ] 检查模型性能: 对比预测与实际收益
- [ ] 检查磁盘空间: `df -h` (日志和缓存可能膨胀)
- [ ] 检查guardian日志有无异常: `grep CRITICAL /tmp/guardian.log`
- [ ] 检查僵尸进程: `ps aux | grep defunct`

---

## 8. 紧急处理手册

### 场景1: 持仓裸奔（无止损保护）
```bash
cat ~/.local/share/auto_trade/state.json | python3 -m json.tool | grep -A5 naked
# Algo Order API 自动挂止损止盈，若失败会标记 naked: true
# 裸仓需在 Binance App 手动挂止损
```

### 场景2: screen会话僵死 (进程在但无输出)
```bash
# 查看screen状态
screen -ls
# 杀掉重建 (guardian下一分钟会自动重建，带.env)
screen -S oi_collector -X quit
screen -S sentiment -X quit
screen -S bc_collector -X quit
```

### 场景3: 数据大面积过期
```bash
# 检查cron是否运行
crontab -l
# 手动运行完整采集 (含K线+OI更新)
python3 /home/myuser/websocket_new/daily_data_collection.py
```

### 场景4: auto_dual_trade 卡死 (锁文件残留)
```bash
ls -la ~/.local/share/auto_trade/auto_dual.lock
# 如果卡死超过2小时，手动删除
rm ~/.local/share/auto_trade/auto_dual.lock
```

### 场景5: COS上传异常
```bash
# 验证COS连接
python3 -c "
from qcloud_cos import CosConfig, CosS3Client
# (需要先加载.env)
"
# 如凭证过期，更新 .env 中 COS_SECRET_ID/COS_SECRET_KEY
```

### 场景5b: 止损止盈单未挂上 (API返回algoId但本地记录失败)
```bash
# 检查最近一次交易日志
grep -A5 "止损单\|止盈单" ~/.local/share/auto_trade/trade.log | tail -20
# 如日志显示 algoId= 开头则是成功
# 如显示 [WARN] 止损+止盈均失败 则需手动挂单
```

### 场景6: Kronos模型加载失败
```bash
# 检查模型文件
ls -lh kronos_finetune/kronos_pretrained/Kronos-base/model.safetensors
ls -lh kronos_finetune/kronos_pretrained/Kronos-Tokenizer-base/model.safetensors
# 如缺失，需要重新下载或从 kronos_finetune.tar.gz 解压
```

---

## 9. 核心脚本清单

| 脚本 | 作用 | 触发方式 |
|------|------|----------|
| `auto_dual_trade.py` | 主交易脚本 (余额<10U时仅更新缓存) | cron 8:00 |
| `daily_data_collection.py` | 每日数据采集 + K线/OI缓存刷新 | cron 6:00 |
| `guardian.py` | 进程守护+文件检查 (含.env注入) | cron 每分钟 |
| `daily_predictor.py` | 特征工程+模型训练 (915维) | 被auto_dual_trade导入 |
| `dual_backtest_clean.py` | 做多+做空回测 | 手动 |
| `kronos_features.py` | Kronos-base 832维嵌入提取 | 被daily_predictor导入 |
| `utils/feature_builder.py` | 特征向量组装 (统一维度) | 被所有特征构建调用 |
| `oi_history_builder.py` | OI日级缓存重建 | 手动/按需 |
| `sector_fetcher.py` | 板块分类更新 | guardian守护 |
| `sector_heatmap.py` | 板块热力图计算 | guardian守护 |
| `sentiment_collector.py` | 情绪数据采集 (每小时) | guardian守护 |
| `fear_greed_collector.py` | 恐慌贪婪采集 | daily_data_collection调用 |
| `liquidation_heatmap.py` | 清算数据采集 | daily_data_collection调用 |
| `collect_tvl.py` | 6链TVL数据采集 | daily_data_collection调用 |
| `collect_btc_mcap.py` | BTC市值采集 | daily_data_collection调用 |
| `collect_btc_dominance.py` | BTC市占率采集 | daily_data_collection调用 |
| `collect_macro_assets.py` | SP500/DXY/黄金采集 | daily_data_collection调用 |
| `blockchair_collector.py` | BTC链上实时数据 | guardian守护(screen) |
| `hashrate_data/collector.py` | BTC算力采集 | guardian守护(文件新鲜度) |
| `stablecoin_data/monitor.py` | 稳定币+溢价监控 | guardian守护(文件新鲜度) |
| `openclaw.../fetch_etf.py` | ETF资金流采集 | daily_data_collection调用 |

---

## 10. 完整数据流 (更新后)

```
每天 6:00  daily_data_collection.py
  ├─ update_klines_oi()            → 刷新K线缓存 + OI缓存 (fcntl锁)
  ├─ fear_greed_collector.py       → 恐慌贪婪
  ├─ collect_btc_mcap.py           → BTC市值
  ├─ collect_btc_dominance.py      → BTC市占率
  ├─ collect_macro_assets.py       → SP500/DXY/黄金
  ├─ collect_tvl.py                → 6链TVL
  ├─ liquidation_heatmap.py        → 清算热力图
  ├─ fetch_etf.py                  → ETF资金流
  ├─ monitor.py                    → 稳定币+溢价
  └─ 文件复制 /tmp/* → data/

每天 8:05  auto_dual_trade.py
  ├─ 检查持仓 (止损-10% / 48h到期)
  ├─ 全量加载K线缓存 (532币) → 过滤 ≥90天 → ~525币
  ├─ 加载OI + 宏观 + 板块 (Kronos已禁用置零)
  ├─ 余额≥10U?
  │   ├─ YES → 训练XGBoost(944维/180天窗/aligned标签) → Permutation Test → 预测(入场日当天) → 交易
  │   └─ NO  → 跳过交易 (数据已刷新)
  └─ 下单 + Algo止损/止盈 (-4130冲突时裸仓记录, 用户手动挂止损)

每分钟   guardian.py
  ├─ screen会话存活检查 (先kill旧session再建新)
  ├─ 文件新鲜度检查 (8项, 各有时效阈值)
  ├─ 过载保护 (每小时最多重启5次)
  └─ 状态输出 /tmp/guardian_status.json
```

## 11. COS 云端备份清单

| COS 路径 | 内容 | 频率 | 采集器 |
|------|------|------|------|
| `klines/cache/notusdt_1d_full.json` | K线缓存 (532币日线含n/tbq, 44.7MB, 7/18已修复OHLC) | 每天 6:00 | daily_data_collection |
| `klines/cache/oi_daily.json` | OI缓存 (531币日级, 0.4MB) | 每天 6:00 | daily_data_collection |
| `klines/bootstrap/*` | 部署种子快照 (current_params/btc_chain.csv/liq_daily/liq_levels_daily) | 手动(变动时) | bootstrap (7/18新增) |
| `klines/blockchair_data/YYYYMMDD/` | BTC链上数据 (CDD/交易量/Mempool) | 每小时 | blockchair_collector |
| `klines/sentiment_data/YYYYMMDD/` | 情绪数据 (费率/多空比) | 每小时 | sentiment_collector |
| `klines/oi_data/YYYYMMDD/` | OI持仓数据 (实时) | 每5分钟 | oi_collector |
| `klines/long_short_data/YYYYMMDD/` | 多空比数据 (实时) | 每5分钟 | oi_collector |
| `klines/liquidation_heatmap/latest.json` | 清算热力图 (最新) | 每天 | liquidation_heatmap |
| `klines/coingecko_data/btc_mcap.json` | BTC市值 | 每天 | collect_btc_mcap |
| `klines/coingecko_data/btc_dominance.json` | BTC市占率 | 每天 | collect_btc_dominance |
| `klines/macro_assets/macro_assets.json` | 宏观资产 (SP500/DXY/黄金) | 每天 | collect_macro_assets |
| `klines/defillama_data/*_tvl.json` | 6链TVL | 每天 | collect_tvl |
| `klines/etf_data/etf_flow.json` | ETF资金流 | 每天 | fetch_etf |
| `klines/stablecoin_data/*.json` | 稳定币净流入/Coinbase溢价/Gap | 每天 | monitor.py |
| `klines/hashrate_data/hashrate_daily.csv` | BTC算力 | 每天 | hashrate/collector |
| `klines/sector_data/crypto_sectors.json` | 板块分类 | 每天 | sector_fetcher |
| `klines/sector_heatmap/sector_heatmap.json` | 板块热力图 | 每天 | sector_heatmap |
| `klines/fear_greed_history.json` | 恐慌贪婪指数 | 每天 | fear_greed_collector |

> 共 **17 个 COS 路径**，覆盖全部本地数据。验证命令: 读取 COS bucket 按 LastModified 排序取最新。
> 7/18 新增 `deploy/cos_paths.json` (部署路径清单) 与 `deploy/bootstrap_from_cos.py` (一键拉取), 部署流程见 `DEPLOY.md`。
> 注意: `klines/cache/kronos_features_cache.json` 自 Kronos 禁用后停更。

---

## 12. 历史版本记录

| 日期 | 事件 |
|------|------|
| 2026-05-21 | 完成auto_dual_trade.py审计，修复6个Critical + 6个High bug |
| 2026-05-21 | 干净回测: Sharpe=24.01 (随机-1.78, 总是做多-2.50) |
| 2026-05-21 | 全528币种重新训练，102维特征 |
| 2026-05-22 | 修复数据采集系统（guardian僵死5天，全部补采） |
| 2026-05-22 | 修复算力收集器CSV→JSON转换 |
| 2026-05-22 | 修复API -4120导致的回滚灾难bug |
| 2026-05-22 | 更新板块分类文件到最新 |
| **2026-05-29** | **Reasonix Code 全面审计+修复 (30+项)** |
| 2026-05-29 | TRAIN_DAYS 500→365, 训练币种 107→341 (覆盖率20%→65%) |
| 2026-05-29 | 移除24h行情筛选, 改为K线缓存直接全量加载 |
| 2026-05-29 | daily_data_collection.py 新增 K线+OI 缓存自动刷新 |
| 2026-05-29 | auto_dual_trade.py 余额不足时仍更新数据缓存 |
| 2026-05-29 | guardian.py 修复: .env注入 + screen旧会话清理 + 无孤儿泄漏 |
| 2026-05-29 | 修复 volatility=0 → 0.02 (6处), safe_float NaN, 418重试 |
| 2026-05-29 | 修复 孤儿仓位回写、OI拉取[:20]限制、K线缓存竞态 |
| 2026-05-29 | 修复 特征维度断言、history归档、TVL偏移验证 |
| 2026-05-29 | 修复 平仓轮询指数退避、数据加载逐文件异常处理 |
| 2026-05-29 | 特性: 915维特征 (Kronos 832D), Permutation Test过拟合保护 |
| 2026-05-29 | COS全量备份验证通过 (16个路径，含K线31.6MB+OI 0.4MB) |
| 2026-05-29 | 系统恢复运行: K线补3531条, 数据新鲜度全部<1h |
| 2026-05-29 | 修复止损/止盈接口: /fapi/v1/order → /fapi/v1/algoOrder (4120已解决) |
| **2026-06-02** | **系统维护: 性能优化 + Bug修复 + 回测验证** |
| 2026-06-02 | 性能: `_fast_winsor_bounds` — np.partition (QuickSelect O(n)) 替代 np.percentile (O(n log n)), 15-20x 加速 |
| 2026-06-02 | 修复: `auto_dual_trade.py` algoId 检查 — Algo Order 返回 `algoId` 但代码检查 `orderId`, 止损止盈永远判失败 |
| 2026-06-02 | 修复: 止损止盈均失败时不再自动回滚平仓 (API 挂了也不该自动平仓烧手续费) |
| 2026-06-02 | 修复: `cancel_all_orders` 新增 `/fapi/v1/algoOpenOrders` 调用, 平仓时同时取消 Algo 条件单 |
| 2026-06-02 | 修复: `liquidation_heatmap.py` liq_daily.json 历史保存 — `isinstance(history, list)` 防御 dict 覆写 |
| 2026-06-02 | 修复: `sector_heatmap.py` 板块热力图 15 天未更新 — guardian 恢复 file_age 检查 + 脚本 TimeoutError 不再吞结果 |
| 2026-06-02 | 回测: clean 365d — 真止损止盈, +549%/Sharpe 4.75/胜率 63.5%, 见 `docs/backtest_results_2026-06-02.md` |
| 2026-06-02 | 回测: 原始 dual_backtest 365d — 伪止损, +348%/Sharpe 1.80, 旧版无阈值版 +3557% (不可信) |
| 2026-06-02 | 更新: 持仓通过 Binance API 实时同步 (state.json), 旧 4 笔已全部平仓 |
| 2026-06-03 | 钱包余额 17.03 USDT, 满足 10U 门槛, 系统可正常交易 |
| 2026-06-03 | 当前持仓: LITUSDT LONG x61.4 @1.7769 (20x杠杆, 手动开仓, 名义109U, PnL -0.43U) |
| 2026-06-03 | 无挂单; 数据全部新鲜 (6/3 06:00 采集); Kronos 缓存 5/31 需增量补算 |
| 2026-06-04 | 修复: `open_ts=None` 崩溃 (`or 0` 防御) + 市价单 `NEW` 状态轮询确认 (开仓/平仓/止损 3处联动) |
| 2026-06-04 | 新增: 币种流动性评分体系 (4维度) — §13, 含断档分析 + 动态排名稳定性 + 阈值筛选方案 |
| 2026-06-04 | 新增: 订单簿深度数据方案 — §14, 含币安 API 调研 + 5个可提取特征 + 采集方案 |
| 2026-06-06 | 回测: GPU 365天流动性筛选 3 次对比（90天/365天/365天+Kronos），全败 |
| 2026-06-06 | 结论: 流动性筛选在所有测试中劣于纯量排序，暂不接入生产。详见 §13.6 |
| **2026-06-06** | **GPU 回测 + 生产维护 + 规则制度化** |
| 2026-06-06 | 修复: `LITUSDT_LONG` 状态文件 `open_ts=None` 脏数据 → 保守估计回填 |
| 2026-06-06 | 修复: 市价单 `status:NEW` 确认 — `close_with_retry` + `monitor_stop_loss.py` 两处联动 |
| 2026-06-06 | 同步: PORTALUSDT 手动仓位纳入系统管理，挂止损止盈 (algoId: 2000001047630079/87) |
| 2026-06-06 | 运维: GPU MCP 端口 24220→22193 更新 `gpu_mcp_proxy.py`；GPU 服务器数据文件全量同步 |
| 2026-06-06 | 规则: `production-change-gate` — 生产改动必须 365 天回测 + 用户审核 |
| 2026-06-06 | 规则: `production-file-boundary` — 🔴核心/🟡支撑/🟢实验/⚪非生产 四级文件边界 |
| 2026-06-06 | 回测: GPU 365天流动性筛选 3 次对比（90天/365天/365天+Kronos），全败 |
| 2026-06-06 | 结论: 流动性筛选在所有测试中劣于纯量排序，暂不接入生产。详见 §13.6 |
| 2026-06-06 | 新增: GPU 回测环境踩坑记录 — §15 |
| 2026-06-06 | 新增: `experiments/liquidity_kronos_bt.py` — GPU 专用 Kronos 回测脚本（926维/12核/CUDA） |
| 2026-06-06 | 新增: `auto_dual_trade.py` 每日持仓同步 — 币安无持仓则清理 state.json 过期记录 |
| **2026-07-18** | **规则变更: `production-change-gate` 回测窗口 365天 → 180天** (180天训练窗口已验证最优, 用户批准; 回测+用户审核不变) |
| 2026-07-18 | 重大修复: K线OHLC冻结bug — 5/下旬以来日更"只追加不刷新", 蜡烛冻结在开盘5分钟 (BTC 6/1收盘偏差3.17%), 污染近2月训练标签+价格特征; 已全字段修复+日更改"追加+刷新末根蜡烛" |
| 2026-07-18 | 修复: 情绪数据6/22起断供25天(SHORT模型top特征) — 币安历史接口回填609小时文件并恢复采集; guardian cron重新启用 |
| 2026-07-18 | 修复: 7/12板块标签修复被/tmp→data/日更复制回退 — 恢复修复版+`data/sector_overrides.json`覆盖层防再回退; TVL采集并行化(30s超时→6.3s) |
| 2026-07-18 | volfeat生产接入: 成交笔数n+主动买入额tbq全量回填(96.9%), 特征942→944维 (tr_ratio笔数量比+tbr主动买卖比, 末两位) |
| 2026-07-18 | **实验(v5三臂180天WF)**: 生产入场时序比回测滞后24h (预测样本i=n-2/特征蜡烛D-2, 回测是特征蜡烛D-1即入场) — lag Sharpe -2.64/Cum -211%/MaxDD 95.8% vs nolag 4.27/+346%/54.3% vs aligned(标签对齐入场open→T+2close + ab改prev_date) 6.33/+469.6%/46.5%; 365d验证前200天lag -45%同样成立(用户决策以180d为准, 提前终止) |
| 2026-07-18 | **aligned全量上线生产** (用户审核通过): 预测样本i=n-1(特征=最新收盘蜡烛), 标签open[T]→close[T+2], ab改prev_date, 2日验证入场基准改开盘价; 观察端同步(MD5一致) |
| 2026-07-18 | Git: 提交5668355并推送GitHub — 原Xgboot仓库已被删, 新建私有仓库 `rainbow3r1u/Xgboot` (账号下另一仓库3.96SHARPE为无关历史, 由3.96SHARPE_repo/维护) |
| 2026-07-18 | 部署体系: 新增 `deploy/cos_paths.json` + `deploy/bootstrap_from_cos.py` + `DEPLOY.md`; COS补齐(回填情绪621个/种子4个/修复版K线缓存+板块标签); 新机器部署=clone→.env→COS拉取→依赖→cron |
| 2026-07-18 | 观察端修复: 情绪数据同步1682文件(同样断供25天) + 生产端新增7:36 rsync每日推送 |
| 2026-07-18 | 实验(BB特征包, aligned+乖离率/%B/带宽 947维, 180d): Sharpe 5.89 vs 基线6.33, MaxDD 57.6% vs 46.5% — **冗余, 不接入** (与price_position/vol_20d重复, 止损34→40次) |
| 2026-07-18 | 验证(aligned 90天WF, 与v3历史同窗口): Sharpe 8.78/Cum +321.8%/MaxDD 28.3%/胜率71% — 与v3 90d成绩(9.10/+294.2%/23.0%)同水平, 确认aligned未透支v3 (窗口4/16→7/14 vs 4/11→7/9) |
| 2026-07-18 | **地球版 1.1 上线**: +vol_raw 原始成交额q特征 (944→945维) — 180d Sharpe 6.33→**8.13**/Cum +610.8%/MaxDD 46.5%→**36.1%**/胜率69%; 用户提案当日回测通过即接入, 08:05首跑 |
| 2026-07-19 | 策略调整(用户): SHORT改多仓模式(过阈值全开, 每笔10U→5U, 日上限5笔), LONG维持TOP1; 杠杆20x→10x; 日报验证板块改版(SHORT展开明细); 决策依据中文特征名; 新上市币自动补入缓存(542币); 中文Meme误杀修正(币安人生/龙虾/我踏马来了恢复Meme标签) |
| 2026-07-20 | **地球版 1.2 上线**: +fund_raw 单币资金费率原值 (945→946维, 样本日前最近8h结算无前视) — 180d Sharpe 8.13→**8.62**/Cum +636.5%/MaxDD→40.3%/胜率69%; 费率数据管道(ddc每日增量+新币全历史) |
| 2026-07-20 | **地球版 1.3 上线**: LONG动量过滤 (特征蜡烛连涨≥2天+20日位置>0.7, SHORT不动) — 180d Sharpe **9.00**/MaxDD **28.3%**/胜率72%, 全维度最优; 用户观察"LONG只选底部币"驱动 |
| 2026-07-20 | 三分支证伪归档: B(1日标签/24h持仓) Sharpe 7.50 不差但输给1.3不换; C(Kronos 832D复测) Sharpe 5.74 明确负贡献, 永久关闭 |
| 2026-07-20 | **止损 -10%→-5% 上线**: 退出规则实验(7天70笔+23天208笔, -5%优于-10%) + 全系统180d回测(Sharpe 9.00→**9.82**, MaxDD 28.3%→34.0%, 胜率72%→64%) — Sharpe提升即接入; 邮件验证口径同步读配置(三口径一致), 历史记录按-5%重建 |
| 2026-07-20 | 实验(训练侧动量模型 TRAIN_MOM_LONG, 只用动量样本训练): Sharpe 9.00/MaxDD **23.8%(史上最浅)** vs 闸门侧 9.82/34.0% — **闸门侧收益更优, 训练侧回撤更浅**; 决策: 保持1.3闸门侧, 训练侧作低回撤备选, 一周后看LONG实盘战绩再定 (GPU坑: 子集训练触发CUDA dense断言, 改CPU解决) |
| 2026-07-18 | 实验(4h分辨率版, 与日线同构仅K线换4h): Sharpe 1.66/Cum +124.7%/MaxDD 62.7% vs 日线8.13/36.1% — **4h预测无效, alpha为日线尺度**; 同日Rule B证伪实验(4h限价等1%回踩≤24h): Sharpe 8.13→**9.97**, 止损36→22, 胜率69→72% — **4h数据用于执行有效, 用于预测无效** |
| 2026-07-27 | 邮件体系合并: 4封邮件(8:19日报/8:27强势股/8:30健康检查/9:00健康报告)合并为9:00晨报总览(daily_digest_email.py, 信息无删减); 强势股板块加成交额降序+成交额列+连续2日≥5%🔥标记; 8:40 cron_monitor交易失败自动重试启用(确认失败才动手) |
| 2026-07-29 | 修复: 预测存档概率被`json default=str`序列化成字符串(numpy标量) — float()强转, 强势股邮件'>='报错修复; 强势股板块首次有真实概率(0看涨, 与LONG偏弱一致) |
| 2026-07-29 | **观察端(38.55.252.66)下线**: 三条同步cron停用(scp K线/OI、rsync情绪、scp预测存档), 健康检查两端MD5一致性检查移除(crypto_sectors.json系两端各自采集的合法不一致, 每日误报) — 现为生产端单端运行 |
| 2026-07-30 | **GPU实验夜(分支台账批量证伪)**: 基线9.82精确复现; RANK排序目标❌(2.51)/时间衰减权重❌(3.91)/DART❌(6.24)/续涨专用模型❌(P@10 52.0%通用vs51.5%专用); **重大发现: winsor 1%/99%截尾压制动量特征右尾(LONG-only 41.55→5.38, 100%归因, 成交量过滤零影响)**; 全系统去截尾 Sharpe **35.57**(Cum+1453%/MaxDD10.7%/胜率91%), 换时段32.82/26.06复核, 0.1%/99.9%温和档32.30, 成本1%臂33.36; 去截尾后动量闸门变负贡献(无闸门44.57); 🔥连涨2日续涨率24.4%(n=3862) vs 非pump 20.3% |
| 2026-07-30 | **地球版1.4候选进入一周实盘观察期(7/30~8/6)**: 生产winsor改0.1%/99.9%(单点`daily_predictor._fast_winsor_bounds`), TRADING_ENABLED保持false只预测, 晨报2日验证板块作前向记录; 观察期满评估: 前向命中率/止盈率 vs 回测91%, 达标才定版1.4并评估开仓 |
| 2026-07-30 | **LONG动量闸门摘除(生产上线, LONG_MOM_FILTER=false)**: 止盈/止损单画像对比 — 止盈单71%系沉睡爆发(入场前5日累计<5%, 中位-3.4%, 入场日中位+26%), 止损单系追涨接盘(前5日中位+18%); 特征贡献揭示LONG内核=对BTC残差(脱β异动探测, 贡献+2.53十倍于次席), vol_raw直接贡献仅4%; 月度胜率86~100%六个月无衰减; 概率饱和regime确认(0.9~1.0), 强势股邮件判定改排名制 |
| 2026-07-30 | GPU补充复核: 深度OOS off800(2023) Sharpe 23.82 / off1200(2022熊市) **12.25 仍高于基线9.82**; soup新regime复测 37.32(叠加有效); rawr原始涨幅❌(32.03无增益); NaN保留❌(32.3零差异); prune死列清零=基线(纯卫生); 混合压力臂(成本2%+滑点) 26.45; mk35新币宇宙无额外增益; 预测公证cron上线(8:20自动提交GitHub, 预测先于结果) |
| 2026-07-30 | **SOUP时间集成上线生产(SOUP_ON=true)**: 模型按日期轮换保留4天, 预测=今日+最近2日模型概率平均(消每日重训抖动), 不足3个自动降级; **完全体(温和档+无闸门+SOUP)回测 Sharpe 44.57/Cum+1535%/MaxDD10.7%/胜率93%**; TP阶梯实验: TP10=42.45/TP15=47.00/**TP20=49.39(峰值)**/TP30=37.64(过峰衰减) — TP20列下轮最大候选, 观察期保持TP10 |
| 2026-07-30 | **地基敌意审计(用户要求"就当它是假的查到底")**: 11条泄漏/前视通道全部排除 — >500%标签过滤仅11/312299样本(0.0035%, 且剔除的是未来大赢单方向相反); 币龄错配3笔/1.7%; 逐笔蜡烛复盘(入场价/特征日/盈亏路径全部正确); **缓存vs币安官方API对质7/7逐根一致**; 入场不利漂移1%/2% Sharpe 42.27/40.22(磨掉≤2点); 宏观全prev_date/板块热度双重后顾/无现役预训练组件(Kronos置零)。**残留风险: ①评估窗选择偏差(~25个实验同窗择优, 前向周解决) ②regime依赖(2022档12.25) ③幸存者偏差(退市币不在宇宙)** |
| 2026-07-30 | 特征家族实验: 残差家族(量残差/res_3d/res_5d)+0.95微升; **量能见顶家族❌反降5.3**(colsample稀释主传感器); SHORT贡献分析: 残差主导+量权重6倍于LONG, 止损单与止盈单签名一致(选币没错, 方向没延续); TP20换时段off180=40.93(MaxDD升至20.3%) |
| 2026-07-30 | **阴性对照(管道照妖镜)**: 特征故意前移1天 → Sharpe **161.36**/胜率**99.4%**/MaxDD5.5% — 时序梯度(lag -2.64 → aligned 44.57 → fshift 161.36)陡峭单调, **管道对时序反应符合理论, 91%非泄漏产物**; 深度OOS 2021时代: off1800(2021-02~08牛市) 13.39/70%, off1600(2021-08~2022-02见顶) 11.76/68% — **五年七窗全部高于生产基线9.82**, alpha跨时代结构性存在, 梯度主因=传感器装备率+宇宙大小+市场结构, 实盘期望修正为Sharpe 23~33/胜率80~88%档 |
| 2026-07-30 | 背离家族❌(EXT+DIV 39.63 vs EXT 43.40); TP20成本臂 47.97(成本1%下仍近峰); 入场漂移臂 42.27(1%)/40.22(2%) |
| 2026-07-31 | 新采集体系: CoinGecko市值/流通量(两阶段97%匹配+改名映射) + 市值历史回填(免API近似法, 524币全历史) + exchangeInfo(696合约onboardDate/板块) + 每日宇宙快照(Earth-Guard⑥); **修复Perm Test与SOUP不一致(用户发现MMT/ON分叉)**: 调用点下移至SOUP平均后+probs_override传入集成概率, 恢复Perm Best=交易Best不变量; 顺带修复早退路径return True解包隐患(b5b0e0a) |

---

## 13-14. (已清理, 2026-07-18 定版瘦身)

> 原「§13 币种流动性筛选」(实验死胡同 — 3次回测均劣于纯量排序, 不接入) 与「§14 订单簿深度」(未实施的规划) 已删除; 结论保留于 §12 历史表。
## 15. GPU 回测环境踩坑记录

> 记录日期: 2026-06-06 | GPU: RTX 3080 (20GB) + 12核 CPU + 31GB RAM @ 175.155.64.171:22193

### 15.1 XGBoost GPU 参数

**正确写法 (XGBoost ≥ 2.x):**
```python
XGBClassifier(device='cuda', n_jobs=12, ...)
```

**错误写法 (已废弃):**
```python
XGBClassifier(tree_method='gpu_hist', ...)  # 3.x 移除此参数
```

### 15.2 特征维度对齐

手动构建特征向量时，各数据缓存返回的默认值维度不一致：

| 缓存 | 预期维度 | 实际可能返回 | 修复 |
|------|:--:|------|------|
| `_fg_features` | 1 | `0` (scalar) | `_as_list(v, n)` 函数补零到 n |
| `_st_features` | 3 | `[1]` (len=1) | 同上 |
| `_bd_features` | 3 | `0` (scalar) | 同上 |

**修复函数:**
```python
def _as_list(v, n=0):
    if isinstance(v, list):
        if len(v) == n: return v
        return (v + [0]*(n-len(v)))[:n]
    if isinstance(v, (int, float)): return [float(v)] + [0.0]*(n-1)
    return [0.0]*n
```

### 15.3 Kronos 缓存变量名

- `dp._kr_features` — 全局缓存字典，✅ 正确
- `dp._kronos_cache` — 不存在，❌ 错误

### 15.4 GPU 服务器数据文件依赖

回测需要 GPU 服务器本地有完整数据文件，不能只靠 K 线缓存：

| 必须同步的目录 | 大小 |
|------|------|
| `defillama_data/` | 8.5MB |
| `sentiment_data/` | 600KB |
| `blockchair_data/` | 7.3MB |
| `coingecko_data/` | 54KB |
| `stablecoin_data/` | 787KB |
| `hashrate_data/` | 147KB |
| `websocket_new/data/{liq_daily,fear_greed,macro_assets,crypto_sectors}.json` | 327KB |

同步命令：
```bash
rsync -avz -e "ssh -p 22193" /home/myuser/<dir>/ root@175.155.64.171:/root/<dir>/
```

### 15.5 进程管理

- **不要直接 SSH 运行长时间脚本** — SSH 管道断开会杀进程
- 使用 `nohup python3 -u script.py > /tmp/log.log 2>&1 &`
- 清理僵尸进程：`pkill -f script_name`
- GPU 服务器 IP:端口变更后需同步更新 `gpu_mcp_proxy.py` 和 `.reasonix/config.json`

### 15.6 GPU 利用率

XGBoost 3.2.0 + CUDA 12.4 工作正常，但利用率不高（~11%）：
- GPU 训练是脉冲式的（每轮几秒满负载，然后 CPU 准备数据）
- 3080 20GB 显存绰绰有余（实际仅用 271MB）
- 真正的瓶颈在 Python 单线程样本构建（GIL），非 GPU

### 15.7 回测脚本位置

```
GPU: /root/reasonix-projects/websocket_new/experiments/liquidity_kronos_bt.py
日志: /tmp/liq_bt.log
结果: /root/reasonix-projects/websocket_new/data/liquidity_kronos_bt.json
```

---

*本文档由 Reasonix Code 更新，用于系统维护和知识传承。*

### 会话记录
| 日期 | 文档 |
|------|------|
| 6/1-2 | `docs/session_20260601_context.md` — Kronos 维度筛选实验 |
| 6/2-3 | `docs/session_20260603_context.md` — Bug修复+回测+系统维护 |
| 6/11-12 | `docs/session_20260611_context.md` — 全链路数据修复+清算100层+韩国溢价恢复 |

---

## 16. 2026-06-11/12 全链路数据修复记录

### 16.1 COS 训练产物上传滞后

**问题**: `train_data_latest.npz` / `xgb_daily_model.pkl` / `kronos_importance_log.json` 在 COS 上停在 5/31，未随每日训练更新。

**修复**: `auto_dual_trade.py` 在 `train_and_predict()` 返回后新增 COS 备份块，每次训练自动上传多头/空头模型、训练数据、重要性日志到 COS。

### 16.2 ETF 数据不累积

**问题**: `fetch_etf.py` 每次全量覆写 `etf_flow.json`，只保留 farside.co.uk 当前展示的 ~2周数据。

**修复**: 改为追加累积模式：读旧文件 → 合并新数据(按日期去重) → 写回。farside.co.uk 源站限制约 2-3 周，但不再丢失已有数据。

### 16.3 情绪数据人为限制 [-90:]

**问题**: `_load_sent_features()` 只读最近 90 个文件（≈4天），丢弃了 814 个文件中的大部分。

**修复**: `daily_predictor.py` 去掉 `[-90:]` 切片，全部文件参与日聚合。覆盖天数 35 天（5/8 起采集）。

### 16.4 OI 缓存不累积 + 只更新 50 币

**问题 A**: `oi_data[s] = d` 每次整条替换，Binance API 只返回 31 天 → 永远只有 31 天。
**问题 B**: `[:50]` 限制每天只更新 50 个币种 → 531 币需 11 天轮转。

**修复 A**: `daily_data_collection.py` 改为 `oi_data[s].update(d)` 合并新时间戳。
**修复 B**: 去掉 `[:50]` 限制，全量 531 币种每日更新（约 531×2 weight，安全）。

**Binance API 限制**: `openInterestHist` 只返回约 31 天，上限无法突破。修复后每天 +1 天累积。

### 16.5 韩国溢价恢复

**问题**: 代码注释 "无采集脚本，数据滞后8天" → `kg = [0.0]` 硬编码禁用。数据文件 5/15 后停止更新。

**修复 A**: `monitor.py` 添加 `btc_korea_premium_index.json` 到采集列表（GitHub 数据源，1286 天历史）。
**修复 B**: `daily_predictor.py` 启用 `kg = _kg_features.get(prev_date, [0.0])`。

数据源: `https://raw.githubusercontent.com/ErcinDedeoglu/crypto-market-data/main/data/daily/`

### 16.6 清算特征升级：7维 → 26维（100层×24h）

**原问题**: `liq_daily.json` 只存 7 个汇总统计值（总多/空清算额、比率、峰值距离等），从 Bitfinex 校准 + 高斯模型产出的 100 层清算分布中丢失了大量信息。

**新设计**:
1. `liquidation_heatmap.py` — 每小时追加完整 100 层快照到 `liq_levels_daily.json`（按日期+小时去重，原子写入，损坏恢复）
2. `daily_predictor.py` — 新增 `_extract_level_features()` 从每层提取13维基特征（5分位×2 多空占比 + 比率 + 峰值位置）；`_load_liquidation_features()` 聚合24小时 → 每特征 mean+std = 26维
3. `auto_dual_trade.py` — 维度 914→933

**100层特征含义**:
| 新特征 | 含义 |
|--------|------|
| Q1~Q5 long/short mean | 5个价格区间的多空清算占比（均值） |
| Q1~Q5 long/short std | 占比的日内波动 |
| ratio_ls mean/std | 多空清算总额比的均值和波动 |
| peak_l_pos mean/std | 多头清算峰位置的均值和波动 |
| peak_s_pos mean/std | 空头清算峰位置的均值和波动 |

**边界防护**: 同小时去重、日期/小时校验、levels完整性验证、JSON损坏恢复、原子写入(.tmp→rename)、每日期上限24个快照、90天滑动窗口、特征值范围校验、回退到旧 liq_daily.json。

### 16.7 特征维度演进

```
最初: 914维
  +6 (清算7→13): 920维
  +13 (清算13→26, mean+std): 933维
```

### 16.8 Binance API 调用量审计

完整审计发现系统每天约 85 万次 Binance API 调用（含 market_monitor_app 的 ~688k 次），但峰值远低于限制:

| 限制项 | 峰值用量 | 占比 |
|--------|:--:|:--:|
| Spot 1200 weight/min | 750 | 63% |
| Futures 2400 weight/min | 633 | 26% |
| 50 req/s | 26 | 52% |

OI 全量 531 币更新安全；K线每天被 daily_collection 和 auto_dual_trade 各拉一次（2小时间隔）。

### 16.9 修改文件清单

| 文件 | 修改内容 |
|------|------|
| `auto_dual_trade.py` | COS备份块, 维度914→933 |
| `daily_predictor.py` | 情绪去[-90:], 韩国溢价恢复, 清算100层→26维 |
| `daily_data_collection.py` | OI合并累积, 去[:50]限制 |
| `fetch_etf.py` (openclaw) | 追加累积模式 |
| `monitor.py` (stablecoin) | 添加韩国溢价采集+COS |
| `liquidation_heatmap.py` | 100层快照保存+边界防护 |

### 16.10 训练窗口放大：365天 → 全量

**问题**: `TRAIN_DAYS=365` 限制了 XGBoost 只能用最近 365 天训练，但韩国溢价(1286天)、稳定币(1286天)、CB溢价(1285天)、算力(1646天)、TVL(~2000天)、跨资产(1255天)、K线(~2000天) 都有大量可用历史被浪费。

**修复**:
| 位置 | 改前 | 改后 |
|------|:--:|:--:|
| `auto_dual_trade.py` 默认值 | `TRAIN_DAYS: 365` | `9999` |
| `current_params.json` | `TRAIN_DAYS: 365` | `9999` |
| `min_required` | `TRAIN_DAYS + 35` | `400` (解耦) |

`TRAIN_DAYS=9999` → `train_days[-9999:]` 取全部可用天数。`min_required` 解耦为固定 400 天（币种最低 K 线门槛），不再受 `TRAIN_DAYS` 影响。

**影响**: 训练样本 ~128k → ~180-200k，训练时间 +2-3 分钟。过拟合风险低（XGBoost max_depth=5 强正则 + Permutation Test 兜底）。

**版本号修正**: 日志标题 "78维完整版 FIXED" → "933维"。

### 16.11 修改文件清单（完整）

| 文件 | 修改内容 |
|------|------|
| `auto_dual_trade.py` | COS备份, 维度914→933, TRAIN_DAYS→全量, 版本号修正 |
| `daily_predictor.py` | 情绪去[-90:], 韩国溢价恢复, 清算100层→26维 |
| `daily_data_collection.py` | OI合并累积, 去[:50]限制 |
| `fetch_etf.py` (openclaw) | 追加累积模式 |
| `monitor.py` (stablecoin) | 添加韩国溢价采集+COS |
| `liquidation_heatmap.py` | 100层快照保存+边界防护 |
| `current_params.json` | TRAIN_DAYS: 365→9999 |

### 16.12 TVL 链扩展：6→9 链

新增 TON/Sui/Polygon 三条链的 TVL 采集，对应板块映射：

| 链 | DeFiLlama ID | 板块映射 | 说明 |
|------|------|------|------|
| TON | `ton` | TON生态 | 精确匹配，130+ 币种 |
| Sui | `sui` | L1 | SUIUSDT 等 L1 标签币种 |
| Polygon | `polygon` | L2 | MATIC/POL 等 L2 标签币种 |

修改: `collect_tvl.py` (CHAINS), `daily_predictor.py` (CHAIN_TVL_MAP, TVL_FEATURE_COUNT 6→9), `auto_dual_trade.py` (维度 933→936).

### 16.13 Git 本地版本管理

**仓库位置**: `/home/myuser/websocket_new/` (remote: `xgboot` → `github.com/rainbow3r1u/earth-1.0`, 私有; 2026-07-18 由 Xgboot 改名为 **地球版 1.0** — GitHub 不支持中文名, 用英文代号, 旧 URL 自动重定向)

**纳入追踪**:
- 所有生产/采集/回测脚本
- 数据文件: `liq_daily.json`, `liq_levels_daily.json`, `fear_greed_history.json`, `crypto_sectors.json`, `sector_overrides.json`, `macro_assets.json`, `sector_heatmap.json`, `liquidation_heatmap.json`, `winsor_bounds_clean_full.json`, `pred_*.json`
- 文档: `SYSTEM_OVERVIEW.md`, `DEPLOY.md`, `docs/`, `EXTERNAL_FILES.md`
- 部署: `deploy/cos_paths.json`, `deploy/bootstrap_from_cos.py`
- 配置: `.gitignore`

**排除** (`.gitignore`):
- 大缓存: `kronos_features_cache.json` (36MB, 可重算), `hourly_backfill.json` (50MB)
- 密钥: `.env`
- 旧模型/回测结果
- K线缓存 (44.7MB, `notusdt_1d_full.json` — 从COS拉取, 见DEPLOY.md)
- 运行日志: `logs/`

**外部文件** (不在仓库根目录下，记录于 `EXTERNAL_FILES.md`):
| 文件 | 用途 |
|------|------|
| `../backtester/config/current_params.json` | 策略参数 |
| `../stablecoin_data/monitor.py` | 稳定币采集 |
| `../openclaw-.../etf_data/fetch_etf.py` | ETF采集 |
| `../gpu_mcp_proxy.py` | GPU代理 |
| `~/.local/share/auto_trade/` | 模型/状态/日志 |

**常用命令**:
```bash
cd /home/myuser/websocket_new
git diff                    # 看未提交改动
git status                  # 看文件状态
git log --oneline -10       # 看提交历史
git checkout -- <file>      # 放弃单个文件改动
git stash                   # 暂存改动
```
