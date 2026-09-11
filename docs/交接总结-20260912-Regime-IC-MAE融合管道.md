# 交接总结 2026-09-12 — Regime-IC-MAE 融合管道

> 目的: 给下一个窗口一个完整接手入口。  
> 范围: 本窗口所有新增/改动 + 当前结论 + 下一步动作。  
> 状态: Earth-1.0 生产冻结未动；本窗口只做数据管道/晨报口径/分析工具。

---

## 1. 本窗口做了什么

### 1.1 宏观资产 STALE 修复
- 根因: `daily_data_collection.py` 给宏观采集器只留 30s，采集器自身重试会超时；手动补跑又只写 `/tmp`，不同步到 `data/`。
- 已做:
  - `daily_data_collection.py` 宏观采集超时 `30s → 180s`
  - `collect_macro_assets.py` 采集成功后自动同步到 `data/macro_assets.json`
- 结果: 健康检查数据新鲜度恢复 OK。

### 1.2 AUC/IC 板块切到 48h 口径
- `forward_ic_check.py` 计算口径改为 `close[T+1] / open[T] - 1`
- 输出: `data/forward_ic_history_48h.json`
- `daily_digest_email.py` 4a4 默认读取 48h 文件，不再读 72h
- 旧 72h 数据保留在 `data/forward_ic_history.json`
- 8:30 公证 cron 已把 `forward_ic_history_48h.json` 纳入提交清单

### 1.3 晨报 Skill 增加 LONG 四指标
- 位置: `.trae/skills/morning-report-analyst/SKILL.md`
- 新增:
  - 到期率 = 到期笔数 / 总结算笔数
  - 到期单均
  - 止损单均
  - LONG 净盈亏
  - 期望公式: `LONG期望/笔 = 到期率 × 到期单均 + (1−到期率) × 止损单均`
- 明确区分: **到期率 ≠ 到期胜率**
  - 到期率 = 活到时间终点的比例
  - 到期胜率 = 到期且盈利的比例
- 当前近7日示例:
  - 到期率 50%
  - 到期胜率 80%
  - 到期单均 +15.70U
  - 止损单均 -15.07U
  - LONG 净盈亏 +22.3U

### 1.4 Regime-IC 静默采集管道
- 新文件: `audit/regime_ic_log.py`
- 输出: `data/regime_ic_history.json`
- 当前 40 天（2026-08-03 ~ 2026-09-11）
- 记录字段:
  - BTC: `ret_1d / vol5 / cum60 / ma20_dev`
  - 山寨: 中位收益 / 上涨占比 / 暴跌>5%占比 / 离散度 / 21d 相对 BTC 超额
  - IC: `ic_long / auc_long / ic_short / auc_short`
  - MAE: `mae_long_n / mae_long_median / mae_long_p90 / mae_long_gt5_pct / mae_long_gt8_pct / mae_long_gt10_pct`
- 不挂 cron，采用静默钩子:
  - `forward_ic_check.py` 结束时调用一次
  - `audit/forward_tracker.py` 结算后调用一次

### 1.5 MAE 静默收集管道
- 数据源: `data/forward_tracker.json` 的 `max_retrace_no_sl`
- 口径: LONG TOP10
- 已并入 `regime_ic_history.json`
- 9/11 当日还没结算，所以为空是正常的。

---

## 2. 本窗口核心结论

### 2.1 LONG IC 深负的定性
- 不是主传感器坏了
- 主因是“风格错配 + 模型映射滞后”
- 粗略拆分:
  - ~60% 来自高波动/高振幅风格暴露
  - ~40% 来自组内选币能力退化
- 不要硬做 vol/amp 中性化选币，回放显示会削掉右尾。

### 2.2 当前市场结构
- 山寨中位已经回到 8/18 那个 360d 低点区附近
- 9/9 和 8/18 形态更像同一类“低点/恐慌日”
- 9/10 只是初步修复，还没有 8/19 式确认日

### 2.3 用户方向
- Regime 闸门可以做
- 先靠静默采集积累 IC ↔ regime ↔ MAE 关系
- 等数据够，再把 regime 闸门和 MAE 分层融合

---

## 3. 关键文件

| 文件 | 作用 |
|---|---|
| `audit/regime_ic_log.py` | Regime-IC-MAE 采集器 |
| `data/regime_ic_history.json` | 输出数据 |
| `data/forward_ic_history_48h.json` | 48h IC/AUC |
| `data/forward_tracker.json` | MAE 原始数据源 |
| `daily_digest_email.py` | 晨报 4a4 已切 48h |
| `.trae/skills/morning-report-analyst/SKILL.md` | 晨报分析 Skill |
| `data/macro_assets.json` | 已恢复自动同步 |

---

## 4. 常用命令

```bash
# 手动刷新 Regime-IC-MAE
python3 audit/regime_ic_log.py

# 手动刷新 48h IC
python3 forward_ic_check.py

# 查看当前 regime-IC 数据
python3 - <<'PY'
import json
d=json.load(open('data/regime_ic_history.json'))['days']
print(len(d), d[0]['date'], '->', d[-1]['date'])
PY
```

---

## 5. 下一步建议

1. 让 `regime_ic_history.json` 自然积累到 60~80 天
2. 再看不同 regime 桶下的 MAE 分布
3. 之后设计 regime 闸门:
   - 高波动 / 深负 IC → 降仓或停开
   - 平静期 / IC 回升 → 恢复正常
4. 再把 MAE 分层叠上去，做双层闸门

---

## 6. 注意事项

- 不要把 vol/amp 中性化直接上生产，会削右尾
- 不要把 `到期率` 和 `到期胜率` 混用
- 9/10 IC_L 还没结算，等 9/12 08:00 CST 之后再看
- 9/11 MAE 还没结算
- 生产仍然冻结, 不要动 Earth-1.0

---

## 7. 管道审计与加固(2026-09-12 凌晨追加, 用户指令"别到时候收集出问题了没人知道")

### 7.1 审计结论(钩子挂载本身是健康的)
- 双钩子挂载正确: `forward_ic_check.py:234`(显式插入 audit/ 到 sys.path) + `forward_tracker.py:68`(同目录), 均有 try/except 不拖垮宿主
- 全量幂等重算设计 = 漏一天自愈; 写入走 tmp+os.replace 原子; 三方日期(pred日=IC日=UTC K线日)对齐无误

### 7.2 发现并已修复的 3 个问题
| # | 问题 | 修复 |
|---|---|---|
| 1 | **时区 bug**(`regime_ic_log.py` 原33行): cutoff 用服务器本地日期(CST)冒充 UTC, CST 00:00~07:59 之间运行会把**未收盘**当日 K 线当已收盘算指标。实锤: 9/12 00:47 手动跑导致数据里 9/11 行是 ~70% 进度的部分日数据 | cutoff 改用 UTC 当日零点; 数据已回刷(9/11 行按口径移除, 今早 08:50 钩子带完整日数据补回); tmp 加 pid 后缀防并发 |
| 2 | **静默失明**: 采集器无 cron, 钩子失败只写各自日志, 停更无人知晓 | `scripts/system_health_check.py` 新增 `chk_regime_ic()`(09:15 体检): updated 新鲜度(>26h WARN / >50h FAIL) + 日期断档 + 最后数据日≥昨日UTC + core/IC/MAE 字段覆盖; 四条负向路径已实测触发 |
| 3 | **体检误报**: `chk_git` 把"本地落后远程 N 提交"(08:50 同步设计内稳态)按 FAIL 计分 → 体检每天 FAIL 告警, 淹没真故障 | 降级为 INFO; 9/12 起体检恢复真实判定 |

### 7.3 已知数据覆盖缺口(**已拍板: 不回填**, 2026-09-12 用户确认)
- **MAE 实际从 8/11 才开始**: forward_tracker 7/28~8/10 的 `max_retrace_no_sl` 全为 None(老日子在字段上线前已结算完, `is_all_settled` 永久跳过重算) → regime 窗口前 8 天(8/03~8/10)无 MAE。**用户拍板: 不强制重算, 后续自然积累即可** — 这 8 天当无 MAE 的正常空白处理, 不是待办
- 9/10 的 MAE 是部分结算快照(n=6/10), 会随钩子自愈, 无需处理

### 7.4 验证快照(2026-09-12 01:05 CST)
- 回刷后: 39 天 2026-08-03 → 2026-09-10 连续; 全链路体检 14 项全 ✅ 总判定 OK
