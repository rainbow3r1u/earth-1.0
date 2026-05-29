# 加密货币ML自动交易系统 — 系统全景文档

> 最后更新: 2026-05-29 (Reasonix Code 全面审计+修复)
> 适用目录: `/home/myuser/websocket_new/`

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
│  6. 构建915维特征 (build_features_78d)                                  │
│       • K线特征: 17维 (归一化收益/波动率/位置/振幅/ streak/背离/OI变化)  │
│       • 波动聚类: 3维 (regime/momentum/persist)                         │
│       • 回归特征: 4维  (β/α/R²/残差 vs BTC)                             │
│       • RSI+背离: 7维  (RSI7/14/30 + 背离4维)                           │
│       • 板块热度: 22维 (ts-86400, 避免当日收益泄露)                      │
│       • 宏观特征: ~56维 (ETF/链上/情绪/恐慌/稳定币/溢价/算力/清算/TVL)   │
│       • Kronos:  832维 (Kronos-base hidden state, L2归一化)            │
│       • 跨资产:   4维  (SP500/DXY/黄金 + 山寨BTC溢价)                   │
│  7. 标签: 2日收益 > 5% = 1 (j=i-1, next_ret=(close[i+1]-close[j])/close[j])│
│  8. Walk-forward训练 XGBoost (做多模型 + 做空模型)                       │
│       • 训练窗口: 最近365天                                            │
│       • 门槛: ≥400天历史的币种 (当前~341/528)                           │
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
| `LEVERAGE` | 2 | 杠杆倍数 |
| `MIN_VOLUME_24H` | 500000 | 最小24h成交量 (仅回退时使用) |
| `TRAIN_DAYS` | 365 | 训练历史窗口天数 (min_required=400天) |
| `MARGIN_STEPS` | [5,8,10,15,20,30] | 保证金阶梯 (USDT) |
| `CAPITAL_BREAKPOINTS` | [25,50,100,200,400] | 钱包余额分档 |

---

## 4. Cron 调度配置

```
* * * * *  cd /home/myuser/websocket_new && /usr/bin/python3 guardian.py >> /tmp/guardian.log 2>&1
0 6 * * *  cd /home/myuser/websocket_new && /usr/bin/python3 daily_data_collection.py >> /tmp/daily_collection.log 2>&1
0 8 * * *  cd /home/myuser/websocket_new && /usr/bin/python3 auto_dual_trade.py >> /tmp/auto_dual_cron.log 2>&1
```

| 时间 | 脚本 | 作用 |
|------|------|------|
| 每分钟 | guardian.py | 检查进程存活+文件新鲜度 |
| 每天6:00 | daily_data_collection.py | 统一采集所有外部数据源 |
| 每天8:00 | auto_dual_trade.py | 检查持仓→训练→预测→交易 |

---

## 5. 日志文件位置

| 日志 | 路径 | 内容 |
|------|------|------|
| 交易日志 | `~/.local/share/auto_trade/trade.log` | auto_dual_trade 所有操作记录 |
| cron交易日志 | `/tmp/auto_dual_cron.log` | cron运行的stdout/stderr |
| 采集日志 | `/tmp/daily_collection.log` | daily_data_collection 输出 |
| 守护日志 | `/tmp/guardian.log` | guardian 每分钟检查记录 |
| OI采集 | `/tmp/oi_collector.log` | OI实时采集记录 |
| 情绪采集 | `/tmp/sentiment.log` | 情绪数据实时采集 |
| Web服务 | `/tmp/web_server.log` | Flask app 日志 |

---

## 6. 已知限制与风险

### 6.1 API限制 (严重)
- **Binance API -4120**: `STOP_MARKET` / `TAKE_PROFIT_MARKET` 不再支持通过 `/fapi/v1/order` 标准接口下单
- **影响**: 自动止损/止盈单无法挂出，新开仓位为"裸仓"
- **缓解**: 脚本已修改为遇-4120不回滚平仓，记录裸仓状态继续持仓
- **人工干预**: 每次开仓后需要手动在Binance App/Web挂止损单（标记价格触发）

### 6.2 模型偏差
- 标签为2日收益>5%，但实际持仓48小时（约2天），存在时间错配
- 训练数据历史窗口365天，市场结构变化可能导致模型失效
- Permutation Test 每天检测过拟合，drop < 0.05 时阻止交易

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
- [ ] **手动补止损**: 对新开仓位挂止损单（标记价格触发, -10%）

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
# 然后在Binance App手动挂止损
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

每天 8:00  auto_dual_trade.py
  ├─ 检查持仓 (止损-10% / 48h到期)
  ├─ 全量加载K线缓存 (528币) → 过滤 ≥400天 → ~341币
  ├─ 加载OI + 宏观 + 板块 + Kronos
  ├─ 余额≥10U?
  │   ├─ YES → 训练XGBoost → Permutation Test → 预测 → 交易
  │   └─ NO  → 跳过交易 (数据已刷新)
  └─ 下单 + 止损/止盈 (API -4120时裸仓记录)

每分钟   guardian.py
  ├─ screen会话存活检查 (先kill旧session再建新)
  ├─ 文件新鲜度检查 (8项, 各有时效阈值)
  ├─ 过载保护 (每小时最多重启5次)
  └─ 状态输出 /tmp/guardian_status.json
```

## 11. COS 云端备份清单

| COS 路径 | 内容 | 频率 | 采集器 |
|------|------|------|------|
| `klines/cache/notusdt_1d_full.json` | K线缓存 (528币日线, 31.6MB) | 每天 6:00 | daily_data_collection |
| `klines/cache/oi_daily.json` | OI缓存 (531币日级, 0.4MB) | 每天 6:00 | daily_data_collection |
| `klines/cache/kronos_features_cache.json` | Kronos特征 (832维, ~35MB) | 每天 8:00 | auto_dual_trade |
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

> 共 **16 个 COS 路径**，覆盖全部本地数据。验证命令: 读取 COS bucket 按 LastModified 排序取最新。

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

---

*本文档由 Reasonix Code 更新，用于系统维护和知识传承。*
