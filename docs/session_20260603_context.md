# Session Context — 2026-06-02/03 系统维护

> 会话时间: 2026-06-02 ~ 2026-06-03
> 本地服务器 4核, GPU 175.155.64.171:24220

---

## 1. Bug 修复

| Bug | 文件 | 问题 | 修复 |
|-----|------|------|------|
| algoId 检查失效 | `auto_dual_trade.py` | Algo Order 返回 `algoId`，代码检查 `orderId`，止损止盈永远判失败 → 自动回滚平仓 | 改 `'algoId' in sl_order or 'orderId' in sl_order`，失败不自动回滚 |
| cancel Algo 单遗漏 | `auto_dual_trade.py` | 平仓时只取消普通单，遗留 Algo 条件单 | 新增 `/fapi/v1/algoOpenOrders` |
| liq_daily.json 崩溃 | `liquidation_heatmap.py` | 文件被覆写为 dict，`h.get('date')` 对 str 崩溃 | `isinstance(history, list)` 防御 |
| sector_heatmap 15天未更新 | `sector_heatmap.py` + `guardian.py` | guardian 注释掉了检查但没补 cron；脚本 Timeout 吞结果 | 恢复 guardian 条目 + as_completed 单独 catch TimeoutError |
| 回测 dd 指标错误 | `gpu_clean_bt.py` | 累计收益回落被当总资金回撤 | 区分 max_dd_cum / max_dd_cap |

## 2. 性能优化

| 优化 | 文件 | 效果 |
|------|------|------|
| `_fast_winsor_bounds` | `daily_predictor.py` | np.partition (QuickSelect O(n)) 替代 np.percentile (全排序 O(n log n))，15-20x 加速 |
| 应用到所有调用点 | `auto_dual_trade.py`、`dual_backtest_clean.py`、GPU 脚本 | 回测每轮 winsor 从 10s → <1s |

## 3. 回测结果

### clean 365d (唯一可信版，真止损止盈)

| 指标 | 值 |
|------|:----:|
| 总收益 | +548.8% |
| Sharpe | 4.75 |
| 胜率 | 63.5% |
| 总资金最大回撤 | ~8-15% (估计) |
| 止损 | 69 次 |
| 止盈 | 128 次 |
| 交易 | 255 笔 (110 天 SKIP) |

代码: `gpu_clean_bt.py`，GPU RTX 3080
与生产对齐: 真止损 ±10% (日K最高/低价触发)、真止盈 ±10%、915维特征、200棵树

### 对照

| 版本 | 收益 | Sharpe | 止损 | 可信 |
|------|------|:------:|:--:|:--:|
| clean 365d | +549% | 4.75 | ✅真 | ✅✅✅ |
| clean 50d | +327% | 16.20 | ✅真 | ✅ |
| 原始 365d | +348% | 1.80 | ⚠️伪 | ⚠️ |
| 旧 365d | +3557% | 10.71 | ⚠️伪+无阈值 | ❌ |
| 自定义 GPU | -1982% | -2.55 | ✅真 | ❌bug |

## 4. 生产配置对齐

| 参数 | 改前 | 改后 | 原因 |
|------|:--:|:--:|------|
| n_estimators | 150 | **200** | 与回测统一 |

## 5. MCP 服务

三个 MCP 已注册 (`~/.reasonix/config.json`):

```
gpu=python3 -u /home/myuser/gpu_mcp_proxy.py       # GPU回测/状态
codegraph=npx ... serve --mcp -p websocket_new      # 代码索引
data=python3 -u websocket_new/mcp_server.py         # 币安/CoinGecko/新闻
```

`mcp_server.py` 新增 MCP stdio 主循环 (之前只有工具函数)。

## 6. 当前系统状态 (2026-06-03 23:30)

| 项目 | 状态 |
|------|------|
| 钱包 | 17.03 USDT ✅ (>10U) |
| 持仓 | LITUSDT LONG x61.4 @1.7769 (20x, 手动) |
| 数据 | 全部新鲜 (6/3 采集) |
| 交易 | 明天 8:00 自动运行 |

## 7. 重要结论

1. **模型有真实选币能力**: ML 在真止损止盈框架下碾压随机 (+549% vs -4%)
2. **回溯≠生产**: 回溯偏乐观 (无杠杆放大回撤、无资金费、等权仓位)
3. **Kronos 贡献在风控**: 30天 ablation 显示收益几乎不变，回撤降 27%
4. **365天模型衰减**: Sharpe 从 50天 16.20 → 365天 4.75 (正常)
5. **伪止损虚高严重**: 旧版 +3557% vs 真版 +549%

## 8. 当前未解决问题

- GPU clean 365d JSON 序列化 bug (numpy.float32)，结果已从日志恢复但完整 trade 列表未保存
- 回测无杠杆/爆仓模拟
- 回测无 Permutation Test 保护
- GPU `gpu_clean_bt.py` 需重新上传运行修复 dd_cap 版本
