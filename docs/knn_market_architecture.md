# KNN Market Architecture 指标研究文档

> 研究日期: 2026-06-07  
> 来源: LuxAlgo / TradingView / ProRealCode  
> 状态: 30天回测通过，待365天GPU验证

---

## 1. 来源

| 来源 | 地址 |
|------|------|
| LuxAlgo 官方 | https://www.luxalgo.com/library/indicator/knn-market-architecture |
| TradingView 源码 | https://www.tradingview.com/script/20YQRCyP-kNN-Market-Architecture-LuxAlgo/ |
| ProRealCode 反编译分析 | https://www.prorealcode.com/home/knn-market-architecture/ |

LuxAlgo 2026 年 3 月发布，支持 TradingView / MetaTrader / NinjaTrader / Thinkorswim。

---

## 2. 指标声称的功能

指标声称用 **k-近邻 (kNN) 分类器** 检测价格枢轴点（高低转折点），跨三个时间尺度：

| 尺度 | 用途 | 检测窗口 |
|------|------|:--:|
| 短期 (ST) | 剥头皮交易 | baseLen |
| 中期 (MT) | 波段交易 | baseLen × 3 |
| 长期 (LT) | 宏观趋势判断 | baseLen × 9 |

附带组件：
- **BOS** (Break of Structure) — 价格突破枢轴线
- **Delta Tank** — 买卖量力量对比
- **Anchored Volume Profile** — 结构区间的成交量分布

---

## 3. 什么有用，什么没用

### ❌ 噱头：kNN 分类器本身

ProRealCode 反编译 Pine Script 源码后发现：

> kNN 分类器**永不拒绝任何检测到的枢轴点**。每一个被窗口检测到的局部高低点都会被画成线。"kNN" 纯粹是营销标签。

结论：**不要实现 kNN 分类器**。它不提供任何额外的信号质量过滤。

### ✅ 有用组件

| 组件 | 为什么有用 | 我们怎么用 |
|------|-----------|-----------|
| **波动率自适应窗口** | ATR(200) 动态调整检测窗口。高波动→大窗口过滤噪音，低波动→小窗口捕捉细微结构。比固定窗口更合理。 | 固定窗口=7（简化版，避免 ATR 重复计算的 O(n²) 开销） |
| **BOS 破结构** | 收盘价穿越枢轴线的次数，反映趋势强度。频繁 BOS 向上 = 强多头趋势；频繁 BOS 向下 = 强空头趋势。 | 计数最近 60 天的 BOS 向上/向下次数 |
| **Delta Tank** | 统计枢轴形成以来的买方成交量 vs 卖方成交量。高 Delta%（买方占优）= 支撑位被真实资金防守；低/负 = 虚的。**这是订单簿的廉价替代品**。 | `delta% = |买量差| / 总成交量 × 100` |
| **价格区间位置** | 当前价在最近高低点之间的位置（0~100%）。接近 0% = 支撑位附近可能反弹；接近 100% = 压力位附近可能回落。 | `price_in_range_pct = (close - low) / (high - low) × 100` |

### ❌ 没用的组件

| 组件 | 为什么没用 | 
|------|-----------|
| **多层时间尺度 (ST/MT/LT)** | 日线交易不需要。日线 K 线本身已经是一天的聚合，MT/LT 的窗口太大（21天/63天）会导致几乎检测不到枢轴。 |
| **Anchored Volume Profile** | 需要 tick 级数据，日线 K 线做不到。而且 XGBoost 吃不了画像结构，只能吃数值特征。 |
| **枢轴线可视化/TradingView 画图** | 回测不需要。 |

---

## 4. 我们的实现

### 4.1 简化原则

- 固定窗口=7，不做自适应（避免性能问题）
- 单一尺度（60 天回顾窗口）
- 只提取数值特征，不画线

### 4.2 提取的 15 维特征

```python
{
    'pivot_high_count': 0,      # 60天内高点数量
    'pivot_low_count': 0,       # 60天内低点数量
    'pivot_total_count': 0,     # 枢轴总数
    'pivot_window': 7,          # 检测窗口大小
    'nearest_pivot_high_dist': 0,  # 距最近高点距离(%)
    'nearest_pivot_high_age': 0,   # 最近高点形成天数
    'nearest_pivot_low_dist': 0,   # 距最近低点距离(%)
    'nearest_pivot_low_age': 0,    # 最近低点形成天数
    'bos_up_count': 0,          # BOS向上次数
    'bos_down_count': 0,        # BOS向下次数
    'delta_tank_high': 0,       # 最近高点Delta Tank(%)
    'delta_tank_high_raw': 0,   # 最近高点原始Delta(量)
    'delta_tank_low': 0,        # 最近低点Delta Tank(%)
    'delta_tank_low_raw': 0,    # 最近低点原始Delta(量)
    'price_in_range_pct': 50,   # 价格在高低区间的位置(%)
}
```

### 4.3 代码位置

```
websocket_new/experiments/knn_market_structure.py
- extract_market_structure_features()  — 单次提取
- precompute_knn_features()            — 预计算缓存（回测用）
```

---

## 5. 回测结果

### 30 天测试（本机，10 维基特征，无 Kronos）

| 指标 | Before (10维) | After (10+15=25维) | 变化 |
|------|:--:|:--:|:--:|
| Sharpe | 0.13 | **4.41** | +4.28 |
| 累计收益 | -2.4% | **+576.2%** | +578.6% |
| 胜率 | 50.0% | **85.7%** | +35.7% |
| 止损次数 | 10 | **2** | -8 |
| 交易笔数 | 28 | 28 | — |

> ⚠️ 只有 28 笔交易 + 10 维基特征（无 Kronos/板块/宏观）。绝对值没意义，对比有效。需 365 天完整验证。

### 365 天完整回测（待跑，需 GPU）

```
特征: 84维基 + 832维Kronos + 15维KNN = 931维
状态: 代码已写 (experiments/knn_structure_bt.py)，等 GPU 恢复
```

---

## 6. 如果接入生产

**不是替换现有特征，是追加**：

```
当前生产特征 (914维) + KNN市场结构 (15维) = 929维
```

每天 6:00 数据采集时，对每个币种用 200+ 天 K 线跑一次 `precompute_knn_features()`，存入缓存。8:00 训练时从缓存读取。

**开销**：200 币 × 2000 根 K 线 × O(n) 枢轴遍历 ≈ 几秒，可忽略。

---

## 7. 结论

| 维度 | 评价 |
|------|------|
| kNN 分类器 | ❌ 噱头，不实现 |
| 自适应波动率窗口 | ✅ 有用但用固定窗口简版即可 |
| BOS 破结构 | ✅ 趋势强度信号 |
| Delta Tank | ✅ 买卖力量对比，订单簿廉价替代品 |
| 多层时间尺度 | ❌ 日线不需要 |
| Volume Profile | ❌ 无 tick 数据 |
| 30 天回测 | ✅ 方向明确，值得推到 365 天验证 |
