---
name: "altcoin-fund-flow"
description: "Analyzes altcoin fund flow (山寨资金): non-TOP50 volume, surge breadth, OI/dump signals, phase classification vs 8/2026 baseline. Invoke when user says '山寨资金SKILL'/'调用山寨资金SKILL', asks about altcoin volume/放量/资金流入流出, or wants the 山寨资金日报 explained."
---

# 山寨资金SKILL — 山寨资金面分析 (成交额/放量/阶段判定)

> 创建: 2026-09-07 (基于当日"8月中旬以来山寨放量变化"会话沉淀)
> 数据体系: altcoin_volume_alert.py (每日 09:05 cron 发「📊 山寨资金日报」邮件)
> 纪律: 遵守 AGENTS.md 铁律6 + §0.5 宪法; 数据先读再开口; 判定给证伪线

---

## 0. 快捷触发: "山寨资金SKILL"

用户说 **"山寨资金SKILL"** 时执行标准序列:

1. 重建近30日序列(§2代码): 非TOP50总额/单币中位/放量币数
2. 阶段划分对比(§3基线)
3. 输出: 当前资金阶段判定 + 与系统盈亏含义(§4) + 证伪线

---

## 1. 数据源与口径 (与邮件完全一致)

| 数据 | 文件 | 字段 |
|---|---|---|
| 日线成交额 | `/home/myuser/backtester/data_cache/notusdt_1d_full.json` | klines → 每币bar的 `q` (quote volume, U) |
| 市值排名(排除TOP50) | `/home/myuser/coingecko_data/mcap_latest.json` | coins → 按mcap降序取前50为排除集 |
| 邮件日志(快查) | `logs/altcoin_volume.log` | grep "已发送" → 标题含当日总额+有无放量 |
| OI/出货监测 | `data/dump_oi_history.json` | records (脚本的OI快照体系) |

**口径要点**:
- "非TOP50" = 全山寨宇宙(~478币)排除市值前50 → 中小山寨口径
- 邮件标题"TOP50=XX亿"是滚动7日窗口, 重建底层数据会有小差异, 趋势一致
- 币安日线UTC 00:00收盘, 当日数据要 08:00 CST 后才完整 (9/7的0.3亿是未收盘假象, 勿误读)

## 2. 标准分析代码 (重建序列)

```python
import json, datetime, statistics
kl = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
mc = json.load(open('/home/myuser/coingecko_data/mcap_latest.json'))['coins']
top50 = {k for k,_ in sorted(mc.items(), key=lambda x: -(x[1].get('mcap') or 0))[:50]}

by_date = {}
for sym, bars in kl.items():
    for b in bars:
        d = datetime.datetime.fromtimestamp(b['t']/1000, tz=datetime.timezone.utc).strftime('%Y-%m-%d')
        by_date.setdefault(d, {})[sym] = b['q']

vol_hist = {}  # 每币历史成交额(算放量)
for d in sorted(by_date.keys()):
    alt = [(s,q) for s,q in by_date[d].items() if s not in top50 and q and q>0]
    if not alt: continue
    total = sum(q for _,q in alt)/1e8          # 总额(亿U)
    med = statistics.median([q for _,q in alt])/1e6  # 中位(百万U)
    surge = sum(1 for s,q in alt
                if len(vol_hist.setdefault(s,[]))>=7
                and q > 2*statistics.mean(vol_hist[s][-7:]))  # 放量币数
    for s,q in alt: vol_hist[s].append(q)
    print(d, f"{total:.1f}亿 中位{med:.1f}M 放量{surge} n={len(alt)}")
```

陷阱: ①日期聚合必须用完整ISO日期(YYYY-MM-DD), %m-%d会跨年撞key; ②当日未收盘数据总量会假性暴跌, 先看时间; ③alt可能为空(median报错), 加保护。

## 3. 阶段基线 (2026-08-10 ~ 09-06 实测, 后续滚动校准)

| 阶段 | 总额 | 单币中位 | 放量币数 | 含义 |
|---|---|---|---|---|
| 平静基线 | ~53亿 | ~2.0M | 28~55 | 存量市 |
| **全面放量潮** | >75亿(峰值91) | >3.5M | **>150 (峰值219, 占44%)** | 增量散户入场, 肥右尾温床 |
| 放量退潮 | 55~65亿 | 2.2~2.7M | 30~50 | 总额惯性+长尾失血 |

**关键历史锚点**: 8/19~8/22 是本轮唯一全面放量事件(86→123→209→219), 恰逢BTC +20%三连阳; 8/25史诗日(+944U)是8/21~22放量堆积的滞后兑现 → **放量领先系统肥日约3~4天**。

**中位数比总量更诚实**: 中位降得比总量深 = 头部集中/长尾失血(存量博弈); 两者同升才是增量山寨季。

## 4. 与交易系统的联动 (对照 AGENTS.md §0.5)

- **公理4应用**: 放量潮退≠不能赚(9/3~9/5批次ZEC+29%/NEAR+25%是结构性行情), 但**全面肥右尾行情(8/25型)需要放量潮配合**。存量市里到期胜率预期应回归基线35%, 不用9/4那种81%的兴奋值外推。
- **山寨季证伪线**(与晨报SKILL Step4触发器互补): 某日放量币数>150 且 总额>75亿 → 增量回归, 加仓叙事才成立; 否则山寨超额收益按存量博弈反弹对待。
- 邮件"无放量"=报警条件未触发, 是准确陈述, 不代表系统该停(方向模型与资金面独立)。

## 5. 汇报模板

```
## 山寨资金SKILL 诊断 (日期)
① 近7日序列表: 日期×[总额/中位/放量数]
② 阶段判定: [平静/放量潮/退潮] + 与§3基线对照
③ 结构信号: 中位vs总量背离(长尾失血?) / 放量币集中度
④ 系统含义: 到期胜率预期校准 + 与当前批次的关系
⑤ 证伪线: 什么数据改变判定
```

## 6. 跨设备迁移

与晨报SKILL同机制: 本文件在服务器 `~/.trae/skills/altcoin-fund-flow/`; 仓库镜像 `websocket_new/.trae/skills/altcoin-fund-flow/SKILL.md`(独立拷贝, 保持一致), 每日08:50 trading_system_github_sync.py 自动推GitHub。新机器: clone仓库 → 拷贝到 `~/.trae/skills/` → SSH连服务器取数。
