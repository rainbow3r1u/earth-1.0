---
name: "short-top5-timing"
description: "Monitors SHORT TOP5 (daily top-prob 5 shorts) win rate and trial-entry timing. Outputs one verdict into the morning report: when SHORT TOP5 is fit for small-capital trial. Invoke when user says 'SHORT TOP5 SKILL' or asks about short-side trial timing/win rate."
---

# SHORT TOP5 试盘时机SKILL — 胜率监控与试盘判定

> 创建: 2026-09-07 (基于"每日SHORT TOP5止损率统计→小资金试盘决策"会话沉淀)
> 数据源: data/hybrid_tracker.json (3.8影子臂SHORT侧, prob排名前5/日)
> 输出契约: **只输出一个结果 — "SHORT TOP5 适不适合小资金试盘"**, 每日合并进晨报邮件(3.9d节)

---

## 0. 核心结论框架 (勿重复推导)

**基准事实 (2026-09-07 实测, 8/3~9/6, 175笔已结算)**:
- SHORT TOP5 总胜率 35% (61/175), 对照全体SHORT 32%、6-10名29% → prob排序有微弱真实信号
- **盈亏平衡线 = 33.3%** (TP10/SL5赔率2:1, 亏损笔均-16.3U含funding税 > 名义基准-15.5U)
- 分阶段: 8月上半脉冲市 38%/日均+12.6U | 放量潮期(8/19-22) 35%/日均-1.2U | **8/23后存量市 32%/日均-4.8U ← 卡在盈亏线下**

**判定规则 (固定, 不临场发挥)**:
```
试盘绿灯 🟢: 近10日TOP5滚动胜率 ≥ 40% 且 资金面=放量潮期(山寨资金SKILL口径: 放量币>150/总额>75亿) 或 BTC破位(<77.3k, 以最新结构低点为准)
试盘黄灯 🟡: 近10日滚动胜率 33~40% (线上但无资金面配合 — 观察)
试盘红灯 🔴: 近10日滚动胜率 < 33% (盈亏线下 — 不试, 当前默认状态)
```

## 1. 统计代码 (每日计算)

```python
import json
d = json.load(open('data/hybrid_tracker.json'))
days = sorted(d.keys())
tp = n = 0; recent_tp = recent_n = 0
recent10 = days[-10:]
for day in days:
    shorts = sorted([t for t in d[day]['trades'] if t['direction']=='SHORT'], key=lambda t: -t['prob'])
    for t in shorts[:5]:
        trig = t.get('trigger')
        if trig in ('止盈','止损','到期'):
            n += 1; tp += (trig=='止盈')
            if day in recent10:
                recent_n += 1; recent_tp += (trig=='止盈')
# 注意: 近10日批次多数未结算(T+2), recent样本天然偏少; 结算满的批次才计入
```

陷阱: ①TOP5按"当批prob排名"取, 不是按币聚合; ②"进行中"单不计入分母; ③近10日窗口含大量未结算批次, 滚动胜率有2天滞后, 判定时说明样本量。

## 2. 晨报合并 (daily_digest_email.py 已部署 3.9d 节)

- 函数: `section_short_top5()` — 单行结论表, 红/黄/绿灯 + 一句话理由 + 触发条件回显
- 位置: 3.9c 之后; 生成失败自动降级文字行, 不影响晨报其余部分
- 色块: 🟢绿底 `#e8f5e9/#1b5e20` / 🟡黄底 `#fffde7/#b8860b` / 🔴红底 `#ffebee/#c00`

## 3. 试盘执行约定 (若某日转绿灯)

- 规格: 40U名义/笔·5x逐仓·SL5%/TP10/48h — 复用实盘残差臂风控框架(公理3: 活到尾巴来)
- 仓位纪律: 初始只试 TOP5 中的 TOP1-2 (日≤2笔), 验证10个批次胜率仍≥40%再放开到TOP5全开
- 停止线: 试盘累计 -20U 或 滚动胜率跌回33%以下 → 停, 回影子臂积累
- **决策权在用户**: 绿灯只代表数据条件满足, 开不开由用户拍板(SKILL只报时机)

## 4. 证伪线与复查

- 本判定体系的命门: "脉冲市胜率高"来自单一样本段(8月上旬), 若下次放量潮TOP5胜率仍<35% → prob排序对SHORT无预测力, 整个SKILL降级为纯监控
- 复查节点: 每次放量潮结束后重算分阶段胜率; 10/23残差臂评审时一并复审
