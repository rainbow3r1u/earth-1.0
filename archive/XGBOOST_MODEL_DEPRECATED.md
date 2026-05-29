# XGBoost每日预测模型详解

## 文件位置

```
/home/myuser/websocket_new/daily_predictor.py
```

cron: `0 8 * * *` 每天北京时间8点自动运行

## 模型是什么

XGBoost (eXtreme Gradient Boosting) — 梯度提升决策树集成。不是大语言模型。

```python
XGBClassifier(
    n_estimators=200,      # 200棵树
    max_depth=5,           # 每棵最深5层
    learning_rate=0.05,    # 学习率
    scale_pos_weight=...,  # 正负样本平衡
    random_state=42,
    eval_metric='logloss'
)
```

## 工作原理

每棵树是一个if-else规则链，判断一个币明天涨>5%的概率。200棵树投票，越后面的树越专注于前面树犯错的样本。

```
输入: 78维特征向量 (17 K线+回归 + 22 板块热度 + 32 宏观 + 3 Kronos + 4 RSI背离)
输出: 0~1的概率 (2天后涨>5%)

训练样本: ~280,000个 (595币×~470天)
正样本: ~10%  ← 2天后涨>5%
负样本: ~90%
```

## 78维特征详解

### 第1-10维: K线+技术指标

| # | 特征 | 计算方式 | 含义 |
|---|------|---------|------|
| 1 | ret_1d | (close[i]-close[i-1])/close[i-1] | 昨天的日收益 |
| 2 | ret_3d | (close[i]-close[i-3])/close[i-3] | 过去3天收益 |
| 3 | ret_5d | (close[i]-close[i-5])/close[i-5] | 过去5天收益 |
| 4 | volatility | std(最近5天日收益) | 波动率 |
| 5 | vol_ratio | vol[i]/mean(vol[i-5:i]) | 成交量是否放大 |
| 6 | price_position | (close-min20)/(max20-min20) | 在20天内的位置 |
| 7 | amplitude | (high-low)/open | 日振幅 |
| 8 | streak | 连续阳线天数 | 上涨趋势强度 |
| 9 | div_sign | 价涨量缩=1, 否则0 | 顶背离信号 |
| 10 | oi_chg | (OI_now-OI_prev)/OI_prev | 持仓量变化 |
| 11-14 | β/α/R²/residual | 20天滚动OLS vs BTC | 系统性/独立收益 |

### 第11-14维: BTC回归特征 (20天滚动OLS)

| # | 特征 | 计算 | 含义 |
|---|------|------|------|
| 11 | β (beta) | Cov(coin,BTC)/Var(BTC) | 跟BTC跟多紧 |
| 12 | α (alpha) | mean(coin)-β×mean(BTC) | 独立超额收益 |
| 13 | R² | 1-SS_res/SS_tot | BTC解释力 |
| 14 | residual | actual - (α+β×BTC_ret) | 无法解释的残差 |

### 第15-36维: 板块热度 (22维)

从 `/tmp/crypto_sectors.json` 加载币种→板块映射。每个板块取TOP15币种均涨幅。

BTCUSDT特殊处理: β=1, α=0, R²=1, residual=0 (因为它自己就是基准)。

币属于多个板块时多个特征同时激活，未命中板块填0。

### 第37-68维: 宏观特征 (32维)

| # | 特征 | 数据源 | 频率 | 注意 |
|---|------|--------|------|------|
| 37 | etf_btc | ETF净流入(BTC) | 日 | T-1避免未来函数 |
| 38 | etf_eth | ETF净流入(ETH) | 日 | T-1避免未来函数 |
| 39 | chain_vol | BTC链上交易量 | 日聚合 | 从60s CSV聚合 |
| 40 | chain_tx | BTC交易笔数 | 日聚合 | 从60s CSV聚合 |
| 41 | chain_fee | BTC平均手续费 | 日聚合 | 从60s CSV聚合 |
| 42 | chain_cdd | 币天销毁/交易量 | 日聚合 | 大户活动指标 |
| 43 | sent_funding | 全市场均资金费率 | 日聚合 | 从每小时聚合 |
| 44 | sent_ls_btc | BTC多空比 | 日聚合 | 从每小时聚合 |
| 45 | sent_ls_eth | ETH多空比 | 日聚合 | 新增 |
| 46 | sent_ls_avg10 | 前10币多空比均值 | 日聚合 | 新增 |
| 47 | sent_ls_high | 极端高多空比计数 | 日聚合 | 新增 |
| 48 | sent_ls_low | 极端低多空比计数 | 日聚合 | 新增 |
| 49 | fear_greed | 恐慌贪婪指数 | 日 | 归一化到0-1 |
| 50 | stablecoin | 稳定币净流入 | 日 | 归一化到1亿U |
| 51 | coinbase_prem | Coinbase溢价指数 | 日 | 新增 |
| 52 | coinbase_gap | Coinbase Premium Gap | 日 | 新增 |
| 53 | btc_mcap | BTC市值7日变化率 | 日 | 新增 |
| 54 | korea_prem | 韩国溢价指数 | 日 | 新增 |
| 55 | hashrate_7d_chg | BTC算力7日变化率 | 日 | 新增 |
| 56 | liq_total_long | 多头清算总量 | 日 | 从 liquidation_heatmap 聚合 |
| 57 | liq_total_short | 空头清算总量 | 日 | 从 liquidation_heatmap 聚合 |
| 58 | liq_ratio | 多空清算比 | 日 | 新增 |
| 59 | liq_long_peak_dist | 多头峰值距离 | 日 | 新增 |
| 60 | liq_short_peak_dist | 空头峰值距离 | 日 | 新增 |
| 61 | liq_funding | 清算时资金费率 | 日 | 新增 |
| 62 | liq_long_ratio | 多头清算占比 | 日 | 新增 |
| 63 | chain_tvl_btc | BTC链TVL 7日变化 | 日 | 新增 |
| 64 | chain_tvl_eth | ETH链TVL 7日变化 | 日 | 新增 |
| 65 | chain_tvl_sol | SOL链TVL 7日变化 | 日 | 新增 |
| 66 | chain_tvl_bsc | BSC链TVL 7日变化 | 日 | 新增 |
| 67 | chain_tvl_arb | ARB链TVL 7日变化 | 日 | 新增 |
| 68 | chain_tvl_base | Base链TVL 7日变化 | 日 | 新增 |

## 数据流

```
┌─────────────┐
│ 数据采集器    │ 7个采集器 24/7 运行
│ OI/链上/情绪  │ → COS + 本地缓存
│ ETF/恐慌贪婪  │
└──────┬──────┘
       │ 每天8点
       ▼
┌─────────────┐
│ fetch_klines │ 拉180天K线(Binance API + 缓存)
│ fetch_oi     │ 拉30天OI
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ 加载宏观特征  │ ETF/链上/情绪/恐慌贪婪 → {date: [values]}
│ 板块热度预计算│ 180天×520币种 → {timestamp: [22 sector heats]}
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ 构建训练样本  │ 每币每天一条样本, timestamp对齐
│ 45维特征+X   │ 54K样本, 10.9%正样本
│ 标签: y=1/0  │ 下一天涨>5% = 1
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ XGBoost训练  │ ~5秒, CPU
│ 存模型到pkl  │ /tmp/xgb_daily_model.pkl
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ 预测         │ 用最新完成的日线特征 → 给每个币打分
│ 输出TOP50    │ /tmp/daily_predictions.json
│ 验证昨日预测  │ 对比实际涨跌 → prediction_tracker.json
└─────────────┘
```

### 第22-43维: 板块热度 (22维)

（板块热度特征不变）

### 第69-71维: Kronos 深度特征 (3维) — Deep B 方案

从 [NeoQuasar/Kronos-small](https://huggingface.co/NeoQuasar/Kronos-small) (24.7M 参数金融时序基础模型) 提取：

| # | 特征 | 计算方式 | 含义 |
|---|------|---------|------|
| 69 | kronos_dir | (pred_close / current_close - 1) | 预测2天后BTC方向 |
| 70 | kronos_vol | mean((high-low)/close) | 预测区间波动率 |
| 71 | kronos_long | max(0, kronos_dir) | 只看多方向强度 |

**已去掉的冗余特征**: `kronos_conf` (与 `kronos_dir` 完全线性相关), `kronos_skew` (区分度低，XGBoost权重为0)。

Kronos 输入: BTC 日线 OHLCV，上下文 512 天，预测未来 2 天。
推理时间: ~2s/日期 (CPU)，磁盘缓存避免重复计算。

## 特征重要性 (Top 5)

| 排名 | 特征 | 重要性 | 说明 |
|------|------|--------|------|
| 1 | amplitude | 0.087 | 日内振幅 — 高波动币更可能爆发 |
| 2 | fear_greed | 0.056 | 恐慌贪婪 — 市场情绪是最大宏观驱动 |
| 3 | etf_eth | 0.054 | ETH ETF流入 — 机构资金方向 |
| 4 | etf_btc | 0.047 | BTC ETF流入 — 同上 |
| 5 | ret_1d | 0.043 | 最近收益 — 动量效应 |
| - | hashrate_7d_chg | - | BTC算力变化 — 矿工行为信号 |
| - | coinbase_gap | - | 机构溢价缺口 |
| - | korea_prem | - | 亚洲市场情绪代理 |
| - | kronos_dir | - | Kronos预测方向 (3维) |
| - | kronos_vol | - | Kronos预测波动率 |
| - | kronos_long | - | Kronos看多强度 |

## 回测方法

滚动窗口Walk-Forward，无未来函数：

```
Day 1-51:    训练 → 预测 Day 52 → 验证实际收益
Day 1-52:    训练 → 预测 Day 53 → 验证实际收益
...
Day 1-152:   训练 → 预测 Day 153 → 验证实际收益
```

每次只用当天之前的数据训练，完全模拟真实场景。

## 当前瓶颈

| 数据源 | 历史 | 影响 |
|--------|------|------|
| 链上 (4维) | ~3天 | 几乎全为0，未学习 |
| 情绪 (6维) | ~8天 | 部分学习 |
| ETF (2维) | ~15天 | 部分学习，已在Top5 |
| 恐慌贪婪 (1维) | 365天 | 充分学习，排名第2 |
| 清算 (7维) | ~1天 | 刚启动，几乎全为0 |
| Kronos (3维) | 500天 | 全历史覆盖，实时推理 |

链上和情绪需要20-30天积累后模型效果会再次提升。
