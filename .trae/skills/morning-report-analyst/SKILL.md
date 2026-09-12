---
name: "morning-report-analyst"
description: "Analyzes the daily morning report (晨报): section diagnosis, loss decomposition, market regime classification, trend outlook with falsification lines. Invoke when user says '晨报SKILL'/'调用晨报SKILL', asks to analyze the report, explain drawdowns/losing streaks, or forecast market trends."
---

# 晨报分析与趋势研判 SKILL

> 创建: 2026-09-04 (基于当日 3.8 六连亏分解会话沉淀)
> 适用目录: `/home/myuser/websocket_new/` (earth-1.0 仓库)
> 角色: 晨报解读员 — 把晨报各板块数据 → 结构化诊断 → 趋势研判 → 向用户汇报
> 更新: 2026-09-10 加入 LONG 四指标(到期率/到期单均/止损单均/LONG净盈亏)
> 更新: 2026-09-12 加入 **3.8b 影子臂 2×2 结构对照**(SL档位×持有时长) + **纠正两条被实测证伪的旧口径**
>       (①"止损单超出基准的部分=funding税"在早出场单上是记账假象, 已修; ②实盘止损已由5%改8%)
> 纪律: 遵守 AGENTS.md 铁律6(批判优先): 证据→正反两面→结论→证伪线; 不给无触发器的时间预测

---

## 0. 快捷触发: "晨报SKILL"

用户说 **"晨报SKILL" / "调用晨报SKILL" / "跑一下晨报分析"** 时, 不等晨报邮件、直接执行全流程并向用户汇报。

### 标准执行序列
1. **Step 0** 流水线核验: date / git冲突UU检查 / 当日auto_dual.log / residual_live.log / digest.log 发送记录
2. **Step 1** 实盘状态: `python3 audit/residual_live.py status` (权益/在持/当日开仓/止损)
3. **Step 2** 3.8+3.9 盈亏分解: 近7日 方向×离场结构×单币TOP坑; **LONG四指标=到期率/到期单均/止损单均/LONG净盈亏**(见§4); **3.8b 2×2结构对照读数**(见§4.5)
4. **Step 3** IC时序: forward_ic_history_48h.json 近10日 IC_L/AUC_L 走势 + 四态链定位
5. **Step 4** 市场regime双层判定: BTC(60日累计/MA20/高低点) + 山寨(翻转次数/广度/对BTC超额)
6. **Step 5** 趋势研判输出(禁止裸预测): regime定性 + 触发器清单 + 推演 + 证伪线

### 标准汇报模板
```
## 晨报SKILL 诊断 (日期 时间)
### ① 系统健康: 流水线4环节表格(✅/❌) + 实盘权益/在持/净盈亏
### ② 盈亏分解: 方向分布/离场结构/单币TOP3 + LONG四指标(到期率/到期单均/止损单均/LONG净盈亏) + 对照§4正期望基准(按结构选对基准列) + §4.5 2×2结构对照读数(实盘结构 vs 备选结构的每笔U)
### ③ IC与模型质量: IC_L轨迹 + 四态链当前状态 + 自愈进度(修复周期4~6天标尺)
### ④ 市场regime: BTC层(趋势/位置) + 山寨层(翻转/广度/超额) 双层定性
### ⑤ 趋势研判: 触发器清单(各带当前读数) + 最近验证点(哪个批次哪天结算) + 证伪线
### ⑥ 需人工决策项: 有则列出, 无则"无需干预"
```

## 1. 晨报板块 → 数据源地图 (先读数据再开口)

| 板块 | 内容 | 数据源文件 |
|---|---|---|
| 3.7 | 多空TOP10全开逐日U | data/top10_forward_cache.json |
| 3.8 | 混合结构影子臂(主臂) LONG无TP/SHORT TP10/SL5/48h/300U名义 | **data/hybrid_tracker.json** |
| **3.8b** | **2×2 结构对照 (SL5/SL8)×(48h/72h) LONG侧** | **data/hybrid_tracker{,_sl8,_h72,_sl8h72}.json** |
| 3.9 | 残差影子臂 vs 主臂LONG对照 | data/residual_tracker.json |
| 3.9b | 主vs残差当日选币重合度 | data/pred_YYYY-MM-DD.json (top10_long vs top10_long_residual) |
| 3.9c | **果实盘**实盘批次生存表 (**9/13起选币=主LONG榜**, 不再是残差榜) | data/residual_live_state.json |
| 5.5 | 四灯驾驶舱 + IC状态四态链 | data/forward_ic_history_48h.json |
| 5.5b | BTC vs 山寨三panel图 | /home/myuser/backtester/data_cache/notusdt_1d_full.json |
| 6 | GitHub同步+仓库体积灯 | logs/trading_system_sync_status.json |

实盘账户实时状态: `cd /home/myuser/websocket_new && python3 audit/residual_live.py status`
今晨流水线核验: `grep "$(date +%F)" logs/auto_dual.log | tail -30` + `tail logs/residual_live.log` + `tail logs/digest.log`

---

## 2. 标准分析流程 (按序执行)

### Step 0: 时序与流水线核验
- `date` 确认当前时间; 08:21(平仓)→08:24(开仓)→08:55(影子结算)→09:00(晨报) 是否都跑了
- `git status --short | grep "^UU"` → 有冲突先报(会导致晨报cron崩溃)

### Step 1: 3.8 盈亏分解 (亏损追问的标配动作)

hybrid_tracker.json 结构: `{date: {day_pnl_u, n_settled, n_total, trades: [{symbol, direction, prob, entry, result, trigger, time, net_u}]}}`

三个维度分解 (trigger ∈ 止损/止盈/到期/进行中):
```python
import json
from collections import defaultdict
d = json.load(open('data/hybrid_tracker.json'))
# 按日×方向 / 按trigger / 按币累计 / TOP亏损单笔
```

**解读标尺 (已验证事实, 勿重新推导)**:
- 止损单笔基准: **SL-5% ≈ -15.5U** / **SL-8% ≈ -24.5U** (300U名义, +0.17%费用滑点); 止盈 ≈ +28.5U
- ⚠️ **2026-09-12 口径修正(旧教训被实测证伪, 勿再沿用)**: 旧版写"超出基准的部分 = funding税", 并举 8/31 SKRUSDT -66.6U 为例 —— 那是**记账假象**: 该单 00:21 入场、**00:52 就被止损(31分钟)**, 期间零次资金费发生, 旧口径却按**全48h窗口**扣了 funding(该币小时级结算, 48h合计-17%)。修正后该单 = **-15.51U**(纯止损+费用)。同理 IOSTUSDT 一笔24分钟的单曾被多扣7.2U。
- **正确的 funding 规则**: 资金费只按**实际持有期**计 —— 早出场单几乎不吃 funding; 只有**持有到接近48h/72h**的单才可能累积大额 funding(小币小时级结算, 极端情况48h可达±17%)。所以"止损单净额超出基准"要先看**该单持有了多久**再下结论。
- **SHORT止损的结构性耦合(仍成立)**: 价格涨5%触发SHORT止损 = 空头挤压时刻 = funding最深负时刻; 挤压行情里空头常被连续收 funding。验证: `GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=X&startTime=入场&endTime=+48h`(**注意看返回的 fundingTime 间隔是1h还是8h**, 决定量级)

### Step 2: IC 时序结构判断 (回答"IC有没有提前预警"类问题)

**结构事实 (2026-09-04 已验证)**: IC(D) 由 D 日预测 T+2 结算算出, 最早出现在 **D+2 的 09:00 晨报**; 开仓在 08:21 → **IC 永远比它自己度量的那批开仓晚48小时, 是"同批确认器"不是雷达**。

回答此类问题必须重建"入场时刻可见IC" (= D-3 日的IC, 因 D-1 晨报含 D-3 的IC):
```python
import json, datetime
ic = {e['date']: e for e in json.load(open('data/forward_ic_history_48h.json'))['days']}
vis = ic.get((datetime.date.fromisoformat(day) - datetime.timedelta(days=3)).isoformat())
```

**模型自愈实测 (2026-09-04)**: 训练每日全量重训, 但180天窗每天仅换血1/180 → 权重端适应需积累样本。两个已验证样本: 8/17起涨转折 IC深负(-0.26/-0.39/-0.27)→8/24转正, 耗时~5天; 8/28横盘转差(-0.17/-0.29)→9/2转正, 耗时~4天。**修复周期标尺=4~6天**。边界: 对持续regime会自愈; 对转折瞬间永远慢半拍(换挡税, 结构性); 翻转速度快于4~6天时自愈失效(即山寨震荡期连亏的微观机制)。

**已验证的日级脱锚反例 (勿重复发现)**: 8/19 IC_L -0.390→当批+192.7; 8/25 IC_L -0.029→+944.5; 8/28 IC_L +0.142→-91.9。IC度量全宇宙排序质量, 交易只吃TOP10尾部 → **IC链适合做体制解读, 不适合做交易闸门**。

5.5 四态判定链 (daily_digest_email.py ~L831): ①🟠早期预警(IC负+AUC好+均值下行) ②🟠修复中断(近2日IC<-0.10) ③🟢修复尾声(均值回升且>-0.05) ④静默。注意8/12型预警后紧跟的是全月最肥两天 → 若据此减仓净效果为负。

### Step 3: 市场环境判定 (趋势研判的核心输入)

三个量化指标, 数据取自 `/home/myuser/backtester/data_cache/notusdt_1d_full.json` (klines键, t/o/h/l/c) 和币安API BTCUSDT 1d:

| 指标 | 算法 | 判读 |
|---|---|---|
| **山寨日线方向翻转次数** | 每日中位收益符号, 数近8日变号次数 | **≥5次 = 山寨震荡(whipsaw)**; ≤2次 = 趋势 |
| 山寨广度 | 上涨币占比 + 暴跌>5%占比 | 上涨>75%且暴跌<5% = 健康趋势; 广度<25% = 普跌 |
| BTC 5日vol | 日收益标准差 | ≤1.5%平静期(双侧吃alpha); >2%波动期(降档等恢复) — memory铁律 |
| **BTC趋势(独立判定)** | 60日累计/MA20偏离/高低点序列 | 见下方"双层regime"铁律 |

**⚠️ 双层regime铁律 (2026-09-04 用户纠错后确立)**: **BTC regime 与山寨 regime 必须分开判定, 禁止用山寨翻转次数给整体市场贴"震荡市"标签**。
案例: 2026-09-04 BTC 60日+27.5%、创新高、站上MA20+6.7% = 明确上涨市; 同期山寨21日累计超额-16% = 弱跟随高波动。正确定性 = **"BTC趋势上涨 + 山寨弱跟随(吸血/背离)"**。
系统交易宇宙是 notusdt(山寨), 盈亏挂在山寨regime上; 但BTC趋势决定山寨方向的先验概率(BTC新高→山寨方向企稳向上概率大)。判趋势必须同时报两层。
计算陷阱: notusdt数据有350+天, 按日期聚合必须用完整ISO日期(YYYY-MM-DD)做key, 用%m-%d会跨年撞key得出错误中位数。

**系统盈亏本质 (已验证)**: 赚趋势延续的钱。历史对照: 8/19~21山寨中位连三同向(+5.2/+4.0/+7.7%)批次赚+283U; 8/28~9/4八天翻转7次, 批次连亏-700U。48h持仓+SL5%在山寨震荡期被双向打脸: LONG挨阴线, SHORT挨V形反弹+funding税。

### Step 4: 趋势研判输出 (禁止裸预测)

**铁律: 不给"N天后结束"式裸预测** — 33天样本里连亏最长2天, 超出样本的事件没有统计基数。输出格式固定为:

```
当前regime: [趋势市/震荡市] + 三指标读数
结束/延续触发器:
  ① 山寨中位收益连续2~3天同向 (当前状态)
  ② BTC 5d vol 回落<1.5% (当前状态)
  ③ IC_L 5日均转正 (当前状态)
推演: 若信号X出现 → Y日结算的Z批次应验证 (批次D入场, D+2结算)
证伪线: 什么数据会推翻"行情税"结论 → 转为"模型失效"
```

**行情税 vs 模型失效的证伪线** (9/4会话确立): 若趋势确认日(如普涨日)入场的批次, 其LONG侧在结算时仍不赚钱 → 模型失效, 非行情税。

---

## 3. 实盘账户特别关注点 (果=小资金试验田 / 米=大资金主粮仓)

- **果**(`audit/residual_live.py`, 币安#1): LONG、40U名义/5x逐仓/**SL-8%**(2026-09-07 由5%调)/72h, 单笔最大失血≈-3.2U, 稳态3批~30笔
- **米**(`audit/hybrid_live.py`, 币安#2): LONG、125U名义/5x逐仓/**SL-8%**/72h, 单笔最大失血≈-10.3U(含费), 10单同止损≈-101U=用户承受线; **SHORT侧 2026-09-08 晚关闭**; 85%守卫容量≈49笔
- 实盘 funding 由交易所真实结算(income API 逐笔记账), 不存在"未发生却被扣"的问题(影子臂 2026-09-12 前有这个口径缺陷, 已修)
- 震荡市中影子臂SHORT侧大亏是"只记账的免费样本", 为10/23终审积累证据, 不要建议关停
- 关键节点: **10/23 的 48h-vs-72h 终审必须用 §4.5 的 2×2 做变量受控对比** —— 不要再拿"实盘(72h+SL8) vs 影子主臂(48h+SL5)"直接相减, 那是两个变量同时不同, 归因会被污染; 另有残差60天评审

> ⚠️ **2026-09-13 起的重要口径变化(读 3.9/3.9c 时必须知道)**:
> **果实盘(果)的选币已由残差榜(top10_long_residual)切换为主LONG榜(top10_long)** —— 依据生产级8天对照(两模型真实选币 × 官方1m × SL-8%/72h, 300U名义): 主LONG +3.04U/笔 vs 残差 -0.68U/笔(主赢7/8天)。
> 因此: ① **3.9c 是果实盘(=主LONG模型)的实盘批次表, 不再是残差臂的实盘成绩**, 不要把它与 3.9 的影子残差臂混读;
> ② 残差臂**只存在于影子(3.9/residual_tracker)** 继续采集数据, 其"防守型"性质(上涨日输/下跌日赢)与9天绝对落后−190U 的归因见项目记忆(止损结构吃掉防守优势);
> ③ 果与米**选币同源**(都 top10_long) → 两账户是同一策略的大小两档, 别再当作 A/B 对照; 米(125U)+果(40U) 同步涨跌, 集中度已上升。

## 4. LONG 正期望对照基准 (2026-09-04 会话确立, 每日晨报对照用)

**命题检验结论** (33天影子臂, 313笔LONG):
- "没灭=盈利" **90%成立不严格**: 110笔到期单中10%(11笔)活满全程仍亏(慢阴跌-0.1~-3.5%, 最典型8/31 BANANAS31 -3.5%)
- **正期望引擎 = 赔率不对称, 不是存活率**: 65%单止损(203笔×-15.5U=-3142U) vs 35%单到期(110笔×+53.7U=+5905U) → **E=+8.8U/笔**
- **72h滚动是放大器不产生期望**: 期望为正滚动复利化, 期望为负滚动加速亏; 期望来源 = 选币alpha + SL5%砍左尾/无TP放右尾的结构
- 单笔止损基准: 影子 SL-5%/48h = -15.5U(300U名义) → **SL-8% = -24.5U**; 实盘 果40U名义≈-3.2U / 米125U名义≈-10.3U(均SL-8%, 含费); 到期盈利中位 +17.4U

**LONG 结构体检四指标 (2026-09-10 加入, 每日必报)**:
- **到期率** = 到期笔数 / 总结算笔数
- **到期单均** = 到期单净盈亏 / 到期笔数
- **止损单均** = 止损单净盈亏 / 止损笔数
- **LONG 净盈亏** = 该窗口 LONG 侧总净盈亏
- 期望公式: **LONG期望/笔 = 到期率 × 到期单均 + (1−到期率) × 止损单均**

> ⚠️ 口径区分: **到期率 = 到期笔数 / 总结算**，衡量“有多少单活到时间终点”，不是“到期盈利占比”。  
> **到期胜率 = 到期且 net_u>0 的笔数 / 到期笔数**，两者不要混用。

**每日对照标尺** — ⚠️ **基准值随结构而变, 必须按"3.8b 对应结构列"取值**(2026-09-12 起四档并存):

| 指标 | SL-5%/48h(原基线臂) | SL-8%/72h(实盘现行结构) | 预警线 |
|---|---|---|---|
| 到期率(到期笔数/总结算) | ~36% | ~48% | 同结构下 <25%连续10组 → 右尾结构瓦解 |
| 到期单均net_u | +45U | +57U | 同结构下持续衰减 → 右尾衰竭 |
| 止损单均net_u | ≈-15.5U | ≈-24.5U | 超出该结构基准 → 查"实际持有期内的真实funding" |
| LONG净盈亏(期望/笔) | +6.25U | +14.01U | 连续10组≤0 → 正期望前提崩溃 |
| 批次级"全存活但整批负" | 33天0次 | 同 | 出现即报(慢阴跌市特征) |
| IC_L 5日均 | ≥0 | 同 | 周线持续负 → alpha衰减 |

> 上表数字取自 2026-08-03~09-12 的 41 天 2×2 实测(§4.5)。**跨结构直接比较到期率/止损单均没有意义**
> (延长持有必然让更多单被打到、单个损失更深; 放宽止损必然抬高到期率) —— 要比就比**每笔期望**。

**对照解读规则**: 单日偏离基准=噪声(种子噪声铁律: |ΔSharpe|<3.5都是噪声); 连续5日同向偏离才升级为用户决策项。震荡期LONG临时负期望(如8/29~9/3 -287.6U)是已知形态, 不改基准。

## 4.5 影子臂 2×2 结构对照 (2026-09-12 加入, 每日必读 3.8b 节)

**它解决什么问题**: 实盘 9/3(果)/9/8(米) 把「持有 48h→72h」与「止损 5%→8%」**同时**改了, 而原影子对照臂两项都没动
→ 直接比"72h vs 48h"时 SL 变量未被控制。2026-09-12 起影子链并行维护四档**同源同日**(同批pred、同日08:21入场、300U名义),
差异**只**来自结构参数, 可干净拆分单变量贡献。数据源: `data/hybrid_tracker{,_sl8,_h72,_sl8h72}.json`, 由 08:46 cron 维护。

**实测结果 (LONG侧, 41天 2026-08-03~09-12, 已回刷到修正后funding口径)**:

| 结构 | 笔数 | 累计U | **每笔U** | 到期率 | 止损单均 | 到期单均 |
|---|---|---|---|---|---|---|
| SL-5%/48h (原基线) | 396 | +2474.5 | **+6.25** | 35.9% | -15.54 | +45.22 |
| SL-8%/48h (仅改止损) | 392 | +2698.6 | **+6.88** | 54.8% | -24.46 | +32.69 |
| SL-5%/72h (仅改持有) | 394 | +4857.9 | **+12.33** | 30.5% | -15.57 | +76.03 |
| SL-8%/72h (实盘现行) | 385 | +5392.9 | **+14.01** | 47.5% | -24.53 | +56.54 |

**单变量增量(vs 原基线)**: 仅改止损 **+0.64U/笔(+10%)** | 仅改持有 **+6.08U/笔(+97%)** | 两者同时 **+7.76U/笔(+124%)**

**结论(每日汇报照此口径)**:
1. **收益提升几乎全部来自"持有48h→72h"(+97%), 放宽止损只是次要放大器(+10%)**, 两者近似可加。
2. 机理自洽: 72h 让到期单均 +45.22→+76.03U(右尾变肥, 公理3"活到尾巴来"); 而 48h 下单纯放宽止损只是把更多单拖到终点(到期率35.9%→54.8%)却让到期单均反降(45.22→32.69)。
3. **这与 9/2 预研的"+111%"不矛盾但更干净**: 那个数字是在 SL 也变了的条件下测的; 现在有了受控拆分。
4. 每日读数动作: 看四档"每笔U"的**相对排序是否稳定**。若连续多日出现"SL-8%/72h 不再领先 SL-5%/72h" → 结构结论动摇。

**证伪线(什么数据会推翻上述结论)**:
- 若排除 8~9 月的几个尾部暴涨日后, 72h 的优势消失 → 说明优势只是"恰好赶上趋势行情", 非结构性(需按 regime 分层重算);
- 若实盘(125U/40U名义, 5x逐仓)按真实成交复核的每笔期望与影子档差距过大 → 影子口径失真优先于结构结论;
- 若**同结构下**到期单均持续衰减 + 到期率跌破 25%(连续10组) → 右尾结构本身瓦解, 此时谈结构优劣无意义。


## 5. 汇报风格 (用户偏好)

- 中文, 结论先行, 表格优先; 关键数字给精确来源(文件+日期)
- HTML报告类输出才用色块; 对话内用文字表格
- 盈亏分解必给: 方向分布/离场结构/单币TOP坑/ funding税占比
- 趋势研判必给: 当前regime判定 + 触发器清单 + 证伪线
- 需要用户决策时给选项, 不擅自改生产配置

## 6. 常用代码片段

山寨广度+中位收益:
```python
import json, datetime, statistics
kl = json.load(open('/home/myuser/backtester/data_cache/notusdt_1d_full.json'))['klines']
by_date = {}
for sym, bars in kl.items():
    for b in bars:
        day = datetime.datetime.fromtimestamp(b['t']/1000, tz=datetime.timezone.utc).strftime('%Y-%m-%d')
        by_date.setdefault(day, {})[sym] = (b['o'], b['c'])
# 逐日: med=statistics.median(rets), up=占比, crash=<-5%占比
```

BTC走势+5d vol: `GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=25`

IC↔批次盈亏对齐: forward_ic_history_48h.json (days[].date/ic_long/auc_long) × hybrid_tracker.json (day_pnl_u)

LONG四指标 (到期率/到期单均/止损单均/LONG净盈亏) 计算:
```python
import json, statistics
d = json.load(open('data/hybrid_tracker.json'))
# 默认近7个已结算日; 也可按批次/日期区间传参
days = sorted([k for k,v in d.items() if v.get('n_settled',0) == v.get('n_total',0)])[-7:]
tr = [t for ds in days for t in d[ds].get('trades',[]) if t.get('direction')=='LONG' and t.get('net_u') is not None]
sl = [t for t in tr if t.get('trigger')=='止损']
exp = [t for t in tr if t.get('trigger')=='到期']
def s(lst): return sum(t['net_u'] for t in lst)
print('到期率', f'{len(exp)/max(len(tr),1)*100:.1f}%')
print('到期单均', f'{s(exp)/max(len(exp),1):+.2f}U')
print('止损单均', f'{s(sl)/max(len(sl),1):+.2f}U')
print('LONG净盈亏', f'{s(tr):+.1f}U')
print('LONG期望/笔', f'{s(tr)/max(len(tr),1):+.2f}U')
print('到期胜率', f'{sum(1 for t in exp if t["net_u"]>0)/max(len(exp),1)*100:.1f}%')
```

2×2 结构对照 (对应晨报 3.8b 节; 四档同源同日 → 可直接比较每笔U):
```python
import json
FILES = [('SL-5%/48h(原基线)', 'data/hybrid_tracker.json'),
         ('SL-8%/48h',          'data/hybrid_tracker_sl8.json'),
         ('SL-5%/72h',          'data/hybrid_tracker_h72.json'),
         ('SL-8%/72h(实盘现行)', 'data/hybrid_tracker_sl8h72.json')]
def stat(p):
    d = json.load(open(p))
    tr = [t for ds in d for t in d[ds].get('trades', [])
          if t.get('direction') == 'LONG' and t.get('net_u') is not None]
    ex = [t for t in tr if t['trigger'] == '到期']
    sl = [t for t in tr if t['trigger'] == '止损']
    tot = sum(t['net_u'] for t in tr)
    return len(tr), tot, tot/max(len(tr), 1), len(ex)/max(len(tr), 1)*100
base = None
for label, p in FILES:
    n, tot, per, exp = stat(p)
    base = base or per
    print(f'{label:<20} {n:>4}笔 {tot:>+9.1f}U 每笔{per:>+7.2f}U 到期率{exp:>5.1f}%'
          f'  (vs基线 {(per/base-1)*100:+.0f}%)')
# 单变量读数: 仅改止损=第2行-第1行; 仅改持有=第3行-第1行; 两者=第4行-第1行
# 近7日版本: 把 tr 的筛选改成只取最近7个"已全结算日"(n_settled==n_total)的 trades
```


## 7. 环境依赖与跨设备迁移

**本SKILL必须在交易服务器上执行** — 所有数据文件/日志/实盘状态都在服务器本地:
- 服务器工作目录: `/home/myuser/websocket_new/` (earth-1.0 私有仓库)
- 数据: `data/*.json` 每日由cron更新(08:05训练/08:21实盘/08:55影子/09:00晨报), 离线副本无时效性
- SKILL本体位置: `~/.trae/skills/morning-report-analyst/SKILL.md` (本地文件, **不随TRAE账号云同步**)

**换电脑场景**:
| 场景 | 可用性 | 动作 |
|---|---|---|
| 新电脑TRAE远程连接本服务器 | ✅ 完整可用 | 无需任何操作 |
| 新电脑本地TRAE | ⚠️ 知识可用/数据不可用 | ①拷贝本目录到新机 `~/.trae/skills/` ②分析需SSH到服务器执行(连接方式见AGENTS.md/GPU文档) |

**迁移后自动生效**: SKILL自包含全部解读知识(funding税基准/IC时序结构/双层regime/自愈周期/正期望基准), 不依赖对话历史或memory; 但项目memory(`~/.trae-cn/memory/`)与SKILL不在同一目录, 换机后memory为空, 以SKILL+AGENTS.md为知识源。

**跨设备同步机制 (2026-09-04 部署, 2026-09-06 勘误, 2026-09-12 补漏)**: 仓库内 `websocket_new/.trae/skills/morning-report-analyst/SKILL.md` 是本文件的**独立拷贝(非符号链接, 两处md5需人工保持一致)** → 每日 08:50 trading_system_github_sync.py 自动把最新版推到 GitHub(rainbow3r1u/earth-1.0, 私有)。新机器恢复: `git clone` 仓库后将 `.trae/skills/morning-report-analyst/` 拷到 `~/.trae/skills/` 即可。

> ⚠️ **2026-09-12 实测发现副本漂移**: TRAE 实际加载的 `~/.trae/skills/...`(9/6 版) 比仓库副本(9/11 版) **少了 9/10 加入的 LONG 四指标等 36 行** —— 即分析员此前一直在用旧 SKILL。**GitHub 同步只覆盖仓库副本, 本地那份必须手动同步**, 改完 SKILL 后执行:
> ```bash
> cp /home/myuser/websocket_new/.trae/skills/morning-report-analyst/SKILL.md ~/.trae/skills/morning-report-analyst/SKILL.md
> md5sum ~/.trae/skills/morning-report-analyst/SKILL.md /home/myuser/websocket_new/.trae/skills/morning-report-analyst/SKILL.md   # 两行须一致
> ```
