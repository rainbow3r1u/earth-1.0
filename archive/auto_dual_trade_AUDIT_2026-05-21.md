# auto_dual_trade.py 实盘审计报告 — 2026-05-21

> 完整审计：致命bug → 高风险bug → 中低风险bug

---

## 🔴 Critical Bugs（6个，全部修复）

### CRITICAL-1: 标签定义错误 — 模型完全不匹配
**位置**: `build_features_78d()` 第566行
```python
# 原代码（错误）: 训练1日预测模型
next_ret = (closes[i+1] - closes[i]) / closes[i]

# 修复: 训练2日预测模型（与回测一致）
next_ret = (closes[i+1] - closes[j]) / closes[j]  # j=i-1
```
**影响**: 回测Sharpe=24对原脚本**完全无效**。实盘信号质量不可知。
**状态**: ✅ 已修复

### CRITICAL-2: 无止盈逻辑
**位置**: `main()` 开仓后
**现状**: 只下止损单，没有+10%止盈
**回测假设**: 对称±10%止盈止损
**修复**: 新增 `TAKE_PROFIT_PCT=10.0` + `place_take_profit_order()`
**状态**: ✅ 已修复

### CRITICAL-3: 板块热度数据泄露
**位置**: `build_features_78d()` 第548行
```python
# 原代码（错误）: 使用当日板块热度（包含当日收益信息）
sector_feats = dp._get_sector_features(sym, ts, ...)

# 修复: 使用前一日板块热度
sector_feats = dp._get_sector_features(sym, ts-86400, ...)
```
**影响**: 训练时偷看了当日板块收益，实盘没有当日信息，模型会失效。
**状态**: ✅ 已修复

### CRITICAL-4: API错误检查不完整
**位置**: `main()` 第701-704行
```python
# 原代码: 只检查'code' in account
if 'code' in account:  # 但error dict没有'code'!
    return
# 错误结果: wallet=0，误判本金不足

# 修复: 增加None检查和'error'检查
account = get_account()
if account is None or 'code' in account or 'error' in account:
    return
```
**状态**: ✅ 已修复

### CRITICAL-5: get_positions返回错误时脚本崩溃
**位置**: `check_and_close()` 第262行
```python
# 原代码: 如果positions是dict（API错误），遍历dict的keys
positions = get_positions()
active = [p for p in positions if ...]  # positions是dict → 崩溃

# 修复: 增加类型检查
positions = get_positions()
if positions is None or not isinstance(positions, list):
    return state
```
**状态**: ✅ 已修复

### CRITICAL-6: state.json损坏时脚本崩溃
**位置**: `load_state()` 第243行
```python
# 原代码: 没有try-except
with open(STATE_FILE) as f:
    data = json.load(f)

# 修复: 增加try-except + 备份损坏文件
try:
    with open(STATE_FILE) as f:
        data = json.load(f)
except Exception as e:
    # 备份并重置
```
**状态**: ✅ 已修复

---

## 🟠 High Bugs（6个，全部修复）

### HIGH-1: 止损单接口错误
**位置**: `place_stop_loss_order()` 第147行
```python
# 原代码: /fapi/v1/algoOrder（条件单接口，可能不支持）
# 修复: /fapi/v1/order + STOP_MARKET + closePosition=true
```
**状态**: ✅ 已修复

### HIGH-2: 双重平仓风险
**位置**: `check_and_close()` + `place_stop_loss_order()`
- bot市价平后，algoOrder止损单可能仍触发（取消有延迟）
**修复**: 市价平前先 `cancel_all_orders(symbol)`
**状态**: ✅ 已修复

### HIGH-3: Kronos预计算过慢
**位置**: `main()` 第776行
```python
# 原代码: 收集所有K线时间戳（~40,000个）
sorted_days = sorted({k['t'] // 1000 for kls in klines.values() for k in kls})

# 修复: 只收集交易日（~500个）
all_ts_for_kronos = set()
for i in range(25, len(kls) - 2):
    all_ts_for_kronos.add(timestamps[i])
```
**状态**: ✅ 已修复

### HIGH-4: close_with_retry中remaining可能变负数
**位置**: `close_with_retry()` 第174行
```python
# 原代码: remaining -= filled  # 如果filled>remaining，remaining变负
# 修复: remaining = max(remaining - filled, 0) + total_filled累计判断
```
**状态**: ✅ 已修复

### HIGH-5: 止盈止损单插针误触发
**位置**: `place_stop_loss_order()` / `place_take_profit_order()`
- 默认按最新价触发，插针可能导致误触发
**修复**: 新增 `workingType='MARK_PRICE'`（按标记价格触发，更平滑）
**状态**: ✅ 已修复

### HIGH-6: FUTURES_INFO_CACHE缓存不刷新
**位置**: `get_step_size()` / `get_tick_size()`
- 币安可能调整最小下单量，缓存永远用旧的
**修复**: 新增 `_refresh_futures_info()`，缓存>7天自动刷新
**状态**: ✅ 已修复

---

## 🟡 Medium Bugs（4个，全部修复）

### MEDIUM-1: 模型缓存7天太长
**位置**: `train_and_predict()` 第625行
- 市场状态每天都在变，7天前的模型可能已过时
**修复**: 改为1天缓存
**状态**: ✅ 已修复

### MEDIUM-2: 孤儿仓位open_ts保守估计
**位置**: `check_and_close()` 第337行
```python
# 原代码: 假设已持24h（实际可能47h，导致总持71h）
'open_ts': int(time.time()) - 24*3600

# 修复: 保守估计40h
'open_ts': int(time.time()) - 40*3600
```
**状态**: ✅ 已修复

### MEDIUM-3: history列表无限增长
**位置**: `check_and_close()` 末尾
- 每次平仓追加到history，state文件越来越大
**修复**: 限制history保留最近200条
**状态**: ✅ 已修复

### MEDIUM-4: 大量bare except
**位置**: 多处
- `except:` 会捕获KeyboardInterrupt和SystemExit
**修复**: 全部改为 `except Exception:`
**状态**: ✅ 已修复

---

## 🟢 Low Issues（2个，全部修复）

### LOW-1: 资金费率未处理
**位置**: 无
- 2天持仓要交1-2次资金费，做空时可能侵蚀利润
**修复**: 
- 新增 `get_funding_rate()` 函数
- 开仓前检查资金费率，高费率时发出警告
- 可用资金预留0.5%资金费缓冲
**状态**: ✅ 已修复

### LOW-2: 可用资金未预留资金费缓冲
**位置**: `main()` 开仓前
```python
# 原代码: if available < margin: return
# 修复: if available < margin + funding_buffer: return
```
**状态**: ✅ 已修复

---

## ⚠️ 已知风险（无法完全修复）

### RISK-1: closePosition=true 平掉全部持仓
- 止损/止盈单使用 `closePosition=true`，会平掉该币种的**全部**持仓
- 如果用户有多个策略或手动持仓在同一币种，会被一并平掉
- **缓解**: 确保该账号只有本bot在交易

### RISK-2: 市价单滑点
- 小市值币市价单滑点可能2-5%，回测按0计算
- **缓解**: 限制交易Top50流动性充足的币种

### RISK-3: 资金费率黑天鹅
- 极端行情资金费率可能高达1%（每8小时），2天6次=6%
- **缓解**: 已添加资金费率检查和缓冲，但无法预测极端情况

### RISK-4: 模型与实盘分布偏移
- 回测用历史数据，实盘是未来数据
- 市场结构变化时模型可能失效
- **缓解**: 每日重新训练，滚动验证

---

## 修复后文件

| 文件 | 说明 |
|------|------|
| `auto_dual_trade_fixed.py` | **修复版实盘脚本**（1106行） |
| `auto_dual_trade_AUDIT_2026-05-21.md` | 本审计报告 |

---

## 使用修复版前的关键步骤

1. **先在测试网跑3-5天**
```bash
BASE_URL = 'https://testnet.binancefuture.com'
```

2. **确认止盈止损单能正常触发**
- 下一个小仓位单
- 手动观察止损/止盈单是否在币安订单列表中
- 确认 `closePosition=true` 能正确平仓全部

3. **确认workingType=MARK_PRICE有效**
- 检查订单详情中是否显示按标记价格触发

4. **关键配置检查**
```python
STOP_LOSS_PCT = 10.0   # 止损-10%
TAKE_PROFIT_PCT = 10.0 # 止盈+10% ← 新增
LEVERAGE = 2           # 杠杆
```

5. **不要直接替换原脚本**
```bash
cp auto_dual_trade.py auto_dual_trade_backup.py
cp auto_dual_trade_fixed.py auto_dual_trade.py
```

---

## 修复统计

| 级别 | 数量 | 状态 |
|------|------|------|
| 🔴 Critical | 6 | 全部修复 |
| 🟠 High | 6 | 全部修复 |
| 🟡 Medium | 4 | 全部修复 |
| 🟢 Low | 2 | 全部修复 |
| ⚠️ Risk | 4 | 已知，需人工注意 |

---

*审计时间: 2026-05-21*
*修复版: auto_dual_trade_fixed.py (1106行)*
*核心结论: 原脚本的6个Critical bug意味着实盘模型与回测模型完全不匹配。修复后，实盘逻辑与回测框架一致。*
